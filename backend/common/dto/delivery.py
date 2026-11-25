from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from common.dto.base import FrozenDTO
from common.dto.hitl import HITLChoiceOption
from common.dto.turn_lifecycle import (
    CANONICAL_RUN_EVENT_KINDS,
    CanonicalRunEventPayload,
    RunStartedPayload,
    validate_canonical_payload,
)


class DeliveryEmitStatus(StrEnum):
    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"


class DeliveryEventBase(FrozenDTO):
    conversation_id: str
    timestamp: datetime | None = None
    trace_id: str | None = None


class ProcessingStatusEvent(DeliveryEventBase):
    event_type: Literal["processing_status"] = "processing_status"
    message_id: str
    status: Literal[
        "queued",
        "processing",
        "awaiting_input",
        "completed",
        "failed",
        "canceled",
        "rejected",
        "rate_limited",
        "error",
    ]
    agent_id: str | None = None
    details: dict | None = None
    related_message_id: str | None = None
    client_request_id: str | None = None
    agents: list[dict] | None = None
    delivery_id: str | None = None


class RunEventNotification(DeliveryEventBase):
    """Canonical run event with strict payload validation.

    Only canonical run event kinds cross this boundary, each as its matching
    closed DTO, and every event requires complete Turn-root correlation.
    """

    event_type: Literal["run_event"] = "run_event"
    event_id: str = Field(min_length=1)
    delivery_id: str | None = None
    run_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    run_event_type: str = Field(min_length=1)
    payload: CanonicalRunEventPayload
    correlation_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_canonical_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        kind = value.get("run_event_type")
        if kind not in CANONICAL_RUN_EVENT_KINDS:
            raise ValueError(f"unknown canonical run event type: {kind}")
        copied = dict(value)
        copied["payload"] = validate_canonical_payload(
            str(kind), copied.get("payload") or {}
        )
        return copied

    @model_validator(mode="after")
    def _validate_canonical_root(self) -> RunEventNotification:
        if not self.correlation_id:
            raise ValueError("canonical run events require correlation_id")
        if isinstance(self.payload, RunStartedPayload):
            if self.payload.hybro_turn_id != self.run_id:
                raise ValueError("hybro_turn_id must equal run_id")
        return self


class AgentMessagePartial(DeliveryEventBase):
    event_type: Literal["agent_message_partial"] = "agent_message_partial"
    message_id: str
    agent_id: str
    content_delta: str


class AgentMessageFinal(DeliveryEventBase):
    event_type: Literal["agent_message_final"] = "agent_message_final"
    message_id: str
    agent_id: str
    content: dict = Field(default_factory=dict)
    delivery_id: str | None = None


class TaskSubmittedEvent(DeliveryEventBase):
    event_type: Literal["task_submitted"] = "task_submitted"
    run_id: str | None = None
    opaque_public_call_id: str | None = None
    message_id: str
    task_id: str
    agent_name: str
    agent_id: str | None = None
    status: str = "working"
    related_message_id: str | None = None
    created_at: str | None = None
    step_number: int | None = None
    total_steps: int | None = None
    task_content: str | None = None
    client_request_id: str | None = None

    @model_validator(mode="after")
    def _canonical_card_is_closed(self) -> TaskSubmittedEvent:
        if self.run_id is None:
            return self
        public_id = self.opaque_public_call_id or ""
        if (
            not public_id.startswith("inv_")
            or self.message_id != f"orchestrator:{self.run_id}:{public_id}"
        ):
            raise ValueError("canonical task card identity is invalid")
        if not self.client_request_id or not self.related_message_id:
            raise ValueError("canonical task cards require exact Turn roots")
        if any(
            value is not None
            for value in (
                self.agent_id,
                self.task_content,
                self.step_number,
                self.total_steps,
            )
        ):
            raise ValueError("canonical task cards forbid private/legacy content")
        return self


class TaskUpdateEvent(DeliveryEventBase):
    event_type: Literal["task_update"] = "task_update"
    run_id: str | None = None
    opaque_public_call_id: str | None = None
    message_id: str
    status: str
    delivery_id: str | None = None
    content: str | None = None
    error: str | None = None
    requires_input: bool = False
    requires_auth: bool = False
    status_message: str | None = None
    agent_name: str | None = None
    agent_id: str | None = None
    related_message_id: str | None = None
    created_at: str | None = None
    step_number: int | None = None
    total_steps: int | None = None
    task_content: str | None = None
    parts: list[dict] | None = None
    client_request_id: str | None = None

    @model_validator(mode="after")
    def _canonical_card_is_closed(self) -> TaskUpdateEvent:
        if self.run_id is None:
            return self
        public_id = self.opaque_public_call_id or ""
        if (
            not public_id.startswith("inv_")
            or self.message_id != f"orchestrator:{self.run_id}:{public_id}"
        ):
            raise ValueError("canonical task card identity is invalid")
        if not self.client_request_id or not self.related_message_id:
            raise ValueError("canonical task cards require exact Turn roots")
        if any(
            value is not None
            for value in (
                self.content,
                self.error,
                self.parts,
                self.agent_id,
                self.task_content,
                self.status_message,
                self.step_number,
                self.total_steps,
            )
        ):
            raise ValueError("canonical task cards forbid raw/private content")
        return self


class ArtifactUpdateEvent(DeliveryEventBase):
    event_type: Literal["artifact_update"] = "artifact_update"
    message_id: str
    agent_id: str
    artifact: Any
    append: bool = False
    last_chunk: bool = False
    client_request_id: str | None = None


class ErrorEvent(DeliveryEventBase):
    event_type: Literal["error"] = "error"
    error: str
    error_type: str | None = None
    message_id: str | None = None
    agent_id: str | None = None
    retry_after_seconds: int | None = None
    user_requests_used: int | None = None
    user_requests_limit: int | None = None
    system_requests_used: int | None = None
    system_requests_limit: int | None = None
    client_request_id: str | None = None


class CancellationEvent(DeliveryEventBase):
    event_type: Literal["cancellation"] = "cancellation"
    message_id: str
    reason: str | None = None


class HITLRequestEvent(DeliveryEventBase):
    event_type: Literal["hitl_request"] = "hitl_request"
    run_id: str | None = None
    request_id: str
    message_id: str
    source: str
    prompt: str = Field(max_length=4_000)
    prompt_type: str
    choices: list[str] | None = None
    header: str | None = None
    required: bool = True
    allow_other: bool = False
    choice_options: list[HITLChoiceOption] | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    agent_label: str | None = None
    source_step_id: str | None = None
    interaction_id: str | None = None
    interaction_status: str | None = None
    interaction_version: int | None = None
    application_status: str | None = None
    question_count: int = 1
    question_index: int = 0
    related_message_id: str | None = None
    related_user_message_id: str | None = None
    client_request_id: str | None = None

    @model_validator(mode="after")
    def _canonical_root_is_closed(self) -> HITLRequestEvent:
        if self.run_id is None:
            return self
        if not self.client_request_id or not self.related_user_message_id:
            raise ValueError("canonical HITL requests require exact Turn roots")
        if any(
            value is not None
            for value in (
                self.agent_id,
                self.agent_name,
                self.source_step_id,
                self.interaction_status,
                self.interaction_version,
                self.application_status,
                self.related_message_id,
            )
        ):
            raise ValueError("canonical HITL requests forbid legacy/private metadata")
        if self.agent_label is not None and len(self.agent_label) > 160:
            raise ValueError("canonical HITL agent_label is too long")
        if self.choices is not None and (
            len(self.choices) > 20 or any(len(choice) > 500 for choice in self.choices)
        ):
            raise ValueError("canonical HITL choices exceed public bounds")
        if self.source not in {"agent", "supervisor", "system"}:
            raise ValueError("canonical HITL source is not allowlisted")
        if self.prompt_type not in {
            "text",
            "textarea",
            "choice",
            "single_choice",
            "multi_choice",
            "confirmation",
            "approval",
            "authentication",
            "date",
        }:
            raise ValueError("canonical HITL prompt type is not allowlisted")
        return self


class HITLResolvedEvent(DeliveryEventBase):
    event_type: Literal["hitl_resolved"] = "hitl_resolved"
    run_id: str | None = None
    request_id: str
    message_id: str
    source: str
    status: str = "resolved"
    interaction_id: str | None = None
    interaction_status: str | None = None
    interaction_version: int | None = None
    application_status: str | None = None
    question_count: int = 1
    question_index: int = 0
    error_message: str | None = None
    answer_ref: str | None = None
    # The owner's answer as they would read it; only on responded questions.
    answer_text: str | None = Field(default=None, max_length=1_000)
    # Who answered, on responded questions only: the owner, or a client with
    # the owner's grant, named by ``answered_by_client_id``.
    answered_by: Literal["owner", "client"] | None = None
    answered_by_client_id: str | None = None
    related_message_id: str | None = None
    related_user_message_id: str | None = None
    client_request_id: str | None = None

    @model_validator(mode="after")
    def _canonical_root_is_closed(self) -> HITLResolvedEvent:
        if self.run_id is None:
            return self
        if not self.client_request_id or not self.related_user_message_id:
            raise ValueError("canonical HITL responses require exact Turn roots")
        if self.status not in {"responded", "expired", "canceled", "error"}:
            raise ValueError("canonical HITL response status is not allowlisted")
        if self.source not in {"agent", "supervisor", "system"}:
            raise ValueError("canonical HITL response source is not allowlisted")
        if any(
            value is not None
            for value in (
                self.error_message,
                self.interaction_status,
                self.interaction_version,
                self.application_status,
                self.related_message_id,
            )
        ):
            raise ValueError("canonical HITL responses forbid legacy/private metadata")
        return self


DeliveryEvent = Annotated[
    ProcessingStatusEvent
    | RunEventNotification
    | AgentMessagePartial
    | AgentMessageFinal
    | TaskSubmittedEvent
    | TaskUpdateEvent
    | ArtifactUpdateEvent
    | ErrorEvent
    | CancellationEvent
    | HITLRequestEvent
    | HITLResolvedEvent,
    Field(discriminator="event_type"),
]


__all__ = [
    "AgentMessageFinal",
    "AgentMessagePartial",
    "ArtifactUpdateEvent",
    "CancellationEvent",
    "DeliveryEvent",
    "DeliveryEventBase",
    "ErrorEvent",
    "HITLRequestEvent",
    "HITLResolvedEvent",
    "ProcessingStatusEvent",
    "RunEventNotification",
    "TaskSubmittedEvent",
    "TaskUpdateEvent",
]
