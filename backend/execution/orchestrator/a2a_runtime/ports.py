"""Narrow owner and transport ports for the A2A runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer

from ..models import ToolInvocation, ToolObservation
from .models import (
    A2ACancellationCommand,
    A2AContinuationCommand,
    A2ADispatchCommand,
    A2ADispatchReceipt,
    A2AModelReplyCommand,
    A2AObservationConflictRecord,
    A2AObservationInboxRecord,
    AgentCallLedgerRecord,
    AgentToolBindingRecord,
    DurableHITLAnswerRecord,
    FrozenCallResourceManifest,
    HITLRouteSnapshotV2,
    MaterializedResourcePart,
    NormalizedA2AObservation,
    PreparedInvocationSnapshot,
    RoomEpoch,
    VerifiedAuthReferenceBinding,
)

StoreOutcome = Literal["accepted", "replayed", "conflict", "error"]
HITLAbandonOutcome = Literal["accepted", "replayed", "absent", "conflict", "error"]


class AgentCallAnswer(Protocol):
    """What the check rules answer for one new Agent call."""

    @property
    def answer(self) -> str: ...

    @property
    def rule_id(self) -> str | None: ...


@runtime_checkable
class AgentCallCheckPort(Protocol):
    """May this new Agent call go? ``policy`` implements it; the container
    wires it. It is asked once per new call, never for a reply, a continuation
    or a recovery re-dispatch of a call that already passed."""

    async def check_agent_call(self, agent_id: str) -> AgentCallAnswer: ...


class AuthorizationRefreshPort(Protocol):
    async def authorize(
        self,
        *,
        binding: AgentToolBindingRecord,
        requesting_subject_id: str,
        conversation_id: str,
        room_epoch: int,
        resource_refs: list[str],
    ) -> Literal["authorized", "denied", "transient_failure"]: ...


class AgentToolBindingStore(Protocol):
    async def insert(self, record: AgentToolBindingRecord) -> StoreOutcome: ...

    async def load(self, binding_id: str) -> AgentToolBindingRecord | None: ...

    async def list_for_run(self, run_id: str) -> list[AgentToolBindingRecord]: ...

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int: ...


class PreparedInvocationSnapshotReader(Protocol):
    async def read_prepared(
        self, invocation: ToolInvocation
    ) -> PreparedInvocationSnapshot | None: ...


class AgentCallLedgerStore(Protocol):
    async def insert(self, record: AgentCallLedgerRecord) -> StoreOutcome: ...

    async def load(
        self, run_id: str, invocation_id: str
    ) -> AgentCallLedgerRecord | None: ...

    async def load_by_record_id(
        self, call_record_id: str
    ) -> AgentCallLedgerRecord | None: ...

    async def find_by_alias(
        self, binding_scope: str, *, task_id: str | None, context_id: str | None
    ) -> AgentCallLedgerRecord | None: ...

    async def cas(
        self,
        record: AgentCallLedgerRecord,
        *,
        expected_state_version: int,
    ) -> StoreOutcome: ...

    async def claim(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> AgentCallLedgerRecord | None: ...

    async def renew(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        renewed_at: datetime,
    ) -> AgentCallLedgerRecord | None: ...

    async def release(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime | None,
        released_at: datetime,
    ) -> AgentCallLedgerRecord | None: ...

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[AgentCallLedgerRecord]: ...

    async def list_for_run(self, run_id: str) -> list[AgentCallLedgerRecord]: ...

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int: ...


class ObservationInboxStore(Protocol):
    async def insert(self, record: A2AObservationInboxRecord) -> StoreOutcome: ...

    async def load(self, observation_id: str) -> A2AObservationInboxRecord | None: ...

    async def load_by_source_identity(
        self, source_identity: str
    ) -> A2AObservationInboxRecord | None: ...

    async def cas(
        self,
        record: A2AObservationInboxRecord,
        *,
        expected_state_version: int,
        owner_id: str | None = None,
        claim_token: str | None = None,
    ) -> StoreOutcome: ...

    async def claim(
        self,
        observation_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        claim_token: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> A2AObservationInboxRecord | None: ...

    async def renew(
        self,
        observation_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        claim_token: str,
        lease_expires_at: datetime,
        renewed_at: datetime,
    ) -> A2AObservationInboxRecord | None: ...

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]: ...

    async def list_due_for_call(
        self, call_record_id: str, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]: ...

    async def delete_by_binding_scope(self, binding_scope: str) -> int: ...

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int: ...


class NormalizedObservationRecorder(Protocol):
    async def record(
        self, observation: NormalizedA2AObservation
    ) -> tuple[StoreOutcome, A2AObservationInboxRecord]: ...

    async def mark_ledger_applied(self, observation_id: str) -> None: ...

    async def mark_executor_outcome(
        self,
        observation_id: str,
        *,
        outcome_digest: str,
    ) -> None: ...


class ObservationConflictStore(Protocol):
    async def insert(self, record: A2AObservationConflictRecord) -> StoreOutcome: ...

    async def list_for_source(
        self, source_identity: str
    ) -> list[A2AObservationConflictRecord]: ...

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int: ...


class A2ADispatchPort(Protocol):
    async def dispatch(self, command: A2ADispatchCommand) -> A2ADispatchReceipt: ...

    async def inspect(self, command: A2ADispatchCommand) -> A2ADispatchReceipt: ...

    async def continue_task(
        self, command: A2AContinuationCommand
    ) -> A2ADispatchReceipt: ...

    async def dispatch_model_reply(
        self, command: A2AModelReplyCommand
    ) -> A2ADispatchReceipt: ...

    async def inspect_continuation(
        self, command: A2AContinuationCommand
    ) -> A2ADispatchReceipt: ...

    async def cancel(self, command: A2ACancellationCommand) -> A2ADispatchReceipt: ...

    async def inspect_cancellation(
        self, command: A2ACancellationCommand
    ) -> A2ADispatchReceipt: ...

    def is_command_retry_safe(self, transport_kind: str) -> bool: ...


class ResourceMaterializerPort(Protocol):
    async def materialize(
        self,
        manifest: FrozenCallResourceManifest,
        *,
        conversation_id: str,
        room_epoch: int,
        allowed_input_modes: list[str],
        deadline_at: datetime,
    ) -> list[MaterializedResourcePart]: ...

    async def materialize_inbound_artifacts(
        self,
        *,
        call: AgentCallLedgerRecord,
        artifact_refs: list[str],
        observation_id: str,
    ) -> list[str]: ...


class HITLApplicationPort(Protocol):
    async def create_or_replay(
        self,
        *,
        call: AgentCallLedgerRecord,
        interaction: A2AInteractionSpec,
        interaction_fingerprint: str,
    ) -> str: ...

    async def activate(
        self,
        interaction_id: str,
        *,
        call_record_id: str,
        interaction_fingerprint: str,
    ) -> StoreOutcome: ...

    async def abandon(
        self,
        interaction_id: str,
        *,
        call_record_id: str,
        reason: str,
    ) -> HITLAbandonOutcome: ...

    async def read_interaction(
        self, interaction_id: str
    ) -> tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str] | None: ...

    async def get_eligible_interactions(
        self, conversation_id: str
    ) -> list[tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str]]: ...

    async def get_published_interactions(
        self, conversation_id: str
    ) -> list[tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str]]: ...

    async def publish(self, interaction_id: str, *, call_record_id: str) -> str: ...

    async def read_answers(
        self, interaction_id: str, interaction_revision: int
    ) -> list[HITLQuestionAnswer] | None: ...

    async def read_answer_record(
        self, interaction_id: str, interaction_revision: int
    ) -> DurableHITLAnswerRecord | None: ...

    async def answer(
        self,
        *,
        interaction_id: str,
        interaction_revision: int,
        route_fingerprint: str,
        answers: list[HITLQuestionAnswer],
        authenticated_answerer_id: str,
        verified_auth_reference_digests: list[str],
        verified_auth_references: list[VerifiedAuthReferenceBinding],
    ) -> str: ...


class ToolObservationSink(Protocol):
    async def deliver(self, run_id: str, observation: ToolObservation) -> None: ...


class ObservationIngressAuthenticator(Protocol):
    async def authenticate(
        self,
        *,
        source_kind: str,
        headers: dict[str, str],
        body: bytes,
    ) -> str: ...


class RoomEpochStore(Protocol):
    async def read_active(self, conversation_id: str) -> RoomEpoch | None: ...

    async def activate(
        self, conversation_id: str, creation_id: str, *, activated_at: datetime
    ) -> tuple[StoreOutcome, RoomEpoch | None]: ...

    async def deactivate(
        self,
        conversation_id: str,
        epoch: int,
        deletion_id: str,
        *,
        deactivated_at: datetime,
    ) -> tuple[StoreOutcome, RoomEpoch | None]: ...

    async def verify_active(self, conversation_id: str, epoch: int) -> bool: ...

    async def verify_cleanup_epoch(
        self, conversation_id: str, epoch: int, deletion_id: str
    ) -> bool: ...
