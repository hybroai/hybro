"""Canonical wait semantics: suspended Runs and acknowledged calls may sleep.

The Run deadline counts active time only. While a Run waits for the owner or an
external Agent its clock is stopped, generic recovery leaves it dormant, and an
acknowledged remote call keeps being polled until its own wait ceiling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from execution.model_loop.context import ContextCompiler
from execution.orchestrator.a2a_runtime.ledger import transition_call
from execution.orchestrator.budget import BudgetPolicy, resumed_deadline_at
from execution.orchestrator.kernel import OrchestratorKernel
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.models import (
    BudgetState,
    FrozenToolCatalogEntry,
    FrozenToolCatalogSnapshot,
    TextPart,
    ToolObservation,
    ToolResult,
)
from execution.orchestrator.public_projection import PublicProjectionTranslator
from execution.orchestrator.session import (
    DORMANT_RECOVERY_DELAY,
    DefaultRunFactory,
    schedule_run_recovery,
)
from tests.orchestrator_harness.a2a_in_memory import InMemoryRoomEpochStore
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import (
    NOW,
    FixedIDs,
    NeverCancelled,
    ScriptedModelRuntime,
    drain_pass_end_hooks,
    final_events,
    make_run,
    tool_events,
    user_message,
)
from .test_orchestrator_a2a_recovery import Dispatch, service

DEADLINE_SECONDS = 120  # make_run()'s profile deadline


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class PublicHistory:
    """Durable public room events through the production translator."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.translator = PublicProjectionTranslator()
        self.sequence = 0

    async def read(self, _conversation_id, _run_id):
        return list(self.records.values())

    async def lifecycle(self, event_type, run, payload):
        self.sequence += 1
        projected = self.translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=self.sequence,
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


def _pause(status: str):
    return tool_events(("call-1", "fake_agent_pause", f'{{"status":"{status}"}}'))


def _completion(entry, *, observation_id: str, at: datetime) -> ToolObservation:
    assert entry.invocation is not None
    return ToolObservation(
        observation_id=observation_id,
        invocation_id=entry.invocation.invocation_id,
        outcome=ToolResult(
            call_id=entry.call_id,
            tool_name=entry.tool_name,
            status="completed",
            content=[TextPart(text="the late answer")],
            artifact_refs=[],
        ),
        observed_at=at,
    )


async def _kernel(scripts, *, clock: MutableClock):
    store = InMemoryOrchestratorRunStore()
    run = make_run()
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    model = ScriptedModelRuntime(scripts)
    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=model,
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=clock,
        id_factory=FixedIDs(),
    )
    history = PublicHistory()
    kernel.canonical_event_reader = history.read
    await history.lifecycle("run_started", run, {"mode": "fast"})
    return kernel, store, model, run.run_id, history.lifecycle


# -- Run deadline clock -----------------------------------------------------


def test_resumed_deadline_extends_by_the_suspended_duration():
    budget = BudgetState(deadline_at=NOW, suspended_at=NOW - timedelta(minutes=5))

    assert resumed_deadline_at(budget, now=NOW + timedelta(hours=2)) == NOW + (
        timedelta(hours=2, minutes=5)
    )
    assert resumed_deadline_at(BudgetState(deadline_at=NOW), now=NOW) == NOW


@pytest.mark.parametrize(
    ("pause", "status"),
    [("input_required", "awaiting_user"), ("waiting_external", "waiting_external")],
)
async def test_suspended_run_outlives_its_deadline_and_resumes(pause, status):
    clock = MutableClock()
    kernel, _store, model, run_id, lifecycle = await _kernel(
        [_pause(pause), final_events("resumed")], clock=clock
    )

    paused = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert paused.outcome == status
    assert paused.run.budget.suspended_at == NOW
    deadline = paused.run.budget.deadline_at
    assert deadline == NOW + timedelta(seconds=DEADLINE_SECONDS)

    # Days pass. Re-driving the suspended Run must not exhaust its budget.
    clock.value = deadline + timedelta(days=3)
    for _ in range(2):
        again = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
        assert again.outcome == status
        assert again.run.status == status
        assert again.run.budget.deadline_at == deadline
        assert again.run.budget.suspended_at == NOW

    answered_at = clock.value
    entry = again.run.tool_batches[0].entries[0]
    resumed = await kernel.observe_tool(
        run_id,
        _completion(entry, observation_id="late-1", at=answered_at),
        signal=NeverCancelled(),
        lifecycle=lifecycle,
    )

    assert resumed.outcome == "final_answer"
    assert resumed.run.status == "completed"
    assert resumed.run.budget.suspended_at is None
    assert resumed.run.budget.deadline_at == deadline + (answered_at - NOW)
    assert len(model.requests) == 2


async def test_active_time_still_counts_after_resume():
    """The stopped clock is a pause, not a reset: a running Run still expires."""
    clock = MutableClock()
    kernel, _store, _model, run_id, lifecycle = await _kernel(
        [_pause("waiting_external")], clock=clock
    )
    paused = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    entry = paused.run.tool_batches[0].entries[0]

    clock.value = NOW + timedelta(days=1)
    resumed_deadline = NOW + timedelta(days=1, seconds=DEADLINE_SECONDS)

    # After the resume checkpoint, active time runs past the extended deadline
    # before the next model turn.
    async def advance(*_args):
        clock.value = resumed_deadline + timedelta(seconds=1)

    result = await kernel.observe_tool(
        run_id,
        _completion(entry, observation_id="late-2", at=clock.value),
        signal=NeverCancelled(),
        lifecycle=_after("message_completed", advance, lifecycle),
    )

    assert result.outcome == "budget_exhausted"
    assert result.run.terminal_reason == "deadline"
    assert result.run.budget.deadline_at == resumed_deadline


def _after(event_type, callback, inner):
    async def lifecycle(seen_type, run, payload):
        await inner(seen_type, run, payload)
        if seen_type == event_type:
            await callback(seen_type, run, payload)

    return lifecycle


async def test_checkpoint_keeps_the_clock_stopped_across_suspended_statuses():
    clock = MutableClock()
    kernel, store, _model, run_id, _lifecycle = await _kernel([], clock=clock)
    run = await store.load(run_id)

    waiting = await kernel._checkpoint(
        run, updates={"status": "waiting_external"}, command_id="to-waiting"
    )
    assert waiting.budget.suspended_at == NOW

    clock.value = NOW + timedelta(hours=1)
    awaiting = await kernel._checkpoint(
        waiting, updates={"status": "awaiting_user"}, command_id="to-awaiting"
    )
    assert awaiting.budget.suspended_at == NOW
    assert awaiting.budget.deadline_at == run.budget.deadline_at

    clock.value = NOW + timedelta(hours=3)
    # A caller-supplied budget update is merged with the suspension bookkeeping.
    supplied = awaiting.budget.model_copy(update={"agent_calls_used": 4})
    running = await kernel._checkpoint(
        awaiting,
        updates={"status": "running", "budget": supplied},
        command_id="to-running",
    )
    assert running.budget.agent_calls_used == 4
    assert running.budget.suspended_at is None
    assert running.budget.deadline_at == run.budget.deadline_at + timedelta(hours=3)

    clock.value = NOW + timedelta(hours=4)
    unchanged = await kernel._checkpoint(
        running, updates={"active_attempt": None}, command_id="no-status-change"
    )
    assert unchanged.budget == running.budget


# -- Composed generic recovery ----------------------------------------------


class CrashingModelRuntime(ScriptedModelRuntime):
    """Scripted turns, then a non-recoverable process failure."""

    async def stream_turn(self, request, *, signal):
        if not self.scripts:
            self.requests.append(request)
            raise RuntimeError("process died mid-turn")
        async for event in super().stream_turn(request, signal=signal):
            yield event


async def _composed(monkeypatch, model, **composition_kwargs):
    import orchestrator_composition as composition
    from execution.adapters.session_host import RoomSessionHost
    from tests.test_orchestrator_composition import _deps

    clock = MutableClock()

    class DriverDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.value

    monkeypatch.setattr(composition, "datetime", DriverDatetime)
    store = InMemoryOrchestratorRunStore()
    for name, instance in {
        "MongoOrchestratorRunStore": store,
        "GatewayModelRuntime": model,
        "A2AAgentToolRuntime": RecordingFakeToolRuntime(),
    }.items():
        monkeypatch.setattr(
            composition, name, lambda *args, _instance=instance, **kwargs: _instance
        )
    monkeypatch.setattr(
        composition,
        "DefaultRunFactory",
        lambda: DefaultRunFactory(clock=clock, id_factory=FixedIDs()),
    )
    monkeypatch.setattr(
        composition,
        "RoomSessionHost",
        lambda **kwargs: RoomSessionHost(**kwargs, clock=clock),
    )
    constructed: list[OrchestratorKernel] = []

    def create_kernel(**kwargs):
        kernel = OrchestratorKernel(**kwargs, clock=clock)
        constructed.append(kernel)
        return kernel

    monkeypatch.setattr(composition, "OrchestratorKernel", create_kernel)
    events: list[str] = []
    history = PublicHistory()

    async def listener(event):
        events.append(event.event_type)
        await history.lifecycle(
            event.event_type, await store.load(event.run_id), event.payload
        )

    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    deps = _deps()
    runtime = composition.create_orchestrator_runtime(
        mongo=deps.mongo,
        settings_obj=deps.settings,
        llm_gateway=deps.llm_gateway,
        model_registry=deps.model_registry,
        agent_registry=deps.agent_registry,
        exclusion_reader=deps.exclusion_reader,
        room_ownership_reader=deps.room_ownership_reader,
        epoch_store=epochs,
        room_files=deps.room_files,
        message_writer=MagicMock(),
        session_listener=listener,
        canonical_event_reader=history.read,
        **composition_kwargs,
    )
    seed = make_run()
    tool = StaticFakeToolCatalog().resolve(seed, "fake_agent_pause")
    await runtime.session_host.create_session(
        conversation_id=seed.conversation_id,
        profile=seed.profile,
        candidate_scope=seed.candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=FrozenToolCatalogSnapshot(
            catalog_id="test",
            created_at=NOW,
            entries=[
                FrozenToolCatalogEntry(definition=tool.definition, binding=tool.binding)
            ],
        ),
    )
    return runtime, store, clock, constructed, events


async def _generic_recovery(runtime) -> None:
    await dict(runtime.recovery_cycle.phases)["generic_runs"]()


async def test_generic_recovery_leaves_a_suspended_run_dormant(monkeypatch):
    model = ScriptedModelRuntime([_pause("input_required"), final_events("done")])
    runtime, store, clock, constructed, events = await _composed(monkeypatch, model)

    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    assert paused.outcome == "awaiting_user"
    run_id = paused.run.run_id
    parked = await store.load(run_id)
    # The driver hands the claim back with a finite, far-future schedule.
    assert parked.recovery_claim.owner_id is None
    assert parked.recovery_claim.next_attempt_at == NOW + DORMANT_RECOVERY_DELAY
    runtime.session_host.drop_session("room-1")

    clock.value = NOW + timedelta(days=2)
    kernels_before, events_before = len(constructed), len(events)
    await _generic_recovery(runtime)
    await _generic_recovery(runtime)

    current = await store.load(run_id)
    assert current.status == "awaiting_user"
    assert len(constructed) == kernels_before
    assert events[events_before:] == []

    # A Run that is due anyway (an older watchdog row, or an explicit wake) is
    # re-driven once, stays suspended, publishes no new run_started, and goes
    # back to the dormant schedule.
    await schedule_run_recovery(store, run_id, next_attempt_at=clock.value)
    await _generic_recovery(runtime)
    redriven = await store.load(run_id)
    assert redriven.status == "awaiting_user"
    assert len(constructed) == kernels_before + 1
    assert "run_started" not in events[events_before:]
    assert redriven.recovery_claim.next_attempt_at == (
        clock.value + DORMANT_RECOVERY_DELAY
    )
    await _generic_recovery(runtime)
    assert len(constructed) == kernels_before + 1

    # The owner answers through the run-addressed sink three days late.
    answered_at = clock.value
    entry = redriven.tool_batches[0].entries[0]
    await runtime.observation_sink.deliver(
        run_id, _completion(entry, observation_id="answer-1", at=answered_at)
    )
    completed = await store.load(run_id)
    assert completed.status == "completed"
    assert completed.budget.deadline_at == parked.budget.deadline_at + (
        answered_at - NOW
    )


async def test_every_pass_end_reports_its_run(monkeypatch):
    # An owner's answer parked while the Run was busy is applied when a pass
    # ends: the live session, recovery and the run-addressed sink each report
    # the end of their pass.
    model = ScriptedModelRuntime([_pause("input_required"), final_events("done")])
    ended: list[str] = []

    async def pass_ended(run_id: str) -> None:
        ended.append(run_id)

    runtime, store, clock, _constructed, _events = await _composed(
        monkeypatch, model, run_pass_ended=pass_ended
    )

    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    run_id = paused.run.run_id
    await drain_pass_end_hooks(runtime.pass_end_hooks)
    assert ended == [run_id]

    runtime.session_host.drop_session("room-1")
    await schedule_run_recovery(store, run_id, next_attempt_at=clock.value)
    await _generic_recovery(runtime)
    await drain_pass_end_hooks(runtime.pass_end_hooks)
    assert ended == [run_id, run_id]

    entry = (await store.load(run_id)).tool_batches[0].entries[0]
    await runtime.observation_sink.deliver(
        run_id, _completion(entry, observation_id="answer-1", at=clock.value)
    )
    await drain_pass_end_hooks(runtime.pass_end_hooks)
    assert (await store.load(run_id)).status == "completed"
    assert ended == [run_id, run_id, run_id]


async def test_a_failed_recovery_pass_still_reports_its_run(monkeypatch):
    model = ScriptedModelRuntime([_pause("input_required")])
    ended: list[str] = []

    async def pass_ended(run_id: str) -> None:
        ended.append(run_id)

    runtime, store, clock, _constructed, _events = await _composed(
        monkeypatch, model, run_pass_ended=pass_ended
    )
    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    run_id = paused.run.run_id
    runtime.session_host.drop_session("room-1")
    await drain_pass_end_hooks(runtime.pass_end_hooks)
    ended.clear()

    async def fails(self, *_args, **_kwargs):
        raise RuntimeError("recovered pass failed")

    monkeypatch.setattr(OrchestratorKernel, "run", fails)
    await schedule_run_recovery(store, run_id, next_attempt_at=clock.value)
    await _generic_recovery(runtime)
    await drain_pass_end_hooks(runtime.pass_end_hooks)

    assert ended == [run_id]
    assert (await store.load(run_id)).recovery_claim.owner_id is None


async def test_a_failing_pass_end_hook_is_logged_not_raised(caplog):
    import logging

    from execution.orchestrator.a2a_runtime.observations import RunPassEndHooks

    async def pass_ended(_run_id: str) -> None:
        raise RuntimeError("hook secret-detail")

    hooks = RunPassEndHooks(pass_ended)
    with caplog.at_level(logging.WARNING):
        hooks.notify("run-1", conversation_id="room-1")
        await drain_pass_end_hooks(hooks)

    [record] = [
        r for r in caplog.records if r.getMessage() == "run_pass_end_hook_failed"
    ]
    assert record.run_id == "run-1"
    assert "secret-detail" not in caplog.text


async def test_a_cancelled_pass_reports_nothing():
    from execution.orchestrator.a2a_runtime.observations import RunPassEndHooks

    ended: list[str] = []

    async def pass_ended(run_id: str) -> None:
        ended.append(run_id)

    hooks = RunPassEndHooks(pass_ended)
    with pytest.raises(asyncio.CancelledError):
        with hooks.after_pass(lambda: "run-1", conversation_id="room-1"):
            raise asyncio.CancelledError
    with pytest.raises(RuntimeError):
        with hooks.after_pass(lambda: "run-2", conversation_id="room-1"):
            raise RuntimeError("the pass failed")
    with hooks.after_pass(lambda: "run-3", conversation_id="room-1"):
        pass
    await drain_pass_end_hooks(hooks)

    assert ended == ["run-2", "run-3"]


async def test_closed_hooks_start_no_more():
    from execution.orchestrator.a2a_runtime.observations import RunPassEndHooks

    ended: list[str] = []

    async def pass_ended(run_id: str) -> None:
        ended.append(run_id)

    hooks = RunPassEndHooks(pass_ended)
    hooks.notify("run-1", conversation_id="room-1")
    hooks.close()
    hooks.notify("run-2", conversation_id="room-1")
    await drain_pass_end_hooks(hooks)

    assert ended == ["run-1"]


async def test_waiting_external_run_completes_from_a_late_result(monkeypatch):
    model = ScriptedModelRuntime([_pause("waiting_external"), final_events("done")])
    runtime, store, clock, _constructed, _events = await _composed(monkeypatch, model)

    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    assert paused.outcome == "waiting_external"
    runtime.session_host.drop_session("room-1")
    clock.value = NOW + timedelta(days=6)
    await _generic_recovery(runtime)
    assert (await store.load(paused.run.run_id)).status == "waiting_external"

    entry = paused.run.tool_batches[0].entries[0]
    await runtime.observation_sink.deliver(
        paused.run.run_id, _completion(entry, observation_id="late", at=clock.value)
    )
    assert (await store.load(paused.run.run_id)).status == "completed"


async def test_run_addressed_resume_rearms_the_watchdog_at_the_new_deadline(
    monkeypatch,
):
    model = CrashingModelRuntime([_pause("input_required")])
    runtime, store, clock, constructed, events = await _composed(monkeypatch, model)
    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    run_id = paused.run.run_id
    runtime.session_host.drop_session("room-1")

    clock.value = NOW + timedelta(hours=5)
    entry = paused.run.tool_batches[0].entries[0]
    with pytest.raises(RuntimeError, match="process died"):
        await runtime.observation_sink.deliver(
            run_id, _completion(entry, observation_id="answer-1", at=clock.value)
        )

    crashed = await store.load(run_id)
    new_deadline = paused.run.budget.deadline_at + timedelta(hours=5)
    assert crashed.status == "running"
    assert crashed.budget.deadline_at == new_deadline
    assert crashed.recovery_claim.next_attempt_at == new_deadline

    clock.value = new_deadline - timedelta(seconds=1)
    kernels_before = len(constructed)
    await _generic_recovery(runtime)
    assert len(constructed) == kernels_before
    assert (await store.load(run_id)).status == "running"

    clock.value = new_deadline + timedelta(seconds=1)
    events_before = len(events)
    await _generic_recovery(runtime)
    recovered = await store.load(run_id)
    assert len(constructed) == kernels_before + 1
    assert "run_started" in events[events_before:]
    assert recovered.status == "budget_exhausted"
    assert recovered.terminal_reason == "deadline"


# -- Agent-call deadline ----------------------------------------------------


def _working(*, task_id: str | None = "task-1"):
    record = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    return transition_call(
        record,
        to_state="working",
        updated_at=NOW,
        a2a_task_id=task_id,
        a2a_context_id="context-1" if task_id else None,
    )


def _continuation_pending(*, task_id: str | None = "task-1"):
    return transition_call(
        _working(task_id=task_id), to_state="continuation_pending", updated_at=NOW
    )


# ledger_record() freezes the call deadline at NOW and accepts the call at NOW.
PAST_FROZEN_DEADLINE = NOW + timedelta(days=3)


@pytest.mark.parametrize("build", [_working, _continuation_pending])
async def test_acknowledged_call_survives_its_frozen_deadline(build):
    record = build()
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="accepted"))

    await runtime.recover_call(record, now=PAST_FROZEN_DEADLINE)

    current = await ledger.load_by_record_id(record.call_record_id)
    assert current.state == record.state
    assert current.error_code is None
    assert current.next_attempt_at is not None
    assert current.next_attempt_at > PAST_FROZEN_DEADLINE


async def test_acknowledged_call_after_continuation_survives_its_frozen_deadline():
    """A continuation returns the call to working with the original deadline."""
    from tests.test_orchestrator_a2a_hitl_recovery_auth import (
        questionnaire_answers,
        setup_waiting,
    )

    (
        coordinator,
        waiting_ledger,
        _hitl,
        _refs,
        _dispatch,
        call,
        route,
    ) = await setup_waiting()
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    record = await waiting_ledger.load_by_record_id(call.call_record_id)
    assert record.continuation_command is not None
    assert record.dispatch_snapshot.deadline_at == NOW
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="accepted"))

    await runtime.recover_call(record, now=PAST_FROZEN_DEADLINE)

    current = await ledger.load_by_record_id(record.call_record_id)
    assert current.state == "working"
    assert current.error_code is None


async def test_late_result_is_found_by_polling_past_the_frozen_deadline():
    record = _working()
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="terminal"))

    await runtime.recover_call(record, now=PAST_FROZEN_DEADLINE)

    current = await ledger.load_by_record_id(record.call_record_id)
    assert current.state == "completed"


@pytest.mark.parametrize("build", [_working, _continuation_pending])
async def test_call_without_a_task_alias_still_expires_at_its_deadline(build):
    record = build(task_id=None)
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="accepted"))

    await runtime.recover_call(record, now=PAST_FROZEN_DEADLINE)

    current = await ledger.load_by_record_id(record.call_record_id)
    assert current.state == "expired"
    assert current.error_code == "call_deadline_exceeded"


async def test_acknowledged_call_expires_past_the_wait_ceiling():
    record = _working()
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="accepted"))

    await runtime.recover_call(
        record, now=record.accepted_at + timedelta(hours=168, seconds=1)
    )

    current = await ledger.load_by_record_id(record.call_record_id)
    assert current.state == "expired"
    assert current.error_code == "call_deadline_exceeded"


async def test_wait_ceiling_is_configurable():
    record = _working()
    runtime, ledger, _ = await service(record, dispatch=Dispatch(outcome="accepted"))
    runtime.call_wait_ceiling = timedelta(hours=48)

    await runtime.recover_call(record, now=record.accepted_at + timedelta(hours=47))
    assert (await ledger.load_by_record_id(record.call_record_id)).state == "working"

    current = await ledger.load_by_record_id(record.call_record_id)
    await runtime.recover_call(current, now=record.accepted_at + timedelta(hours=49))
    assert (await ledger.load_by_record_id(record.call_record_id)).state == "expired"


def test_composition_injects_the_configured_wait_ceiling():
    from types import SimpleNamespace

    import orchestrator_composition as composition
    from tests.test_orchestrator_composition import _deps

    deps = _deps()
    settings = SimpleNamespace(
        **vars(deps.settings), orchestrator_call_wait_ceiling_hours=12.5
    )
    captured = {}
    real = composition.A2ACallRecoveryService

    def capture(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(composition, "A2ACallRecoveryService", capture)
        composition.create_orchestrator_runtime(
            mongo=deps.mongo,
            settings_obj=settings,
            llm_gateway=deps.llm_gateway,
            model_registry=deps.model_registry,
            agent_registry=deps.agent_registry,
            exclusion_reader=deps.exclusion_reader,
            room_ownership_reader=deps.room_ownership_reader,
            epoch_store=deps.epoch_store,
            room_files=deps.room_files,
            message_writer=MagicMock(),
        )

    assert captured["call_wait_ceiling"] == timedelta(hours=12.5)
