"""Passthrough profile spike: one Agent call, zero model turns, real kernel.

A passthrough Run sends the user's text to the single mentioned Agent,
forwards a typed Agent interaction to the owner without a model decision,
and publishes the Agent's answer as the final message.
"""

from __future__ import annotations

import pytest

from delivery.snapshot import RoomEventFold
from execution.model_loop.context import ContextCompiler
from execution.orchestrator.a2a_runtime.catalog_assembler import (
    agent_tool_input_schema,
)
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.kernel import (
    SURFACE_AGENT_QUESTIONS_TOOL_NAME,
    OrchestratorKernel,
)
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.models import (
    AssistantMessage,
    AuthorizationBasis,
    CandidateScopeSnapshot,
    OrchestratorProfile,
    TextPart,
    ToolDefinition,
    ToolInteractionQuestion,
    ToolObservation,
    ToolResult,
    ToolSuspension,
)
from execution.orchestrator.ports import ParkedInteractionCloseout
from execution.orchestrator.profiles import (
    ProfileConfiguration,
    resolve_profile_snapshot,
)
from execution.orchestrator.public_projection import PublicProjectionTranslator
from execution.orchestrator.session import DefaultRunFactory, RoomAgentSessionConfig
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)

from ._orchestrator_helpers import (
    NOW,
    FixedClock,
    FixedIDs,
    NeverCancelled,
    ScriptedModelRuntime,
    user_message,
)

AGENT = "agent_travel"
SECOND_SKILL = "agent_travel_skill_b"
OTHER_AGENT = "agent_other"


def passthrough_profile() -> OrchestratorProfile:
    return resolve_profile_snapshot(
        ProfileConfiguration(
            profile_id="passthrough",
            max_model_turns=0,
            grace_model_turns=0,
            max_agent_calls=1,
            max_parallel_calls=1,
            max_transport_retries_per_call=2,
            max_compactions=0,
            deadline_seconds=300,
            initial_routing="explicit_agent_first",
            tool_execution="sequential",
            finalization="pass_through",
        )
    )


def agent_tool(name: str, label: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        label=label,
        description=f"{label} Agent",
        input_schema=agent_tool_input_schema([]),
        execution_mode="parallel",
        side_effect_level="external",
    )


def make_passthrough_run(*, agent_ids=("agent-1",), source="mention", text="hello"):
    config = RoomAgentSessionConfig(
        session_id="session-1",
        conversation_id="room-1",
        profile=passthrough_profile(),
        candidate_scope=CandidateScopeSnapshot(
            snapshot_id="scope-1",
            source=source,
            conversation_id="room-1",
            agent_ids=list(agent_ids),
            authorization_basis=AuthorizationBasis(kind="mention"),
        ),
        room_epoch=1,
        requesting_subject_id="user-1",
        tool_catalog=StaticFakeToolCatalog(),
    )
    return DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()).create_run(
        config=config, message=user_message(text), client_request_id="request-1"
    )


class AgentRuntime(RecordingFakeToolRuntime):
    """Fake A2A Agent tools keyed by tool name, with HITL parking."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, list[object]] = {}
        self.published: list[tuple[str, str]] = []
        self.abandoned: list[tuple[str, str]] = []
        self.tasks: list[str] = []
        self.parked: dict[str, tuple[str, str]] = {}
        self.crash_once = False

    async def execute(self, invocation, acceptance, *, signal):
        if self.crash_once:
            self.crash_once = False
            raise ProcessDied
        stored = self.acceptances.get(invocation.idempotency_key)
        assert stored == acceptance
        self.execute_log.append(invocation.invocation_id)
        self.tasks.append(invocation.arguments["task"])
        script = self.answers[invocation.tool.definition.name]
        outcome = script.pop(0)
        if isinstance(outcome, ToolSuspension):
            if outcome.call_record_id is not None:
                self.parked[outcome.call_record_id] = (
                    invocation.invocation_id,
                    invocation.tool.definition.name,
                )
            return outcome.model_copy(
                update={"invocation_id": invocation.invocation_id}
            )
        assert isinstance(outcome, ToolResult)
        return outcome.model_copy(
            update={
                "call_id": invocation.invocation_id,
                "tool_name": invocation.tool.definition.name,
            }
        )

    async def publish_parked_interaction(self, *, call_record_id, interaction_id):
        self.published.append((call_record_id, interaction_id))

    async def abandon_parked_interaction(self, *, call_record_id, interaction_id):
        self.abandoned.append((call_record_id, interaction_id))
        call_id, tool_name = self.parked[call_record_id]
        return ParkedInteractionCloseout(
            result=ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                status="canceled",
                content=[],
                artifact_refs=[],
                error_code="canceled",
            ),
            local_cancellation_won=True,
        )


class ProcessDied(BaseException):
    """Simulates the process dying in the middle of a remote call."""


def answer(text: str, *, status: str = "completed", error_code=None) -> ToolResult:
    return ToolResult(
        call_id="placeholder",
        tool_name="placeholder",
        status=status,
        content=[TextPart(text=text)] if text else [],
        artifact_refs=[],
        error_code=error_code,
        error_message=error_code,
    )


def question(
    *, call_record_id="parent-1", interaction_id="interaction-1", fingerprint="fp-1"
) -> ToolSuspension:
    return ToolSuspension(
        invocation_id="placeholder",
        status="input_required",
        call_record_id=call_record_id,
        interaction_id=interaction_id,
        interaction_fingerprint=fingerprint,
        questions=[
            ToolInteractionQuestion(
                question_id="q1",
                prompt="May I ask the seller for the call for funds?",
                answer_kind="text",
            )
        ],
    )


def waiting() -> ToolSuspension:
    return ToolSuspension(invocation_id="placeholder", status="waiting_external")


class PublicHistory:
    """Durable public room events through the production translator."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.events: list[str] = []
        self.translator = PublicProjectionTranslator()

    async def read(self, _conversation_id, _run_id):
        return list(self.records.values())

    async def lifecycle(self, event_type, run, payload):
        self.events.append(event_type)
        projected = self.translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=len(self.events),
                timestamp=NOW,
                payload=payload,
                conversation_id=run.conversation_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
            ),
            catalog=run.tool_catalog,
        )
        if projected is None or projected.event_id in self.records:
            return
        self.records[projected.event_id] = {
            "room_id": run.conversation_id,
            "room_seq": len(self.records) + 1,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": projected.event_id,
                "run_id": projected.run_id,
                "seq": projected.seq,
                "type": projected.kind,
                "payload": projected.payload,
                "correlation_id": projected.client_request_id,
            },
        }

    def public_types(self) -> list[str]:
        return [
            record["payload_public"]["type"]
            for record in sorted(self.records.values(), key=lambda r: r["room_seq"])
        ]

    def fold(self, run) -> dict:
        """Fold the public history plus the projected final and settlement."""
        fold = RoomEventFold()
        for record in sorted(self.records.values(), key=lambda r: r["room_seq"]):
            assert fold.apply(record), record
        room_seq = len(self.records)
        if run.status == "completed":
            room_seq += 1
            assert fold.apply(
                {
                    "room_id": run.conversation_id,
                    "room_seq": room_seq,
                    "kind": "agent_response",
                    "ts": NOW.isoformat(),
                    "payload_public": {
                        "message_id": run.proposed_final_message_id,
                        "content": _final_text(run),
                        "client_request_id": run.client_request_id,
                        "related_message_id": run.request.user_message_id,
                    },
                }
            )
        room_seq += 1
        settled = {
            "room_id": run.conversation_id,
            "room_seq": room_seq,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": f"run-settled:{run.run_id}",
                "run_id": run.run_id,
                "seq": len(self.events) + 1,
                "type": "run_settled",
                "payload": _settled_payload(run),
                "correlation_id": run.client_request_id,
            },
        }
        assert fold.apply(settled)
        return fold.state(room_seq=room_seq)["turns"][0]


def _settled_payload(run) -> dict:
    payload = {
        "status": run.status,
        "started_at": run.created_at.isoformat(),
        "settled_at": NOW.isoformat(),
        "duration_ms": 1,
    }
    if run.status == "completed":
        payload["final_message_id"] = run.proposed_final_message_id
    elif run.status == "canceled":
        payload["cancellation_code"] = run.cancellation_cause
        if run.cancellation_cause == "policy" and run.denied_rule_id:
            payload["rule_id"] = run.denied_rule_id
    else:
        payload.update(
            status="failed",
            failure_code="internal_error",
            error_summary="The run could not be completed.",
        )
    return payload


async def make_passthrough_kernel(
    *,
    tools: list[ToolDefinition] | None = None,
    run=None,
    runtime: AgentRuntime | None = None,
):
    run = run or make_passthrough_run()
    store = InMemoryOrchestratorRunStore()
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    model = ScriptedModelRuntime([])
    runtime = runtime or AgentRuntime()
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=model,
        tool_runtime=runtime,
        tool_catalog=StaticFakeToolCatalog(tools or [agent_tool(AGENT, "Travel")]),
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )
    history = PublicHistory()
    kernel.canonical_event_reader = history.read
    await history.lifecycle("run_started", run, {"mode": "passthrough"})
    return kernel, store, model, runtime, history, run.run_id


def _final_text(run) -> str:
    final = next(
        item
        for item in run.transcript
        if isinstance(item, AssistantMessage)
        and item.message_id == run.proposed_final_message_id
    )
    return "".join(part.text for part in final.content if isinstance(part, TextPart))


def _agent_entry(run):
    return run.tool_batches[0].entries[0]


def _complete(entry, text: str, *, observation_id: str) -> ToolObservation:
    return ToolObservation(
        observation_id=observation_id,
        invocation_id=entry.invocation.invocation_id,
        outcome=ToolResult(
            call_id=entry.call_id,
            tool_name=entry.tool_name,
            status="completed",
            content=[TextPart(text=text)],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )


# -- Profile representation ---------------------------------------------------


def test_passthrough_profile_needs_no_model_route_or_prompt():
    profile = passthrough_profile()

    assert profile.profile_id == "passthrough"
    assert profile.model is None
    assert profile.prompt is None
    assert profile.max_model_turns == 0
    assert profile.initial_routing == "explicit_agent_first"
    assert profile.finalization == "pass_through"


def test_model_profiles_still_require_a_model_route_and_prompt():
    from execution.orchestrator.models import OrchestratorProfile

    data = passthrough_profile().model_dump()
    data["profile_id"] = "fast"
    data["max_model_turns"] = 1
    with pytest.raises(ValueError, match="model route and prompt"):
        OrchestratorProfile.model_validate(data)


# -- Dispatch -----------------------------------------------------------------


async def test_passthrough_completes_with_one_call_and_zero_model_turns():
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("Works were voted in 2023.")]

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "final_answer"
    assert result.run.status == "completed"
    assert model.requests == []
    assert result.run.budget.model_turns_used == 0
    assert result.run.budget.agent_calls_used == 1
    assert runtime.tasks == ["hello"]
    assert _final_text(result.run) == "Works were voted in 2023."
    assert history.public_types() == [
        "run_started",
        "turn_start",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
    ]
    message_ends = [
        record["payload_public"]["payload"]
        for record in history.records.values()
        if record["payload_public"]["type"] == "message_end"
    ]
    assert [end["stop_reason"] for end in message_ends] == ["tool_use", "stop"]
    assert message_ends[-1]["text"] == "Works were voted in 2023."
    turn = history.fold(result.run)
    assert turn["state"] == "completed"
    assert len(turn["internal_turns"]) == 2


async def test_passthrough_dispatches_once_to_the_first_skill():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        tools=[agent_tool(AGENT, "Travel"), agent_tool(SECOND_SKILL, "Travel B")]
    )
    runtime.answers[AGENT] = [answer("first skill")]

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "final_answer"
    assert [entry.tool_name for entry in result.run.tool_batches[0].entries] == [AGENT]
    assert len(runtime.execute_log) == 1
    assert model.requests == []


@pytest.mark.parametrize(
    ("agent_ids", "source"),
    [(("agent-1", "agent-2"), "mention"), (("agent-1",), "room_default")],
)
async def test_passthrough_requires_exactly_one_mentioned_agent(agent_ids, source):
    run = make_passthrough_run(agent_ids=agent_ids, source=source)
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        run=run,
        tools=[agent_tool(AGENT, "Travel"), agent_tool(OTHER_AGENT, "Other")],
    )

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "failed"
    assert result.run.terminal_reason == "passthrough_requires_one_agent"
    assert runtime.execute_log == []
    assert model.requests == []
    assert history.fold(result.run)["state"] == "failed"


async def test_passthrough_without_request_text_fails_without_dispatch():
    run = make_passthrough_run(text="   ")
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        run=run
    )

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "failed"
    assert result.run.terminal_reason == "passthrough_requires_text"
    assert runtime.execute_log == []
    assert history.fold(result.run)["state"] == "failed"


@pytest.mark.parametrize("status", ["failed", "rejected", "canceled", "expired"])
async def test_unsuccessful_agent_result_fails_the_run(status):
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("", status=status, error_code="agent_error")]

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "failed"
    assert result.run.status == "failed"
    assert result.run.terminal_reason == f"agent_{status}"
    assert model.requests == []
    assert history.fold(result.run)["state"] == "failed"


# -- Owner approval -----------------------------------------------------------


async def test_typed_input_required_is_published_and_parks_without_a_model():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "awaiting_user"
    assert result.run.status == "awaiting_user"
    assert model.requests == []
    assert runtime.published == [("parent-1", "interaction-1")]
    surface = result.run.tool_batches[1]
    assert [entry.tool_name for entry in surface.entries] == [
        SURFACE_AGENT_QUESTIONS_TOOL_NAME
    ]
    assert surface.entries[0].state == "input_required"
    # Both synthetic messages belong to the dispatch turn.
    assert surface.internal_turn_id == result.run.tool_batches[0].internal_turn_id
    surface_assistant = next(
        item
        for item in result.run.transcript
        if isinstance(item, AssistantMessage)
        and item.message_id == surface.assistant_message_id
    )
    assert surface_assistant.tool_calls[0].arguments == {}
    assert history.public_types().count("turn_start") == 1


async def test_owner_answer_resumes_and_completes_without_a_model():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    # The owner approved; the continued Agent task finished with the answer.
    resumed = await kernel.observe_tool(
        run_id,
        _complete(
            _agent_entry(parked.run), "12,400 EUR for unit 4B", observation_id="obs-1"
        ),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert resumed.outcome == "final_answer"
    assert resumed.run.status == "completed"
    assert _final_text(resumed.run) == "12,400 EUR for unit 4B"
    assert model.requests == []
    assert all(
        entry.state == "terminal"
        for batch in resumed.run.tool_batches
        for entry in batch.entries
    )
    turn = history.fold(resumed.run)
    assert turn["state"] == "completed"


async def test_answered_surface_waits_for_the_agent_instead_of_resurfacing():
    """Production order: the owner's answer closes the surface call first.

    The answer publication terminalizes the surface_agent_questions entry
    while the Agent entry is still the presented input_required child. The
    next step must wait for the Agent's continuation result, not surface the
    answered questions again under the same synthetic message id.
    """
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    run = parked.run
    surface_batch = run.tool_batches[1]
    surface = surface_batch.entries[0]
    answered_surface = surface_batch.model_copy(
        update={
            "entries": [
                surface.model_copy(
                    update={
                        "state": "terminal",
                        "buffered_terminal_result": ToolResult(
                            call_id=surface.call_id,
                            tool_name=surface.tool_name,
                            status="completed",
                            content=[TextPart(text="The user's answers were applied.")],
                            artifact_refs=[],
                        ),
                    }
                )
            ]
        }
    )
    answered = run.model_copy(
        update={
            "status": "running",
            "tool_batches": [run.tool_batches[0], answered_surface],
            "state_version": run.state_version + 1,
        }
    )
    stored = await store.cas_mutate(
        answered,
        expected_state_version=run.state_version,
        command_id="test:answer-published",
    )
    assert stored.outcome == "accepted"

    waiting = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert waiting.outcome == "waiting_external"
    assert waiting.run.status == "waiting_external"
    assert len(waiting.run.tool_batches) == 2
    assert model.requests == []

    done = await kernel.observe_tool(
        run_id,
        _complete(_agent_entry(waiting.run), "Kyoto plan", observation_id="obs-2"),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert done.outcome == "final_answer"
    assert _final_text(done.run) == "Kyoto plan"
    assert model.requests == []


async def test_a_follow_up_question_is_forwarded_again_without_a_model():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    entry = _agent_entry(parked.run)

    again = await kernel.observe_tool(
        run_id,
        ToolObservation(
            observation_id="obs-follow-up",
            invocation_id=entry.invocation.invocation_id,
            outcome=question(
                interaction_id="interaction-2", fingerprint="fp-2"
            ).model_copy(update={"invocation_id": entry.invocation.invocation_id}),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert again.outcome == "awaiting_user"
    assert runtime.published == [
        ("parent-1", "interaction-1"),
        ("parent-1", "interaction-2"),
    ]
    assert model.requests == []


async def test_cancel_while_parked_abandons_the_interaction():
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    canceling = await store.request_cancellation(
        run_id,
        expected_state_version=parked.run.state_version,
        command_id="cancel:user",
        cause="user_requested",
        requested_at=NOW,
    )
    assert canceling.run.status == "canceling"

    result = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="user:user-1",
        cancellation_cause="user_requested",
        lifecycle=history.lifecycle,
    )

    assert result.outcome == "aborted"
    assert result.run.status == "canceled"
    assert ("parent-1", "interaction-1") in runtime.abandoned
    assert model.requests == []
    assert history.fold(result.run)["state"] == "canceled"


# -- External waits -----------------------------------------------------------


async def test_late_completion_found_by_polling_completes_the_run():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [waiting()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert parked.outcome == "waiting_external"

    done = await kernel.observe_tool(
        run_id,
        _complete(_agent_entry(parked.run), "late answer", observation_id="poll-1"),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert done.outcome == "final_answer"
    assert _final_text(done.run) == "late answer"
    assert model.requests == []
    assert history.fold(done.run)["state"] == "completed"


async def test_late_failure_found_by_polling_fails_the_run():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [waiting()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    entry = _agent_entry(parked.run)

    done = await kernel.observe_tool(
        run_id,
        ToolObservation(
            observation_id="poll-failed",
            invocation_id=entry.invocation.invocation_id,
            outcome=ToolResult(
                call_id=entry.call_id,
                tool_name=entry.tool_name,
                status="failed",
                content=[],
                artifact_refs=[],
                error_code="remote_failed",
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert done.outcome == "failed"
    assert done.run.terminal_reason == "agent_failed"
    assert model.requests == []
    assert history.fold(done.run)["state"] == "failed"


# -- Recovery -----------------------------------------------------------------


async def test_crash_after_the_synthetic_final_recovers_to_completion(monkeypatch):
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("final words")]
    real_finalize = kernel._finalize_canonical_answer
    crashed = False

    async def crash_once(run, assistant, *, lifecycle):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise ProcessDied
        return await real_finalize(run, assistant, lifecycle=lifecycle)

    monkeypatch.setattr(kernel, "_finalize_canonical_answer", crash_once)
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=history.lifecycle)
    interrupted = await store.load(run_id)
    assert interrupted.status == "finalizing"

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.outcome == "final_answer"
    assert _final_text(recovered.run) == "final words"
    assert model.requests == []
    assert len(runtime.execute_log) == 1
    assert history.fold(recovered.run)["state"] == "completed"


async def test_crash_mid_call_recovers_without_a_model_retry():
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("after restart")]
    runtime.crash_once = True

    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=history.lifecycle)
    interrupted = await store.load(run_id)
    assert interrupted.status == "running"
    assert _agent_entry(interrupted).state in {"accepted", "executing"}

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.outcome == "final_answer"
    assert _final_text(recovered.run) == "after restart"
    assert model.requests == []
    assert "model_retry_scheduled" not in history.events
    assert recovered.run.budget.agent_calls_used == 1
    assert history.fold(recovered.run)["state"] == "completed"


async def test_crash_after_the_synthetic_dispatch_recovers_without_a_model():
    """A crash between the dispatch checkpoint and its public message end."""
    kernel, store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("recovered dispatch")]

    async def die_after_message_start(event_type, run, payload):
        await history.lifecycle(event_type, run, payload)
        if event_type == "message_started":
            raise ProcessDied

    with pytest.raises(ProcessDied):
        await kernel.run(
            run_id, signal=NeverCancelled(), lifecycle=die_after_message_start
        )

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.outcome == "final_answer"
    assert _final_text(recovered.run) == "recovered dispatch"
    assert len(runtime.execute_log) == 1
    assert model.requests == []
    assert "model_retry_scheduled" not in history.events
    assert history.fold(recovered.run)["state"] == "completed"


async def test_crash_inside_the_failure_turn_still_fails_the_run():
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("", status="failed", error_code="agent_error")]
    failure_turns = 0

    async def die_in_failure_turn(event_type, run, payload):
        nonlocal failure_turns
        await history.lifecycle(event_type, run, payload)
        if event_type == "message_started" and run.tool_batches:
            entry = run.tool_batches[0].entries[0]
            if entry.state == "terminal" and failure_turns == 0:
                failure_turns += 1
                raise ProcessDied

    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=die_in_failure_turn)

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.outcome == "failed"
    assert recovered.run.terminal_reason == "agent_failed"
    assert model.requests == []
    assert history.fold(recovered.run)["state"] == "failed"


# -- Check rules ----------------------------------------------------------------


class DenyingAgentRuntime(AgentRuntime):
    """A deny rule names the Agent, so accept refuses the call."""

    async def accept(self, invocation):
        from execution.orchestrator.ports import AgentCallDenied

        raise AgentCallDenied("rule_deny_travel")


async def test_a_deny_rule_cancels_the_run_for_policy_without_dispatch():
    from execution.orchestrator.public_projection import canonical_settlement_payload
    from session.projection import resolve_state
    from session.waiter import CorrelatedOutcome

    runtime = DenyingAgentRuntime()
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        runtime=runtime
    )

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.run.status == "canceled"
    assert result.run.cancellation_cause == "policy"
    assert result.run.terminal_reason == "agent_call_denied"
    assert result.run.denied_rule_id == "rule_deny_travel"
    assert runtime.execute_log == []
    assert model.requests == []
    payload = canonical_settlement_payload(result.run)
    assert payload["cancellation_code"] == "policy"
    assert payload["rule_id"] == "rule_deny_travel"
    # The public history and the settlement fold under the closed contract.
    assert history.fold(result.run)["state"] == "canceled"
    outcome = CorrelatedOutcome(seen=True, run_id=run_id, settlement=payload)
    state, reason = resolve_state(outcome, closed=False)
    assert state == "denied"
    assert reason == "The owner's check rule rule_deny_travel denied this agent call."


@pytest.mark.parametrize(
    "crash_at", ["before_flush", "before_turn_end", "before_settlement"]
)
async def test_a_deny_survives_a_restart_and_still_ends_denied(monkeypatch, crash_at):
    runtime = DenyingAgentRuntime()
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        runtime=runtime
    )
    real_checkpoint = kernel._checkpoint
    real_terminate = kernel._terminate
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        command = kwargs.get("command_id", "")
        if crash_at == "before_flush" and not crashed:
            if command.startswith("complete-tool-batch:"):
                crashed = True
                raise ProcessDied
        return await real_checkpoint(run, *args, **kwargs)

    async def terminate(run, **kwargs):
        nonlocal crashed
        if crash_at == "before_settlement" and not crashed:
            crashed = True
            raise ProcessDied
        return await real_terminate(run, **kwargs)

    async def lifecycle(event_type, run, payload):
        nonlocal crashed
        if crash_at == "before_turn_end" and not crashed:
            if event_type == "turn_completed":
                crashed = True
                raise ProcessDied
        await history.lifecycle(event_type, run, payload)

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    monkeypatch.setattr(kernel, "_terminate", terminate)
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.run.status == "canceled"
    assert recovered.run.cancellation_cause == "policy"
    assert recovered.run.terminal_reason == "agent_call_denied"
    assert recovered.run.denied_rule_id == "rule_deny_travel"
    assert runtime.execute_log == []
    assert model.requests == []
    assert history.fold(recovered.run)["state"] == "canceled"


async def test_a_deny_during_a_user_cancel_leaves_the_cancel_to_finish(monkeypatch):
    kernel, store, _model, _runtime, history, run_id = await make_passthrough_kernel(
        runtime=DenyingAgentRuntime()
    )
    real_terminate = kernel._terminate

    async def cancel_first(run, **kwargs):
        # The user's Stop wins the race to the Run just before the deny settles.
        current = await store.load(run_id)
        requested = await store.request_cancellation(
            run_id,
            expected_state_version=current.state_version,
            command_id="cancel:user",
            cause="user_requested",
            requested_at=NOW,
        )
        assert requested.outcome == "accepted"
        return await real_terminate(run, **kwargs)

    monkeypatch.setattr(kernel, "_terminate", cancel_first)

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "cancellation_pending"
    assert (await store.load(run_id)).cancellation_cause == "user_requested"


@pytest.mark.parametrize("restart", [False, True])
async def test_an_agent_failing_with_the_deny_code_is_an_agent_failure(
    monkeypatch, restart
):
    # The remote Agent chooses its own error code. An accepted call that ran is
    # never a rule's refusal, whatever code the Agent returns.
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [
        answer("", status="failed", error_code="agent_call_denied")
    ]
    if restart:
        real_terminate = kernel._terminate
        crashed = False

        async def terminate(run, **kwargs):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise ProcessDied
            return await real_terminate(run, **kwargs)

        monkeypatch.setattr(kernel, "_terminate", terminate)
        with pytest.raises(ProcessDied):
            await kernel.run(
                run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
            )

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "failed"
    assert result.run.status == "failed"
    assert result.run.terminal_reason == "agent_failed"
    assert result.run.cancellation_cause is None
    assert len(runtime.execute_log) == 1
    assert model.requests == []
    assert history.fold(result.run)["state"] == "failed"


async def test_a_crash_while_recording_the_deny_still_ends_denied(monkeypatch):
    # The budget unit is already spent (max_agent_calls is 1). The rule and the
    # refused entry are stored together, so the restart finds the refusal and
    # does not count the call against the budget again.
    runtime = DenyingAgentRuntime()
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel(
        runtime=runtime
    )
    real_checkpoint = kernel._checkpoint
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        stored = await real_checkpoint(run, *args, **kwargs)
        if not crashed and "denied_rule_id" in kwargs["updates"]:
            # The process dies just after the rule is first stored.
            crashed = True
            raise ProcessDied
        return stored

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=history.lifecycle)
    monkeypatch.setattr(kernel, "_checkpoint", real_checkpoint)

    recovered = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert recovered.run.status == "canceled"
    assert recovered.run.cancellation_cause == "policy"
    assert recovered.run.terminal_reason == "agent_call_denied"
    assert recovered.run.denied_rule_id == "rule_deny_travel"
    assert runtime.execute_log == []
    assert model.requests == []


async def test_an_agent_failing_with_the_deny_code_after_a_question_is_a_failure():
    # The surface entry that forwarded the question is closed with a copy of
    # the Agent's result, code included. It was never accepted either, yet no
    # rule refused anything.
    kernel, _store, model, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [question()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    entry = _agent_entry(parked.run)

    done = await kernel.observe_tool(
        run_id,
        ToolObservation(
            observation_id="obs-denied-code",
            invocation_id=entry.invocation.invocation_id,
            outcome=ToolResult(
                call_id=entry.call_id,
                tool_name=entry.tool_name,
                status="failed",
                content=[],
                artifact_refs=[],
                error_code="agent_call_denied",
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )

    assert done.outcome == "failed"
    assert done.run.terminal_reason == "agent_failed"
    assert done.run.cancellation_cause is None
    assert model.requests == []
    assert history.fold(done.run)["state"] == "failed"
