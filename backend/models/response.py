from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from common.a2a_constants import CommonTaskState
from common.types import AgentCard
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


class InspectionCenterResponse(BaseModel):
    agent_url: str
    agent_card: AgentCard | None = None
    result: list[str]
    status_code: int = 200


class OrchestrationResponse(BaseModel):
    task_id: str | None = None
    conversation_id: str | None = None
    meta_task_ids: list[str] | None = None
    room_agent_message_list: list[RoomAgentMessage] | None = None
    agent_id: str | None = None
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
    conversation_id: str | None = None
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
    conversation_id: str
    title: str
    last_activity_at: datetime
    is_pinned: bool = False
    pin_order: float | None = None
    status: str = "idle"


class RoomHistoryResponse(BaseModel):
    items: list[RoomHistoryItem]


class RoomCenterActiveRunsResponse(BaseModel):
    """Lightweight payload for reconnect / reconcile without full room settings."""

    conversation_id: str | None = None
    active_runs: list[ActiveRunRef] | None = None
    success: bool
    error: str | None = None
    status_code: int = 200


class RoomCenterUserMessageResponse(BaseModel):
    conversation_id: str | None = None
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


class RoomCenterRoomMessageResponse(BaseModel):
    conversation_id: str | None = None
    message_list: list[RoomMessage] | None = None
    has_more: bool | None = None
    next_cursor: str | None = None
    success: bool
    error: str | None = None
    status_code: int = 200
