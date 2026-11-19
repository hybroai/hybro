from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from execution.model_loop.context import ContextCompiler
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.kernel import OrchestratorKernel
from execution.orchestrator.models import (
    AuthorizationBasis,
    CandidateScopeSnapshot,
    ModelStreamEvent,
    OrchestratorRunState,
    TextPart,
    UserMessage,
)
from execution.orchestrator.profiles import (
    ModelRouteConfiguration,
    ProfileConfiguration,
    PromptConfiguration,
    resolve_profile_snapshot,
)
from execution.orchestrator.session import (
    DefaultRunFactory,
    EventCancellationSignal,
    RoomAgentSessionConfig,
)
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedIDs:
    def __init__(self) -> None:
        self.value = 0

    def new_id(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.value}"


class ScriptedModelRuntime:
    def __init__(self, scripts: list[list[ModelStreamEvent]]) -> None:
        self.scripts = list(scripts)
        self.requests = []

    async def stream_turn(self, request, *, signal) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        if signal.cancelled:
            yield ModelStreamEvent(kind="error", error_class="aborted", retryable=False)
            return
        if not self.scripts:
            raise AssertionError("unexpected model turn")
        for event in self.scripts.pop(0):
            yield event


class NeverCancelled:
    cancelled = False

    async def wait(self) -> None:
        await asyncio.Event().wait()


def profile(
    *,
    tool_execution: str = "parallel",
    max_model_turns: int = 5,
    grace_model_turns: int = 1,
    context_window: int = 4096,
    max_output_tokens: int = 256,
    max_agent_calls: int = 10,
    max_input_tokens_total: int | None = 10000,
    max_output_tokens_total: int | None = 5000,
):
    return resolve_profile_snapshot(
        ProfileConfiguration(
            profile_id="fast",
            max_model_turns=max_model_turns,
            grace_model_turns=grace_model_turns,
            max_agent_calls=max_agent_calls,
            max_parallel_calls=min(3, max_agent_calls),
            max_transport_retries_per_call=1,
            max_provider_retries_total=4,
            max_input_tokens_total=max_input_tokens_total,
            max_output_tokens_total=max_output_tokens_total,
            max_compactions=1,
            deadline_seconds=120,
            initial_routing="explicit_agent_first",
            tool_execution=tool_execution,
            finalization="light",
        ),
        model=ModelRouteConfiguration(
            route="test_openai_native",
            provider="openai",
            model_id="gpt-test",
            api="chat_completions",
            supports_native_tools=True,
            supports_provider_strict_schema=True,
            supports_local_structured_action=False,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            temperature=0,
            provider_timeout_seconds=30,
            max_provider_retries=2,
        ),
        prompt=PromptConfiguration(
            prompt_id="test", version="1", rendered_system_prompt="Be useful."
        ),
    )


def session_config(**profile_kwargs) -> RoomAgentSessionConfig:
    return RoomAgentSessionConfig(
        session_id="session-1",
        conversation_id="room-1",
        profile=profile(**profile_kwargs),
        candidate_scope=CandidateScopeSnapshot(
            snapshot_id="scope-1",
            source="test",
            conversation_id="room-1",
            agent_ids=[],
            authorization_basis=AuthorizationBasis(kind="room_member"),
        ),
        room_epoch=1,
        requesting_subject_id="user-1",
        tool_catalog=StaticFakeToolCatalog(),
    )


def user_message(text: str = "hello") -> UserMessage:
    return UserMessage(
        message_id="user-1", content=[TextPart(text=text)], created_at=NOW
    )


def make_run(**profile_kwargs) -> OrchestratorRunState:
    config = session_config(**profile_kwargs)
    return DefaultRunFactory(
        clock=FixedClock(),
        id_factory=FixedIDs(),
    ).create_run(config=config, message=user_message(), client_request_id="request-1")


async def make_kernel(
    scripts: list[list[ModelStreamEvent]],
    *,
    run: OrchestratorRunState | None = None,
    tool_runtime: RecordingFakeToolRuntime | None = None,
    run_store: InMemoryOrchestratorRunStore | None = None,
    supervisor_hitl=None,
):
    run = run or make_run()
    store = run_store or InMemoryOrchestratorRunStore()
    created = await store.create(run, command_id="create")
    assert created.outcome == "accepted"
    runtime = ScriptedModelRuntime(scripts)
    tools = tool_runtime or RecordingFakeToolRuntime()
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=runtime,
        tool_runtime=tools,
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
        supervisor_hitl=supervisor_hitl,
    )
    return kernel, store, runtime, tools


def final_events(text: str = "done") -> list[ModelStreamEvent]:
    return [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(kind="text_delta", attempt=1, delta=text),
        ModelStreamEvent(kind="finish", attempt=1, finish_reason="stop"),
    ]


def tool_events(*calls: tuple[str, str, str]) -> list[ModelStreamEvent]:
    events = [ModelStreamEvent(kind="attempt_started", attempt=1)]
    for call_id, name, arguments_json in calls:
        events.extend(
            [
                ModelStreamEvent(
                    kind="tool_call_start",
                    attempt=1,
                    call_id=call_id,
                    tool_name=name,
                ),
                ModelStreamEvent(
                    kind="tool_call_arguments_delta",
                    attempt=1,
                    call_id=call_id,
                    delta=arguments_json,
                ),
                ModelStreamEvent(kind="tool_call_end", attempt=1, call_id=call_id),
            ]
        )
    events.append(
        ModelStreamEvent(kind="finish", attempt=1, finish_reason="tool_calls")
    )
    return events


__all__ = [
    "EventCancellationSignal",
    "FixedClock",
    "FixedIDs",
    "NOW",
    "NeverCancelled",
    "ScriptedModelRuntime",
    "final_events",
    "make_kernel",
    "make_run",
    "profile",
    "session_config",
    "tool_events",
    "user_message",
]
