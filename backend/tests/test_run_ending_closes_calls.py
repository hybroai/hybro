"""Every final ending of a Run closes its open Agent calls.

A Run ends for good when the kernel commits a terminal status: completed,
failed (including an interrupted driver that recovery settles), canceled, or
budget_exhausted (including the deadline). After that commit the kernel hands
the Run to its call closeout, which cancels each open call locally and sends
the Agent a best-effort remote cancel through the owner's cancel mechanism. A
Run that suspends or that recovery resumes is not terminal, so its calls are
left alone. The call recovery job closes an open call whose Run has already
ended, so calls stuck from before this change stop filling its batch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

import orchestrator_composition
from execution.orchestrator.a2a_runtime.cancellation import (
    PARKED_CLOSEOUT_REASON,
    persist_local_cancellation,
)
from execution.orchestrator.a2a_runtime.errors import RecoverableCheckpointError
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import (
    apply_observation,
    transition_call,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ARuntimePolicy,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.recovery import A2ACallRecoveryService
from execution.orchestrator.models import TextPart
from tests._orchestrator_a2a_helpers import ledger_record
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    ScriptedModelRuntime,
    final_events,
    make_kernel,
    tool_events,
    user_message,
)
from tests.orchestrator_harness.a2a_in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryHITLApplicationPort,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryRoomEpochStore,
)
from tests.test_orchestrator_a2a_recovery import Checkpoints, Dispatch
from tests.test_orchestrator_remote_cancellation import (
    RecordingDispatch,
    _coordinator,
    _settle,
)
from tests.test_orchestrator_rule_ask import PAUSE, room_chat
from tests.test_orchestrator_wait_semantics import _composed, _pause

SIGNAL = NeverCancelled()
CLOSEOUT_FAILED = "run_call_closeout_failed"
CLOSED = "a2a_call_closed_for_ended_run"


class Closeout:
    """Records the Runs the kernel hands to its call closeout."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.runs = []

    async def __call__(self, run) -> None:
        self.runs.append(run)
        if self.fail:
            raise RuntimeError("ledger unavailable")


# -- The kernel hands every final ending to the call closeout -------------------


async def test_a_completed_run_closes_its_calls():
    kernel, store, _model, _tools = await make_kernel([final_events()])
    kernel.run_call_closeout = closeout = Closeout()
    run_id = next(iter(store.runs))

    result = await kernel.run(run_id, signal=SIGNAL)

    assert result.outcome == "final_answer"
    assert [run.status for run in closeout.runs] == ["completed"]
    assert closeout.runs[0].run_id == run_id


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("failed", "tool_failure"),
        ("failed", "interrupted"),
        ("budget_exhausted", "deadline"),
    ],
    ids=["failed", "interrupted", "deadline"],
)
async def test_a_run_ended_by_terminalize_closes_its_calls(status, reason):
    kernel, store, _model, _tools = await make_kernel([final_events()])
    kernel.run_call_closeout = closeout = Closeout()
    run_id = next(iter(store.runs))

    result = await kernel.terminalize(run_id, status=status, reason=reason)

    assert result.run.status == status
    assert [run.status for run in closeout.runs] == [status]


async def test_a_stopped_run_closes_its_calls():
    kernel, store, _model, _tools = await make_kernel([final_events()])
    kernel.run_call_closeout = closeout = Closeout()
    run_id = next(iter(store.runs))
    run = store.runs[run_id]
    requested = await store.request_cancellation(
        run_id,
        expected_state_version=run.state_version,
        command_id=f"cancel:{run_id}:user_requested",
        cause="user_requested",
        requested_at=NOW,
    )
    assert requested.outcome == "accepted"

    result = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="cancellation requested",
        cancellation_cause="user_requested",
    )

    assert result.run.status == "canceled"
    assert [run.status for run in closeout.runs] == ["canceled"]


async def test_a_run_that_waits_on_an_agent_keeps_its_calls():
    kernel, store, _model, _tools, _port, room, run_id = await room_chat(
        [tool_events(("call-1", PAUSE, '{"status":"waiting_external"}'))], ask={}
    )
    kernel.run_call_closeout = closeout = Closeout()

    result = await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    assert result.outcome == "waiting_external"
    assert closeout.runs == []


async def test_a_failed_closeout_is_logged_and_the_run_still_ends(caplog):
    kernel, store, _model, _tools = await make_kernel([final_events()])
    kernel.run_call_closeout = Closeout(fail=True)
    run_id = next(iter(store.runs))
    caplog.set_level(logging.WARNING)

    result = await kernel.terminalize(run_id, status="failed", reason="tool_failure")

    assert result.run.status == "failed"
    assert store.runs[run_id].status == "failed"
    (record,) = [r for r in caplog.records if r.getMessage() == CLOSEOUT_FAILED]
    assert record.run_id == run_id


# -- The closeout uses the owner's cancel ------------------------------------------


async def test_the_closeout_cancels_each_open_call_and_tells_the_agent():
    coordinator, ledger, dispatch, calls = await _coordinator()
    close = orchestrator_composition.run_call_closeout(coordinator)

    await close(SimpleNamespace(run_id="run-1", status="completed"))
    await _settle()

    for record in calls.values():
        stored = await ledger.load_by_record_id(record.call_record_id)
        assert stored.state == "canceled"
        assert stored.cancellation_reason == "run_ended"
    # Only calls with a remote task get the remote cancel.
    assert sorted(c.call_record_id for c in dispatch.commands) == sorted(
        calls[name].call_record_id for name in ("aliased", "parked")
    )


async def test_the_closeout_tells_the_agent_of_a_parked_call_the_ending_closed():
    coordinator, ledger, dispatch, calls = await _coordinator()
    # The ending closes the call parked on the Agent's question before its
    # terminal commit (A2AAgentToolRuntime.abandon_parked_interaction), and a
    # Stop already canceled another call and told its Agent.
    parked = calls["parked"]
    await persist_local_cancellation(ledger, parked, reason=PARKED_CLOSEOUT_REASON)
    stopped = calls["aliased"]
    await persist_local_cancellation(ledger, stopped, reason="user_requested")
    close = orchestrator_composition.run_call_closeout(coordinator)

    await close(SimpleNamespace(run_id="run-1", status="failed"))
    await _settle()

    assert [c.call_record_id for c in dispatch.commands] == [parked.call_record_id]


async def test_a_failed_parked_notify_still_cancels_the_open_calls():
    coordinator, ledger, dispatch, calls = await _coordinator()

    async def broken(run_id: str) -> None:
        raise RuntimeError("ledger unavailable")

    coordinator.notify_parked_closeouts = broken
    close = orchestrator_composition.run_call_closeout(coordinator)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await close(SimpleNamespace(run_id="run-1", status="completed"))
    await _settle()

    for record in calls.values():
        stored = await ledger.load_by_record_id(record.call_record_id)
        assert stored.state == "canceled"
    assert len(dispatch.commands) == 2


async def test_a_cancel_left_pending_tells_the_agent_once_recovery_finishes_it():
    call = _working("run-1", "call-1")
    service, ledger, remote = await _recovery([call], {})
    coordinator = service.cancellation
    cas = ledger.cas
    failed = []

    async def terminal_write_fails_once(record, *, expected_state_version):
        if record.state == "canceled" and not failed:
            failed.append(record.call_record_id)
            raise RecoverableCheckpointError("store unavailable")
        return await cas(record, expected_state_version=expected_state_version)

    ledger.cas = terminal_write_fails_once
    close = orchestrator_composition.run_call_closeout(coordinator)

    with pytest.raises(RuntimeError, match="remains pending"):
        await close(SimpleNamespace(run_id="run-1", status="failed"))
    await _settle()
    assert (await ledger.load_by_record_id(call.call_record_id)).state == (
        "cancel_pending"
    )
    assert remote.commands == []

    # The cancellation recovery phase finishes the local cancel.
    assert await coordinator.recover_call(call_record_id=call.call_record_id) == (
        "canceled"
    )
    await _settle()

    assert [c.call_record_id for c in remote.commands] == [call.call_record_id]


async def test_the_real_parked_closeout_is_found_by_the_closeout():
    from execution.orchestrator.a2a_runtime.runtime import A2AAgentToolRuntime

    coordinator, ledger, dispatch, calls = await _coordinator()
    parked = calls["parked"]

    class Finalizer:
        async def finalize_interaction(self, **kwargs) -> None:
            return None

    # The ending's own parked closeout, with only what it uses.
    owner = SimpleNamespace(ledger=ledger, terminal_finalizer=Finalizer())
    closeout = await A2AAgentToolRuntime.abandon_parked_interaction(
        owner,
        call_record_id=parked.call_record_id,
        interaction_id=parked.pending_interaction_id,
    )
    assert closeout.local_cancellation_won is True

    await coordinator.notify_parked_closeouts("run-1")
    await _settle()

    assert [c.call_record_id for c in dispatch.commands] == [parked.call_record_id]


async def test_the_closeout_does_not_wait_for_the_agent():
    release = asyncio.Event()

    class SlowDispatch(RecordingDispatch):
        async def cancel(self, command):
            await release.wait()
            return await super().cancel(command)

    coordinator, ledger, _dispatch, calls = await _coordinator(dispatch=SlowDispatch())
    close = orchestrator_composition.run_call_closeout(coordinator)

    await asyncio.wait_for(close(SimpleNamespace(run_id="run-1", status="failed")), 1)

    stored = await ledger.load_by_record_id(calls["aliased"].call_record_id)
    assert stored.state == "canceled"
    release.set()
    await _settle()


async def test_a_cancel_left_pending_fails_the_closeout():
    class Pending:
        async def notify_parked_closeouts(self, run_id):
            return None

        async def cancel_run(self, run_id, **kwargs):
            return {"call-1": "canceled", "call-2": "cancel_pending"}

    close = orchestrator_composition.run_call_closeout(Pending())

    # The kernel logs the failure; call recovery closes the call later.
    with pytest.raises(RuntimeError, match="remains pending"):
        await close(SimpleNamespace(run_id="run-1", status="completed"))


# -- The composed runtime -------------------------------------------------------


def _record_cancels(runtime) -> list[tuple[str, dict]]:
    # The composed call ledger is a Mongo mock; the Run has no calls on it.
    calls: list[tuple[str, dict]] = []

    async def recording(run_id, **kwargs):
        calls.append((run_id, kwargs))
        return {}

    async def no_parked_calls(run_id):
        return None

    runtime.cancellation_coordinator.cancel_run = recording
    runtime.cancellation_coordinator.notify_parked_closeouts = no_parked_calls
    return calls


async def test_the_composed_kernel_closes_the_calls_when_a_run_ends(monkeypatch):
    model = ScriptedModelRuntime([final_events("done")])
    runtime, _store, _clock, constructed, _events = await _composed(monkeypatch, model)
    cancels = _record_cancels(runtime)

    ended = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )

    assert ended.run.status == "completed"
    assert constructed[-1].run_call_closeout is not None
    assert cancels == [
        (ended.run.run_id, {"reason": "run_ended", "notify_remote": True}),
    ]


async def test_the_composed_call_recovery_closes_calls_of_ended_runs(monkeypatch):
    built: list[A2ACallRecoveryService] = []

    def capture(**kwargs):
        built.append(A2ACallRecoveryService(**kwargs))
        return built[-1]

    monkeypatch.setattr(orchestrator_composition, "A2ACallRecoveryService", capture)
    model = ScriptedModelRuntime([final_events("done")])
    runtime, *_ = await _composed(monkeypatch, model)

    (service,) = built
    assert service.cancellation is runtime.cancellation_coordinator
    assert service.run_store is not None


async def test_recovery_of_a_stopped_run_tells_the_agents_to_stop(monkeypatch):
    model = ScriptedModelRuntime([_pause("input_required"), final_events("done")])
    runtime, store, clock, _constructed, _events = await _composed(monkeypatch, model)
    paused = await runtime.session_host.prompt(
        "room-1", user_message(), client_request_id="request-1"
    )
    run_id = paused.run.run_id
    runtime.session_host.drop_session("room-1")
    parked = await store.load(run_id)
    requested = await store.request_cancellation(
        run_id,
        expected_state_version=parked.state_version,
        command_id=f"cancel:{run_id}:user_requested",
        cause="user_requested",
        requested_at=clock.value,
    )
    assert requested.outcome == "accepted"
    cancels = _record_cancels(runtime)

    await dict(runtime.recovery_cycle.phases)["generic_runs"]()

    assert (await store.load(run_id)).status == "canceled"
    # Recovery cancels the calls before it settles the Run; the closeout of
    # the ending then finds nothing open.
    assert cancels == [
        (run_id, {"reason": "cancellation_recovery", "notify_remote": True}),
        (run_id, {"reason": "run_ended", "notify_remote": True}),
    ]


# -- The call recovery job closes calls of Runs that ended --------------------------


class Runs:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    async def load(self, run_id):
        status = self.statuses.get(run_id)
        return None if status is None else SimpleNamespace(status=status)


def _working(run_id: str, call_id: str):
    # Each call of a Run occupies its own source slot in the assistant batch
    # and has its own remote task.
    slot = int(call_id.rsplit("-", 1)[1])
    task_id = f"task-{slot}"
    record = transition_call(
        ledger_record(run_id=run_id, call_id=call_id).model_copy(
            update={"source_index": slot}
        ),
        to_state="ready_to_dispatch",
        updated_at=NOW,
    )
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    return transition_call(
        record,
        to_state="working",
        updated_at=NOW,
        a2a_task_id=task_id,
        a2a_context_id=f"context-{slot}",
        next_attempt_at=NOW,
    )


async def _recovery(records, statuses, *, batch_limit=100):
    ledger = InMemoryAgentCallLedgerStore()
    for record in records:
        assert await ledger.insert(record) == "accepted"
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    observations = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    remote = RecordingDispatch()
    hitl = InMemoryHITLApplicationPort()
    from execution.orchestrator.a2a_runtime.cancellation import (
        A2ACancellationCoordinator,
    )

    coordinator = A2ACancellationCoordinator(
        ledger=ledger,
        room_epochs=epochs,
        dispatch=remote,
        observations=observations,
        hitl=hitl,
    )
    service = A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=Checkpoints(True),
        room_epochs=epochs,
        dispatch=Dispatch(outcome="accepted"),
        observations=observations,
        recover_dispatch=lambda _record: asyncio.sleep(0),
        hitl=hitl,
        policy=A2ARuntimePolicy(recovery_batch_limit=batch_limit),
        run_store=Runs(statuses),
        cancellation=coordinator,
    )
    return service, ledger, remote


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_recovery_closes_a_stuck_call_of_an_ended_run(status, caplog):
    stuck = _working("run-old", "call-1")
    service, ledger, remote = await _recovery([stuck], {"run-old": status})
    caplog.set_level(logging.INFO)

    recovered = await service.recover_due(due_at=NOW + timedelta(seconds=1))
    await _settle()

    stored = await ledger.load_by_record_id(stuck.call_record_id)
    assert recovered == 1
    assert stored.state == "canceled"
    assert stored.cancellation_reason == "run_ended"
    assert [c.call_record_id for c in remote.commands] == [stuck.call_record_id]
    assert await ledger.list_due(due_at=NOW + timedelta(days=1), limit=100) == []
    (record,) = [r for r in caplog.records if r.getMessage() == CLOSED]
    assert record.run_id == "run-old"
    assert record.run_status == status
    assert record.call_state == "canceled"


async def test_closed_stuck_calls_stop_filling_the_recovery_batch():
    # Due calls are listed by (next_attempt_at, call_record_id); these record
    # ids put both stuck calls ahead of the live one, so they fill a batch of
    # two on their own.
    stuck = [_working("run-old", f"call-{n}") for n in (2, 3)]
    live = _working("run-late", "call-3")
    assert max(r.call_record_id for r in stuck) < live.call_record_id
    service, ledger, _remote = await _recovery(
        [*stuck, live],
        {"run-old": "failed", "run-late": "waiting_external"},
        batch_limit=2,
    )
    due = NOW + timedelta(seconds=1)

    await service.recover_due(due_at=due)

    for record in stuck:
        stored = await ledger.load_by_record_id(record.call_record_id)
        assert stored.state == "canceled"
    untouched = await ledger.load_by_record_id(live.call_record_id)
    assert untouched.next_attempt_at == NOW

    await service.recover_due(due_at=due)

    # Once the stuck calls are closed, the live call gets its turn and is still
    # the Agent's.
    reached = await ledger.load_by_record_id(live.call_record_id)
    assert reached.state == "working"
    assert reached.next_attempt_at > due


@pytest.mark.parametrize("status", ["running", "waiting_external", "canceling", None])
async def test_recovery_leaves_the_calls_of_a_run_that_has_not_ended(status):
    # ``None``: the Run is gone, so recovery cannot tell that it ended.
    call = _working("run-1", "call-1")
    service, ledger, remote = await _recovery([call], {"run-1": status})

    await service.recover_due(due_at=NOW + timedelta(seconds=1))
    await _settle()

    stored = await ledger.load_by_record_id(call.call_record_id)
    assert stored.state == "working"
    assert remote.commands == []


async def test_recovery_logs_the_state_a_concurrent_winner_left(caplog):
    call = _working("run-old", "call-1")
    service, ledger, remote = await _recovery([call], {"run-old": "completed"})
    caplog.set_level(logging.INFO)
    coordinator = service.cancellation
    close = coordinator.close_call_of_ended_run

    async def agent_result_first(call_record_id, *, reason):
        # The Agent's own result lands between recovery's load and recovery's
        # own cancel.
        current = await ledger.load_by_record_id(call_record_id)
        result = NormalizedA2AObservation(
            observation_id="agent-result-1",
            call_record_id=call_record_id,
            source_kind="inspection",
            source_identity="agent:result-1",
            binding_scope=current.endpoint_scope_digest,
            event_kind="terminal",
            status="completed",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            content=[TextPart(text="done")],
        )
        completed = apply_observation(current, result, recent_limit=100)
        outcome = await ledger.cas(
            completed, expected_state_version=current.state_version
        )
        assert outcome == "accepted"
        return await close(call_record_id, reason=reason)

    coordinator.close_call_of_ended_run = agent_result_first

    await service.recover_due(due_at=NOW + timedelta(seconds=1))
    await _settle()

    stored = await ledger.load_by_record_id(call.call_record_id)
    assert stored.state == "completed"
    assert remote.commands == []
    (record,) = [r for r in caplog.records if r.getMessage() == CLOSED]
    assert record.call_state == "completed"


async def test_recovery_skips_a_call_deleted_while_it_closes_it():
    calls = [_working("run-old", f"call-{n}") for n in (1, 2)]
    service, ledger, _remote = await _recovery(calls, {"run-old": "failed"})
    close = service.cancellation.close_call_of_ended_run

    async def delete_then_close(call_record_id, *, reason):
        # Room deletion removes the first call between recovery's load and
        # its close; the second call is put back to stay in the batch.
        if call_record_id == calls[0].call_record_id:
            kept = await ledger.load_by_record_id(calls[1].call_record_id)
            await ledger.delete_by_epoch("room-1", 1)
            assert await ledger.insert(kept) == "accepted"
        return await close(call_record_id, reason=reason)

    service.cancellation.close_call_of_ended_run = delete_then_close

    await service.recover_due(due_at=NOW + timedelta(seconds=1))

    assert await ledger.load_by_record_id(calls[0].call_record_id) is None
    # The rest of the batch still ran.
    second = await ledger.load_by_record_id(calls[1].call_record_id)
    assert second.state == "canceled"


async def test_recovery_logs_a_call_deleted_while_it_closes_it(caplog):
    call = _working("run-old", "call-1")
    service, ledger, _remote = await _recovery([call], {"run-old": "failed"})
    caplog.set_level(logging.INFO)

    async def deleted(call_record_id, *, reason):
        raise KeyError(call_record_id)

    service.cancellation.close_call_of_ended_run = deleted

    await service.recover_due(due_at=NOW + timedelta(seconds=1))

    (record,) = [r for r in caplog.records if r.getMessage() == CLOSED]
    assert (record.call_record_id, record.call_state) == (
        call.call_record_id,
        "deleted",
    )


async def test_recovery_closes_a_call_of_an_ended_run_in_a_retired_room():
    # A room deletion that stopped after retiring the epoch leaves the call
    # open; the fenced cancel would refuse it and stop the batch.
    calls = [_working("run-old", f"call-{n}") for n in (1, 2)]
    service, ledger, _remote = await _recovery(calls, {"run-old": "failed"})
    status, _ = await service.cancellation.room_epochs.deactivate(
        "room-1", 1, "delete-1", deactivated_at=NOW
    )
    assert status == "accepted"

    await service.recover_due(due_at=NOW + timedelta(seconds=1))

    for record in calls:
        stored = await ledger.load_by_record_id(record.call_record_id)
        assert stored.state == "canceled"


async def test_closing_a_call_of_an_ended_run_closes_its_question_before_the_send(
    caplog,
):
    # The parked call's question must close even if the read for the remote
    # cancel fails; the call is terminal and recovery never returns to it.
    coordinator, ledger, dispatch, calls = await _coordinator()
    parked = calls["parked"]
    finalized = []
    finalize = coordinator.terminal_interactions.finalize

    async def record_finalize(record):
        finalized.append(record.call_record_id)
        await finalize(record)

    coordinator.terminal_interactions.finalize = record_finalize
    load = ledger.load_by_record_id
    loads = 0

    async def failing_reload(call_record_id):
        nonlocal loads
        loads += 1
        if loads > 1:
            raise RuntimeError("ledger unavailable")
        return await load(call_record_id)

    ledger.load_by_record_id = failing_reload
    caplog.set_level(logging.WARNING)

    state = await coordinator.close_call_of_ended_run(
        parked.call_record_id, reason="run_ended"
    )
    await _settle()

    assert state == "canceled"
    assert finalized == [parked.call_record_id]
    assert dispatch.commands == []
    assert [r for r in caplog.records if r.getMessage() == "remote_cancellation_failed"]


async def test_a_closeout_that_fails_twice_keeps_both_errors():
    class Broken:
        async def notify_parked_closeouts(self, run_id):
            raise RuntimeError("parked notify failed")

        async def cancel_run(self, run_id, *, reason, notify_remote):
            raise RuntimeError("cancel failed")

    close = orchestrator_composition.run_call_closeout(Broken())

    with pytest.raises(RuntimeError, match="cancel failed") as raised:
        await close(SimpleNamespace(run_id="run-1", status="failed"))
    assert str(raised.value.__cause__) == "parked notify failed"


# -- End to end: a kernel ending with the real closeout -----------------------------


async def test_an_ended_run_cancels_its_open_call_on_the_ledger():
    kernel, store, _model, _tools = await make_kernel([final_events()])
    run_id = next(iter(store.runs))
    open_call = _working(run_id, "call-1")
    service, ledger, remote = await _recovery([open_call], {})
    kernel.run_call_closeout = orchestrator_composition.run_call_closeout(
        service.cancellation
    )

    result = await kernel.run(run_id, signal=SIGNAL)
    await _settle()

    assert result.outcome == "final_answer"
    stored = await ledger.load_by_record_id(open_call.call_record_id)
    assert stored.state == "canceled"
    assert stored.cancellation_reason == "run_ended"
    assert [c.call_record_id for c in remote.commands] == [open_call.call_record_id]


async def test_a_run_a_stop_reached_first_is_not_closed_by_another_ending():
    kernel, store, _model, _tools = await make_kernel([final_events()])
    kernel.run_call_closeout = closeout = Closeout()
    run_id = next(iter(store.runs))
    run = store.runs[run_id]
    requested = await store.request_cancellation(
        run_id,
        expected_state_version=run.state_version,
        command_id=f"cancel:{run_id}:user_requested",
        cause="user_requested",
        requested_at=NOW,
    )
    assert requested.outcome == "accepted"

    result = await kernel.terminalize(run_id, status="failed", reason="tool_failure")

    # The Stop settles the Run later, and that ending runs the closeout.
    assert result.outcome == "cancellation_pending"
    assert store.runs[run_id].status == "canceling"
    assert closeout.runs == []
