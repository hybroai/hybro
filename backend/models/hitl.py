"""Human-in-the-Loop (HITL) data models.

Defines the models for HITL request/response lifecycle and event types.

See docs/HITL_DESIGN.md for full design details.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from common.dto.base import stored_conversation_id
from common.dto.hitl import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
    HITLRouteSnapshot,
)
from common.utils.time import utcnow

# ---------------------------------------------------------------------------
# HITL lifecycle enums
# ---------------------------------------------------------------------------


class HITLEventType(str, Enum):
    """Events in the human-in-the-loop lifecycle."""

    INPUT_REQUESTED = "hitl_request"
    INPUT_RECEIVED = "hitl_input_received"
    INPUT_EXPIRED = "hitl_input_expired"
    INPUT_CANCELED = "hitl_input_canceled"
    ERROR = "hitl_error"


class HITLPromptType(str, Enum):
    """Type of control the client should render for a HITL question."""

    TEXT = "text"
    TEXTAREA = "textarea"
    CHOICE = "choice"
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    CONFIRMATION = "confirmation"
    APPROVAL = "approval"
    AUTHENTICATION = "authentication"
    DATE = "date"


class HITLStatus(str, Enum):
    """Lifecycle status of a single HITL request."""

    PENDING = "pending"
    ANSWER_RECORDED = "answer_recorded"
    RESPONDED = "responded"
    EXPIRED = "expired"
    CANCELED = "canceled"


class HITLInteractionStatus(str, Enum):
    """Durable lifecycle of one user-visible interaction/questionnaire."""

    MATERIALIZING = "materializing"
    OPEN = "open"
    PARTIALLY_ANSWERED = "partially_answered"
    ANSWERS_RECORDED = "answers_recorded"
    APPLYING = "applying"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    APPLIED = "applied"
    CANCELED = "canceled"
    EXPIRED = "expired"
    FAILED = "failed"


class HITLResumeCommandStatus(str, Enum):
    """State of the durable remote A2A continuation command."""

    PENDING = "pending"
    DELIVERING = "delivering"
    ACKNOWLEDGED = "acknowledged"
    PROJECTED = "projected"
    RETRYABLE_ERROR = "retryable_error"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    PERMANENT_FAILURE = "permanent_failure"


# ---------------------------------------------------------------------------
# HITLRequest — persisted to MongoDB
# ---------------------------------------------------------------------------


class HITLCreationQuestion(BaseModel):
    """Immutable inventory entry used to resume aggregate materialization."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    prompt: str
    prompt_type: HITLPromptType = HITLPromptType.TEXT
    choices: list[str] | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    source_step_id: str | None = None
    continuation_message_id: str | None = None
    display_message_id: str | None = None


_HITLRequestId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
_HITLUserInput = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class HITLRequest(BaseModel):
    """One immutable ordered member of a persisted HITL interaction."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    schema_version: Literal[3]
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    interaction_id: str
    question_index: int = Field(ge=0, le=99)
    question_count: int = Field(ge=1, le=100)
    conversation_id: str = stored_conversation_id()
    user_message_id: str

    # Independent classifications. Routing is owned only by the interaction snapshot.
    application_route: HITLApplicationRoute
    public_source: HITLPublicSource
    evidence_origin: HITLEvidenceOrigin
    source_step_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None

    # A2A continuation context (for agent-sourced requests)
    a2a_task_id: str | None = None
    a2a_context_id: str | None = None
    continuation_message_id: str | None = None

    # Frontend display: when set, the SSE event uses this as `message_id`
    # instead of continuation_message_id.  This prevents supervisor CLARIFY
    # HITL prompts from overwriting the user's own chat message entity.
    display_message_id: str | None = None
    client_request_id: str | None = None

    # Durable orchestration linkage.
    orchestration_run_id: str | None = None

    # The question
    prompt: str
    # Server-side fingerprint of the unsanitized agent prompt. It is persisted
    # for no-progress detection but is never included in public HITL DTOs.
    agent_prompt_hash: str | None = None
    prompt_type: HITLPromptType = HITLPromptType.TEXT
    choices: list[str] | None = None

    # Public lifecycle projection metadata (not routing authority).
    interaction_status: HITLInteractionStatus | None = None
    interaction_version: int | None = Field(default=None, ge=1)
    application_status: str | None = None
    application_error: str | None = None

    # Lifecycle
    status: HITLStatus = HITLStatus.PENDING
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    # Response and recoverable projection state.
    user_input: str | None = None
    answer_digest: str | None = None
    answered_at: datetime | None = None
    responded_at: datetime | None = None
    responded_by_user_id: str | None = None
    # The client that answered as the owner; None when the owner answered.
    answered_by_client_id: str | None = None
    claim_id: str | None = None
    routing_completed_at: datetime | None = None
    application_projected: bool = False
    cancellation_reconciled: bool = False
    open_projection_claim_id: str | None = None
    open_projection_claimed_at: datetime | None = None
    open_projection_completed_at: datetime | None = None

    # Durable owning-run terminal reconciliation for cancel/expiry failures.
    owning_run_terminal_status: Literal["canceled", "failed"] | None = None
    owning_run_terminal_reason: str | None = None


class HITLInteraction(BaseModel):
    """Durable aggregate that owns answer collection and application."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    schema_version: Literal[3]
    interaction_id: str
    conversation_id: str = stored_conversation_id()
    user_message_id: str
    orchestration_run_id: str | None = None
    application_route: HITLApplicationRoute
    public_source: HITLPublicSource
    evidence_origin: HITLEvidenceOrigin
    route_snapshot: HITLRouteSnapshot
    route_fingerprint: str
    creation_inventory: list[HITLCreationQuestion] = Field(min_length=1, max_length=100)
    request_ids: list[str] = Field(default_factory=list)
    request_order: list[dict[str, Any]] = Field(default_factory=list)
    expected_request_count: int = Field(ge=1, le=100)
    required_request_ids: list[str] = Field(default_factory=list)
    status: HITLInteractionStatus = HITLInteractionStatus.MATERIALIZING
    version: int = Field(default=1, ge=1)
    expires_at: datetime | None = None
    answer_request_ids: list[str] = Field(default_factory=list)
    answer_refs: list[dict[str, str]] = Field(default_factory=list)
    answer_digest: str | None = None
    application_revision: int = Field(default=0, ge=0)
    application_claim_id: str | None = None
    application_lease_expires_at: datetime | None = None
    application_attempts: int = Field(default=0, ge=0)
    application_error: str | None = None
    application_started_at: datetime | None = None
    run_projection_status: Literal["pending", "applying", "applied", "failed"] = (
        "pending"
    )
    run_projection_claim_id: str | None = None
    run_projection_lease_expires_at: datetime | None = None
    run_projection_error: str | None = None
    applied_at: datetime | None = None
    terminal_reason: str | None = None
    member_terminal_status: Literal["canceled", "expired"] | None = None
    owning_run_terminal_status: Literal["canceled", "failed"] | None = None
    terminal_reconciled: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_state(self) -> HITLInteraction:  # noqa: C901
        if self.application_route != self.route_snapshot.route:
            raise ValueError("application_route must match route_snapshot.route")
        if self.route_fingerprint != self.route_snapshot.fingerprint:
            raise ValueError("route_fingerprint does not match route_snapshot")
        if self.orchestration_run_id != self.route_snapshot.orchestration_run_id:
            raise ValueError(
                "supervisor orchestration_run_id must match route_snapshot"
            )
        inventory_ids = [item.request_id for item in self.creation_inventory]
        if len(set(inventory_ids)) != len(inventory_ids):
            raise ValueError("creation_inventory request_ids must be unique")
        if len(inventory_ids) != self.expected_request_count:
            raise ValueError("creation_inventory must be complete")
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("request_ids must be unique")
        if len(self.request_ids) > self.expected_request_count:
            raise ValueError("request count exceeds expected_request_count")
        if not set(self.required_request_ids).issubset(self.request_ids):
            raise ValueError("required_request_ids must be attached")
        if not set(self.answer_request_ids).issubset(self.request_ids):
            raise ValueError("answer_request_ids must be attached")
        complete_states = {
            HITLInteractionStatus.ANSWERS_RECORDED,
            HITLInteractionStatus.APPLYING,
            HITLInteractionStatus.DELIVERY_UNCERTAIN,
            HITLInteractionStatus.APPLIED,
        }
        if self.status in complete_states and not set(
            self.required_request_ids
        ).issubset(self.answer_request_ids):
            raise ValueError("complete interaction is missing required answers")
        if self.status == HITLInteractionStatus.APPLIED and self.applied_at is None:
            raise ValueError("applied interaction requires applied_at")
        if (
            self.status == HITLInteractionStatus.APPLYING
            and not self.application_claim_id
        ):
            raise ValueError("applying interaction requires an application claim")
        return self


class HITLSupervisorEffectCommand(BaseModel):
    """Durable journal for the local supervisor continuation effect."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    command_id: str
    kind: Literal["supervisor_resume"] = "supervisor_resume"
    interaction_id: str
    application_revision: int = Field(ge=1)
    orchestration_run_id: str
    answer_request_ids: list[str] = Field(min_length=1)
    answer_digest: str
    status: HITLResumeCommandStatus = HITLResumeCommandStatus.PENDING
    version: int = Field(default=1, ge=1)
    claim_id: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    response_snapshot: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    uncertain_since: datetime | None = None
    aggregate_applied_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_effect(self) -> HITLSupervisorEffectCommand:
        if not self.orchestration_run_id.strip():
            raise ValueError("orchestration_run_id must not be blank")
        if self.status == HITLResumeCommandStatus.DELIVERING and not self.claim_id:
            raise ValueError("delivering command requires a claim")
        return self


class HITLBatchAnswer(BaseModel):
    """One answer in an atomic questionnaire submission."""

    request_id: _HITLRequestId
    user_input: _HITLUserInput


class HITLBatchResponseRequest(BaseModel):
    """Submit every required answer for one durable HITL interaction."""

    interaction_id: str = Field(..., min_length=1, max_length=128)
    answers: list[HITLBatchAnswer] = Field(..., min_length=1, max_length=100)
    client_request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_unique_answers(self) -> HITLBatchResponseRequest:
        request_ids = [answer.request_id for answer in self.answers]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("answers must contain unique request_ids")
        return self
