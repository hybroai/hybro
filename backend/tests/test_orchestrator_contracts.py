from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from execution.adapters.hitl import HITLApplicationStore
from execution.orchestrator import (
    ORCHESTRATOR_COLLECTIONS,
    TOOL_RESULT_STATUSES,
    ArtifactRefPart,
    AssistantMessage,
    BudgetState,
    DataPart,
    EventProjector,
    MalformedToolArgumentsError,
    ModelMessage,
    ModelRouteConfiguration,
    ModelRuntime,
    ModelStreamAssembler,
    ModelStreamAssemblyError,
    ModelStreamEvent,
    ModelTurnRequest,
    OrchestratorEvent,
    OrchestratorEventStore,
    OrchestratorProfile,
    OrchestratorRunState,
    OrchestratorRunStore,
    ProfileConfiguration,
    PromptConfiguration,
    ProviderConformanceCase,
    ProviderConformanceError,
    ResolvedModelSnapshot,
    SessionNotice,
    TextPart,
    ToolCall,
    ToolCatalog,
    ToolDefinition,
    ToolResult,
    ToolRuntime,
    TruncatedToolCallError,
    UnsupportedProviderCapabilities,
    UsageRecord,
    UserMessage,
    evaluate_event_append,
    resolve_model_snapshot,
    resolve_profile_snapshot,
    run_provider_conformance,
    validate_tool_result_correlation,
)
from execution.orchestrator.a2a_runtime.models import AGENT_CALL_STATES
from execution.orchestrator.a2a_runtime.ports import HITLApplicationPort
from execution.orchestrator.models import AgentMessage
from execution.orchestrator.ports import InvocationOutcomeCheckpointReader

NOW = datetime(2026, 3, 12, tzinfo=UTC)


def model_config(**updates) -> ModelRouteConfiguration:
    values = {
        "route": "test-route",
        "provider": "openai",
        "model_id": "test-model",
        "api": "chat_completions",
        "supports_native_tools": True,
        "supports_provider_strict_schema": True,
        "supports_local_structured_action": True,
        "context_window": 32_000,
        "max_output_tokens": 2_000,
        "temperature": 0.2,
        "provider_timeout_seconds": 30,
        "max_provider_retries": 2,
    }
    values.update(updates)
    return ModelRouteConfiguration(**values)


def profile_config(profile_id: str = "fast") -> ProfileConfiguration:
    return ProfileConfiguration(
        profile_id=profile_id,
        max_model_turns=3,
        grace_model_turns=1,
        max_agent_calls=2,
        max_parallel_calls=1,
        max_transport_retries_per_call=2,
        max_compactions=1,
        deadline_seconds=60,
        initial_routing="explicit_agent_first",
        tool_execution="sequential",
        finalization="light",
    )


def test_resolved_model_prompt_and_profile_snapshot_serialization():
    profile = resolve_profile_snapshot(
        profile_config(),
        model=model_config(),
        prompt=PromptConfiguration(
            prompt_id="system", version="1", rendered_system_prompt="Be useful."
        ),
    )

    restored = type(profile).model_validate_json(profile.model_dump_json())

    assert restored == profile
    assert restored.model.tool_strategy == "native"
    assert len(restored.prompt.content_digest) == 64
    assert restored.model.structured_action_validation == "unsupported"


def test_persisted_provider_api_and_thinking_boundaries_are_closed():
    with pytest.raises(ValueError):
        model_config(provider="gemini")
    with pytest.raises(ValueError):
        model_config(api="generate_content")
    thinking = profile_config().model_copy(update={"thinking_level": "high"})
    prompt = PromptConfiguration(
        prompt_id="system",
        version="1",
        rendered_system_prompt="Be useful.",
    )
    with pytest.raises(ValueError, match="thinking_level"):
        resolve_profile_snapshot(thinking, model=model_config(), prompt=prompt)
    supported = resolve_profile_snapshot(
        thinking,
        model=model_config(supported_thinking_levels=["high"]),
        prompt=prompt,
    )
    assert supported.thinking_level == "high"


def test_provider_capability_strategy_resolution_and_rejection():
    structured = resolve_model_snapshot(
        model_config(supports_native_tools=False),
    )
    assert structured.tool_strategy == "structured_action"

    with pytest.raises(UnsupportedProviderCapabilities):
        resolve_model_snapshot(
            model_config(
                supports_native_tools=False,
                supports_provider_strict_schema=False,
                supports_local_structured_action=False,
            )
        )
    with pytest.raises(UnsupportedProviderCapabilities):
        resolve_model_snapshot(
            model_config(
                supports_native_tools=False,
                supports_provider_strict_schema=False,
                supports_local_structured_action=False,
            )
        )
    with pytest.raises(UnsupportedProviderCapabilities):
        resolve_model_snapshot(model_config(), preferred_strategy="unsupported")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "updates",
    [
        {"tool_strategy": "native", "supports_native_tools": False},
        {
            "tool_strategy": "structured_action",
            "structured_action_validation": "provider_strict",
            "supports_provider_strict_schema": False,
        },
        {
            "tool_strategy": "structured_action",
            "structured_action_validation": "local",
            "supports_local_structured_action": False,
        },
    ],
)
def test_persisted_model_and_profile_reject_impossible_strategy_capabilities(updates):
    payload = resolve_model_snapshot(model_config()).model_dump()
    payload.update(updates)
    with pytest.raises(ValidationError, match="strategy requires"):
        ResolvedModelSnapshot.model_validate(payload)

    profile = resolve_profile_snapshot(
        profile_config(),
        model=model_config(),
        prompt=PromptConfiguration(
            prompt_id="system", version="1", rendered_system_prompt="Be useful."
        ),
    ).model_dump()
    profile["model"] = payload
    with pytest.raises(ValidationError, match="strategy requires"):
        OrchestratorProfile.model_validate(profile)


def test_message_and_content_discriminated_unions_round_trip():
    messages = [
        UserMessage(
            message_id="user-1",
            content=[TextPart(text="hello"), DataPart(data={"n": 1})],
            created_at=NOW,
        ),
        AssistantMessage(
            message_id="assistant-1",
            content=[ArtifactRefPart(artifact_ref="artifact-1")],
            tool_calls=[],
            finish_reason="stop",
            usage=UsageRecord(input_tokens=2, output_tokens=3),
            created_at=NOW,
        ),
        SessionNotice(
            notice_id="notice-1", code="wrap_up", content="finish now", created_at=NOW
        ),
    ]

    adapter = TypeAdapter(list[AgentMessage])
    restored = adapter.validate_json(adapter.dump_json(messages))

    assert [message.kind for message in restored] == [
        "user",
        "assistant",
        "session_notice",
    ]
    assert isinstance(restored[0].content[1], DataPart)


@pytest.mark.parametrize(
    ("profile_id", "native_tools", "expected_strategy"),
    [("fast", True, "native"), ("ultimate", False, "structured_action")],
)
def test_fast_and_ultimate_test_profiles_resolve_without_production_binding(
    profile_id, native_tools, expected_strategy
):
    resolved = resolve_profile_snapshot(
        profile_config(profile_id),
        model=model_config(supports_native_tools=native_tools),
        prompt=PromptConfiguration(
            prompt_id=f"{profile_id}-prompt",
            version="1",
            rendered_system_prompt=profile_id,
        ),
    )
    assert resolved.profile_id == profile_id
    assert resolved.model.tool_strategy == expected_strategy


def test_normalized_model_message_content_union_serializes():
    message = ModelMessage(
        role="assistant",
        content=[
            {"kind": "text", "text": "working"},
            {
                "kind": "tool_call",
                "call_id": "call-1",
                "tool_name": "agent",
                "arguments": {"task": "work"},
            },
        ],
    )
    assert ModelMessage.model_validate_json(message.model_dump_json()) == message


def test_model_stream_assembly_text_tools_usage_and_finish_reason():
    assembler = ModelStreamAssembler()
    events = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(kind="text_delta", delta="Checking "),
        ModelStreamEvent(
            kind="tool_call_start", call_id="call-1", tool_name="call_agent"
        ),
        ModelStreamEvent(
            kind="tool_call_arguments_delta",
            call_id="call-1",
            delta='{"agent_id":"a-1",',
        ),
        ModelStreamEvent(
            kind="tool_call_arguments_delta", call_id="call-1", delta='"task":"x"}'
        ),
        ModelStreamEvent(kind="tool_call_end", call_id="call-1"),
        ModelStreamEvent(kind="usage", usage=UsageRecord(output_tokens=4)),
        ModelStreamEvent(
            kind="finish",
            finish_reason="tool_calls",
            provider_request_id="provider-1",
        ),
    ]
    for event in events:
        assembler.accept(event)

    assistant = assembler.build(message_id="assistant-1", created_at=NOW)

    assert assistant.finish_reason == "tool_calls"
    assert assistant.tool_calls[0].arguments == {"agent_id": "a-1", "task": "x"}
    assert assistant.usage == UsageRecord(output_tokens=4)
    assert assembler.provider_request_id == "provider-1"


def test_model_stream_assembles_final_text_and_parallel_calls():
    text = ModelStreamAssembler()
    text.accept(ModelStreamEvent(kind="text_delta", delta="final answer"))
    text.accept(ModelStreamEvent(kind="finish", finish_reason="stop"))
    assert text.build(message_id="final", created_at=NOW).content == [
        TextPart(text="final answer")
    ]

    parallel = ModelStreamAssembler()
    for call_id in ("call-1", "call-2"):
        parallel.accept(
            ModelStreamEvent(
                kind="tool_call_start", call_id=call_id, tool_name="call_agent"
            )
        )
        parallel.accept(
            ModelStreamEvent(
                kind="tool_call_arguments_delta", call_id=call_id, delta="{}"
            )
        )
        parallel.accept(ModelStreamEvent(kind="tool_call_end", call_id=call_id))
    parallel.accept(ModelStreamEvent(kind="finish", finish_reason="tool_calls"))
    assert [
        call.call_id
        for call in parallel.build(message_id="calls", created_at=NOW).tool_calls
    ] == ["call-1", "call-2"]


@pytest.mark.parametrize("finish_reason", ["length", "error", "aborted"])
def test_incomplete_calls_are_not_executable_for_non_tool_finish(finish_reason):
    assembler = ModelStreamAssembler()
    assembler.accept(
        ModelStreamEvent(kind="tool_call_start", call_id="call-1", tool_name="agent")
    )
    assembler.accept(ModelStreamEvent(kind="finish", finish_reason=finish_reason))

    if finish_reason == "length":
        with pytest.raises(TruncatedToolCallError):
            assembler.build_outcome(message_id="assistant-1", created_at=NOW)
        return

    outcome = assembler.build_outcome(message_id="assistant-1", created_at=NOW)
    assert outcome.assistant is None
    assert outcome.kind == (
        "aborted" if finish_reason == "aborted" else "provider_error"
    )


def test_tool_call_finish_rejects_truncated_or_malformed_arguments():
    truncated = ModelStreamAssembler()
    truncated.accept(
        ModelStreamEvent(kind="tool_call_start", call_id="call-1", tool_name="agent")
    )
    truncated.accept(ModelStreamEvent(kind="finish", finish_reason="tool_calls"))
    with pytest.raises(TruncatedToolCallError, match="truncated") as truncated_error:
        truncated.build(message_id="assistant-1", created_at=NOW)
    assert truncated_error.value.code == "truncated_tool_call"

    malformed = ModelStreamAssembler()
    malformed.accept(
        ModelStreamEvent(kind="tool_call_start", call_id="call-1", tool_name="agent")
    )
    malformed.accept(
        ModelStreamEvent(
            kind="tool_call_arguments_delta", call_id="call-1", delta="not-json"
        )
    )
    malformed.accept(ModelStreamEvent(kind="tool_call_end", call_id="call-1"))
    malformed.accept(ModelStreamEvent(kind="finish", finish_reason="tool_calls"))
    with pytest.raises(
        MalformedToolArgumentsError, match="malformed"
    ) as malformed_error:
        malformed.build(message_id="assistant-1", created_at=NOW)
    assert malformed_error.value.code == "malformed_tool_arguments"


def test_retry_attempt_discards_incomplete_previous_attempt_output():
    assembler = ModelStreamAssembler()
    assembler.accept(ModelStreamEvent(kind="attempt_started", attempt=1))
    assembler.accept(ModelStreamEvent(kind="text_delta", delta="discarded"))
    assembler.accept(
        ModelStreamEvent(
            kind="attempt_failed", attempt=1, error_class="network", retryable=True
        )
    )
    assembler.accept(
        ModelStreamEvent(
            kind="retry_scheduled",
            attempt=2,
            error_class="network",
            retryable=True,
        )
    )
    assembler.accept(ModelStreamEvent(kind="attempt_started", attempt=2))
    assembler.accept(ModelStreamEvent(kind="text_delta", delta="kept"))
    assembler.accept(ModelStreamEvent(kind="finish", finish_reason="stop"))

    assert assembler.build(message_id="final", created_at=NOW).content == [
        TextPart(text="kept")
    ]


def test_retry_events_have_typed_classification_and_visible_attempts():
    classes = {
        "authentication",
        "rate_limit",
        "timeout",
        "network",
        "provider_5xx",
        "context_overflow",
        "invalid_request",
        "content_filter",
        "aborted",
        "unknown",
    }
    for error_class in classes:
        event = ModelStreamEvent(
            kind="attempt_failed",
            attempt=2,
            error_class=error_class,
            retryable=error_class in {"rate_limit", "timeout", "network"},
        )
        assert event.attempt == 2
        assert event.error_class == error_class
    with pytest.raises(ValidationError):
        ModelStreamEvent(kind="attempt_failed", error_class="other")


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "attempt_started"},
        {"kind": "attempt_failed", "attempt": 1},
        {
            "kind": "attempt_failed",
            "attempt": 1,
            "error_class": "network",
        },
        {"kind": "retry_scheduled", "attempt": 2},
        {
            "kind": "retry_scheduled",
            "attempt": 2,
            "error_class": "network",
            "retryable": False,
        },
    ],
)
def test_malformed_retry_events_are_rejected(payload):
    with pytest.raises(ValidationError):
        ModelStreamEvent.model_validate(payload)


def test_retry_assembler_preserves_metadata_and_rejects_out_of_order_attempts():
    assembler = ModelStreamAssembler()
    assembler.accept(ModelStreamEvent(kind="attempt_started", attempt=1))
    failed = ModelStreamEvent(
        kind="attempt_failed",
        attempt=1,
        error_class="timeout",
        retryable=True,
        provider_request_id="provider-attempt-1",
    )
    scheduled = ModelStreamEvent(
        kind="retry_scheduled",
        attempt=2,
        error_class="timeout",
        retryable=True,
        retry_delay_ms=25,
    )
    assembler.accept(failed)
    assembler.accept(scheduled)
    assert assembler.retry_events == [failed, scheduled]

    with pytest.raises(ModelStreamAssemblyError, match="scheduled retry"):
        assembler.accept(ModelStreamEvent(kind="attempt_started", attempt=3))

    inconsistent = ModelStreamAssembler()
    inconsistent.accept(ModelStreamEvent(kind="attempt_started", attempt=1))
    inconsistent.accept(failed)
    with pytest.raises(ModelStreamAssemblyError, match="preserve"):
        inconsistent.accept(
            ModelStreamEvent(
                kind="retry_scheduled",
                attempt=2,
                error_class="rate_limit",
                retryable=True,
            )
        )


class _NeverCancelled:
    cancelled = False

    async def wait(self) -> None:
        return None


class _OfflineProviderRuntime:
    def __init__(self, strategy: str) -> None:
        self.strategy = strategy

    async def stream_turn(
        self, request: ModelTurnRequest, *, signal: _NeverCancelled
    ) -> AsyncIterator[ModelStreamEvent]:
        assert not signal.cancelled
        assert request.model.tool_strategy == self.strategy
        scenario = request.messages[0].content[0].text  # type: ignore[union-attr]
        events: dict[str, list[ModelStreamEvent]] = {
            "final_text": [
                ModelStreamEvent(kind="text_delta", delta="final answer"),
                ModelStreamEvent(kind="finish", finish_reason="stop"),
            ],
            "one_tool_call": [
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-1", tool_name="call_agent"
                ),
                ModelStreamEvent(
                    kind="tool_call_arguments_delta", call_id="call-1", delta="{}"
                ),
                ModelStreamEvent(kind="tool_call_end", call_id="call-1"),
                ModelStreamEvent(kind="finish", finish_reason="tool_calls"),
            ],
            "parallel_tool_calls": [
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-1", tool_name="call_agent"
                ),
                ModelStreamEvent(
                    kind="tool_call_arguments_delta", call_id="call-1", delta="{}"
                ),
                ModelStreamEvent(kind="tool_call_end", call_id="call-1"),
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-2", tool_name="call_agent"
                ),
                ModelStreamEvent(
                    kind="tool_call_arguments_delta", call_id="call-2", delta="{}"
                ),
                ModelStreamEvent(kind="tool_call_end", call_id="call-2"),
                ModelStreamEvent(kind="finish", finish_reason="tool_calls"),
            ],
            "malformed_arguments": [
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-1", tool_name="call_agent"
                ),
                ModelStreamEvent(
                    kind="tool_call_arguments_delta", call_id="call-1", delta="{"
                ),
                ModelStreamEvent(kind="tool_call_end", call_id="call-1"),
                ModelStreamEvent(kind="finish", finish_reason="tool_calls"),
            ],
            "truncated_call": [
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-1", tool_name="call_agent"
                ),
                ModelStreamEvent(kind="finish", finish_reason="tool_calls"),
            ],
            "streaming_text": [
                ModelStreamEvent(kind="text_delta", delta="stream"),
                ModelStreamEvent(kind="text_delta", delta="ed"),
                ModelStreamEvent(kind="finish", finish_reason="stop"),
            ],
            "usage": [
                ModelStreamEvent(kind="usage", usage=UsageRecord(output_tokens=2)),
                ModelStreamEvent(kind="finish", finish_reason="stop"),
            ],
            "abort": [
                ModelStreamEvent(
                    kind="tool_call_start", call_id="call-1", tool_name="call_agent"
                ),
                ModelStreamEvent(kind="finish", finish_reason="aborted"),
            ],
            "retry_classification": [
                ModelStreamEvent(kind="attempt_started", attempt=1),
                ModelStreamEvent(
                    kind="attempt_failed",
                    attempt=1,
                    error_class="network",
                    retryable=True,
                ),
                ModelStreamEvent(
                    kind="retry_scheduled",
                    attempt=2,
                    error_class="network",
                    retryable=True,
                ),
                ModelStreamEvent(kind="attempt_started", attempt=2),
                ModelStreamEvent(kind="text_delta", delta="recovered"),
                ModelStreamEvent(
                    kind="finish",
                    finish_reason="stop",
                    provider_request_id="offline-request",
                ),
            ],
        }
        for event in events[scenario]:
            yield event


def _conformance_cases(strategy: str) -> list[ProviderConformanceCase]:
    route = model_config(supports_native_tools=strategy == "native")
    cases = []
    for scenario in (
        "final_text",
        "one_tool_call",
        "parallel_tool_calls",
        "malformed_arguments",
        "truncated_call",
        "streaming_text",
        "usage",
        "abort",
        "retry_classification",
    ):
        cases.append(
            ProviderConformanceCase(
                scenario=scenario,
                request=ModelTurnRequest(
                    turn_id=f"conformance:{strategy}:{scenario}",
                    model=resolve_model_snapshot(route),
                    system_prompt="offline conformance",
                    messages=[
                        ModelMessage(
                            role="user",
                            content=[{"kind": "text", "text": scenario}],
                        )
                    ],
                    tools=[
                        ToolDefinition(
                            name="call_agent",
                            label="Agent",
                            description="Call an agent",
                            input_schema={"type": "object"},
                            execution_mode="parallel",
                            side_effect_level="external",
                        )
                    ],
                ),
                created_at=NOW,
                expected_text={
                    "final_text": "final answer",
                    "streaming_text": "streamed",
                }.get(scenario),
                expected_error_class=(
                    "network" if scenario == "retry_classification" else None
                ),
            )
        )
    return cases


class _UnrelatedAssemblyFailureRuntime(_OfflineProviderRuntime):
    def __init__(self, strategy: str, failing_scenario: str) -> None:
        super().__init__(strategy)
        self.failing_scenario = failing_scenario

    async def stream_turn(
        self, request: ModelTurnRequest, *, signal: _NeverCancelled
    ) -> AsyncIterator[ModelStreamEvent]:
        scenario = request.messages[0].content[0].text  # type: ignore[union-attr]
        if scenario == self.failing_scenario:
            yield ModelStreamEvent(kind="finish", finish_reason="unrelated")
            return
        async for event in super().stream_turn(request, signal=signal):
            yield event


@pytest.mark.parametrize("strategy", ["native", "structured_action"])
async def test_offline_provider_runtime_passes_provider_neutral_conformance(strategy):
    results = await run_provider_conformance(
        _OfflineProviderRuntime(strategy),
        _conformance_cases(strategy),
        signal=_NeverCancelled(),
    )

    assert {result.scenario for result in results} == {
        "final_text",
        "one_tool_call",
        "parallel_tool_calls",
        "malformed_arguments",
        "truncated_call",
        "streaming_text",
        "usage",
        "abort",
        "retry_classification",
    }
    assert results[-1].provider_request_id == "offline-request"


@pytest.mark.parametrize("scenario", ["malformed_arguments", "truncated_call"])
async def test_provider_conformance_rejects_unrelated_assembly_failures(scenario):
    runtime = _UnrelatedAssemblyFailureRuntime("native", scenario)

    with pytest.raises(ProviderConformanceError, match="unexpected assembly error"):
        await run_provider_conformance(
            runtime,
            _conformance_cases("native"),
            signal=_NeverCancelled(),
        )


def test_tool_status_inventory_and_result_correlation_are_complete():
    assert TOOL_RESULT_STATUSES == {
        "completed",
        "failed",
        "canceled",
        "rejected",
        "expired",
    }
    assert len(AGENT_CALL_STATES) == 15
    call = ToolCall(call_id="call-1", tool_name="agent", arguments={})
    result = ToolResult(
        call_id="call-1",
        tool_name="agent",
        status="completed",
        content=[],
        artifact_refs=[],
    )
    validate_tool_result_correlation(call, result)
    with pytest.raises(ValueError, match="correlate"):
        validate_tool_result_correlation(
            call, result.model_copy(update={"call_id": "other"})
        )


def test_run_schema_version_and_budget_values_are_rejected_early():
    with pytest.raises(ValidationError):
        OrchestratorRunState.model_validate({"schema_version": 2})
    with pytest.raises(ValidationError):
        UsageRecord(input_tokens=-1)
    with pytest.raises(ValidationError):
        BudgetState(deadline_at=NOW, grace_turns_used=-1)
    invalid_profile = profile_config().model_dump()
    invalid_profile["max_parallel_calls"] = 5
    with pytest.raises(ValidationError):
        ProfileConfiguration.model_validate(invalid_profile)
    invalid_profile = profile_config().model_dump()
    invalid_profile["grace_model_turns"] = -1
    with pytest.raises(ValidationError):
        ProfileConfiguration.model_validate(invalid_profile)


def event(event_id: str, sequence: int, state_version: int = 1) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id=event_id,
        event_type="turn_started",
        session_id="room-1",
        run_id="run-1",
        room_id="room-1",
        room_epoch=1,
        sequence=sequence,
        state_version=state_version,
        causation_id=f"command-{sequence}",
        payload={},
        created_at=NOW,
    )


def test_event_envelope_ordering_and_idempotency():
    first = event("event-1", 1)
    second = event("event-2", 2, 2)

    assert evaluate_event_append([], first).outcome == "accepted"
    assert evaluate_event_append([first], first).outcome == "replayed"
    assert evaluate_event_append([first], second).outcome == "accepted"
    assert evaluate_event_append([first], event("event-3", 3)).outcome == "conflict"


def test_protocols_are_narrow_and_explicit():
    expected = {
        ModelRuntime: {"stream_turn"},
        ToolRuntime: {
            "accept",
            "execute",
            "dispatch_model_reply",
            "publish_parked_interaction",
            "abandon_parked_interaction",
        },
        ToolCatalog: {"list_tools", "resolve"},
        HITLApplicationPort: {
            "create_or_replay",
            "activate",
            "abandon",
            "read_interaction",
            "get_eligible_interactions",
            "get_published_interactions",
            "publish",
            "read_answers",
            "read_answer_record",
            "answer",
        },
        HITLApplicationStore: {
            "ensure_interaction",
            "load_interaction",
            "get_eligible_interactions",
            "get_published_interactions",
            "mark_eligible",
            "mark_published",
            "abandon",
            "load_answer",
            "ensure_answer",
        },
        OrchestratorEventStore: {"append", "read"},
        InvocationOutcomeCheckpointReader: {
            "is_outcome_checkpointed",
            "has_processed_observation",
            "is_run_terminal",
        },
        EventProjector: {"project"},
        OrchestratorRunStore: {
            "create",
            "load",
            "cas_mutate",
            "request_cancellation",
            "repair_canceling_recovery",
            "claim_recovery",
            "renew_recovery",
            "release_recovery",
            "list_due_runs",
            "claim_projection_intent",
            "complete_projection_intent",
            "block_projection_intent",
            "release_projection_intent",
            "list_due_projection_intents",
        },
    }
    for protocol, methods in expected.items():
        actual = {
            name
            for name, value in protocol.__dict__.items()
            if inspect.isfunction(value) and not name.startswith("_")
        }
        assert actual == methods


def test_recovery_and_projection_claim_ports_require_cas_version_and_lease():
    claim_recovery = inspect.signature(OrchestratorRunStore.claim_recovery).parameters
    claim_projection = inspect.signature(
        OrchestratorRunStore.claim_projection_intent
    ).parameters
    assert {"expected_state_version", "owner_id", "lease_expires_at"} <= set(
        claim_recovery
    )
    assert {"expected_state_version", "owner_id", "lease_expires_at"} <= set(
        claim_projection
    )


def test_unbound_collection_metadata_contains_required_indexes():
    collections = {item.name: item for item in ORCHESTRATOR_COLLECTIONS}
    assert set(collections) == {
        "orchestrator_runs",
        "orchestrator_run_events",
        "orchestrator_recovery_leases",
    }
    lease_indexes = {
        item.name: item for item in collections["orchestrator_recovery_leases"].indexes
    }
    assert set(lease_indexes) == {
        "orchestrator_recovery_lease_run_unique",
        "orchestrator_recovery_lease_due",
    }
    assert lease_indexes["orchestrator_recovery_lease_due"].keys == (
        ("quarantined_at", 1),
        ("next_attempt_at", 1),
        ("lease_expires_at", 1),
        ("run_id", 1),
    )
    run_names = {item.name for item in collections["orchestrator_runs"].indexes}
    assert {
        "orchestrator_run_id_unique",
        "orchestrator_active_room_unique_canceling",
        "orchestrator_canceling_recovery",
        "orchestrator_client_request",
        "orchestrator_tool_call_id",
        "orchestrator_recovery_due",
        "orchestrator_projection_due",
    } <= run_names
    indexes = {item.name: item for item in collections["orchestrator_runs"].indexes}
    assert indexes["orchestrator_tool_call_id"].keys == (
        ("run_id", 1),
        ("tool_batches.entries.call_id", 1),
    )
    assert {
        "orchestrator_agent_call_id",
        "orchestrator_a2a_task",
        "orchestrator_a2a_context",
    }.isdisjoint(indexes)
