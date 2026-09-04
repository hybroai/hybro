"""Two-phase ToolRuntime backed by the durable external A2A call ledger."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from common.dto.hitl import A2AInteractionSpec, HITLQuestionSpec

from ..control import ClientCancellationRequested
from ..models import (
    AgentToolInput,
    TextPart,
    ToolAcceptance,
    ToolDefinition,
    ToolInteractionQuestion,
    ToolInvocation,
    ToolResult,
    ToolSuspension,
)
from ..ports import CancellationSignal, InvocationCheckpointReader
from .cancellation import persist_local_cancellation
from .errors import (
    AgentCardContractError,
    AmbiguousRemoteEffectError,
    RecoverableAdapterError,
    RecoverableAuthorizationError,
    RecoverableCheckpointError,
    RecoverableEpochError,
    RecoverableResourceError,
    RecoverableTransportError,
)
from .ingress import ObservationIngressError
from .interaction_outcome import (
    CanonicalHITLControlPublisher,
    emit_hitl_request_events,
    park_call_for_interaction,
)
from .ledger import (
    apply_observation,
    bind_authoritative_aliases,
    ownership_alias_keys,
    transition_call,
)
from .models import (
    A2ADispatchCommand,
    A2ADispatchReceipt,
    A2AJoinBinding,
    A2AModelReplyCommand,
    A2ARuntimePolicy,
    AgentCallLedgerRecord,
    NormalizedA2AObservation,
)
from .ports import (
    A2ADispatchPort,
    AgentCallLedgerStore,
    AuthorizationRefreshPort,
    HITLApplicationPort,
    NormalizedObservationRecorder,
    PreparedInvocationSnapshotReader,
    ResourceMaterializerPort,
    RoomEpochStore,
)
from .resources import verify_materialized_digests
from .terminal_interactions import TerminalInteractionFinalizer


class A2AAcceptanceConflict(RuntimeError):
    pass


class A2AAcceptanceDenied(PermissionError):
    pass


class _RecoveryCancellationSignal:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        await asyncio.Event().wait()


class A2AAgentToolRuntime:
    def __init__(
        self,
        *,
        ledger: AgentCallLedgerStore,
        prepared_reader: PreparedInvocationSnapshotReader,
        checkpoint_reader: InvocationCheckpointReader,
        authorization: AuthorizationRefreshPort,
        room_epochs: RoomEpochStore,
        resources: ResourceMaterializerPort,
        dispatch: A2ADispatchPort,
        observations: NormalizedObservationRecorder,
        terminal_finalizer: TerminalInteractionFinalizer,
        hitl: HITLApplicationPort | None = None,
        hitl_delivery: Any | None = None,
        run_store: Any | None = None,
        canonical_hitl_control: CanonicalHITLControlPublisher | None = None,
        public_secret_values: tuple[str, ...] = (),
        policy: A2ARuntimePolicy | None = None,
        worker_id: str = "a2a-runtime",
    ) -> None:
        self.ledger = ledger
        self.prepared_reader = prepared_reader
        self.checkpoint_reader = checkpoint_reader
        self.authorization = authorization
        self.room_epochs = room_epochs
        self.resources = resources
        self.dispatch = dispatch
        self.observations = observations
        self.terminal_finalizer = terminal_finalizer
        self.hitl = hitl
        self.hitl_delivery = hitl_delivery
        self.run_store = run_store
        self.canonical_hitl_control = canonical_hitl_control
        self.public_secret_values = public_secret_values
        self.policy = policy or A2ARuntimePolicy()
        self.worker_id = worker_id

    async def accept(self, invocation: ToolInvocation) -> ToolAcceptance:
        existing = await self.ledger.load(invocation.run_id, invocation.invocation_id)
        if existing is not None:
            if not _invocation_matches_record(invocation, existing):
                raise A2AAcceptanceConflict("invocation replay does not match ledger")
            return existing.acceptance

        prepared = await self.prepared_reader.read_prepared(invocation)
        if prepared is None:
            raise A2AAcceptanceDenied("prepared invocation snapshot is missing")
        identity = _acceptance_material(invocation, prepared)
        binding = prepared.binding
        if (
            binding.binding_id != invocation.tool.binding.binding_id
            or binding.binding_digest != invocation.tool.binding.binding_digest
            or binding.tool_name != invocation.tool.definition.name
            or not _definition_matches_frozen_binding(
                invocation.tool.definition, binding.definition
            )
            or binding.requesting_subject_digest
            != _digest(prepared.requesting_subject_id)
        ):
            raise A2AAcceptanceConflict("frozen binding does not correlate")
        if not await self.room_epochs.verify_active(
            prepared.room_id, prepared.room_epoch
        ):
            raise A2AAcceptanceDenied("Room epoch is not active")
        await self._require_run_accepts_new_call(invocation.run_id)
        parsed = AgentToolInput.model_validate(invocation.arguments)
        resource_refs = [ref.ref_id for ref in prepared.resource_manifest.refs]
        decision = await self.authorization.authorize(
            binding=binding,
            requesting_subject_id=prepared.requesting_subject_id,
            room_id=prepared.room_id,
            room_epoch=prepared.room_epoch,
            resource_refs=resource_refs,
        )
        if decision != "authorized":
            raise A2AAcceptanceDenied(
                "authorization denied"
                if decision == "denied"
                else "authorization unavailable"
            )

        now = datetime.now(UTC)
        call_record_id = _stable("call", invocation.run_id, invocation.invocation_id)
        acceptance_id = _stable(
            "acceptance",
            invocation.run_id,
            invocation.invocation_id,
            invocation.idempotency_key,
        )
        command_id = _stable("dispatch", call_record_id)
        message_id = _stable("message", call_record_id)
        arguments_digest = _digest_json(invocation.arguments)
        dispatch_snapshot = {
            "command_id": command_id,
            "message_id": message_id,
            "task": parsed.task,
            "agent_id": binding.agent_id,
            "skill_id": binding.skill_id,
            "endpoint_scope": binding.endpoint_scope,
            "transport_kind": binding.transport_kind,
            "direct_mode": _select_direct_mode(binding),
            "requesting_subject_digest": _digest(prepared.requesting_subject_id),
            "room_id": prepared.room_id,
            "room_epoch": prepared.room_epoch,
            "deadline_at": invocation.deadline_at,
            "resource_manifest": prepared.resource_manifest,
        }
        record = AgentCallLedgerRecord(
            call_record_id=call_record_id,
            invocation_id=invocation.invocation_id,
            acceptance_id=acceptance_id,
            idempotency_key=invocation.idempotency_key,
            run_id=invocation.run_id,
            room_id=prepared.room_id,
            room_epoch=prepared.room_epoch,
            assistant_message_id=invocation.assistant_message_id,
            source_index=invocation.source_index,
            tool_name=invocation.tool.definition.name,
            execution_mode=binding.definition.execution_mode,
            side_effect_level=binding.definition.side_effect_level,
            binding_id=binding.binding_id,
            binding_digest=binding.binding_digest,
            agent_id=binding.agent_id,
            skill_id=binding.skill_id,
            card_digest=binding.card_digest,
            endpoint_scope_digest=binding.endpoint_scope_digest,
            arguments_digest=arguments_digest,
            requesting_subject_digest=_digest(prepared.requesting_subject_id),
            dispatch_snapshot=dispatch_snapshot,
            resource_manifest=prepared.resource_manifest,
            runtime_policy=self.policy,
            state="accepted",
            transport_kind=binding.transport_kind,
            dispatch_command_id=command_id,
            accepted_at=now,
            updated_at=now,
        )
        outcome = await self.ledger.insert(record)
        if outcome == "conflict":
            raise A2AAcceptanceConflict("call ledger identity conflict")
        if outcome not in {"accepted", "replayed"}:
            raise RuntimeError(f"call ledger acceptance failed: {outcome}")
        persisted = await self.ledger.load(invocation.run_id, invocation.invocation_id)
        if persisted is None or _record_acceptance_material(persisted) != identity:
            raise A2AAcceptanceConflict("persisted acceptance does not correlate")
        await self._fence_accepted_call(persisted)
        return persisted.acceptance

    async def _require_run_accepts_new_call(self, run_id: str) -> None:
        if await self._run_blocks_new_call(run_id):
            raise A2AAcceptanceDenied("owning Run is not accepting Agent calls")

    async def _fence_accepted_call(self, call: AgentCallLedgerRecord) -> None:
        if not await self._run_blocks_new_call(call.run_id):
            return
        canceled = await persist_local_cancellation(
            self.ledger,
            call,
            reason="owning Run was canceled during Agent-call acceptance",
        )
        raise A2AAcceptanceDenied(
            f"owning Run stopped while Agent call became {canceled.state}"
        )

    async def _run_blocks_new_call(self, run_id: str) -> bool:
        if self.run_store is None:
            return False
        run = await self.run_store.load(run_id)
        return run is None or run.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }

    async def execute(
        self,
        invocation: ToolInvocation,
        acceptance: ToolAcceptance,
        *,
        signal: CancellationSignal,
    ) -> ToolResult | ToolSuspension:
        try:
            return await self._execute(invocation, acceptance, signal=signal)
        except (
            RecoverableAdapterError,
            RecoverableCheckpointError,
            RecoverableAuthorizationError,
            RecoverableEpochError,
            RecoverableResourceError,
            RecoverableTransportError,
            AmbiguousRemoteEffectError,
            TimeoutError,
        ):
            # After durable acceptance, expected persistence/checkpoint outages are
            # recoverable lifecycle states. Never let Kernel translate them into a
            # competing generic terminal ToolResult.
            return _suspension(invocation)

    async def recover_dispatch(
        self,
        record: AgentCallLedgerRecord,
        invocation: ToolInvocation,
    ) -> None:
        """Re-enter the normal accepted-call lifecycle during background recovery.

        Recovery may only dispatch through :meth:`execute`, which owns the claim,
        attempt counter, receipt/observation application, backoff, and terminal CAS.
        The frozen invocation check prevents a stale Run snapshot from sending.
        """

        current = await self.ledger.load_by_record_id(record.call_record_id)
        if current is None or not _invocation_matches_record(invocation, current):
            raise RecoverableCheckpointError(
                "durable invocation is unavailable for call recovery"
            )
        await self.execute(
            invocation,
            current.acceptance,
            signal=_RecoveryCancellationSignal(),
        )

    async def _execute(  # noqa: C901
        self,
        invocation: ToolInvocation,
        acceptance: ToolAcceptance,
        *,
        signal: CancellationSignal,
    ) -> ToolResult | ToolSuspension:
        record = await self.ledger.load(invocation.run_id, invocation.invocation_id)
        if record is None:
            return _result(invocation, "failed", "call_ledger_missing")
        if acceptance != record.acceptance:
            # A mismatched caller cannot finalize or supersede durable call
            # authority, especially while an attached interaction is active.
            return _suspension(invocation)
        if record.terminal_result is not None:
            return await self._finalized_terminal_or_suspension(record, invocation)
        checkpointed = await self.checkpoint_reader.is_acceptance_checkpointed(
            invocation.run_id,
            invocation.invocation_id,
            acceptance.acceptance_id,
            acceptance.idempotency_key,
            record.binding_digest,
        )
        if not checkpointed or signal.cancelled:
            return _suspension(invocation)

        now = datetime.now(UTC)
        claimed = await self.ledger.claim(
            record.call_record_id,
            expected_state_version=record.state_version,
            owner_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=self.policy.claim_lease_seconds),
            claimed_at=now,
        )
        if claimed is None:
            return await self._persisted_outcome_or_suspension(invocation)
        record = claimed

        # Exhaustive execution fence. Only an accepted/ready call can invoke dispatch.
        if record.state == "dispatching":
            uncertain = transition_call(
                record,
                to_state="delivery_uncertain",
                updated_at=datetime.now(UTC),
                error_code="dispatch_receipt_missing",
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=datetime.now(UTC),
            )
            await self.ledger.cas(
                uncertain, expected_state_version=record.state_version
            )
            return _suspension(invocation)
        if record.state not in {"accepted", "ready_to_dispatch"}:
            await self._release(record)
            return _suspension(invocation)

        try:
            if not await self.room_epochs.verify_active(
                record.room_id, record.room_epoch
            ):
                return await self._terminal(
                    record, invocation, "expired", "room_epoch_gone"
                )
            prepared = await self.prepared_reader.read_prepared(invocation)
            if prepared is None or (
                prepared.binding.binding_id != record.binding_id
                or prepared.binding.binding_digest != record.binding_digest
                or _digest(prepared.requesting_subject_id)
                != record.requesting_subject_digest
            ):
                return await self._terminal(
                    record, invocation, "rejected", "prepared_snapshot_mismatch"
                )
            decision = await self.authorization.authorize(
                binding=prepared.binding,
                requesting_subject_id=prepared.requesting_subject_id,
                room_id=record.room_id,
                room_epoch=record.room_epoch,
                resource_refs=[ref.ref_id for ref in record.resource_manifest.refs],
            )
            record = await self._renew_and_verify_epoch(record)
            if record is None:
                return _suspension(invocation)
            if decision == "denied":
                return await self._terminal(
                    record, invocation, "rejected", "authorization_revoked"
                )
            if decision == "transient_failure":
                await self._release(record)
                return _suspension(invocation)
            materialized = await self.resources.materialize(
                record.resource_manifest,
                room_id=record.room_id,
                room_epoch=record.room_epoch,
                allowed_input_modes=prepared.binding.input_modes,
                deadline_at=record.dispatch_snapshot.deadline_at,
            )
            verify_materialized_digests(record.resource_manifest, materialized)
            record = await self._renew_and_verify_epoch(record)
            if record is None:
                return _suspension(invocation)
        except (
            RecoverableAdapterError,
            RecoverableAuthorizationError,
            RecoverableEpochError,
            RecoverableResourceError,
            TimeoutError,
        ):
            await self._release(record)
            raise

        if record.state == "accepted":
            ready = transition_call(
                record, to_state="ready_to_dispatch", updated_at=datetime.now(UTC)
            )
            if await self.ledger.cas(
                ready, expected_state_version=record.state_version
            ) not in {"accepted", "replayed"}:
                return _suspension(invocation)
            record = ready
        if record.state != "ready_to_dispatch":
            await self._release(record)
            return _suspension(invocation)
        dispatching = transition_call(
            record,
            to_state="dispatching",
            updated_at=datetime.now(UTC),
            transport_attempts=record.transport_attempts + 1,
        )
        if await self.ledger.cas(
            dispatching, expected_state_version=record.state_version
        ) not in {"accepted", "replayed"}:
            return _suspension(invocation)
        record = dispatching
        record = await self._renew_and_verify_epoch(record)
        if record is None:
            return _suspension(invocation)

        command = _dispatch_command(record, materialized_resources=materialized)
        if self.run_store is not None:
            owning_run = await self.run_store.load(invocation.run_id)
            if owning_run is None or owning_run.status in {
                "canceling",
                "completed",
                "failed",
                "canceled",
                "budget_exhausted",
            }:
                raise ClientCancellationRequested("owning Run is not dispatchable")
        try:
            receipt, record = await self._run_fenced_dispatch(
                record, command, signal=signal
            )
        except AgentCardContractError:
            latest = await self.ledger.load(invocation.run_id, invocation.invocation_id)
            if latest is not None and latest.claim_owner == self.worker_id:
                record = latest
            renewed = await self._renew_and_verify_epoch(record)
            if renewed is None:
                return _suspension(invocation)
            return await self._terminal(
                renewed,
                invocation,
                "failed",
                "agent_card_contract_error",
            )
        except RecoverableTransportError:
            # Card resolution happens before remote message delivery. Return the
            # call to ready_to_dispatch so recovery reuses the frozen command and
            # message ID instead of entering ambiguous-effect inspection.
            latest = await self.ledger.load(invocation.run_id, invocation.invocation_id)
            if latest is not None and latest.claim_owner == self.worker_id:
                record = latest
            renewed = await self._renew_and_verify_epoch(record)
            if renewed is None:
                return _suspension(invocation)
            if (
                renewed.transport_attempts
                >= renewed.runtime_policy.max_transport_attempts
            ):
                return await self._terminal(
                    renewed,
                    invocation,
                    "failed",
                    "agent_card_transport_unavailable",
                )
            delay = min(
                renewed.runtime_policy.retry_backoff_initial_seconds
                * (2 ** max(renewed.transport_attempts - 1, 0)),
                renewed.runtime_policy.retry_backoff_max_seconds,
            )
            retry = transition_call(
                renewed,
                to_state="ready_to_dispatch",
                updated_at=datetime.now(UTC),
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
            )
            await self.ledger.cas(retry, expected_state_version=renewed.state_version)
            return _suspension(invocation)
        except (
            RecoverableAdapterError,
            RecoverableEpochError,
            AmbiguousRemoteEffectError,
            TimeoutError,
        ):
            # Reload latest record in case heartbeat loop advanced state_version
            latest = await self.ledger.load(invocation.run_id, invocation.invocation_id)
            if latest is not None and latest.claim_owner == self.worker_id:
                record = latest
            # The expired dispatching record is intentionally left for recovery to
            # classify as uncertain when lease ownership was lost during the await.
            renewed = await self._renew_and_verify_epoch(record)
            if renewed is None:
                return _suspension(invocation)
            uncertain = transition_call(
                renewed,
                to_state="delivery_uncertain",
                updated_at=datetime.now(UTC),
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=datetime.now(UTC),
            )
            await self.ledger.cas(
                uncertain, expected_state_version=renewed.state_version
            )
            return _suspension(invocation)

        # ------------------------------------------------------------------
        # Evidence preservation: record terminal or interaction observation immediately.
        # ------------------------------------------------------------------
        if receipt.terminal_observation is not None:
            obs = receipt.terminal_observation
            if obs.call_record_id is None:
                obs = obs.model_copy(update={"call_record_id": record.call_record_id})
            await self.observations.record(obs)
            receipt = receipt.model_copy(update={"terminal_observation": obs})
        elif receipt.interaction_observation is not None:
            obs = receipt.interaction_observation
            if obs.call_record_id is None:
                obs = obs.model_copy(update={"call_record_id": record.call_record_id})
            await self.observations.record(obs)
            receipt = receipt.model_copy(update={"interaction_observation": obs})

        renewed = await self._renew_and_verify_epoch(record)
        if renewed is None:
            # Lease was lost, but observation is durably preserved.
            return _suspension(invocation)
        record = renewed

        if receipt.outcome == "delivery_uncertain":
            uncertain = transition_call(
                record,
                to_state="delivery_uncertain",
                updated_at=datetime.now(UTC),
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=datetime.now(UTC),
            )
            await self.ledger.cas(
                uncertain, expected_state_version=record.state_version
            )
            return _suspension(invocation)
        if (
            receipt.outcome == "interaction"
            and receipt.interaction_observation is not None
        ):
            return await self._interaction_result(
                record, invocation, receipt.interaction_observation
            )
        if receipt.outcome == "accepted":
            try:
                aliases = bind_authoritative_aliases(
                    record, task_id=receipt.task_id, context_id=receipt.context_id
                )
            except ValueError:
                uncertain = transition_call(
                    record,
                    to_state="delivery_uncertain",
                    updated_at=datetime.now(UTC),
                    error_code="authoritative_alias_conflict",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=datetime.now(UTC),
                )
                await self.ledger.cas(
                    uncertain, expected_state_version=record.state_version
                )
                return _suspension(invocation)
            if not any(alias.kind == "task" for alias in aliases):
                uncertain = transition_call(
                    record,
                    to_state="delivery_uncertain",
                    updated_at=datetime.now(UTC),
                    error_code="authoritative_alias_missing",
                    claim_owner=None,
                    claim_expires_at=None,
                    next_attempt_at=datetime.now(UTC),
                )
                await self.ledger.cas(
                    uncertain, expected_state_version=record.state_version
                )
                return _suspension(invocation)
            working = transition_call(
                record,
                to_state="working",
                updated_at=datetime.now(UTC),
                a2a_task_id=receipt.task_id,
                a2a_context_id=receipt.context_id,
                ownership_aliases=aliases,
                ownership_alias_keys=ownership_alias_keys(aliases),
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=datetime.now(UTC)
                + timedelta(seconds=self.policy.retry_backoff_initial_seconds),
            )
            outcome = await self.ledger.cas(
                working, expected_state_version=record.state_version
            )
            if outcome not in {"accepted", "replayed"}:
                # A collision or competing terminal winner is recovered from the
                # persisted call; never create a second dispatch.
                return await self._persisted_outcome_or_suspension(invocation)
            return _suspension(invocation)

        observation = receipt.terminal_observation
        if observation is None:
            status = "rejected" if receipt.outcome == "rejected" else "failed"
            return await self._terminal(record, invocation, status, "dispatch_rejected")
        terminal = apply_observation(
            record, observation, recent_limit=self.policy.recent_observation_id_limit
        )
        outcome = await self.ledger.cas(
            terminal, expected_state_version=record.state_version
        )
        if outcome not in {"accepted", "replayed"} or terminal.terminal_result is None:
            return await self._persisted_outcome_or_suspension(invocation)
        assert terminal.terminal_result_digest is not None
        await self.observations.mark_executor_outcome(
            observation.observation_id,
            outcome_digest=terminal.terminal_result_digest,
        )
        return await self._finalized_terminal_or_suspension(terminal, invocation)

    async def _terminal(
        self,
        record: AgentCallLedgerRecord,
        invocation: ToolInvocation,
        status: str,
        error_code: str,
    ) -> ToolResult | ToolSuspension:
        result = _result(invocation, status, error_code)
        terminal = transition_call(
            record,
            to_state=status,
            updated_at=datetime.now(UTC),
            terminal_result=result,
            terminal_result_digest=sha256(
                result.model_dump_json().encode()
            ).hexdigest(),
            error_code=error_code,
            error_message=result.error_message,
        )
        outcome = await self.ledger.cas(
            terminal, expected_state_version=record.state_version
        )
        if outcome in {"accepted", "replayed"}:
            return await self._finalized_terminal_or_suspension(terminal, invocation)
        return await self._persisted_outcome_or_suspension(invocation)

    async def _persisted_outcome_or_suspension(
        self, invocation: ToolInvocation
    ) -> ToolResult | ToolSuspension:
        current = await self.ledger.load(invocation.run_id, invocation.invocation_id)
        if current is not None and current.terminal_result is not None:
            return await self._finalized_terminal_or_suspension(current, invocation)
        return _suspension(invocation)

    async def _finalized_terminal_or_suspension(
        self,
        record: AgentCallLedgerRecord,
        invocation: ToolInvocation,
    ) -> ToolResult | ToolSuspension:
        if record.terminal_result is None:
            return _suspension(invocation)
        await self.terminal_finalizer.finalize(record)
        return record.terminal_result

    async def _interaction_result(
        self,
        record: AgentCallLedgerRecord,
        invocation: ToolInvocation,
        observation: NormalizedA2AObservation,
    ) -> ToolResult | ToolSuspension:
        """Bind typed HITL or return a silent completed request for untyped agents.

        Typed ``interaction_spec`` (travel clarify): create/activate a durable
        HITL aggregate, park the call in ``input_required`` /
        ``auth_required``, and return ``ToolSuspension`` so the kernel waits
        for the user answer + continuation.

        Missing spec (cyber silent recovery): record the request text as a
        completed tool result so the next model turn can continue without a
        UI challenge.

        Invalid spec: fail closed as a terminal failed tool result.
        """
        _record_outcome, persisted_observation = await self.observations.record(
            observation
        )
        observation = persisted_observation.observation
        renewed = await self._renew_and_verify_epoch(record)
        if renewed is None:
            return _suspension(invocation)
        if self.hitl is None and observation.interaction_spec is not None:
            raise RuntimeError("HITL port not bound but interaction spec received")
        try:
            persisted, kind = await park_call_for_interaction(
                call=renewed,
                observation=observation,
                hitl=self.hitl,
                cas=self._cas_interaction_winner,
            )
        except RecoverableCheckpointError:
            return await self._persisted_outcome_or_suspension(invocation)
        if kind == "typed_waiting":
            interaction = _matching_parked_interaction(persisted, observation)
            if interaction is None:
                # A concurrent CAS winner owns another lifecycle state. Never
                # checkpoint or publish the losing questionnaire.
                return await self._persisted_outcome_or_suspension(invocation)
            await self.observations.mark_ledger_applied(observation.observation_id)
            # Model-first HITL: the interaction is presented to the model as a
            # tool_interaction message. User-facing hitl_request publication is
            # deferred until the kernel escalates (request_user_input) or
            # degrades; it must never fire here or the user is asked before the
            # model has decided.
            return self._interaction_suspension(
                invocation, persisted, observation, interaction
            )
        return await self._finalize_interaction_terminal(
            persisted, invocation, observation
        )

    async def _cas_interaction_winner(
        self, candidate: AgentCallLedgerRecord, expected: int
    ) -> AgentCallLedgerRecord:
        outcome = await self.ledger.cas(candidate, expected_state_version=expected)
        if outcome in {"accepted", "replayed"}:
            return candidate
        winner = await self.ledger.load_by_record_id(candidate.call_record_id)
        if winner is None:
            raise RecoverableCheckpointError(
                "interaction CAS winner could not be classified"
            )
        return winner

    async def dispatch_model_reply(  # noqa: C901
        self,
        invocation: ToolInvocation,
        *,
        parent_call_record_id: str,
        interaction_fingerprint: str | None,
        signal: CancellationSignal,
    ) -> ToolResult | ToolSuspension:
        del signal
        call = await self.ledger.load_by_record_id(parent_call_record_id)
        if call is None:
            return _result(invocation, "failed", "call_ledger_missing")
        # Replay dedup: a prior join binding for this exact invocation means the
        # counter was already persisted; a replay re-dispatches idempotently
        # (deterministic command id) without re-checking or re-counting.
        already_joined = any(
            binding.join_invocation_id == invocation.invocation_id
            for binding in call.model_reply_joins
        )
        # Re-dispatch after the parent already resolved: return the persisted
        # outcome instead of failing as "not interactive" or re-sending.
        if already_joined and call.state not in {"input_required", "auth_required"}:
            if call.terminal_result is not None:
                try:
                    await self.terminal_finalizer.finalize(call)
                except RecoverableCheckpointError:
                    return await self._model_reply_suspension(invocation, call)
                return ToolResult(
                    call_id=invocation.invocation_id,
                    tool_name=invocation.tool.definition.name,
                    status=call.terminal_result.status,
                    content=call.terminal_result.content,
                    artifact_refs=call.terminal_result.artifact_refs,
                    error_code=call.terminal_result.error_code,
                    error_message=call.terminal_result.error_message,
                )
            return await self._model_reply_suspension(invocation, call)
        if (
            call.state not in {"input_required", "auth_required"}
            or call.pending_interaction_id is None
        ):
            return _result(
                invocation,
                "failed",
                "join_target_not_interactive",
            )
        if call.a2a_task_id is None or call.a2a_context_id is None:
            return _result(invocation, "failed", "continuation_target_missing")
        fingerprint = interaction_fingerprint or call.interaction_fingerprint or ""
        if not already_joined and call.model_reply_rounds.get(fingerprint, 0) >= 2:
            return ToolResult(
                call_id=invocation.invocation_id,
                tool_name=invocation.tool.definition.name,
                status="failed",
                content=[],
                artifact_refs=[],
                error_code="auto_reply_limit_reached",
                error_message=(
                    "The platform will not auto-reply to the same Agent "
                    "question again. Ask the user or conclude from evidence."
                ),
            )
        try:
            parsed = AgentToolInput.model_validate(invocation.arguments)
        except Exception:
            return _result(invocation, "failed", "invalid_tool_call")
        command_id = _stable(
            "model-reply",
            call.call_record_id,
            fingerprint,
            invocation.invocation_id,
        )
        command = A2AModelReplyCommand(
            command_id=command_id,
            transport_kind=call.transport_kind,
            call_record_id=call.call_record_id,
            binding_id=call.binding_id,
            binding_digest=call.binding_digest,
            requesting_subject_digest=call.requesting_subject_digest,
            task_id=call.a2a_task_id,
            context_id=call.a2a_context_id,
            room_id=call.room_id,
            room_epoch=call.room_epoch,
            message_text=parsed.task,
            interaction_fingerprint=fingerprint or None,
            created_at=datetime.now(UTC),
        )
        rounds = dict(call.model_reply_rounds)
        joins = list(call.model_reply_joins)
        if not already_joined:
            rounds[fingerprint] = rounds.get(fingerprint, 0) + 1
            joins.append(
                A2AJoinBinding(
                    join_invocation_id=invocation.invocation_id,
                    command_id=command_id,
                    interaction_fingerprint=fingerprint or None,
                    created_at=datetime.now(UTC),
                )
            )
        updated = call.model_copy(
            update={
                "model_reply_rounds": rounds,
                "model_reply_joins": joins,
                "state_version": call.state_version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        if await self.ledger.cas(
            updated, expected_state_version=call.state_version
        ) not in {"accepted", "replayed"}:
            return await self._model_reply_suspension(invocation, call)
        call = updated
        try:
            receipt = await self.dispatch.dispatch_model_reply(command)
        except AgentCardContractError:
            return await self._terminalize_parent_for_join(
                call,
                invocation,
                error_code="agent_card_contract_error",
            )
        except (
            RecoverableTransportError,
            AmbiguousRemoteEffectError,
            TimeoutError,
        ):
            return await self._model_reply_suspension(invocation, call)

        parent = await self.ledger.load_by_record_id(call.call_record_id)
        if parent is None:
            return await self._model_reply_suspension(invocation, call)
        # A response observation can only apply from ``resuming``; leave the
        # parent parked for a bare accepted acknowledgement (no observation).
        has_observation = receipt.terminal_observation is not None or (
            receipt.outcome == "interaction"
            and receipt.interaction_observation is not None
        )
        if has_observation and parent.state in {"input_required", "auth_required"}:
            resuming = transition_call(
                parent,
                to_state="resuming",
                updated_at=datetime.now(UTC),
            )
            if await self.ledger.cas(
                resuming, expected_state_version=parent.state_version
            ) in {"accepted", "replayed"}:
                parent = resuming
            else:
                parent = await self.ledger.load_by_record_id(call.call_record_id)
                if parent is None:
                    return await self._model_reply_suspension(invocation, call)
        observation = receipt.terminal_observation
        if observation is not None:
            if observation.call_record_id is None:
                observation = observation.model_copy(
                    update={"call_record_id": parent.call_record_id}
                )
            _, inbox = await self.observations.record(observation)
            observation = inbox.observation
            terminal = apply_observation(
                parent,
                observation,
                recent_limit=parent.runtime_policy.recent_observation_id_limit,
            )
            await self.ledger.cas(terminal, expected_state_version=parent.state_version)
            if terminal.terminal_result is None:
                return await self._model_reply_suspension(invocation, call)
            result = terminal.terminal_result
            await self.terminal_finalizer.finalize(terminal)
            return ToolResult(
                call_id=invocation.invocation_id,
                tool_name=invocation.tool.definition.name,
                status=result.status,
                content=result.content,
                artifact_refs=result.artifact_refs,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        if (
            receipt.outcome == "interaction"
            and receipt.interaction_observation is not None
        ):
            observation = receipt.interaction_observation
            if observation.call_record_id is None:
                observation = observation.model_copy(
                    update={"call_record_id": parent.call_record_id}
                )
            try:
                persisted, kind = await park_call_for_interaction(
                    call=parent,
                    observation=observation,
                    hitl=self.hitl,
                    cas=self._cas_interaction_winner,
                )
            except RecoverableCheckpointError:
                return await self._model_reply_suspension(invocation, call)
            if kind == "typed_waiting":
                interaction = _matching_parked_interaction(persisted, observation)
                if interaction is None:
                    return await self._model_reply_suspension(invocation, call)
                await self.observations.mark_ledger_applied(observation.observation_id)
                return self._interaction_suspension(
                    invocation, persisted, observation, interaction
                )
            return await self._finalize_interaction_terminal(
                persisted, invocation, observation
            )
        # A bare accepted acknowledgement means the Agent is still working; the
        # response observation arrives later via the parent call's inbox/poll
        # path (translated to this join invocation). Do not treat it as a
        # retryable transport failure.
        delivery_state = (
            "accepted" if receipt.outcome == "accepted" else "transport_uncertain"
        )
        return await self._model_reply_suspension(
            invocation, call, delivery_state=delivery_state
        )

    async def _terminalize_parent_for_join(
        self,
        call: AgentCallLedgerRecord,
        join_invocation: ToolInvocation,
        *,
        error_code: str,
    ) -> ToolResult | ToolSuspension:
        """Persist the permanent parent failure before resolving its join."""

        parent_result = ToolResult(
            call_id=call.invocation_id,
            tool_name=call.tool_name,
            status="failed",
            content=[TextPart(text="The Agent call could not complete.")],
            artifact_refs=[],
            error_code=error_code,
            error_message=error_code.replace("_", " "),
        )
        terminal = transition_call(
            call,
            to_state="failed",
            updated_at=datetime.now(UTC),
            terminal_result=parent_result,
            terminal_result_digest=sha256(
                parent_result.model_dump_json().encode()
            ).hexdigest(),
            error_code=error_code,
            error_message=parent_result.error_message,
        )
        outcome = await self.ledger.cas(
            terminal, expected_state_version=call.state_version
        )
        winner = terminal
        if outcome not in {"accepted", "replayed"}:
            loaded = await self.ledger.load_by_record_id(call.call_record_id)
            if loaded is None or loaded.terminal_result is None:
                return await self._model_reply_suspension(join_invocation, call)
            winner = loaded
        try:
            await self.terminal_finalizer.finalize(winner)
        except RecoverableCheckpointError:
            return await self._model_reply_suspension(join_invocation, winner)
        result = winner.terminal_result
        assert result is not None
        return ToolResult(
            call_id=join_invocation.invocation_id,
            tool_name=join_invocation.tool.definition.name,
            status=result.status,
            content=result.content,
            artifact_refs=result.artifact_refs,
            error_code=result.error_code,
            error_message=result.error_message,
        )

    async def _model_reply_suspension(
        self,
        invocation: ToolInvocation,
        call: AgentCallLedgerRecord,
        *,
        delivery_state: Literal[
            "accepted", "transport_uncertain"
        ] = "transport_uncertain",
    ) -> ToolSuspension:
        """Correlated suspension for a model-reply continuation.

        Carries the parent call identity and parked-interaction metadata so the
        kernel can re-dispatch the same invocation after a recoverable transport
        failure or restart instead of stalling in ``waiting_external``.

        ``delivery_state`` distinguishes an accepted-but-still-working reply
        (``accepted``: wait for the async observation) from a genuinely
        uncertain delivery (``transport_uncertain``: safe to re-dispatch).
        """
        questions: list[ToolInteractionQuestion] = []
        interaction_id = call.pending_interaction_id
        if self.hitl is not None and interaction_id is not None:
            try:
                parked = await self.hitl.read_interaction(interaction_id)
            except Exception:
                parked = None
            if parked is not None:
                interaction, _route, _fingerprint = parked
                questions = [
                    _interaction_question(question)
                    for question in interaction.questions
                ]
        return ToolSuspension(
            invocation_id=invocation.invocation_id,
            status="waiting_external",
            delivery_state=delivery_state,
            call_record_id=call.call_record_id,
            interaction_id=interaction_id,
            interaction_fingerprint=call.interaction_fingerprint,
            questions=questions,
        )

    def _interaction_suspension(
        self,
        invocation: ToolInvocation,
        persisted: AgentCallLedgerRecord,
        observation: NormalizedA2AObservation,
        interaction: A2AInteractionSpec,
    ) -> ToolSuspension:
        waiting_state = (
            observation.event_kind
            if observation.event_kind in {"input_required", "auth_required"}
            else "input_required"
        )
        questions = [
            _interaction_question(question) for question in interaction.questions
        ]
        return ToolSuspension(
            invocation_id=invocation.invocation_id,
            status=waiting_state,
            call_record_id=persisted.call_record_id,
            interaction_id=persisted.pending_interaction_id
            or interaction.interaction_id,
            interaction_fingerprint=persisted.interaction_fingerprint
            or _digest_json(interaction.model_dump(mode="json")),
            questions=questions,
        )

    async def _emit_parked_hitl_events(
        self,
        persisted: AgentCallLedgerRecord,
        interaction: A2AInteractionSpec,
    ) -> None:
        assert persisted.pending_interaction_id == interaction.interaction_id
        await emit_hitl_request_events(
            record=persisted,
            interaction=interaction,
            interaction_id=persisted.pending_interaction_id,
            hitl_delivery=self.hitl_delivery,
            run_store=self.run_store,
            canonical_control=self.canonical_hitl_control,
            public_secret_values=self.public_secret_values,
        )

    async def publish_parked_interaction(
        self,
        *,
        call_record_id: str,
        interaction_id: str,
    ) -> None:
        """Deferred user-facing publication of a parked interaction (F5 degrade).

        The initial/join park paths stay silent (model-first); the kernel calls
        this only when it escalates the interaction to the user directly.
        """
        call = await self.ledger.load_by_record_id(call_record_id)
        if call is None or call.pending_interaction_id != interaction_id:
            return
        if self.hitl is None:
            return
        parked = await self.hitl.read_interaction(interaction_id)
        if parked is None:
            return
        interaction, _route, _fingerprint = parked
        # Durable user-visibility switch: only published interactions enter
        # the pending projection (REST/snapshot). Mark before emitting so the
        # projection can never observe a half-published interaction.
        await self.hitl.publish(interaction_id, call_record_id=call_record_id)
        await self._emit_parked_hitl_events(call, interaction)

    async def abandon_parked_interaction(
        self,
        *,
        call_record_id: str,
        interaction_id: str,
        terminal_state: str,
    ) -> None:
        """Close exact parked ownership before Run terminalization.

        The finalizer is idempotent (``absent`` is a valid replay), so the
        kernel must call it even when the call ledger has already converged.
        Store/finalizer failures propagate: closing the public Tool/Run while
        the exact interaction may remain actionable would violate the terminal
        winner invariant.
        """
        await self.terminal_finalizer.finalize_interaction(
            interaction_id=interaction_id,
            call_record_id=call_record_id,
            terminal_state=terminal_state,
        )

    async def _finalize_interaction_terminal(
        self,
        persisted: AgentCallLedgerRecord,
        invocation: ToolInvocation,
        observation: NormalizedA2AObservation,
    ) -> ToolResult | ToolSuspension:
        if persisted.terminal_result is None:
            return await self._persisted_outcome_or_suspension(invocation)
        if persisted.terminal_result_digest is not None:
            with suppress(ObservationIngressError):
                await self.observations.mark_executor_outcome(
                    observation.observation_id,
                    outcome_digest=persisted.terminal_result_digest,
                )
        await self.terminal_finalizer.finalize(persisted)
        return persisted.terminal_result

    async def _renew_and_verify_epoch(
        self, record: AgentCallLedgerRecord
    ) -> AgentCallLedgerRecord | None:
        now = datetime.now(UTC)
        renewed = await self.ledger.renew(
            record.call_record_id,
            expected_state_version=record.state_version,
            owner_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=self.policy.claim_lease_seconds),
            renewed_at=now,
        )
        if renewed is None:
            return None
        if not await self.room_epochs.verify_active(
            renewed.room_id, renewed.room_epoch
        ):
            return None
        return renewed

    async def _run_fenced_dispatch(
        self,
        record: AgentCallLedgerRecord,
        command: A2ADispatchCommand,
        *,
        signal: Any = None,
    ) -> tuple[A2ADispatchReceipt, AgentCallLedgerRecord]:
        current_record = [record]
        stop_heartbeat = asyncio.Event()

        async def _heartbeat_loop() -> None:
            interval = self.policy.claim_renew_interval_seconds

            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
                if stop_heartbeat.is_set():
                    break
                renewed = await self._renew_and_verify_epoch(current_record[0])
                if renewed is None:
                    break
                current_record[0] = renewed

        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        dispatch_task = asyncio.create_task(self.dispatch.dispatch(command))
        cancellation_task = (
            asyncio.create_task(signal.wait())
            if signal is not None
            else asyncio.create_task(asyncio.Event().wait())
        )

        try:
            done, _ = await asyncio.wait(
                {dispatch_task, heartbeat_task, cancellation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancellation_task in done or (signal is not None and signal.cancelled):
                raise ClientCancellationRequested("dispatch cancelled by client signal")

            if not stop_heartbeat.is_set() and dispatch_task not in done:
                raise RecoverableEpochError(
                    "claim lease or room epoch was lost during dispatch"
                )

            receipt = await dispatch_task
            return receipt, current_record[0]
        finally:
            stop_heartbeat.set()
            for task in (dispatch_task, heartbeat_task, cancellation_task):
                if not task.done():
                    task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(
                    dispatch_task,
                    heartbeat_task,
                    cancellation_task,
                    return_exceptions=True,
                )

    async def _release(self, record: AgentCallLedgerRecord) -> None:
        await self.ledger.release(
            record.call_record_id,
            expected_state_version=record.state_version,
            owner_id=self.worker_id,
            next_attempt_at=datetime.now(UTC)
            + timedelta(seconds=self.policy.retry_backoff_initial_seconds),
            released_at=datetime.now(UTC),
        )


def _dispatch_command(
    record: AgentCallLedgerRecord,
    *,
    materialized_resources: list,
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
        materialized_resources=materialized_resources,
        room_id=record.room_id,
        room_epoch=record.room_epoch,
        deadline_at=record.dispatch_snapshot.deadline_at,
    )


def _select_direct_mode(binding) -> str | None:
    if binding.transport_kind != "direct":
        return None
    capabilities = set(binding.direct_capabilities)
    for mode in ("stream", "sync", "poll"):
        if mode in capabilities:
            return mode
    raise A2AAcceptanceDenied("direct Agent has no supported delivery capability")


def _definition_matches_frozen_binding(
    invocation: ToolDefinition, frozen: ToolDefinition
) -> bool:
    """Permit only the live resource-ref schema to differ from the binding.

    Agent identity and execution semantics remain frozen. The Kernel validates
    arguments against the current Run schema, while acceptance independently
    freezes and authorizes the selected resource manifest.
    """

    return invocation == frozen.model_copy(
        update={"input_schema": invocation.input_schema}
    )


def _acceptance_material(invocation: ToolInvocation, prepared) -> tuple[object, ...]:
    return (
        _stable("call", invocation.run_id, invocation.invocation_id),
        invocation.run_id,
        invocation.invocation_id,
        invocation.idempotency_key,
        invocation.tool.binding.binding_digest,
        _digest_json(invocation.arguments),
        prepared.resource_manifest.content_digest,
        prepared.room_epoch,
    )


def _invocation_matches_record(
    invocation: ToolInvocation, record: AgentCallLedgerRecord
) -> bool:
    return (
        record.call_record_id
        == _stable("call", invocation.run_id, invocation.invocation_id)
        and record.run_id == invocation.run_id
        and record.invocation_id == invocation.invocation_id
        and record.idempotency_key == invocation.idempotency_key
        and record.binding_id == invocation.tool.binding.binding_id
        and record.binding_digest == invocation.tool.binding.binding_digest
        and record.tool_name == invocation.tool.definition.name
        and record.execution_mode == invocation.tool.definition.execution_mode
        and record.side_effect_level == invocation.tool.definition.side_effect_level
        and record.arguments_digest == _digest_json(invocation.arguments)
    )


def _record_acceptance_material(record: AgentCallLedgerRecord) -> tuple[object, ...]:
    return (
        record.call_record_id,
        record.run_id,
        record.invocation_id,
        record.idempotency_key,
        record.binding_digest,
        record.arguments_digest,
        record.resource_manifest.content_digest,
        record.room_epoch,
    )


def _matching_parked_interaction(
    record: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
) -> A2AInteractionSpec | None:
    raw_spec = observation.interaction_spec
    if raw_spec is None or record.pending_interaction_id is None:
        return None
    interaction = A2AInteractionSpec.model_validate(raw_spec)
    fingerprint = _digest_json(interaction.model_dump(mode="json"))
    if (
        record.pending_interaction_id != interaction.interaction_id
        or record.interaction_fingerprint != fingerprint
        or record.state not in {"input_required", "auth_required"}
    ):
        return None
    return interaction


def _interaction_question(question: HITLQuestionSpec) -> ToolInteractionQuestion:
    choices = list(question.choices) if question.choices else None
    return ToolInteractionQuestion(
        question_id=question.question_id,
        interaction_kind=question.interaction_kind.value,
        prompt=question.prompt,
        answer_kind=question.answer_kind.value,
        required=question.required,
        choices=choices,
    )


def _result(invocation: ToolInvocation, status: str, error_code: str) -> ToolResult:
    return ToolResult(
        call_id=invocation.invocation_id,
        tool_name=invocation.tool.definition.name,
        status=status,
        content=[TextPart(text="The Agent call could not complete.")],
        artifact_refs=[],
        error_code=error_code,
        error_message=error_code.replace("_", " "),
    )


def _suspension(invocation: ToolInvocation) -> ToolSuspension:
    return ToolSuspension(
        invocation_id=invocation.invocation_id, status="waiting_external"
    )


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _stable(prefix: str, *parts: str) -> str:
    return f"{prefix}-{_digest_json([*parts])}"


def _digest_json(value: object) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode()).hexdigest()
