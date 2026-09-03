from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from common.a2a_constants import CommonTaskState
from common.types import AgentCard
from models.agent import Agent, coerce_legacy_agent_card
from models.room import Room, RoomAgentMessage, RoomMessage, RoomUserMessage


class Step(BaseModel):
    step_id: str
    description: str
    agent_id: str | None = None
    status: str = CommonTaskState.SUBMITTED.value
    input_data: Any | None = None
    output_data: Any | None = None
    priority: int = 2  # Default priority
    dependencies: list[str] = Field(default_factory=list)
    error: str | None = None
    result: Any | None = None
    agent_name: str | None = None
    is_remote_agent: bool | None = False


class TaskResponse(BaseModel):
    task_id: str
    status: str = CommonTaskState.SUBMITTED.value
    steps: list[Step] = Field(default_factory=list)
    result: Any | None = None
    error: str | None = None


class UserResponse(BaseModel):
    session_id: str
    task_id: str
    result: str


class InspectionCenterResponse(BaseModel):
    agent_url: str
    agent_card: AgentCard | None = None
    result: list[str]
    status_code: int = 200

    @field_validator("agent_card", mode="before")
    @classmethod
    def _coerce_agent_card(cls, value: Any) -> Any:
        return coerce_legacy_agent_card(value)


class InsepectionCenterConnectionValidationResponse(BaseModel):
    agent_url: str
    agent_card: AgentCard | None = None
    is_valid: bool
    result: list[str] | None = None
    status_code: int = 200

    @field_validator("agent_card", mode="before")
    @classmethod
    def _coerce_agent_card(cls, value: Any) -> Any:
        return coerce_legacy_agent_card(value)


class OrchestrationResponse(BaseModel):
    task_id: str | None = None
    room_id: str | None = None
    meta_task_ids: list[str] | None = None
    room_agent_message_list: list[RoomAgentMessage] | None = None
    agent_id: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class DebatationCenterResponse(BaseModel):
    task_id: str
    agent_id: str
    step_id: str
    result: Any | None = None
    error: str | None = None
    status_code: int = 200


class AgentCenterResponse(BaseModel):
    agent_url: str | None = None
    agent_id: str | None = None
    provider_id: str | None = None
    agent_card: AgentCard | None = None
    agent: Agent | None = None
    agents: list[Agent] | None = None
    public_url: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200

    @field_validator("agent_card", mode="before")
    @classmethod
    def _coerce_agent_card(cls, value: Any) -> Any:
        return coerce_legacy_agent_card(value)


class ChatResponse(BaseModel):
    user_name: str
    user_input: str
    session_id: str | None = None
    task_id: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomAgentRef(BaseModel):
    """Resolved agent reference with availability status."""

    id: str
    name: str | None = None
    availability: str = "available"  # available | inaccessible | inactive | deleted


class ScopeResolutionError(BaseModel):
    """Structured error for dispatch scope resolution failures."""

    code: str  # invalid_target | group_not_usable | unauthorized_mention | empty_scope
    message: str


class ActiveRunRef(BaseModel):
    """Lightweight run shape for room setting reconcile payloads."""

    state: str
    trigger_message_id: str | None = None
    agent_id: str | None = None
    updated_at: datetime | None = None


class RoomCenterRoomSettingResponse(BaseModel):
    room_id: str | None = None
    room_agent_set: list[str] | None = None
    resolved_agents: list[RoomAgentRef] | None = None
    room_default_status: str | None = None  # ok | degraded | empty | all_unavailable
    active_runs: list[ActiveRunRef] | None = None
    room: Room | None = None
    room_list: list[Room] | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomHistoryItem(BaseModel):
    room_id: str
    title: str
    last_activity_at: datetime
    is_pinned: bool = False
    pin_order: float | None = None
    status: str = "idle"


class RoomHistoryResponse(BaseModel):
    items: list[RoomHistoryItem]


class RoomCenterActiveRunsResponse(BaseModel):
    """Lightweight payload for reconnect / reconcile without full room settings."""

    room_id: str | None = None
    active_runs: list[ActiveRunRef] | None = None
    turn_completion_kind: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomCenterUserMessageResponse(BaseModel):
    room_id: str | None = None
    message_id: str | None = None
    dispatch_root_message_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    message: RoomUserMessage | None = None
    message_list: list[RoomUserMessage] | None = None
    scope_resolution_error: ScopeResolutionError | None = None
    preflight_outcome: Literal["ready", "completed", "failed", "canceled"] | None = None
    preflight_details: dict[str, Any] | str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomCenterAgentMessageResponse(BaseModel):
    room_id: str | None = None
    message_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    message: RoomAgentMessage | None = None
    a2a_response: Any | None = None
    a2a_message: Any | None = None
    message_list: list[RoomAgentMessage] | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomCenterRoomMessageResponse(BaseModel):
    room_id: str | None = None
    message_list: list[RoomMessage] | None = None
    has_more: bool | None = None
    next_cursor: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


# ============== Discovery API Response Models ==============


class DiscoveryErrorResponse(BaseModel):
    """Standardized error response for Discovery API."""

    error: str  # Error code: "invalid_key", "no_agent_found", "missing_key", etc.
    message: str  # Human-readable error message


class DiscoveryAgentResult(BaseModel):
    """A single agent result from the Discovery API."""

    agent_card: dict  # A2A Protocol AgentCard as dictionary
    match_score: float  # Lexical relevance score (0.0 to 1.0)


class DiscoveryResponse(BaseModel):
    """Successful response from the Discovery API."""

    query: str  # The original search query
    agents: list[DiscoveryAgentResult]  # List of matching agents
    count: int  # Number of agents returned


class APIKeyErrorResponse(BaseModel):
    error: str
    message: str


class APIKeyItemResponse(BaseModel):
    key_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    is_active: bool
    usage_count: int


class APIKeyListResponse(BaseModel):
    keys: list[APIKeyItemResponse]
    count: int


class APIKeyCreateResponse(BaseModel):
    key_id: str
    name: str
    created_at: datetime
    api_key: str


class APIKeyOperationResponse(BaseModel):
    success: bool
    message: str
