from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.context import ContextCompiler
from execution.orchestrator.kernel import (
    KernelConflict,
    KernelRunResult,
    OrchestratorKernel,
)
from execution.orchestrator.lifecycle import LifecycleEmitter
from execution.orchestrator.models import TextPart, ToolObservation, ToolResult
from execution.orchestrator.session import (
    DefaultRunFactory,
    RoomAgentSession,
    SessionConflict,
    execution_owner_id,
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
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
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

        async def terminalize(self, run_id, *, status, reason, lifecycle=None, **kw):
            del kw
            run = await store.load(run_id)
            candidate = run.model_copy(
                update={
                    "status": status,
                    "terminal_reason": reason,
                    "state_version": run.state_version + 1,
                }
            )
            saved = await store.cas_mutate(
                candidate,
                expected_state_version=run.state_version,
                command_id=f"terminalize:{status}:{run.state_version}",
            )
            if lifecycle is not None:
                await lifecycle("run_failed", saved.run, {"status": status})
            return KernelRunResult("failed", saved.run)

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

    # Interrupted executions are failed, never handed back to recovery.
    saved = next(iter(store.runs.values()))
    assert saved.status == "failed"
    assert saved.terminal_reason == "interrupted"
    assert saved.recovery_claim.owner_id is None
    assert saved.recovery_claim.next_attempt_at is None


async def test_live_execution_publishes_and_releases_a_driver_lease():
    """A running driver owns the Run's recovery claim so death is detectable."""
    from execution.orchestrator.session import (
        _EXECUTION_LEASE_SECONDS,
        execution_owner_id,
    )

    store = InMemoryOrchestratorRunStore()
    started = asyncio.Event()
    run_id_holder: list[str] = []

    class BlockingKernel:
        async def run(self, run_id, *, signal, lifecycle=None):
            del signal, lifecycle
            run_id_holder.append(run_id)
            started.set()
            return await asyncio.Event().wait()

        async def terminalize(self, run_id, *, status, reason, lifecycle=None, **kw):
            del kw
            run = await store.load(run_id)
            candidate = run.model_copy(
                update={
                    "status": status,
                    "terminal_reason": reason,
                    "state_version": run.state_version + 1,
                }
            )
            saved = await store.cas_mutate(
                candidate,
                expected_state_version=run.state_version,
                command_id=f"terminalize:{status}:{run.state_version}",
            )
            return KernelRunResult("failed", saved.run)

    config = session_config()
    session = RoomAgentSession(
        config=config,
        kernel=BlockingKernel(),
        run_store=store,
        run_factory=DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()),
        clock=FixedClock(),
    )
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-lease")
    )
    await started.wait()

    owner = execution_owner_id(config.session_id)
    leased = next(iter(store.runs.values()))
    assert leased.recovery_claim.owner_id == owner
    assert leased.recovery_claim.lease_expires_at == NOW + timedelta(
        seconds=_EXECUTION_LEASE_SECONDS
    )

    await session.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await prompt_task

    released = next(iter(store.runs.values()))
    assert released.status == "failed"
    assert released.terminal_reason == "interrupted"
    assert released.recovery_claim.owner_id is None
    assert released.recovery_claim.lease_expires_at is None


class _YieldingTerminalizeKernel:
    """Blocks in run(); terminalize() yields between load and CAS like Mongo I/O."""

    def __init__(self, store, *, fail_settlement=False):
        self.store = store
        self.fail_settlement = fail_settlement
        self.started = asyncio.Event()

    async def run(self, run_id, *, signal, lifecycle=None):
        del run_id, signal, lifecycle
        self.started.set()
        return await asyncio.Event().wait()

    async def terminalize(self, run_id, *, status, reason, lifecycle=None, **kw):
        del lifecycle, kw
        run = await self.store.load(run_id)
        for _ in range(5):
            await asyncio.sleep(0)
        if self.fail_settlement:
            raise KernelConflict("checkpoint failed: fixture")
        saved = await self.store.cas_mutate(
            run.model_copy(
                update={
                    "status": status,
                    "terminal_reason": reason,
                    "state_version": run.state_version + 1,
                }
            ),
            expected_state_version=run.state_version,
            command_id=f"terminalize:{status}:{run.state_version}",
        )
        if saved.outcome != "accepted":
            raise KernelConflict(f"checkpoint failed: {saved.outcome}")
        return KernelRunResult("failed", saved.run)


async def _shutdown_mid_run(kernel, store):
    config = session_config()
    session = RoomAgentSession(
        config=config,
        kernel=kernel,
        run_store=store,
        run_factory=DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()),
        clock=FixedClock(),
    )
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-shutdown")
    )
    await kernel.started.wait()
    await session.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await prompt_task
    return session, config, next(iter(store.runs.values()))


async def test_shutdown_settlement_is_not_raced_by_the_cancelled_waiter():
    """The cancelled prompt must not release the lease under terminalize's CAS."""
    store = InMemoryOrchestratorRunStore()

    _, _, saved = await _shutdown_mid_run(_YieldingTerminalizeKernel(store), store)

    assert saved.status == "failed"
    assert saved.terminal_reason == "interrupted"
    assert saved.recovery_claim.owner_id is None
    assert saved.recovery_claim.next_attempt_at is None


async def test_failed_shutdown_settlement_leaves_the_run_to_interrupted_recovery():
    """Keeping the driver claim makes recovery fail the Run instead of resuming it."""
    from execution.orchestrator.session import interrupted_run_needs_settlement

    store = InMemoryOrchestratorRunStore()
    kernel = _YieldingTerminalizeKernel(store, fail_settlement=True)

    session, config, saved = await _shutdown_mid_run(kernel, store)

    assert saved.status == "running"
    assert saved.recovery_claim.owner_id == execution_owner_id(config.session_id)
    assert saved.recovery_claim.lease_expires_at is not None
    assert interrupted_run_needs_settlement(saved.status, saved.recovery_claim.owner_id)
    assert session._lease_task is None


def test_interrupted_run_settlement_truth_table():
    """Only an active Run whose driver lease lapsed is failed as interrupted."""
    from execution.orchestrator.session import interrupted_run_needs_settlement

    driver = execution_owner_id("room:room-1:epoch:1")
    assert interrupted_run_needs_settlement("running", driver)
    assert interrupted_run_needs_settlement("queued", driver)
    assert interrupted_run_needs_settlement("finalizing", driver)
    # A released lease is an intentional wake, not an interruption.
    assert not interrupted_run_needs_settlement("running", None)
    # Recovery workers own their own leases and must keep re-driving.
    assert not interrupted_run_needs_settlement("running", "recovery-worker:abc")
    # Suspended Runs wait for an answer, cancellation, or their deadline.
    assert not interrupted_run_needs_settlement("waiting_external", driver)
    assert not interrupted_run_needs_settlement("awaiting_user", driver)
    assert not interrupted_run_needs_settlement("canceling", driver)


async def test_abandoned_driver_lease_is_detectable_as_interrupted():
    """A driver that dies without releasing leaves an expired, identifiable lease."""
    from execution.orchestrator.session import interrupted_execution_owner

    store = InMemoryOrchestratorRunStore()
    started = asyncio.Event()

    class BlockingKernel:
        async def run(self, run_id, *, signal, lifecycle=None):
            del run_id, signal, lifecycle
            started.set()
            return await asyncio.Event().wait()

    session = RoomAgentSession(
        config=session_config(),
        kernel=BlockingKernel(),
        run_store=store,
        run_factory=DefaultRunFactory(clock=FixedClock(), id_factory=FixedIDs()),
        clock=FixedClock(),
    )
    prompt_task = asyncio.create_task(
        session.prompt(user_message(), client_request_id="request-abandoned")
    )
    await started.wait()
    abandoned = next(iter(store.runs.values()))
    lease = abandoned.recovery_claim.lease_expires_at
    assert lease is not None
    assert interrupted_execution_owner(abandoned.recovery_claim.owner_id)

    # While the lease is fresh, recovery may not steal the Run.
    blocked = await store.claim_recovery(
        abandoned.run_id,
        expected_state_version=abandoned.state_version,
        owner_id="recovery-worker",
        lease_expires_at=NOW + timedelta(seconds=120),
        claimed_at=NOW,
    )
    assert blocked.outcome == "conflict"

    # Simulate process death: nobody releases, the lease simply lapses.
    later = lease + timedelta(seconds=1)
    claimed = await store.claim_recovery(
        abandoned.run_id,
        expected_state_version=abandoned.state_version,
        owner_id="recovery-worker",
        lease_expires_at=later + timedelta(seconds=60),
        claimed_at=later,
    )
    assert claimed.outcome == "accepted"

    prompt_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prompt_task


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
        assert event.conversation_id == "room-1"
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
