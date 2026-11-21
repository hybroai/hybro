"""Strict public DTOs for the Hybro Turn lifecycle."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PublicId = Annotated[str, Field(min_length=1, max_length=256)]
PublicText = Annotated[str, Field(max_length=32_000)]
PublicSummary = Annotated[str, Field(max_length=1_000)]
PublicTimestamp = datetime


class CanonicalEventPayloadDTO(BaseModel):
    """Closed immutable boundary for content persisted to ``room_events``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStartedPayload(CanonicalEventPayloadDTO):
    hybro_turn_id: PublicId
    user_message_id: PublicId
    started_at: PublicTimestamp
    mode: Literal["fast", "direct", "ultimate", "supervisor", "passthrough"]


class TurnStartPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    attempt: int = Field(ge=1)


class MessageStartPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    message_id: PublicId
    role: Literal["assistant"] = "assistant"


class TextDeltaEvent(CanonicalEventPayloadDTO):
    type: Literal["text_delta"] = "text_delta"
    content_index: int = Field(ge=0)
    delta_index: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    delta: PublicText

    @model_validator(mode="after")
    def _offsets_match_delta(self) -> TextDeltaEvent:
        if self.end_offset != self.start_offset + len(self.delta):
            raise ValueError("text delta offsets must count Unicode code points")
        return self


class MessageUpdatePayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    message_id: PublicId
    assistant_message_event: TextDeltaEvent


class MessageEndPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    message_id: PublicId
    stop_reason: Literal[
        "tool_use",
        "stop",
        "length",
        "content_filter",
        "error",
        "deferred",
        "aborted",
    ]
    disposition: Literal["commentary", "final", "error", "aborted"]
    text: PublicText
    error_summary: PublicSummary | None = None

    @model_validator(mode="after")
    def _closed_terminal_shape(self) -> MessageEndPayload:
        allowed = {
            "commentary": {"tool_use"},
            "final": {"stop"},
            "error": {"length", "content_filter", "error", "deferred"},
            "aborted": {"aborted"},
        }
        if self.stop_reason not in allowed[self.disposition]:
            raise ValueError("invalid message disposition/stop_reason combination")
        if self.disposition == "error" and not self.error_summary:
            raise ValueError("error message_end requires error_summary")
        if self.disposition != "error" and self.error_summary is not None:
            raise ValueError("error_summary is valid only for error disposition")
        return self


SafeSummary = dict[str, str | int | float | bool | None]
PublicLabel = Annotated[str, Field(min_length=1, max_length=160)]


class ExecutionTargetPayload(CanonicalEventPayloadDTO):
    """Safe public identity of an Agent Execution.

    ``name`` is the base Agent Card name (never the skill-qualified trace
    label). Registry ids, provider call ids, and endpoint scopes stay private;
    cards correlate through the opaque public call id instead.
    """

    name: PublicLabel
    source: Literal["cloud", "local", "hub"] | None = None


def _validate_execution_shape(
    payload: ToolExecutionStartPayload
    | ToolExecutionUpdatePayload
    | ToolExecutionEndPayload,
) -> None:
    if payload.execution_kind == "agent":
        if payload.target is None:
            raise ValueError("agent execution requires a target")
    elif payload.target is not None:
        raise ValueError("tool execution must not carry a target")


class ToolExecutionStartPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    tool_call_id: Annotated[str, Field(pattern=r"^inv_[A-Za-z0-9_-]{8,128}$")]
    tool_name: Annotated[str, Field(min_length=1, max_length=160)]
    input: SafeSummary = Field(default_factory=dict)
    execution_kind: Literal["agent", "tool"] = "tool"
    target: ExecutionTargetPayload | None = None
    request_summary: PublicSummary = ""

    @model_validator(mode="after")
    def _closed_execution_shape(self) -> ToolExecutionStartPayload:
        _validate_execution_shape(self)
        return self


class ToolExecutionUpdatePayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    tool_call_id: Annotated[str, Field(pattern=r"^inv_[A-Za-z0-9_-]{8,128}$")]
    tool_name: Annotated[str, Field(min_length=1, max_length=160)]
    update_index: int = Field(ge=1)
    status: Literal["running", "suspended"]
    partial_result: PublicSummary = ""
    execution_kind: Literal["agent", "tool"] = "tool"
    target: ExecutionTargetPayload | None = None

    @model_validator(mode="after")
    def _closed_execution_shape(self) -> ToolExecutionUpdatePayload:
        _validate_execution_shape(self)
        return self


class ToolExecutionEndPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    tool_call_id: Annotated[str, Field(pattern=r"^inv_[A-Za-z0-9_-]{8,128}$")]
    tool_name: Annotated[str, Field(min_length=1, max_length=160)]
    outcome: Literal["completed", "failed", "canceled"]
    result: PublicSummary
    is_error: bool
    duration_ms: int = Field(ge=0)
    failure_reason: (
        Literal["rejected", "expired", "validation", "authorization", "execution"]
        | None
    ) = None
    execution_kind: Literal["agent", "tool"] = "tool"
    target: ExecutionTargetPayload | None = None
    detail_available: bool = False
    # The failing Agent's own reason, from the A2A error metadata.
    error_code: (
        Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")] | None
    ) = None
    error_message: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _closed_outcome_shape(self) -> ToolExecutionEndPayload:
        if self.outcome == "completed":
            valid = not self.is_error and self.failure_reason is None
        elif self.outcome == "failed":
            valid = self.is_error
        else:
            valid = (
                not self.is_error and not self.result and self.failure_reason is None
            )
        if not valid:
            raise ValueError("invalid tool outcome fields")
        if self.detail_available and self.outcome != "completed":
            raise ValueError("detail_available is valid only for completed outcome")
        if self.outcome != "failed" and (
            self.error_code is not None or self.error_message is not None
        ):
            raise ValueError("error details are valid only for failed outcome")
        _validate_execution_shape(self)
        return self


class TurnEndPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    message_id: PublicId | None = None
    tool_call_ids: list[Annotated[str, Field(pattern=r"^inv_[A-Za-z0-9_-]{8,128}$")]]
    status: Literal["completed", "error", "aborted"]

    @model_validator(mode="after")
    def _tool_ids_are_unique(self) -> TurnEndPayload:
        if len(self.tool_call_ids) != len(set(self.tool_call_ids)):
            raise ValueError("turn_end tool_call_ids must be unique")
        if self.status == "completed" and self.message_id is None:
            raise ValueError("completed turn requires message_id")
        return self


class RetryScheduledPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    attempt: int = Field(ge=2)
    delay_ms: int = Field(ge=0)
    error_class: Literal[
        "provider_timeout",
        "provider_error",
        "content_filter",
        "assembly_error",
        "tool_failure",
        "process_restart",
    ]


class ModelDecisionPayload(CanonicalEventPayloadDTO):
    internal_turn_id: PublicId
    decision: Literal[
        "interaction_received",
        "answered_from_context",
        "forwarded_to_user",
        "no_progress",
        "degraded_to_user",
    ]
    agent_label: PublicLabel | None = None
    question_summary: PublicSummary | None = None
    source_summary: PublicSummary | None = None
    reason: PublicSummary | None = None

    @model_validator(mode="after")
    def _decision_context_fields(self) -> ModelDecisionPayload:
        if self.decision in {"answered_from_context", "forwarded_to_user"} and (
            self.agent_label is None
        ):
            raise ValueError(f"{self.decision} requires agent_label")
        if self.decision in {"no_progress", "degraded_to_user"} and not self.reason:
            raise ValueError("terminal decision requires reason")
        return self


class RunWaitingInputPayload(CanonicalEventPayloadDTO):
    interaction_id: PublicId
    request_ids: list[PublicId] = Field(min_length=1)
    requested_at: PublicTimestamp

    @model_validator(mode="after")
    def _request_ids_are_unique(self) -> RunWaitingInputPayload:
        if len(self.request_ids) != len(set(self.request_ids)):
            raise ValueError("request_ids must be unique")
        return self


class RunResumedPayload(CanonicalEventPayloadDTO):
    interaction_id: PublicId
    resolved_request_ids: list[PublicId] = Field(min_length=1)
    resumed_at: PublicTimestamp

    @model_validator(mode="after")
    def _request_ids_are_unique(self) -> RunResumedPayload:
        if len(self.resolved_request_ids) != len(set(self.resolved_request_ids)):
            raise ValueError("resolved_request_ids must be unique")
        return self


class RunSettledPayload(CanonicalEventPayloadDTO):
    status: Literal["completed", "failed", "canceled"]
    started_at: PublicTimestamp
    settled_at: PublicTimestamp
    duration_ms: int = Field(ge=0)
    final_message_id: PublicId | None = None
    failure_code: (
        Literal[
            "budget_exhausted",
            "provider_error",
            "assembly_error",
            "tool_failure",
            "hitl_error",
            "rejected",
            "internal_error",
        ]
        | None
    ) = None
    error_summary: PublicSummary | None = None
    cancellation_code: (
        Literal["user_requested", "room_closed", "shutdown", "policy", "owner_refused"]
        | None
    ) = None
    # The check rule that denied the call; only on a ``policy`` cancel.
    rule_id: PublicId | None = None

    @model_validator(mode="after")
    def _closed_settlement_shape(self) -> RunSettledPayload:
        if self.settled_at < self.started_at:
            raise ValueError("settled_at must not precede started_at")
        if self.status == "completed":
            valid = (
                self.final_message_id is not None
                and self.failure_code is None
                and self.error_summary is None
                and self.cancellation_code is None
            )
        elif self.status == "failed":
            valid = (
                self.final_message_id is None
                and self.failure_code is not None
                and bool(self.error_summary)
                and self.cancellation_code is None
            )
        else:
            valid = (
                self.final_message_id is None
                and self.failure_code is None
                and self.error_summary is None
                and self.cancellation_code is not None
            )
        if self.rule_id is not None and self.cancellation_code != "policy":
            valid = False
        if not valid:
            raise ValueError("invalid run_settled conditional fields")
        return self


CanonicalRunEventPayload = (
    RunStartedPayload
    | TurnStartPayload
    | MessageStartPayload
    | MessageUpdatePayload
    | MessageEndPayload
    | ToolExecutionStartPayload
    | ToolExecutionUpdatePayload
    | ToolExecutionEndPayload
    | TurnEndPayload
    | RetryScheduledPayload
    | ModelDecisionPayload
    | RunWaitingInputPayload
    | RunResumedPayload
    | RunSettledPayload
)

CANONICAL_RUN_EVENT_PAYLOADS: dict[str, type[CanonicalEventPayloadDTO]] = {
    "run_started": RunStartedPayload,
    "turn_start": TurnStartPayload,
    "message_start": MessageStartPayload,
    "message_update": MessageUpdatePayload,
    "message_end": MessageEndPayload,
    "tool_execution_start": ToolExecutionStartPayload,
    "tool_execution_update": ToolExecutionUpdatePayload,
    "tool_execution_end": ToolExecutionEndPayload,
    "turn_end": TurnEndPayload,
    "retry_scheduled": RetryScheduledPayload,
    "model_decision": ModelDecisionPayload,
    "run_waiting_input": RunWaitingInputPayload,
    "run_resumed": RunResumedPayload,
    "run_settled": RunSettledPayload,
}
CANONICAL_RUN_EVENT_KINDS = frozenset(CANONICAL_RUN_EVENT_PAYLOADS)


def validate_canonical_payload(kind: str, payload: object) -> CanonicalRunEventPayload:
    """Validate a payload against its outer canonical subtype discriminator."""

    model = CANONICAL_RUN_EVENT_PAYLOADS.get(kind)
    if model is None:
        raise ValueError(f"unknown canonical run event type: {kind}")
    return model.model_validate(payload)  # type: ignore[return-value]


__all__ = [
    "CANONICAL_RUN_EVENT_KINDS",
    "CANONICAL_RUN_EVENT_PAYLOADS",
    "CanonicalRunEventPayload",
    "ExecutionTargetPayload",
    "MessageEndPayload",
    "MessageStartPayload",
    "MessageUpdatePayload",
    "ModelDecisionPayload",
    "CanonicalEventPayloadDTO",
    "RetryScheduledPayload",
    "RunResumedPayload",
    "RunSettledPayload",
    "RunStartedPayload",
    "RunWaitingInputPayload",
    "TextDeltaEvent",
    "ToolExecutionEndPayload",
    "ToolExecutionStartPayload",
    "ToolExecutionUpdatePayload",
    "TurnEndPayload",
    "TurnStartPayload",
    "validate_canonical_payload",
]
