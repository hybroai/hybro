from __future__ import annotations

import asyncio

import pytest

from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.context import ContextCompiler
from execution.orchestrator.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from execution.orchestrator.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)
from execution.orchestrator.kernel import KernelRunResult, OrchestratorKernel
from execution.orchestrator.lifecycle import LifecycleEmitter
from execution.orchestrator.models import TextPart, ToolObservation, ToolResult
from execution.orchestrator.session import (
    DefaultRunFactory,
    RoomAgentSession,
    SessionConflict,
)
from tests._orchestrator_helpers import (
    NOW,
    FixedClock,
    FixedIDs,
    ScriptedModelRuntime,
    final_events,
    make_run,
    session_config,
    tool_events,
    user_message,
)


def make_session(scripts, *, lifecycle=None):
    store = InMemoryOrchestratorRunStore()
    runtime = ScriptedModelRuntime(scripts)
    catalog = StaticFakeToolCatalog()
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=runtime,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=catalog,
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )
    config = session_config()
    return RoomAgentSession(
        config=config,
        kernel=kernel,
        run_store=store,
        run_factory=DefaultRunFactory(
            clock=FixedClock(),
            id_factory=FixedIDs(),
        ),
        lifecycle=lifecycle,
        clock=FixedClock(),
    ), runtime


@pytest.mark.asyncio
async def test_session_prompt_subscribe_and_awaited_idle_listener_barrier():
    listener_finished = False
    event_types = []

    async def listener(event):
        nonlocal listener_finished
        event_types.append(event.event_type)
        if event.event_type == "session_idle":
            await asyncio.sleep(0)
            listener_finished = True

    lifecycle = LifecycleEmitter()
    lifecycle.subscribe(listener)
    session, _ = make_session([final_events()], lifecycle=lifecycle)

    result = await session.prompt(user_message(), client_request_id="request-1")
    await session.wait_for_idle()

    assert result.outcome == "final_answer"
    assert listener_finished is True
    assert event_types[-2:] == ["run_final_answer_ready", "session_idle"]
    assert {
        "turn_started",
        "model_attempt_started",
        "message_completed",
        "turn_completed",
    }.issubset(event_types)


@pytest.mark.asyncio
async def test_wait_for_idle_waits_for_terminal_listener_settlement():
    idle_listener_entered = asyncio.Event()
    release_listener = asyncio.Event()

    async def listener(event):
        if event.event_type == "session_idle":
            idle_listener_entered.set()
            await release_listener.wait()

    lifecycle = LifecycleEmitter(settlement_timeout_seconds=30)
    lifecycle.subscribe(listener)
    session, _ = make_session([final_events()], lifecycle=lifecycle)
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-1")
    )
    await idle_listener_entered.wait()
    idle_task = asyncio.create_task(session.wait_for_idle())
    await asyncio.sleep(0)

    assert idle_task.done() is False
    release_listener.set()
    await prompt_task
    await idle_task


@pytest.mark.asyncio
async def test_session_generic_observation_resumes_suspended_run():
    session, runtime = make_session(
        [
            tool_events(("wait", "fake_agent_pause", '{"status":"waiting_external"}')),
            final_events("resumed"),
        ]
    )
    waiting = await session.prompt(user_message(), client_request_id="request-1")
    assert waiting.outcome == "waiting_external"

    result = await session.observe_tool(
        ToolObservation(
            observation_id="observation-1",
            invocation_id="wait",
            outcome=ToolResult(
                call_id="wait",
                tool_name="fake_agent_pause",
                status="completed",
                content=[TextPart(text="done")],
                artifact_refs=[],
            ),
            observed_at=NOW,
        )
    )
    assert result.outcome == "final_answer"
    assert len(runtime.requests) == 2


@pytest.mark.asyncio
async def test_session_emits_tool_lifecycle_inventory():
    event_types = []

    async def listener(event):
        event_types.append(event.event_type)

    lifecycle = LifecycleEmitter()
    lifecycle.subscribe(listener)
    session, _ = make_session(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')),
            final_events(),
        ],
        lifecycle=lifecycle,
    )

    await session.prompt(user_message(), client_request_id="request-1")
    await asyncio.sleep(0)

    assert "tool_execution_started" in event_types
    assert "tool_execution_completed" in event_types


@pytest.mark.asyncio
async def test_tool_result_message_waits_for_agent_projection_settlement():
    projection_entered = asyncio.Event()
    release_projection = asyncio.Event()

    async def listener(event):
        if (
            event.event_type == "message_completed"
            and event.payload.get("message_kind") == "tool_result"
        ):
            projection_entered.set()
            await release_projection.wait()

    lifecycle = LifecycleEmitter(settlement_timeout_seconds=30)
    lifecycle.subscribe(listener)
    session, _ = make_session(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')),
            final_events(),
        ],
        lifecycle=lifecycle,
    )

    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-1")
    )
    await projection_entered.wait()
    await asyncio.sleep(0)

    assert prompt_task.done() is False
    release_projection.set()
    result = await prompt_task
    assert result.outcome == "final_answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase_status", "phase_outcome"),
    [
        ("waiting_external", "waiting_external"),
        ("awaiting_user", "awaiting_user"),
    ],
)
async def test_shutdown_immediately_reschedules_interrupted_tool_and_hitl_continuations(
    phase_status,
    phase_outcome,
):
    store = InMemoryOrchestratorRunStore()
    observation_started = asyncio.Event()

    class PhaseBlockingKernel:
        async def run(self, run_id, *, signal, lifecycle=None):
            del signal, lifecycle
            run = await store.load(run_id)
            candidate = run.model_copy(
                update={
                    "status": phase_status,
                    "state_version": run.state_version + 1,
                }
            )
            saved = await store.cas_mutate(
                candidate,
                expected_state_version=run.state_version,
                command_id=f"enter:{phase_status}",
            )
            return KernelRunResult(phase_outcome, saved.run)

        async def observe_tool(self, run_id, observation, *, signal, lifecycle=None):
            del run_id, observation, signal, lifecycle
            observation_started.set()
            await asyncio.Event().wait()

    session = RoomAgentSession(
        config=session_config(),
        kernel=PhaseBlockingKernel(),
        run_store=store,
        run_factory=DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()),
        clock=FixedClock(),
    )
    waiting = await session.prompt(user_message(), client_request_id="request-1")
    assert waiting.outcome == phase_outcome
    observation_task = asyncio.create_task(
        session.observe_tool(
            ToolObservation(
                observation_id=f"observation:{phase_status}",
                invocation_id="call-1",
                outcome=ToolResult(
                    call_id="call-1",
                    tool_name="fake_agent_pause",
                    status="completed",
                    content=[],
                    artifact_refs=[],
                ),
                observed_at=NOW,
            )
        )
    )
    await observation_started.wait()

    await session.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await observation_task

    saved = next(iter(store.runs.values()))
    assert saved.status == phase_status
    assert saved.recovery_claim.next_attempt_at == NOW
    assert saved.recovery_claim.next_attempt_at < saved.budget.deadline_at


async def test_session_events_carry_delivery_correlation_fields():
    """SSE work-log listeners must address room/message without a store hit."""
    observed = []

    async def listener(event):
        observed.append(event)

    lifecycle = LifecycleEmitter()
    lifecycle.subscribe(listener)
    session, _ = make_session(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')),
            final_events(),
        ],
        lifecycle=lifecycle,
    )

    await session.prompt(user_message(), client_request_id="request-run-1")
    await asyncio.sleep(0)

    assert observed
    for event in observed:
        assert event.room_id == "room-1"
        assert event.client_request_id == "request-run-1"
        assert event.user_message_id


@pytest.mark.asyncio
async def test_session_rejects_second_prompt_while_active_and_abort_becomes_idle():
    entered = asyncio.Event()
    release = asyncio.Event()
    store = InMemoryOrchestratorRunStore()

    class BlockingKernel:
        async def run(self, run_id, *, signal, lifecycle=None):
            del lifecycle
            entered.set()
            await release.wait()
            run = await store.load(run_id)
            return KernelRunResult("failed", run)

    session = RoomAgentSession(
        config=session_config(),
        kernel=BlockingKernel(),
        run_store=store,
        run_factory=DefaultRunFactory(
            clock=FixedClock(),
            id_factory=FixedIDs(),
        ),
        clock=FixedClock(),
    )
    first = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-1")
    )
    await entered.wait()
    with pytest.raises(SessionConflict, match="already active"):
        await session.prompt(user_message("second"), client_request_id="request-2")
    release.set()
    await first
    await session.wait_for_idle()


@pytest.mark.asyncio
async def test_client_request_reuse_with_different_fingerprint_conflicts():
    store = InMemoryOrchestratorRunStore()
    first = make_run()
    second = first.model_copy(
        update={
            "run_id": "run-2",
            "request": first.request.model_copy(
                update={"request_fingerprint": "different"}
            ),
        }
    )

    assert (await store.create(first, command_id="first")).outcome == "accepted"
    assert (await store.create(second, command_id="second")).outcome == "conflict"


@pytest.mark.asyncio
async def test_replayed_active_client_request_does_not_start_second_kernel():
    entered = asyncio.Event()
    release = asyncio.Event()
    store = InMemoryOrchestratorRunStore()

    class BlockingKernel:
        calls = 0

        async def run(self, run_id, *, signal, lifecycle=None):
            del signal, lifecycle
            self.calls += 1
            entered.set()
            await release.wait()
            run = await store.load(run_id)
            return KernelRunResult("failed", run)

    kernel = BlockingKernel()

    def session():
        return RoomAgentSession(
            config=session_config(),
            kernel=kernel,
            run_store=store,
            run_factory=DefaultRunFactory(
                clock=FixedClock(),
                id_factory=FixedIDs(),
            ),
            clock=FixedClock(),
        )

    owner = session()
    replay = session()
    owner_task = asyncio.create_task(
        owner.prompt(user_message(), client_request_id="request-shared")
    )
    await entered.wait()

    with pytest.raises(SessionConflict, match="replayed Run is already active"):
        await replay.prompt(user_message(), client_request_id="request-shared")
    with pytest.raises(SessionConflict, match="no active Run"):
        await replay.continue_run()
    await replay.abort()

    assert kernel.calls == 1
    assert len(store.runs) == 1
    release.set()
    await owner_task


@pytest.mark.asyncio
async def test_concurrent_prompt_and_abort_only_signals_durably_canceling_run():
    entered = asyncio.Event()
    store = InMemoryOrchestratorRunStore()
    events = []

    class AbortableKernel:
        async def run(self, run_id, *, signal, lifecycle=None):
            del lifecycle
            entered.set()
            await signal.wait()
            run = await store.load(run_id)
            return KernelRunResult("cancellation_pending", run)

    async def listener(event):
        events.append(event.event_type)

    lifecycle = LifecycleEmitter()
    lifecycle.subscribe(listener)
    session = RoomAgentSession(
        config=session_config(),
        kernel=AbortableKernel(),
        run_store=store,
        run_factory=DefaultRunFactory(
            clock=FixedClock(),
            id_factory=FixedIDs(),
        ),
        lifecycle=lifecycle,
        clock=FixedClock(),
    )
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-1")
    )
    await entered.wait()
    run = next(iter(store.runs.values()))
    requested = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id=f"cancel:{run.run_id}:user_requested",
        cause="user_requested",
        requested_at=FixedClock().now(),
    )
    assert requested.outcome == "accepted"
    abort_task = asyncio.create_task(session.abort())
    await asyncio.gather(prompt_task, abort_task)
    await asyncio.sleep(0)

    assert events.count("run_canceled") == 0
    assert events.count("session_idle") == 1


@pytest.mark.asyncio
async def test_abort_during_terminal_listener_does_not_start_second_kernel_task():
    store = InMemoryOrchestratorRunStore()
    terminal_listener_entered = asyncio.Event()
    release_listener = asyncio.Event()
    events = []

    class ImmediatelyAbortedKernel:
        calls = 0

        async def run(self, run_id, *, signal, lifecycle=None):
            del signal, lifecycle
            self.calls += 1
            run = await store.load(run_id)
            return KernelRunResult("aborted", run)

    async def listener(event):
        events.append(event.event_type)
        if event.event_type == "run_canceled":
            terminal_listener_entered.set()
            await release_listener.wait()

    kernel = ImmediatelyAbortedKernel()
    lifecycle = LifecycleEmitter(settlement_timeout_seconds=30)
    lifecycle.subscribe(listener)
    session = RoomAgentSession(
        config=session_config(),
        kernel=kernel,
        run_store=store,
        run_factory=DefaultRunFactory(
            clock=FixedClock(),
            id_factory=FixedIDs(),
        ),
        lifecycle=lifecycle,
        clock=FixedClock(),
    )
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-1")
    )
    await terminal_listener_entered.wait()
    with pytest.raises(SessionConflict, match="not been durably claimed"):
        await session.abort()
    release_listener.set()
    await prompt_task

    assert kernel.calls == 1
    assert events.count("run_canceled") == 1
    assert events.count("session_idle") == 1


@pytest.mark.asyncio
async def test_continue_requires_run_and_suspended_run_requires_observation():
    session, _ = make_session(
        [tool_events(("wait", "fake_agent_pause", '{"status":"input_required"}'))]
    )
    with pytest.raises(SessionConflict, match="no active"):
        await session.continue_run()
    await session.prompt(user_message(), client_request_id="request-1")
    with pytest.raises(SessionConflict, match="requires a new ToolObservation"):
        await session.continue_run()
