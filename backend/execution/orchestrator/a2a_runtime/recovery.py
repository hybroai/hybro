"""Leased recovery services for the orchestrator A2A runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from common.dto.hitl import A2AInteractionSpec
from common.utils.logger import get_logger

from ..kernel import KernelConflict
from ..models import TextPart, ToolResult
from ..ports import InvocationCheckpointReader
from .cancellation import A2ACancellationCoordinator
from .errors import (
    AgentCardContractError,
    AmbiguousRemoteEffectError,
    RecoverableAdapterError,
    RecoverableCheckpointError,
    RecoverableTransportError,
)
from .hitl import A2AContinuationCoordinator
from .ingress import A2AObservationProcessor
from .interaction_outcome import (
    CanonicalHITLControlPublisher,
    park_call_for_interaction,
)
from .ledger import (
    TERMINAL_AGENT_CALL_STATES,
    apply_observation,
    transition_call,
)
from .models import (
    A2ADispatchCommand,
    A2AObservationInboxRecord,
    A2ARuntimePolicy,
    AgentCallLedgerRecord,
    MaterializedResourcePart,
    NormalizedA2AObservation,
)
from .ports import (
    A2ADispatchPort,
    AgentCallLedgerStore,
    HITLApplicationPort,
    NormalizedObservationRecorder,
    ObservationInboxStore,
    RoomEpochStore,
)

logger = get_logger(__name__)

RecoverDispatch = Callable[[AgentCallLedgerRecord], Awaitable[None]]
RecoverPhase = Callable[[], Awaitable[None]]


class A2ACallRecoveryService:
    def __init__(
        self,
        *,
        ledger: AgentCallLedgerStore,
        checkpoints: InvocationCheckpointReader,
        room_epochs: RoomEpochStore,
        dispatch: A2ADispatchPort,
        observations: NormalizedObservationRecorder,
        recover_dispatch: RecoverDispatch,
        policy: A2ARuntimePolicy | None = None,
        worker_id: str = "a2a-call-recovery",
        hitl: HITLApplicationPort | None = None,
        hitl_delivery: Any | None = None,
        run_store: Any | None = None,
        canonical_hitl_control: CanonicalHITLControlPublisher | None = None,
        public_secret_values: tuple[str, ...] = (),
    ) -> None:
        self.ledger = ledger
        self.checkpoints = checkpoints
        self.room_epochs = room_epochs
        self.dispatch = dispatch
        self.observations = observations
        self.recover_dispatch = recover_dispatch
        self.policy = policy or A2ARuntimePolicy()
        self.worker_id = worker_id
        self.hitl = hitl
        self.hitl_delivery = hitl_delivery
        self.run_store = run_store
        self.canonical_hitl_control = canonical_hitl_control
        self.public_secret_values = public_secret_values

    async def recover_due(self, *, due_at: datetime) -> int:
        records = await self.ledger.list_due(
            due_at=due_at, limit=self.policy.recovery_batch_limit
        )
        recovered = 0
        for record in records:
            try:
                await self.recover_call(record, now=due_at)
                current = await self.ledger.load_by_record_id(record.call_record_id)
            except RecoverableAdapterError:
                continue
            if _general_call_recovery_progressed(record, current, due_at=due_at):
                recovered += 1
        return recovered

    async def recover_call(  # noqa: C901
        self, record: AgentCallLedgerRecord, *, now: datetime
    ) -> bool:
        current = await self.ledger.load_by_record_id(record.call_record_id)
        if current is None or current.state_version != record.state_version:
            return False
        if await self._run_blocks_dispatch(current):
            return False
        claimed = await self.ledger.claim(
            current.call_record_id,
            expected_state_version=current.state_version,
            owner_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=self.policy.claim_lease_seconds),
            claimed_at=now,
        )
        if claimed is None:
            return False
        record = claimed
        if not await self.room_epochs.verify_active(record.room_id, record.room_epoch):
            return await self._expire(record, now=now, code="room_epoch_gone")
        if now > record.dispatch_snapshot.deadline_at and record.state not in {
            "dispatching",
            "delivery_uncertain",
            "resuming",
            "cancel_pending",
            "input_required",
            "auth_required",
        }:
            return await self._expire(record, now=now, code="call_deadline_exceeded")
        receipt_checkpointed = await self.checkpoints.is_acceptance_checkpointed(
            record.run_id,
            record.invocation_id,
            record.acceptance_id,
            record.idempotency_key,
            record.binding_digest,
        )
        record = await self._renew(record, now=now)
        if record is None:
            return False
        if record.state == "accepted":
            if receipt_checkpointed:
                released = await self._release(record, now=now)
                if released is not None:
                    try:
                        await self.recover_dispatch(released)
                    except (
                        RecoverableAdapterError,
                        RecoverableTransportError,
                        AmbiguousRemoteEffectError,
                        TimeoutError,
                    ):
                        await self._reschedule(
                            released,
                            now=now,
                            delay=record.runtime_policy.retry_backoff_initial_seconds,
                        )
            elif now - record.accepted_at >= timedelta(
                seconds=record.runtime_policy.orphan_acceptance_ttl_seconds
            ):
                return await self._expire(record, now=now, code="orphan_acceptance")
            else:
                orphan_at = record.accepted_at + timedelta(
                    seconds=record.runtime_policy.orphan_acceptance_ttl_seconds
                )
                released = await self._release_at(
                    record, now=now, next_attempt_at=orphan_at
                )
            return released is not None
        if record.state == "ready_to_dispatch":
            released = await self._release(
                record,
                now=now,
                delay=(
                    0
                    if receipt_checkpointed
                    else record.runtime_policy.retry_backoff_initial_seconds
                ),
            )
            if receipt_checkpointed and released is not None:
                try:
                    await self.recover_dispatch(released)
                except (
                    RecoverableAdapterError,
                    RecoverableTransportError,
                    AmbiguousRemoteEffectError,
                    TimeoutError,
                ):
                    await self._reschedule(
                        released,
                        now=now,
                        delay=record.runtime_policy.retry_backoff_initial_seconds,
                    )
            return released is not None
        if record.state == "dispatching":
            uncertain = transition_call(
                record,
                to_state="delivery_uncertain",
                updated_at=now,
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=now,
            )
            winner, exact = await self._cas_or_load_winner(
                uncertain, expected_state_version=record.state_version
            )
            return exact or (winner is not None and winner.state != "dispatching")
        if record.state in {
            "cancel_pending",
            "continuation_pending",
            "input_required",
            "auth_required",
        }:
            return (
                await self._release(
                    record,
                    now=now,
                    delay=record.runtime_policy.retry_backoff_initial_seconds,
                )
                is not None
            )
        if record.state == "resuming" and record.continuation_command is not None:
            return (
                await self._release(
                    record,
                    now=now,
                    delay=record.runtime_policy.retry_backoff_initial_seconds,
                )
                is not None
            )
        # Continuation-owned delivery uncertainty is reconciled by the
        # continuation recovery phase, not dispatch inspect.
        if (
            record.state == "delivery_uncertain"
            and record.continuation_command is not None
        ):
            return (
                await self._release(
                    record,
                    now=now,
                    delay=record.runtime_policy.retry_backoff_initial_seconds,
                )
                is not None
            )
        if record.state not in {"delivery_uncertain", "working", "resuming"}:
            return (
                await self._release(
                    record,
                    now=now,
                    delay=record.runtime_policy.retry_backoff_initial_seconds,
                )
                is not None
            )

        command = dispatch_command(record)
        if await self._run_blocks_dispatch(record):
            await self._release(record, now=now)
            return False
        try:
            inspected = await self.dispatch.inspect(command)
        except AgentCardContractError:
            return await self._expire(record, now=now, code="agent_card_contract_error")
        except (
            RecoverableAdapterError,
            RecoverableTransportError,
            AmbiguousRemoteEffectError,
            TimeoutError,
        ):
            inspected = None
        record = await self._renew(record, now=datetime.now(UTC))
        if record is None:
            return False
        if inspected is not None and inspected.terminal_observation is not None:
            observation = inspected.terminal_observation
            _record_outcome, persisted_observation = await self.observations.record(
                observation
            )
            observation = persisted_observation.observation
            record = await self._renew(record, now=datetime.now(UTC))
            if record is None:
                return False
            terminal = apply_observation(
                record,
                observation,
                recent_limit=record.runtime_policy.recent_observation_id_limit,
            )
            winner, exact = await self._cas_or_load_winner(
                terminal, expected_state_version=record.state_version
            )
            return exact or (
                winner is not None and winner.state in TERMINAL_AGENT_CALL_STATES
            )
        if (
            inspected is not None
            and inspected.outcome == "interaction"
            and inspected.interaction_observation is not None
        ):
            observation = inspected.interaction_observation
            if observation.call_record_id is None:
                observation = observation.model_copy(
                    update={"call_record_id": record.call_record_id}
                )
            _record_outcome, persisted_observation = await self.observations.record(
                observation
            )
            observation = persisted_observation.observation
            record = await self._renew(record, now=datetime.now(UTC))
            if record is None:
                return False
            if self.hitl is None:
                # Without a HITL port, only the legacy untyped silent-complete
                # path is safe; typed specs must wait for a wired composition.
                if observation.interaction_spec is not None:
                    return (
                        await self._release(
                            record,
                            now=now,
                            delay=record.runtime_policy.retry_backoff_initial_seconds,
                        )
                        is not None
                    )
                content = list(observation.content or [])
                if not content:
                    content = [TextPart(text="The Agent requested additional input.")]
                result = ToolResult(
                    call_id=record.invocation_id,
                    tool_name=record.tool_name,
                    status="completed",
                    content=content,
                    artifact_refs=list(
                        dict.fromkeys(
                            [*record.artifact_refs, *(observation.artifact_refs or [])]
                        )
                    ),
                    error_code=None,
                    error_message=None,
                )
                terminal = transition_call(
                    record,
                    to_state="completed",
                    updated_at=datetime.now(UTC),
                    artifact_refs=result.artifact_refs,
                    terminal_result=result,
                    terminal_result_digest=sha256(
                        result.model_dump_json().encode()
                    ).hexdigest(),
                )
                winner, exact = await self._cas_or_load_winner(
                    terminal, expected_state_version=record.state_version
                )
                return exact or (
                    winner is not None and winner.state in TERMINAL_AGENT_CALL_STATES
                )
            try:

                async def _cas(
                    candidate: AgentCallLedgerRecord, expected: int
                ) -> AgentCallLedgerRecord:
                    winner, _exact = await self._cas_or_load_winner(
                        candidate, expected_state_version=expected
                    )
                    if winner is None:
                        raise RecoverableCheckpointError(
                            "call recovery CAS winner unavailable"
                        )
                    return winner

                persisted, kind = await park_call_for_interaction(
                    call=record,
                    observation=observation,
                    hitl=self.hitl,
                    cas=_cas,
                )
            except RecoverableCheckpointError:
                return (
                    await self._release(
                        record,
                        now=now,
                        delay=record.runtime_policy.retry_backoff_initial_seconds,
                    )
                    is not None
                )
            if kind == "typed_waiting" and persisted.pending_interaction_id is not None:
                raw_spec = observation.interaction_spec
                if raw_spec is not None:
                    interaction = A2AInteractionSpec.model_validate(raw_spec)
                    fingerprint = sha256(
                        json.dumps(
                            interaction.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                    waiting_state = (
                        observation.event_kind
                        if observation.event_kind in {"input_required", "auth_required"}
                        else "input_required"
                    )
                    if (
                        persisted.state == waiting_state
                        and persisted.pending_interaction_id
                        == interaction.interaction_id
                        and persisted.interaction_fingerprint == fingerprint
                    ):
                        # A concurrent winner may have parked another round.
                        # Mark the exact matching observation applied, but do
                        # NOT publish the questionnaire here: model-first HITL
                        # presents the interaction to the kernel/model, which
                        # alone may escalate it to the user.
                        await self.observations.mark_ledger_applied(
                            observation.observation_id
                        )
            return persisted.state in {
                "input_required",
                "auth_required",
                *TERMINAL_AGENT_CALL_STATES,
            }
        if inspected is not None and inspected.outcome == "accepted":
            if record.state != "working":
                working = transition_call(
                    record,
                    to_state="working",
                    updated_at=now,
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=now
                    + timedelta(
                        seconds=record.runtime_policy.retry_backoff_max_seconds
                    ),
                )
                winner, exact = await self._cas_or_load_winner(
                    working, expected_state_version=record.state_version
                )
                return exact or (
                    winner is not None
                    and (
                        winner.state == "working"
                        or winner.state in TERMINAL_AGENT_CALL_STATES
                    )
                )
            else:
                # Healthy working polling has its own schedule and never consumes
                # delivery-uncertainty attempts.
                return (
                    await self._release(
                        record,
                        now=now,
                        delay=record.runtime_policy.retry_backoff_max_seconds,
                    )
                    is not None
                )
        if record.state == "working":
            if record.continuation_command is not None:
                return (
                    await self._release(
                        record,
                        now=now,
                        delay=record.runtime_policy.retry_backoff_initial_seconds,
                    )
                    is not None
                )
            return (
                await self._release(
                    record,
                    now=now,
                    delay=record.runtime_policy.retry_backoff_max_seconds,
                )
                is not None
            )
        attempts = record.inspection_attempts + 1
        if attempts >= record.runtime_policy.max_uncertain_inspection_attempts:
            return await self._expire(
                record, now=now, code="delivery_uncertainty_exhausted"
            )
        retry = record.model_copy(
            update={
                "inspection_attempts": attempts,
                "claim_owner": None,
                "claim_expires_at": None,
                "next_attempt_at": now
                + timedelta(seconds=_backoff(record.runtime_policy, attempts)),
                "state_version": record.state_version + 1,
                "updated_at": now,
            }
        )
        winner, exact = await self._cas_or_load_winner(
            retry, expected_state_version=record.state_version
        )
        return exact or (
            winner is not None
            and (
                winner.state in TERMINAL_AGENT_CALL_STATES
                or (
                    winner.state == retry.state
                    and winner.inspection_attempts >= retry.inspection_attempts
                )
            )
        )

    async def _run_blocks_dispatch(self, record: AgentCallLedgerRecord) -> bool:
        if self.run_store is None:
            return False
        run = await self.run_store.load(record.run_id)
        return run is None or run.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }

    async def _expire(
        self, record: AgentCallLedgerRecord, *, now: datetime, code: str
    ) -> bool:
        observation = NormalizedA2AObservation(
            observation_id=f"recovery-expiry-{record.call_record_id}-{code}",
            call_record_id=record.call_record_id,
            source_kind="inspection",
            source_identity=f"recovery:{record.call_record_id}:{code}",
            binding_scope=record.endpoint_scope_digest,
            event_kind="terminal",
            observed_at=now,
            task_id=record.a2a_task_id,
            context_id=record.a2a_context_id,
            status="expired",
            content=[TextPart(text="The Agent call expired during recovery.")],
            error_code=code,
            error_message=code.replace("_", " "),
        )
        await self.observations.record(observation)
        renewed = await self._renew(record, now=datetime.now(UTC))
        if renewed is None:
            return False
        expired = apply_observation(
            renewed,
            observation,
            recent_limit=renewed.runtime_policy.recent_observation_id_limit,
        )
        winner, exact = await self._cas_or_load_winner(
            expired, expected_state_version=renewed.state_version
        )
        return exact or (
            winner is not None and winner.state in TERMINAL_AGENT_CALL_STATES
        )

    async def _cas_or_load_winner(
        self,
        candidate: AgentCallLedgerRecord,
        *,
        expected_state_version: int,
    ) -> tuple[AgentCallLedgerRecord | None, bool]:
        try:
            outcome = await self.ledger.cas(
                candidate, expected_state_version=expected_state_version
            )
            if outcome in {"accepted", "replayed"}:
                return candidate, True
            winner = await self.ledger.load_by_record_id(candidate.call_record_id)
        except RecoverableAdapterError:
            return None, False
        if winner is None:
            return None, False
        return winner, winner == candidate

    async def _renew(
        self, record: AgentCallLedgerRecord, *, now: datetime
    ) -> AgentCallLedgerRecord | None:
        lease_base = max(record.claim_expires_at or now, now)
        return await self.ledger.renew(
            record.call_record_id,
            expected_state_version=record.state_version,
            owner_id=self.worker_id,
            lease_expires_at=lease_base
            + timedelta(seconds=self.policy.claim_lease_seconds),
            renewed_at=now,
        )

    async def _release(
        self, record: AgentCallLedgerRecord, *, now: datetime, delay: int = 0
    ) -> AgentCallLedgerRecord | None:
        return await self._release_at(
            record,
            now=now,
            next_attempt_at=now + timedelta(seconds=delay),
        )

    async def _release_at(
        self,
        record: AgentCallLedgerRecord,
        *,
        now: datetime,
        next_attempt_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        return await self.ledger.release(
            record.call_record_id,
            expected_state_version=record.state_version,
            owner_id=self.worker_id,
            next_attempt_at=next_attempt_at,
            released_at=now,
        )

    async def _reschedule(
        self,
        record: AgentCallLedgerRecord,
        *,
        now: datetime,
        delay: int,
    ) -> bool:
        scheduled = record.model_copy(
            update={
                "next_attempt_at": now + timedelta(seconds=delay),
                "state_version": record.state_version + 1,
                "updated_at": now,
            }
        )
        winner, exact = await self._cas_or_load_winner(
            scheduled, expected_state_version=record.state_version
        )
        return exact or (
            winner is not None
            and winner.next_attempt_at is not None
            and winner.next_attempt_at > now
        )


class A2AInboxRecoveryService:
    def __init__(
        self,
        *,
        processor: A2AObservationProcessor,
        inbox: ObservationInboxStore,
        policy: A2ARuntimePolicy | None = None,
    ) -> None:
        self.processor = processor
        self.inbox = inbox
        self.policy = policy or A2ARuntimePolicy()

    async def recover_due(self, *, due_at: datetime) -> int:
        records = await self.inbox.list_due(
            due_at=due_at, limit=self.policy.recovery_batch_limit
        )
        completed = 0
        for record in records:
            try:
                outcome = await self.processor.process(record.observation_id)
            except KernelConflict:
                await self.processor.defer_poison(
                    record.observation_id,
                    error=_kernel_conflict_marker(),
                    now=due_at,
                )
                continue
            except ValueError as exc:
                await self.processor.defer_poison(
                    record.observation_id, error=type(exc).__name__, now=due_at
                )
                continue
            if outcome in {"retryable", "conflict"}:
                continue
            try:
                current = await self.inbox.load(record.observation_id)
            except RecoverableAdapterError:
                continue
            if _inbox_recovery_progressed(record, current):
                completed += 1
        return completed


class A2AContinuationRecoveryService:
    def __init__(
        self, coordinator: A2AContinuationCoordinator, ledger: AgentCallLedgerStore
    ):
        self.coordinator = coordinator
        self.ledger = ledger

    async def recover_due(self, *, due_at: datetime, limit: int = 100) -> int:
        recovered = 0
        for call in await self.ledger.list_due(due_at=due_at, limit=limit):
            if call.state in {"input_required", "auth_required"}:
                await self.coordinator.reconcile_answer(
                    call_record_id=call.call_record_id
                )
                try:
                    current = await self.ledger.load_by_record_id(call.call_record_id)
                except RecoverableAdapterError:
                    continue
                if _call_recovery_progressed(call, current):
                    recovered += 1
                continue
            if call.continuation_command is None or call.state not in {
                "resuming",
                "delivery_uncertain",
                "working",
            }:
                continue
            if call.state == "working" and call.continuation_state != "accepted":
                continue
            await self.coordinator.recover_call(call_record_id=call.call_record_id)
            try:
                current = await self.ledger.load_by_record_id(call.call_record_id)
            except RecoverableAdapterError:
                continue
            if _call_recovery_progressed(call, current):
                recovered += 1
        return recovered


class A2ACancellationRecoveryService:
    def __init__(
        self, coordinator: A2ACancellationCoordinator, ledger: AgentCallLedgerStore
    ):
        self.coordinator = coordinator
        self.ledger = ledger

    async def recover_due(self, *, due_at: datetime, limit: int = 100) -> int:
        recovered = 0
        for call in await self.ledger.list_due(due_at=due_at, limit=limit):
            if call.state != "cancel_pending" or call.cancellation_command is None:
                continue
            await self.coordinator.recover_call(call_record_id=call.call_record_id)
            try:
                current = await self.ledger.load_by_record_id(call.call_record_id)
            except RecoverableAdapterError:
                continue
            if _call_recovery_progressed(call, current):
                recovered += 1
        return recovered


class A2AArtifactRecoveryService:
    """Artifact checkpoints are recovered by replaying artifact inbox rows."""

    def __init__(self, inbox_recovery: A2AInboxRecoveryService) -> None:
        self.inbox_recovery = inbox_recovery

    async def recover_due(self, *, due_at: datetime) -> int:
        return await self.inbox_recovery.recover_due(due_at=due_at)


class A2ARecoveryCycle:
    """Explicit recovery ordering; callers supply unbound phase functions."""

    def __init__(
        self,
        *,
        cancellation: RecoverPhase,
        continuation: RecoverPhase,
        observations: RecoverPhase,
        calls: RecoverPhase,
        artifacts: RecoverPhase,
        generic_runs: RecoverPhase,
        projection: RecoverPhase,
        watchdog: RecoverPhase,
    ) -> None:
        self.phases = (
            ("cancellation", cancellation),
            ("continuation", continuation),
            ("observations", observations),
            ("calls", calls),
            ("artifacts", artifacts),
            ("generic_runs", generic_runs),
            ("projection", projection),
            ("watchdog", watchdog),
        )

    async def run_once(self) -> None:
        """Run phases in order, isolating one phase failure from the rest.

        Cancellation is still propagated so job shutdown remains responsive; any
        other phase exception is logged and the cycle continues to the next
        phase. Watchdog remains the last phase by construction.
        """
        for name, phase in self.phases:
            try:
                await phase()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("A2A recovery phase failed: %s", name, exc_info=True)


def _kernel_conflict_marker() -> str:
    fingerprint = sha256(b"observation-sink:KernelConflict").hexdigest()[:16]
    return f"KernelConflict:{fingerprint}"


def _general_call_recovery_progressed(
    before: AgentCallLedgerRecord,
    current: AgentCallLedgerRecord | None,
    *,
    due_at: datetime,
) -> bool:
    if current is None:
        return False
    if any(
        (
            current.state != before.state,
            current.inspection_attempts != before.inspection_attempts,
            current.transport_attempts != before.transport_attempts,
            current.continuation_attempts != before.continuation_attempts,
            current.cancellation_attempts != before.cancellation_attempts,
            current.terminal_result_digest != before.terminal_result_digest,
            current.error_code != before.error_code,
        )
    ):
        return True
    return (
        current.next_attempt_at is not None
        and current.next_attempt_at > due_at
        and current.next_attempt_at != before.next_attempt_at
    )


def _inbox_recovery_progressed(
    before: A2AObservationInboxRecord,
    current: A2AObservationInboxRecord | None,
) -> bool:
    if current is None:
        return False
    return (
        current.state_version > before.state_version
        and current.state not in {"pending", "claimed"}
        and (
            current.state != before.state
            or current.delivery_state != before.delivery_state
            or current.outcome_digest != before.outcome_digest
        )
    )


def _call_recovery_progressed(
    before: AgentCallLedgerRecord,
    current: AgentCallLedgerRecord | None,
) -> bool:
    if current is None or current.state_version <= before.state_version:
        return False
    return any(
        (
            current.state != before.state,
            current.answer_applied != before.answer_applied,
            current.continuation_state != before.continuation_state,
            current.continuation_attempts != before.continuation_attempts,
            current.cancellation_state != before.cancellation_state,
            current.cancellation_attempts != before.cancellation_attempts,
            current.inspection_attempts != before.inspection_attempts,
            current.terminal_result_digest != before.terminal_result_digest,
        )
    )


def dispatch_command(
    record: AgentCallLedgerRecord,
    *,
    materialized_resources: list[MaterializedResourcePart] | None = None,
) -> A2ADispatchCommand:
    return A2ADispatchCommand(
        command_id=record.dispatch_snapshot.command_id,
        call_record_id=record.call_record_id,
        invocation_id=record.invocation_id,
        message_id=record.dispatch_snapshot.message_id,
        binding_id=record.binding_id,
        agent_id=record.agent_id,
        skill_id=record.skill_id,
        endpoint_scope=record.dispatch_snapshot.endpoint_scope,
        transport_kind=record.transport_kind,
        direct_mode=record.dispatch_snapshot.direct_mode,
        task=record.dispatch_snapshot.task,
        materialized_resources=list(materialized_resources or []),
        room_id=record.room_id,
        room_epoch=record.room_epoch,
        deadline_at=record.dispatch_snapshot.deadline_at,
    )


def _backoff(policy: A2ARuntimePolicy, attempts: int) -> int:
    return min(
        policy.retry_backoff_initial_seconds * (2 ** max(attempts - 1, 0)),
        policy.retry_backoff_max_seconds,
    )
