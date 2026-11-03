"""Best-effort remote cancel on the two user-initiated cancellation paths."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from execution.orchestrator.a2a_runtime.cancellation import A2ACancellationCoordinator
from execution.orchestrator.a2a_runtime.hitl import InMemoryHITLApplicationPort
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryRoomEpochStore,
)
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import transition_call
from execution.orchestrator.a2a_runtime.models import A2ADispatchReceipt
from execution.orchestrator_routing import DualRuntimeRouter

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import NOW
from .test_orchestrator_a2a_hitl import interaction
from .test_orchestrator_a2a_hitl_recovery_auth import _digest_json, questionnaire_spec

QUESTION = questionnaire_spec()


class RecordingDispatch:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands = []

    async def cancel(self, command):
        self.commands.append(command)
        if self.fail:
            raise ConnectionError("agent unreachable")
        return A2ADispatchReceipt(outcome="accepted")


def _call(call_id: str, *, state: str, task_id: str | None):
    # Each call of the Run occupies its own source slot in the assistant batch.
    slot = int(call_id.rsplit("-", 1)[1])
    record = transition_call(
        ledger_record(call_id=call_id).model_copy(update={"source_index": slot}),
        to_state="ready_to_dispatch",
        updated_at=NOW,
    )
    if state == "ready_to_dispatch":
        return record
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    record = transition_call(
        record,
        to_state="working",
        updated_at=NOW,
        a2a_task_id=task_id,
        a2a_context_id=f"context-{slot}" if task_id else None,
    )
    if state == "input_required":
        record = transition_call(
            transition_call(record, to_state="continuation_pending", updated_at=NOW),
            to_state="input_required",
            updated_at=NOW,
            pending_interaction_id=QUESTION.interaction_id,
            interaction_revision=1,
            interaction_fingerprint=_digest_json(QUESTION.model_dump(mode="json")),
        )
    return record


async def _coordinator(*, dispatch=None):
    ledger = InMemoryAgentCallLedgerStore()
    calls = {
        "aliased": _call("call-1", state="working", task_id="task-1"),
        "parked": _call("call-2", state="input_required", task_id="task-2"),
        "unaliased": _call("call-3", state="ready_to_dispatch", task_id=None),
    }
    for record in calls.values():
        assert await ledger.insert(record) == "accepted"
    hitl = InMemoryHITLApplicationPort()
    parked = calls["parked"]
    await hitl.create_or_replay(
        call=parked,
        interaction=QUESTION,
        interaction_fingerprint=parked.interaction_fingerprint,
    )
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    dispatch = dispatch or RecordingDispatch()
    coordinator = A2ACancellationCoordinator(
        ledger=ledger,
        room_epochs=epochs,
        dispatch=dispatch,
        observations=A2AObservationIngress(
            inbox=InMemoryObservationInboxStore(),
            conflicts=InMemoryObservationConflictStore(),
            ledger=ledger,
            authenticator=RejectExternalIngressAuthenticator(),
        ),
        hitl=hitl,
    )
    return coordinator, ledger, dispatch, calls


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_cancel_run_stays_local_by_default():
    coordinator, ledger, dispatch, calls = await _coordinator()

    results = await coordinator.cancel_run("run-1", reason="cancellation_recovery")
    await _settle()

    assert set(results.values()) == {"canceled"}
    assert dispatch.commands == []


async def test_cancel_run_notifies_each_live_call_with_a_task_alias():
    coordinator, ledger, dispatch, calls = await _coordinator()
    # A call that already finished is not canceled remotely.
    finished = _call("call-4", state="working", task_id="task-4")
    assert await ledger.insert(finished) == "accepted"
    await coordinator.cancel_call(
        call_record_id=finished.call_record_id, reason="earlier"
    )

    results = await coordinator.cancel_run(
        "run-1", reason="user:user-1", notify_remote=True
    )
    await _settle()

    assert set(results.values()) == {"canceled"}
    assert sorted(command.call_record_id for command in dispatch.commands) == sorted(
        [calls["aliased"].call_record_id, calls["parked"].call_record_id]
    )
    for command in dispatch.commands:
        persisted = await ledger.load_by_record_id(command.call_record_id)
        assert persisted.cancellation_command == command
        assert command.reason == "user:user-1"


async def test_remote_cancel_failure_is_logged_not_raised(caplog):
    coordinator, ledger, dispatch, calls = await _coordinator(
        dispatch=RecordingDispatch(fail=True)
    )

    with caplog.at_level(logging.WARNING):
        results = await coordinator.cancel_run(
            "run-1", reason="hitl_canceled", notify_remote=True
        )
        await _settle()

    assert set(results.values()) == {"canceled"}
    assert len(dispatch.commands) == 2
    records = [
        r for r in caplog.records if r.getMessage() == "remote_cancellation_failed"
    ]
    assert sorted(r.call_record_id for r in records) == sorted(
        command.call_record_id for command in dispatch.commands
    )
    assert {r.error_type for r in records} == {"ConnectionError"}
    assert "agent unreachable" not in caplog.text


async def test_remote_cancel_does_not_delay_local_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowDispatch(RecordingDispatch):
        async def cancel(self, command):
            started.set()
            await release.wait()
            return await super().cancel(command)

    coordinator, ledger, dispatch, calls = await _coordinator(dispatch=SlowDispatch())

    results = await asyncio.wait_for(
        coordinator.cancel_run("run-1", reason="user:user-1", notify_remote=True),
        timeout=1,
    )
    assert set(results.values()) == {"canceled"}
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await _settle()
    assert len(dispatch.commands) == 2


def _router(coordinator, *, run_store, hitl_port):
    async def reconcile_cancellation(_run):
        return SimpleNamespace(run=SimpleNamespace(status="canceled"))

    router = DualRuntimeRouter.__new__(DualRuntimeRouter)
    router._runtime = SimpleNamespace(
        hitl_port=hitl_port,
        hitl_delivery=SimpleNamespace(emit_checked=AsyncMock(return_value=None)),
        run_store=run_store,
        cancellation_coordinator=coordinator,
        session_host=SimpleNamespace(
            signal_run_cancellation=AsyncMock(),
            reconcile_cancellation=reconcile_cancellation,
        ),
    )
    return router


def _runs():
    run = SimpleNamespace(
        run_id="run-1",
        conversation_id="room-1",
        status="awaiting_user",
        state_version=1,
        cancellation_command_id=None,
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[],
    )
    canceling = SimpleNamespace(
        **{
            **run.__dict__,
            "status": "canceling",
            "state_version": 2,
            "cancellation_command_id": "cancel:run-1:user_requested",
        }
    )
    return run, canceling


async def test_user_message_cancellation_sends_one_remote_cancel_per_aliased_call():
    coordinator, _ledger, dispatch, calls = await _coordinator()
    run, canceling = _runs()
    router = _router(
        coordinator,
        run_store=SimpleNamespace(
            load_by_user_message_id=AsyncMock(return_value=run),
            request_cancellation=AsyncMock(
                return_value=SimpleNamespace(outcome="accepted", run=canceling)
            ),
        ),
        hitl_port=SimpleNamespace(get_eligible_interactions=AsyncMock(return_value=[])),
    )

    ack = await router.route_cancellation_by_user_message(
        "user-1", reason="user:user-1"
    )
    await _settle()

    assert ack.status == "canceled"
    assert sorted(command.call_record_id for command in dispatch.commands) == sorted(
        [calls["aliased"].call_record_id, calls["parked"].call_record_id]
    )


async def test_hitl_cancellation_sends_one_remote_cancel_per_aliased_call():
    coordinator, _ledger, dispatch, calls = await _coordinator()
    run, canceling = _runs()
    route = SimpleNamespace(
        conversation_id="room-1",
        orchestration_run_id="run-1",
        call_record_id=calls["parked"].call_record_id,
        interaction_revision=1,
    )
    router = _router(
        coordinator,
        run_store=SimpleNamespace(
            load=AsyncMock(return_value=run),
            request_cancellation=AsyncMock(
                return_value=SimpleNamespace(outcome="accepted", run=canceling)
            ),
        ),
        hitl_port=SimpleNamespace(
            read_interaction=AsyncMock(
                return_value=(interaction(), route, "fingerprint")
            ),
            abandon=AsyncMock(return_value="accepted"),
        ),
    )
    router._emit_hitl_resolved_events = AsyncMock()

    version = await router.cancel_hitl_interaction(
        conversation_id="room-1", interaction_id="interaction-1", expected_version=1
    )
    await _settle()

    assert version == 1
    assert sorted(command.call_record_id for command in dispatch.commands) == sorted(
        [calls["aliased"].call_record_id, calls["parked"].call_record_id]
    )
    assert {command.reason for command in dispatch.commands} == {"hitl_canceled"}


def _user_cancellation_router(coordinator, *, session_host):
    run, canceling = _runs()
    router = _router(
        coordinator,
        run_store=SimpleNamespace(
            load_by_user_message_id=AsyncMock(return_value=run),
            request_cancellation=AsyncMock(
                return_value=SimpleNamespace(outcome="accepted", run=canceling)
            ),
        ),
        hitl_port=SimpleNamespace(get_eligible_interactions=AsyncMock(return_value=[])),
    )
    router._runtime.session_host = session_host
    return router


async def test_failed_local_cancellation_reconciliation_is_logged_as_deferred(caplog):
    coordinator, *_ = await _coordinator()
    router = _user_cancellation_router(
        coordinator,
        session_host=SimpleNamespace(
            signal_run_cancellation=AsyncMock(),
            reconcile_cancellation=AsyncMock(side_effect=RuntimeError("store down")),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="execution.orchestrator_routing"):
        ack = await router.route_cancellation_by_user_message(
            "user-1", reason="user:user-1"
        )
        await _settle()

    assert ack.status == "cancellation_pending"
    [record] = [
        r
        for r in caplog.records
        if r.getMessage() == "run_cancellation_reconciliation_deferred"
    ]
    assert record.run_id == "run-1"
    assert record.conversation_id == "room-1"
    assert record.client_request_id == "cr-1"
    assert record.error_type == "RuntimeError"


async def test_failed_post_cancellation_signal_is_logged(caplog):
    coordinator, *_ = await _coordinator()
    router = _user_cancellation_router(
        coordinator,
        session_host=SimpleNamespace(
            signal_run_cancellation=AsyncMock(side_effect=RuntimeError("gone")),
            reconcile_cancellation=AsyncMock(
                return_value=SimpleNamespace(run=SimpleNamespace(status="canceled"))
            ),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="execution.orchestrator_routing"):
        ack = await router.route_cancellation_by_user_message(
            "user-1", reason="user:user-1"
        )
        await _settle()

    assert ack.status == "canceled"
    [record] = [
        r for r in caplog.records if r.getMessage() == "run_cancellation_cleanup_failed"
    ]
    assert record.run_id == "run-1"
    assert record.conversation_id == "room-1"
    assert record.error_type == "RuntimeError"
