from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from common.dto.base import FrozenDTO
from common.dto.hitl import HITLChoiceOption


class RunState(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class _StrictAgentScope(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MentionAgentScope(_StrictAgentScope):
    source: Literal["mention"]
    agent_ids: list[str] = Field(min_length=1)


class RoomDefaultAgentScope(_StrictAgentScope):
    source: Literal["room_default"]


class AllAgentsScope(_StrictAgentScope):
    source: Literal["all_agents"]


class SavedGroupAgentScope(_StrictAgentScope):
    source: Literal["saved_group"]
    group_id: str = Field(min_length=1)


AgentScopeInput = Annotated[
    MentionAgentScope | RoomDefaultAgentScope | AllAgentsScope | SavedGroupAgentScope,
    Field(discriminator="source"),
]


class ExecutionRequest(FrozenDTO):
    conversation_id: str
    sender_id: str
    sender_name: str | None = None
    message: dict[str, JsonValue] | None = None
    message_text: str | None = None
    attachments: list[dict[str, JsonValue]] | None = None
    inline_file_ids: list[str] | None = None
    parent_message_id: str | None = None
    client_request_id: str | None = None
    mode: Literal["direct", "supervisor", "passthrough"] = "direct"
    agent_scope: AgentScopeInput = Field(
        default_factory=lambda: RoomDefaultAgentScope(source="room_default")
    )


class ExecutionAck(FrozenDTO):
    conversation_id: str | None = None
    message_id: str | None = None
    dispatch_root_message_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    message: dict[str, JsonValue] | None = None
    message_list: list[dict[str, JsonValue]] | None = None
    scope_resolution_error: dict[str, JsonValue] | None = None
    preflight_outcome: Literal["ready", "completed", "failed", "canceled"] | None = None
    preflight_details: dict[str, JsonValue] | str | None = None
    success: bool = True
    error: str | None = None
    status_code: int = 200
    should_start_orchestration: bool = True


class CancellationAck(FrozenDTO):
    status: str
    cancellation_applied: bool
    reconciled: bool


class RunInfo(FrozenDTO):
    run_id: str
    conversation_id: str
    state: RunState | str
    trigger_message_id: str | None = None
    agent_id: str | None = None
    parent_run_id: str | None = None
    seq: int = 0
    error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None


class HITLRequest(FrozenDTO):
    request_id: str
    conversation_id: str
    user_message_id: str
    source: Literal["agent", "supervisor", "system"]
    prompt: str
    message_id: str | None = None
    source_step_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    a2a_task_id: str | None = None
    a2a_context_id: str | None = None
    continuation_message_id: str | None = None
    display_message_id: str | None = None
    client_request_id: str | None = None
    orchestration_run_id: str | None = None
    prompt_type: Literal[
        "text",
        "textarea",
        "choice",
        "single_choice",
        "multi_choice",
        "confirmation",
        "approval",
        "authentication",
        "date",
    ] = "text"
    choices: list[str] | None = None
    header: str | None = None
    required: bool = True
    allow_other: bool = False
    choice_options: list[HITLChoiceOption] | None = None
    interaction_id: str | None = None
    # The stored kind of an Agent's typed question; None for other questions.
    interaction_kind: (
        Literal["questionnaire", "auth_challenge", "policy_decision"] | None
    ) = None
    interaction_status: str | None = None
    interaction_version: int | None = None
    application_status: str | None = None
    application_error: str | None = None
    question_count: int = 1
    question_index: int = 0
    status: Literal[
        "pending",
        "answer_recorded",
        "responded",
        "resolved",
        "expired",
        "canceled",
    ] = "pending"
    expires_at: datetime | None = None
    created_at: datetime | None = None
    user_input: str | None = None
    responded_at: datetime | None = None
    responded_by_user_id: str | None = None

    @model_validator(mode="after")
    def populate_message_id(self) -> "HITLRequest":
        if self.message_id is None:
            object.__setattr__(
                self,
                "message_id",
                self.display_message_id
                or self.continuation_message_id
                or self.user_message_id,
            )
        return self


class HITLResponse(FrozenDTO):
    request_id: str
    status: str = "ok"
    response_text: str | None = None
    responder_id: str | None = None
    resolved_at: datetime | None = None
    reclaimed: bool | None = None
    error: str | None = None
    client_request_id: str | None = None


__all__ = [
    "AgentScopeInput",
    "AllAgentsScope",
    "ExecutionAck",
    "ExecutionRequest",
    "HITLRequest",
    "MentionAgentScope",
    "HITLResponse",
    "RoomDefaultAgentScope",
    "RunInfo",
    "RunState",
    "SavedGroupAgentScope",
]
