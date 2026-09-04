from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
from execution.orchestrator.a2a_runtime.ledger import apply_observation, transition_call
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    NormalizedA2AObservation,
)

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import NOW


class Dispatch:
    def __init__(self) -> None:
        self.commands = []

    async def cancel(self, command):
        self.commands.append(command)
        return A2ADispatchReceipt(outcome="accepted")

    async def inspect_cancellation(self, command):
        self.commands.append(command)
        return A2ADispatchReceipt(outcome="accepted")


class OneConflictLedger(InMemoryAgentCallLedgerStore):
    def __init__(self) -> None:
        super().__init__()
        self.conflicted = False

    async def cas(self, record, *, expected_state_version):
        if not self.conflicted and record.state == "cancel_pending":
            current = await self.load_by_record_id(record.call_record_id)
            assert current is not None
            bumped = current.model_copy(
                update={
                    "state_version": current.state_version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            assert (
                await super().cas(bumped, expected_state_version=current.state_version)
                == "accepted"
            )
            self.conflicted = True
            return "conflict"
        return await super().cas(record, expected_state_version=expected_state_version)


async def setup(*, hitl=None, ledger=None):
    ledger = ledger or InMemoryAgentCallLedgerStore()
    record = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    record = transition_call(record, to_state="working", updated_at=NOW)
    await ledger.insert(record)
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    dispatch = Dispatch()
    inbox = InMemoryObservationInboxStore()
    observations = A2AObservationIngress(
        inbox=inbox,
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    coordinator = A2ACancellationCoordinator(
        ledger=ledger,
        room_epochs=epochs,
        dispatch=dispatch,
        observations=observations,
        hitl=hitl or InMemoryHITLApplicationPort(),
    )
    return coordinator, ledger, epochs, dispatch, record


async def test_cancellation_is_local_terminal_before_remote_cleanup():
    coordinator, ledger, _, dispatch, record = await setup()

    result = await coordinator.cancel_call(
        call_record_id=record.call_record_id, reason="user canceled"
    )

    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert result == "canceled"
    assert persisted is not None
    assert persisted.state == "canceled"
    assert persisted.cancellation_command_id is not None
    assert persisted.terminal_result is not None
    assert persisted.terminal_result.status == "canceled"
    assert dispatch.commands == []


async def test_cancellation_retries_a_nonterminal_cas_winner():
    ledger = OneConflictLedger()
    coordinator, ledger, _, _, record = await setup(ledger=ledger)

    result = await coordinator.cancel_call(
        call_record_id=record.call_record_id, reason="user canceled"
    )

    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert ledger.conflicted is True
    assert result == "canceled"
    assert persisted is not None and persisted.state == "canceled"


async def test_cancellation_replay_is_idempotent():
    coordinator, ledger, _, _, record = await setup()

    first = await coordinator.cancel_call(
        call_record_id=record.call_record_id, reason="user canceled"
    )
    first_record = await ledger.load_by_record_id(record.call_record_id)
    second = await coordinator.cancel_call(
        call_record_id=record.call_record_id, reason="user canceled"
    )
    second_record = await ledger.load_by_record_id(record.call_record_id)

    assert first == second == "canceled"
    assert first_record == second_record


async def test_recovery_terminalizes_legacy_cancel_pending_call_locally():
    coordinator, ledger, _, _, record = await setup()
    command_result = await coordinator.cancel_call(
        call_record_id=record.call_record_id, reason="user canceled"
    )
    assert command_result == "canceled"

    assert await coordinator.recover_call(call_record_id=record.call_record_id) == (
        "canceled"
    )
    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert persisted is not None and persisted.state == "canceled"


async def test_late_completion_cannot_replace_local_cancellation():
    coordinator, ledger, _, _, record = await setup()
    assert (
        await coordinator.cancel_call(
            call_record_id=record.call_record_id, reason="user canceled"
        )
        == "canceled"
    )
    canceled = await ledger.load_by_record_id(record.call_record_id)
    assert canceled is not None
    late = NormalizedA2AObservation(
        observation_id="late-completion",
        call_record_id=record.call_record_id,
        source_kind="webhook",
        source_identity="agent:test",
        binding_scope=record.endpoint_scope_digest,
        event_kind="terminal",
        observed_at=datetime.now(UTC),
        status="completed",
    )

    with pytest.raises(ValueError, match="terminal observation conflicts"):
        apply_observation(canceled, late, recent_limit=16)
    assert await ledger.load_by_record_id(record.call_record_id) == canceled


async def test_nonterminal_observation_is_absorbed_while_cancel_marker_wins():
    coordinator, ledger, _, _, record = await setup()
    command_id = "cancel-existing"
    from execution.orchestrator.a2a_runtime.models import A2ACancellationCommand

    pending = transition_call(
        record,
        to_state="cancel_pending",
        updated_at=NOW,
        cancellation_command=A2ACancellationCommand(
            command_id=command_id,
            transport_kind=record.transport_kind,
            call_record_id=record.call_record_id,
            reason="user canceled",
            created_at=NOW,
        ),
        cancellation_command_id=command_id,
        cancellation_reason="user canceled",
        cancellation_state="pending",
    )
    assert await ledger.cas(pending, expected_state_version=record.state_version) == (
        "accepted"
    )
    working = NormalizedA2AObservation(
        observation_id="late-working",
        call_record_id=record.call_record_id,
        source_kind="direct",
        source_identity="agent:test",
        binding_scope=record.endpoint_scope_digest,
        event_kind="working",
        observed_at=datetime.now(UTC),
    )
    assert apply_observation(pending, working, recent_limit=16) == pending

    assert await coordinator.recover_call(call_record_id=record.call_record_id) == (
        "canceled"
    )


async def test_active_epoch_fence_rejects_stale_cancellation():
    coordinator, ledger, epochs, _, record = await setup()
    await epochs.deactivate("room-1", 1, "delete-1", deactivated_at=NOW)

    with pytest.raises(PermissionError, match="epoch fence"):
        await coordinator._cancel_call(
            call_record_id=record.call_record_id, reason="user canceled"
        )
    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert persisted is not None and persisted.state == "working"


async def test_deletion_cancellation_recovery_reuses_persisted_epoch_fence():
    coordinator, ledger, epochs, _, record = await setup()
    await epochs.deactivate("room-1", 1, "delete-1", deactivated_at=NOW)
    from execution.orchestrator.a2a_runtime.models import A2ACancellationCommand

    command = A2ACancellationCommand(
        command_id="cancel-delete",
        transport_kind=record.transport_kind,
        call_record_id=record.call_record_id,
        reason="room deleted",
        deletion_id="delete-1",
        created_at=NOW,
    )
    pending = transition_call(
        record,
        to_state="cancel_pending",
        updated_at=NOW,
        cancellation_command=command,
        cancellation_command_id=command.command_id,
        cancellation_reason=command.reason,
        cancellation_state="pending",
    )
    assert await ledger.cas(pending, expected_state_version=record.state_version) == (
        "accepted"
    )

    assert await coordinator.recover_call(call_record_id=record.call_record_id) == (
        "canceled"
    )


async def test_deletion_epoch_can_terminalize_call_without_remote_cleanup():
    coordinator, ledger, epochs, dispatch, record = await setup()
    await epochs.deactivate("room-1", 1, "delete-1", deactivated_at=NOW)

    result = await coordinator.cancel_call(
        call_record_id=record.call_record_id,
        reason="room deleted",
        deletion_id="delete-1",
    )

    persisted = await ledger.load_by_record_id(record.call_record_id)
    assert result == "canceled"
    assert persisted is not None and persisted.state == "canceled"
    assert dispatch.commands == []
