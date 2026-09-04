"""Durable local cancellation for external A2A calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

from ..models import TextPart
from .errors import RecoverableAdapterError, RecoverableCheckpointError
from .ledger import TERMINAL_AGENT_CALL_STATES, apply_observation, transition_call
from .models import (
    A2ACancellationCommand,
    A2ARuntimePolicy,
    AgentCallLedgerRecord,
    NormalizedA2AObservation,
)
from .ports import (
    A2ADispatchPort,
    AgentCallLedgerStore,
    HITLApplicationPort,
    NormalizedObservationRecorder,
    RoomEpochStore,
)
from .terminal_interactions import TerminalInteractionFinalizer


class A2ACancellationCoordinator:
    """Make local cancellation terminal without waiting for a remote Agent."""

    def __init__(
        self,
        *,
        ledger: AgentCallLedgerStore,
        room_epochs: RoomEpochStore,
        dispatch: A2ADispatchPort,
        observations: NormalizedObservationRecorder,
        hitl: HITLApplicationPort,
        policy: A2ARuntimePolicy | None = None,
        worker_id: str = "a2a-cancellation",
    ) -> None:
        self.ledger = ledger
        self.room_epochs = room_epochs
        # These ports remain constructor dependencies for composition compatibility.
        # Cancellation no longer waits on either remote transport operation.
        self.dispatch = dispatch
        self.observations = observations
        self.terminal_interactions = TerminalInteractionFinalizer(hitl)
        self.policy = policy or A2ARuntimePolicy()
        self.worker_id = worker_id

    async def cancel_call(
        self,
        *,
        call_record_id: str,
        reason: str,
        deletion_id: str | None = None,
    ) -> str:
        try:
            return await self._cancel_call(
                call_record_id=call_record_id,
                reason=reason,
                deletion_id=deletion_id,
            )
        except RecoverableAdapterError:
            return "cancel_pending"

    async def _cancel_call(
        self,
        *,
        call_record_id: str,
        reason: str,
        deletion_id: str | None = None,
    ) -> str:
        call = await self.ledger.load_by_record_id(call_record_id)
        if call is None:
            raise KeyError(call_record_id)
        if call.state in TERMINAL_AGENT_CALL_STATES:
            return await self._finalized_state(call)
        if not await self._epoch_authorized(call, deletion_id):
            raise PermissionError("cancellation epoch fence rejected")
        persisted = await persist_local_cancellation(
            self.ledger,
            call,
            reason=reason,
            deletion_id=deletion_id,
        )
        return await self._finalized_state(persisted)

    async def recover_call(self, *, call_record_id: str) -> str:
        call = await self.ledger.load_by_record_id(call_record_id)
        if call is None:
            raise KeyError(call_record_id)
        deletion_id = (
            call.cancellation_command.deletion_id
            if call.cancellation_command is not None
            else None
        )
        return await self.cancel_call(
            call_record_id=call_record_id,
            reason="cancellation_recovery",
            deletion_id=deletion_id,
        )

    async def _finalized_state(self, record: AgentCallLedgerRecord) -> str:
        if record.state in TERMINAL_AGENT_CALL_STATES:
            await self.terminal_interactions.finalize(record)
        return record.state

    async def _epoch_authorized(
        self, call: AgentCallLedgerRecord, deletion_id: str | None
    ) -> bool:
        if deletion_id is None:
            return await self.room_epochs.verify_active(call.room_id, call.room_epoch)
        return await self.room_epochs.verify_cleanup_epoch(
            call.room_id, call.room_epoch, deletion_id
        )

    async def cancel_run(
        self, run_id: str, *, reason: str, deletion_id: str | None = None
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        for call in await self.ledger.list_for_run(run_id):
            results[call.call_record_id] = await self.cancel_call(
                call_record_id=call.call_record_id,
                reason=reason,
                deletion_id=deletion_id,
            )
        return results


async def persist_local_cancellation(
    ledger: AgentCallLedgerStore,
    call: AgentCallLedgerRecord,
    *,
    reason: str,
    deletion_id: str | None = None,
) -> AgentCallLedgerRecord:
    """Persist an absorbing canceled winner without remote transport I/O."""

    for _attempt in range(8):
        if call.state in TERMINAL_AGENT_CALL_STATES:
            return call
        if call.state != "cancel_pending":
            command_id = f"cancel-{_stable([call.call_record_id, reason[:1000], deletion_id or 'active'])}"
            command = A2ACancellationCommand(
                command_id=command_id,
                transport_kind=call.transport_kind,
                call_record_id=call.call_record_id,
                reason=reason[:1000],
                deletion_id=deletion_id,
                created_at=datetime.now(UTC),
            )
            pending = transition_call(
                call,
                to_state="cancel_pending",
                updated_at=datetime.now(UTC),
                cancellation_command=command,
                cancellation_command_id=command_id,
                cancellation_reason=reason[:1000],
                cancellation_state="pending",
            )
            outcome = await ledger.cas(
                pending, expected_state_version=call.state_version
            )
            if outcome in {"accepted", "replayed"}:
                call = pending
            else:
                winner = await ledger.load_by_record_id(call.call_record_id)
                if winner is None:
                    raise RecoverableCheckpointError(
                        "cancellation CAS winner could not be classified"
                    )
                call = winner
                continue
        command = call.cancellation_command
        if command is None:
            raise RecoverableCheckpointError(
                "cancel-pending Agent call has no cancellation command"
            )
        terminal = apply_observation(
            call,
            _canceled_observation(call, command),
            recent_limit=call.runtime_policy.recent_observation_id_limit,
        )
        outcome = await ledger.cas(terminal, expected_state_version=call.state_version)
        if outcome in {"accepted", "replayed"}:
            return terminal
        winner = await ledger.load_by_record_id(call.call_record_id)
        if winner is None:
            raise RecoverableCheckpointError(
                "cancellation terminal CAS winner could not be classified"
            )
        call = winner
    raise RecoverableCheckpointError("Agent-call cancellation CAS did not converge")


def _canceled_observation(
    call: AgentCallLedgerRecord, command: A2ACancellationCommand
) -> NormalizedA2AObservation:
    return NormalizedA2AObservation(
        observation_id=f"cancel-observation-{command.command_id}",
        call_record_id=call.call_record_id,
        source_kind="inspection",
        source_identity=f"cancel:{command.command_id}",
        binding_scope=call.endpoint_scope_digest,
        event_kind="terminal",
        status="canceled",
        observed_at=datetime.now(UTC),
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
        content=[TextPart(text="The Agent call was canceled.")],
        error_code="canceled",
        error_message=command.reason[:500],
    )


def _stable(parts: list[str]) -> str:
    canonical = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode()).hexdigest()
