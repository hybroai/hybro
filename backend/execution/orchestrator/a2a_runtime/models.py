"""Private durable contracts for the orchestrator A2A runtime."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, SecretStr, StringConstraints, model_validator

from common.dto.base import FrozenDTO, stored_conversation_id
from common.dto.hitl import HITLQuestionAnswer, HITLRouteSnapshot

from ..models import (
    ContentPart,
    ContractModel,
    DataPart,
    ToolAcceptance,
    ToolDefinition,
    ToolResult,
)


class A2ADurableModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


AgentCallState = Literal[
    "accepted",
    "ready_to_dispatch",
    "dispatching",
    "delivery_uncertain",
    "working",
    "continuation_pending",
    "input_required",
    "auth_required",
    "resuming",
    "cancel_pending",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "expired",
]
AGENT_CALL_STATES = frozenset(AgentCallState.__args__)
TERMINAL_AGENT_CALL_STATES = frozenset(
    {"completed", "failed", "canceled", "rejected", "expired"}
)
ACTIVE_AGENT_CALL_STATES = AGENT_CALL_STATES - TERMINAL_AGENT_CALL_STATES


class A2ARuntimePolicy(A2ADurableModel):
    max_task_chars: int = Field(default=20_000, gt=0, le=100_000)
    max_callback_body_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    max_normalized_observation_bytes: int = Field(default=256 * 1024, gt=0)
    claim_lease_seconds: float = Field(default=30.0, gt=0)
    claim_renew_interval_seconds: float = Field(default=10.0, gt=0)
    max_transport_attempts: int = Field(default=3, gt=0)
    max_uncertain_inspection_attempts: int = Field(default=3, gt=0)
    max_authorization_refresh_attempts: int = Field(default=3, gt=0)
    retry_backoff_initial_seconds: int = Field(default=1, gt=0)
    retry_backoff_max_seconds: int = Field(default=60, gt=0)
    orphan_acceptance_ttl_seconds: int = Field(default=300, gt=0)
    recent_observation_id_limit: int = Field(default=128, gt=0, le=128)
    recovery_batch_limit: int = Field(default=100, gt=0, le=1000)

    @model_validator(mode="after")
    def _policy_is_coherent(self) -> A2ARuntimePolicy:
        if self.claim_renew_interval_seconds >= self.claim_lease_seconds:
            raise ValueError("claim renewal must occur before lease expiry")
        if self.retry_backoff_initial_seconds > self.retry_backoff_max_seconds:
            raise ValueError("initial retry backoff exceeds maximum")
        return self


class AgentToolCandidate(ContractModel):
    agent_id: str
    skill_id: str | None = None
    # Root Agent Card name. ``display_name`` may include a skill suffix for
    # trace/tool labels, while cards must retain the actual Agent identity.
    agent_display_name: str | None = None
    display_name: str
    description: str = ""
    card_digest: str
    endpoint_scope: str
    endpoint_scope_digest: str
    transport_kind: Literal["direct", "relay"]
    direct_capabilities: list[Literal["sync", "stream", "poll"]] = Field(
        default_factory=lambda: ["sync", "poll"]
    )
    active: bool = True
    authorized: bool = True
    excluded: bool = False
    input_modes: list[str] = Field(default_factory=lambda: ["text"])
    output_modes: list[str] = Field(default_factory=list)
    execution_mode: Literal["sequential", "parallel"] = "parallel"


class AgentToolBindingRecord(A2ADurableModel):
    schema_version: Literal[1] = 1
    binding_id: str
    binding_digest: str
    run_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    tool_name: str
    definition: ToolDefinition
    agent_id: str
    skill_id: str | None = None
    # Optional for schema-v1 compatibility with bindings written before the
    # public Agent-card name was denormalized.
    agent_display_name: str | None = None
    card_digest: str
    endpoint_scope: str
    endpoint_scope_digest: str
    transport_kind: Literal["direct", "relay"]
    direct_capabilities: list[Literal["sync", "stream", "poll"]] = Field(
        default_factory=lambda: ["sync", "poll"]
    )
    candidate_scope_id: str
    candidate_scope_revision: int = Field(ge=1)
    authorization_basis_digest: str
    # Denormalized AuthorizationBasis.kind so the refresh adapter can honor
    # scope semantics without re-resolving the Run. Per-turn explicit scopes
    # (all_active_agents, mention, explicit_selection) skip the room-membership
    # gate; roster-derived kinds (room_member, saved_group_member) require it.
    authorization_kind: Literal[
        "room_member",
        "saved_group_member",
        "explicit_selection",
        "mention",
        "all_active_agents",
    ]
    requesting_subject_digest: str
    input_modes: list[str]
    output_modes: list[str]
    compatible_resource_refs: list[str] = Field(default_factory=list)
    created_at: datetime


class FrozenCallResourceRef(A2ADurableModel):
    ref_id: str
    kind: Literal["context", "artifact", "attachment"]
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    source_message_id: str
    source_agent_id: str | None = None
    mime_type: str | None = None
    size_bytes: int = Field(default=0, ge=0)
    content_digest: str
    projection_id: str | None = None
    materialization_digest: str | None = None


class FrozenCallResourceManifest(A2ADurableModel):
    schema_version: Literal[1] = 1
    manifest_id: str
    refs: list[FrozenCallResourceRef] = Field(default_factory=list)
    content_digest: str

    @model_validator(mode="after")
    def _refs_are_unique(self) -> FrozenCallResourceManifest:
        ids = [ref.ref_id for ref in self.refs]
        if len(ids) != len(set(ids)):
            raise ValueError("resource refs must be unique")
        return self


class ImmutableA2ADispatchSnapshot(A2ADurableModel):
    schema_version: Literal[1] = 1
    command_id: str
    message_id: str
    task: str
    agent_id: str
    skill_id: str | None = None
    endpoint_scope: str
    transport_kind: Literal["direct", "relay"]
    direct_mode: Literal["sync", "stream", "poll"] | None = None
    requesting_subject_digest: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    deadline_at: datetime
    resource_manifest: FrozenCallResourceManifest


class A2AOwnershipAlias(A2ADurableModel):
    kind: Literal["task", "context", "relay_journal"]
    value: str
    binding_scope: str
    authoritative: bool = True


class AgentCallLedgerRecord(A2ADurableModel):
    schema_version: Literal[1] = 1
    state_version: int = Field(default=0, ge=0)
    call_record_id: str
    invocation_id: str
    acceptance_id: str
    idempotency_key: str
    run_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    assistant_message_id: str
    source_index: int = Field(ge=0)
    tool_name: str
    execution_mode: Literal["sequential", "parallel"] = "parallel"
    side_effect_level: Literal["read", "write", "external"] = "external"
    binding_id: str
    binding_digest: str
    agent_id: str
    skill_id: str | None = None
    card_digest: str
    endpoint_scope_digest: str
    arguments_digest: str
    requesting_subject_digest: str
    dispatch_snapshot: ImmutableA2ADispatchSnapshot
    resource_manifest: FrozenCallResourceManifest
    runtime_policy: A2ARuntimePolicy = Field(default_factory=A2ARuntimePolicy)
    output_schema_id: None = None
    output_schema_version: None = None
    output_schema_digest: None = None
    state: AgentCallState
    transport_kind: Literal["direct", "relay"]
    transport_attempts: int = Field(default=0, ge=0)
    inspection_attempts: int = Field(default=0, ge=0)
    dispatch_command_id: str
    a2a_task_id: str | None = None
    a2a_context_id: str | None = None
    ownership_aliases: list[A2AOwnershipAlias] = Field(default_factory=list)
    ownership_alias_keys: list[str] = Field(default_factory=list)
    pending_interaction_id: str | None = None
    interaction_revision: int | None = Field(default=None, ge=1)
    interaction_fingerprint: str | None = None
    # Bounded model-driven reply accounting, persisted on the parent call.
    model_reply_rounds: dict[str, int] = Field(default_factory=dict)
    model_reply_joins: list[A2AJoinBinding] = Field(default_factory=list)
    answer_applied: HITLAnswerAppliedMarker | None = None
    consumed_auth_references: list[VerifiedAuthReferenceBinding] = Field(
        default_factory=list
    )
    continuation_command: A2AContinuationCommand | None = None
    continuation_state: (
        Literal["pending", "dispatching", "delivery_uncertain", "accepted"] | None
    ) = None
    continuation_attempts: int = Field(default=0, ge=0)
    # Authorization refresh is distinct from remote delivery attempts. Keeping
    # its own durable counter prevents denied/transient refresh outcomes from
    # becoming an unbounded one-second recovery loop after an answer is saved.
    authorization_refresh_attempts: int = Field(default=0, ge=0)
    artifact_refs: list[str] = Field(default_factory=list)
    latest_observation_cursor: str | None = None
    recent_observation_ids: list[str] = Field(default_factory=list, max_length=128)
    terminal_result: ToolResult | None = None
    terminal_result_digest: str | None = None
    # SHA-256 of the push-notification token sent with the latest dispatch
    # attempt. The token itself is never stored; a call without a digest
    # (push disabled, or a record written before push registration existed)
    # rejects every push and is served by polling.
    push_token_digest: str | None = None
    cancellation_command: A2ACancellationCommand | None = None
    cancellation_command_id: str | None = None
    cancellation_reason: str | None = None
    cancellation_state: (
        Literal["pending", "dispatching", "delivery_uncertain", "accepted"] | None
    ) = None
    cancellation_attempts: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    accepted_at: datetime
    updated_at: datetime
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def _record_is_consistent(self) -> AgentCallLedgerRecord:  # noqa: C901
        if len(self.recent_observation_ids) != len(set(self.recent_observation_ids)):
            raise ValueError("recent observation IDs must be unique")
        expected_alias_keys = sorted(
            f"{alias.binding_scope}|{alias.kind}|{alias.value}"
            for alias in self.ownership_aliases
            if alias.authoritative
        )
        if sorted(self.ownership_alias_keys) != expected_alias_keys:
            raise ValueError("ownership alias keys do not match authoritative aliases")
        if self.state in TERMINAL_AGENT_CALL_STATES:
            if self.terminal_at is None or self.terminal_result is None:
                raise ValueError("terminal call requires terminal time and result")
        elif self.terminal_at is not None:
            raise ValueError("nonterminal call cannot have terminal time")
        if self.terminal_result is not None:
            if self.terminal_result.call_id != self.invocation_id:
                raise ValueError("terminal result call ID does not correlate")
            if self.terminal_result.tool_name != self.tool_name:
                raise ValueError("terminal result tool name does not correlate")
            digest = sha256(self.terminal_result.model_dump_json().encode()).hexdigest()
            if self.terminal_result_digest != digest:
                raise ValueError("terminal result digest does not correlate")
        if (
            self.dispatch_snapshot.conversation_id != self.conversation_id
            or self.dispatch_snapshot.room_epoch != self.room_epoch
            or self.dispatch_snapshot.agent_id != self.agent_id
            or self.dispatch_snapshot.requesting_subject_digest
            != self.requesting_subject_digest
            or self.dispatch_snapshot.resource_manifest != self.resource_manifest
        ):
            raise ValueError("immutable dispatch snapshot does not correlate")
        if any(
            alias.binding_scope != self.endpoint_scope_digest
            for alias in self.ownership_aliases
        ):
            raise ValueError("ownership alias scope does not correlate")
        reference_digests = [
            binding.reference_digest for binding in self.consumed_auth_references
        ]
        if len(reference_digests) != len(set(reference_digests)):
            raise ValueError(
                "authorization references cannot be reused across challenges"
            )
        if self.answer_applied is not None:
            marker = self.answer_applied
            if (
                marker.interaction_id != self.pending_interaction_id
                or marker.interaction_revision != self.interaction_revision
                or marker.answerer_digest != self.requesting_subject_digest
            ):
                raise ValueError("applied HITL answer does not correlate")
        if self.continuation_command is not None:
            command = self.continuation_command
            if (
                command.call_record_id != self.call_record_id
                or command.binding_id != self.binding_id
                or command.binding_digest != self.binding_digest
                or command.requesting_subject_digest != self.requesting_subject_digest
                or command.conversation_id != self.conversation_id
                or command.room_epoch != self.room_epoch
            ):
                raise ValueError("continuation command does not correlate")
            if self.continuation_state is None:
                raise ValueError("continuation command requires durable state")
        elif self.continuation_state is not None:
            raise ValueError("continuation state requires a command")
        if self.cancellation_command is not None:
            if (
                self.cancellation_command.call_record_id != self.call_record_id
                or self.cancellation_command.command_id != self.cancellation_command_id
            ):
                raise ValueError("cancellation command does not correlate")
            if self.cancellation_state is None:
                raise ValueError("cancellation command requires durable state")
        elif (
            self.cancellation_state is not None
            or self.cancellation_command_id is not None
        ):
            raise ValueError("cancellation state requires a command")
        return self

    @property
    def acceptance(self) -> ToolAcceptance:
        return ToolAcceptance(
            acceptance_id=self.acceptance_id,
            invocation_id=self.invocation_id,
            idempotency_key=self.idempotency_key,
            accepted_at=self.accepted_at,
        )


class MaterializedResourcePart(ContractModel):
    ref_id: str
    kind: Literal["text", "data", "file"]
    content_digest: str
    payload: str | dict[str, object] | list[object]
    mime_type: str | None = None
    metadata: dict[str, object] | None = None


class A2APushNotificationConfig(ContractModel):
    """Per-attempt push registration sent with a dispatch; never persisted."""

    url: str
    token: SecretStr


class A2ADispatchCommand(ContractModel):
    command_id: str
    call_record_id: str
    invocation_id: str
    message_id: str
    binding_id: str
    agent_id: str
    skill_id: str | None = None
    endpoint_scope: str
    transport_kind: Literal["direct", "relay"]
    direct_mode: Literal["sync", "stream", "poll"] | None = None
    task: str
    materialized_resources: list[MaterializedResourcePart]
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    deadline_at: datetime
    push_notification: A2APushNotificationConfig | None = None


class InlineDataArtifact(A2ADurableModel):
    """Addressable metadata for one DataPart retained in observation content."""

    ref_id: str
    artifact_id: str | None = None
    artifact_name: str | None = None
    artifact_description: str | None = None
    artifact_index: int = Field(ge=0)
    part_index: int = Field(ge=0)
    content_index: int = Field(ge=0)
    mime_type: str = "application/json"
    size_bytes: int = Field(gt=0)
    content_digest: str


class NormalizedA2AObservation(A2ADurableModel):
    observation_id: str
    call_record_id: str | None = None
    source_kind: Literal["direct", "webhook", "relay", "poll", "inspection"]
    source_identity: str
    binding_scope: str
    event_kind: Literal[
        "working", "artifact", "input_required", "auth_required", "terminal"
    ]
    observed_at: datetime
    task_id: str | None = None
    context_id: str | None = None
    agent_id: str | None = None
    status: Literal["completed", "failed", "canceled", "rejected", "expired"] | None = (
        None
    )
    content: list[ContentPart] = Field(default_factory=list)
    inline_artifacts: list[InlineDataArtifact] = Field(
        default_factory=list, max_length=20
    )
    artifact_refs: list[str] = Field(default_factory=list)
    interaction_spec: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None
    cursor: str | None = None

    @model_validator(mode="after")
    def _terminal_has_status(self) -> NormalizedA2AObservation:
        if self.event_kind == "terminal" and self.status is None:
            raise ValueError("terminal observation requires status")
        if self.event_kind != "terminal" and self.status is not None:
            raise ValueError("only terminal observation may carry status")
        refs = [artifact.ref_id for artifact in self.inline_artifacts]
        if len(refs) != len(set(refs)):
            raise ValueError("inline artifact refs must be unique")
        if any(ref not in self.artifact_refs for ref in refs):
            raise ValueError("inline artifact refs must be advertised")
        for artifact in self.inline_artifacts:
            if artifact.content_index >= len(self.content):
                raise ValueError("inline artifact content index is out of range")
            if not isinstance(self.content[artifact.content_index], DataPart):
                raise ValueError("inline artifact must address a DataPart")
        return self


class A2APushFrame(ContractModel):
    """One authenticated push notification, normalized for correlation.

    Only a terminal ``Task`` snapshot carries an observation: status and
    artifact updates are deltas that omit earlier output, so they are never
    recorded as call evidence.
    """

    frame: Literal["task", "status_update", "artifact_update"]
    task_id: str = Field(min_length=1)
    context_id: str | None = None
    event_kind: Literal[
        "working", "artifact", "input_required", "auth_required", "terminal"
    ]
    observation: NormalizedA2AObservation | None = None

    @model_validator(mode="after")
    def _only_terminal_task_snapshots_carry_evidence(self) -> A2APushFrame:
        if self.observation is not None and (
            self.frame != "task" or self.event_kind != "terminal"
        ):
            raise ValueError("only a terminal Task snapshot carries an observation")
        return self


class A2ADispatchReceipt(ContractModel):
    outcome: Literal[
        "accepted",
        "terminal",
        "delivery_uncertain",
        "rejected",
        "interaction",
    ]
    task_id: str | None = None
    context_id: str | None = None
    terminal_observation: NormalizedA2AObservation | None = None
    # input-required/auth-required answers: the Agent's request for input is
    # the durable result of the invocation and must reach the kernel as a tool
    # result (not be polled away as "still working").
    interaction_observation: NormalizedA2AObservation | None = None
    transport_journal_id: str | None = None

    @model_validator(mode="after")
    def _terminal_receipt_has_observation(self) -> A2ADispatchReceipt:
        if self.outcome == "terminal" and self.terminal_observation is None:
            raise ValueError("terminal receipt requires an observation")
        if self.outcome == "interaction" and self.interaction_observation is None:
            raise ValueError("interaction receipt requires an observation")
        return self


InboxState = Literal[
    "pending",
    "claimed",
    "ledger_applied",
    "outcome_pending",
    "session_applied",
    "completed",
    "quarantined",
]


class A2AObservationInboxRecord(A2ADurableModel):
    schema_version: Literal[1] = 1
    state_version: int = Field(default=0, ge=0)
    observation_id: str
    source_kind: Literal["direct", "webhook", "relay", "poll", "inspection"]
    source_identity: str
    payload_digest: str
    received_at: datetime
    binding_scope: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    call_record_id: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    agent_id: str | None = None
    event_kind: Literal[
        "working", "artifact", "input_required", "auth_required", "terminal"
    ]
    observation: NormalizedA2AObservation
    state: InboxState = "pending"
    delivery_route: Literal["unresolved", "executor", "observation_sink"] = "unresolved"
    delivery_state: Literal["pending", "checkpointed", "completed"] = "pending"
    outcome_digest: str | None = None
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    last_error: str | None = None


class A2AObservationConflictRecord(A2ADurableModel):
    schema_version: Literal[1] = 1
    conflict_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    source_identity: str
    accepted_observation_id: str
    accepted_payload_digest: str
    conflicting_payload_digest: str
    binding_scope: str
    received_at: datetime
    status: Literal["open", "resolved"] = "open"


class PreparedInvocationSnapshot(A2ADurableModel):
    schema_version: Literal[1] = 1
    run_id: str
    invocation_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    requesting_subject_id: str
    binding: AgentToolBindingRecord
    resource_manifest: FrozenCallResourceManifest


class VerifiedAuthReferenceBinding(A2ADurableModel):
    reference_digest: str
    proof_digest: str
    interaction_id: str
    interaction_revision: int = Field(ge=1)
    route_fingerprint: str
    interaction_fingerprint: str
    question_id: str
    challenge_digest: str
    answer_digest: str


class DurableHITLAnswerRecord(A2ADurableModel):
    interaction_id: str
    interaction_revision: int = Field(ge=1)
    route_fingerprint: str
    authenticated_answerer_id: str
    answer_digest: str
    answers: list[HITLQuestionAnswer]
    verified_auth_reference_digests: list[str] = Field(default_factory=list)
    verified_auth_references: list[VerifiedAuthReferenceBinding] = Field(
        default_factory=list
    )
    applied_at: datetime


class HITLAnswerAppliedMarker(A2ADurableModel):
    interaction_id: str
    interaction_revision: int = Field(ge=1)
    route_fingerprint: str
    answer_digest: str
    answerer_digest: str
    verified_auth_reference_digests: list[str] = Field(default_factory=list)
    verified_auth_references: list[VerifiedAuthReferenceBinding] = Field(
        default_factory=list
    )
    applied_at: datetime


class A2AContinuationCommand(A2ADurableModel):
    command_id: str
    transport_kind: Literal["direct", "relay"]
    call_record_id: str
    interaction_id: str
    interaction_revision: int = Field(ge=1)
    answer_digest: str
    answers: list[HITLQuestionAnswer]
    binding_id: str
    binding_digest: str
    requesting_subject_digest: str
    task_id: str
    context_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    created_at: datetime


class A2AModelReplyCommand(A2ADurableModel):
    """Model-driven reply to an already-parked agent interaction.

    Reuses the same remote task/context so the Agent keeps its session state.
    Distinct from ``A2AContinuationCommand``: a model reply is free text, not a
    typed answer inventory.
    """

    command_id: str
    transport_kind: Literal["direct", "relay"]
    call_record_id: str
    binding_id: str
    binding_digest: str
    requesting_subject_digest: str
    task_id: str
    context_id: str
    conversation_id: str = stored_conversation_id()
    room_epoch: int = Field(ge=1)
    message_text: str
    interaction_fingerprint: str | None = None
    source: Literal["model_reply"] = "model_reply"
    created_at: datetime


class A2AJoinBinding(A2ADurableModel):
    """Durable correlation between a model-reply invocation and its parent call.

    Persisted on the parent call ledger record; lets the runtime translate a
    parent-keyed observation into the join invocation's kernel ToolObservation.
    """

    join_invocation_id: str
    command_id: str
    interaction_fingerprint: str | None = None
    created_at: datetime


class A2ACancellationCommand(A2ADurableModel):
    command_id: str
    transport_kind: Literal["direct", "relay"]
    call_record_id: str
    reason: str
    deletion_id: str | None = None
    created_at: datetime


class RoomEpoch(A2ADurableModel):
    schema_version: Literal[1] = 1
    conversation_id: str = stored_conversation_id()
    epoch: int = Field(ge=1)
    high_water_mark: int = Field(ge=1)
    active: bool
    creation_id: str
    deletion_id: str | None = None
    updated_at: datetime


_NonBlankId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
_StrictVersion = Annotated[int, Field(strict=True, ge=1)]


class HITLRouteSnapshotV2(FrozenDTO):
    """Invocation-owned route added for the orchestrator runtime."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, serialize_by_alias=True
    )

    schema_version: Literal[2] = 2
    route: Literal["orchestrator_tool"] = "orchestrator_tool"
    orchestration_run_id: _NonBlankId
    call_record_id: _NonBlankId
    invocation_id: _NonBlankId
    conversation_id: _NonBlankId = stored_conversation_id()
    room_epoch: _StrictVersion
    binding_id: _NonBlankId
    agent_id: _NonBlankId
    task_id: _NonBlankId | None = None
    context_id: _NonBlankId | None = None
    interaction_revision: _StrictVersion
    interaction_fingerprint: _NonBlankId

    @model_validator(mode="after")
    def validate_authoritative_aliases(self) -> Self:
        for value in (self.task_id, self.context_id):
            if value is not None and _is_provisional_identifier(value):
                raise ValueError("V2 route aliases must be authoritative")
        return self

    def _fingerprint_payload(self) -> dict[str, object]:
        """Canonical fingerprint input with literal stored keys, None included."""

        return {
            "schema_version": self.schema_version,
            "route": self.route,
            "orchestration_run_id": self.orchestration_run_id,
            "call_record_id": self.call_record_id,
            "invocation_id": self.invocation_id,
            "room_id": self.conversation_id,
            "room_epoch": self.room_epoch,
            "binding_id": self.binding_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "interaction_revision": self.interaction_revision,
            "interaction_fingerprint": self.interaction_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        # Persisted as ``route_fingerprint`` and recomputed on answer replay, so
        # the hash input uses literal keys (including the stored "room_id")
        # independent of attribute names. None values are omitted.
        canonical = self._fingerprint_payload()
        canonical_json = json.dumps(
            {key: value for key, value in canonical.items() if value is not None},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical_json.encode()).hexdigest()


HITLRouteSnapshotUnion = Annotated[
    HITLRouteSnapshot | HITLRouteSnapshotV2,
    Field(discriminator="schema_version"),
]


def _is_provisional_identifier(value: str) -> bool:
    normalized = value.casefold()
    return normalized in {"pending", "provisional", "unknown"} or normalized.startswith(
        ("pending-", "relay-pending-", "provisional-")
    )
