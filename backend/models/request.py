from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from common.idempotency import MAX_CLIENT_REQUEST_ID_LENGTH
from common.types import AgentCard, Message, MessageRole, Part, Task, TextPart
from models.agent import Agent, coerce_legacy_agent_card
from models.room import (
    Room,
    RoomAgentMessage,
    RoomMessage,
    RoomUserMessage,
)


class PaginationParams(BaseModel):
    page: int | None = Field(default=1, ge=1, description="Page number (1-indexed)")
    limit: int | None = Field(
        default=10, ge=1, le=100, description="Number of items per page"
    )

    @property
    def skip(self) -> int:
        if not self.page:
            return 0
        return (self.page - 1) * self.limit


class APIKeyCreateRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Friendly name for the API key",
    )


class FilterParams(BaseModel):
    # all nullable
    filters: dict[str, Any] | None = Field(
        default_factory=dict, description="MongoDB filter conditions"
    )
    sort_by: str | None = Field(default=None, description="Field to sort by")
    sort_order: int | None = Field(
        default=-1, description="Sort order: 1 for ascending, -1 for descending"
    )


class TaskRequest(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    query: str
    context: dict[str, Any] | None = Field(default_factory=dict)
    message: Message | None = None

    def to_message(self) -> Message:
        """Convert request to an internal message.

        Message overrides must already use ``common.types.Message``; SDK or
        external message shapes should be normalized at the adapter boundary.
        """
        if self.message:
            return self.message

        return Message(
            message_id=uuid4().hex,
            role=MessageRole.USER,
            parts=[Part(root=TextPart(text=self.query))],
            metadata=self.context,
        )


class AgentTaskRequest(BaseModel):
    task_id: str
    agent_id: str
    step_id: str
    input_data: Any
    context: dict[str, Any] | None = Field(default_factory=dict)
    message: Message | None = None

    def to_message(self) -> Message:
        """Convert agent task request to an internal message.

        Message overrides must already use ``common.types.Message``; SDK or
        external message shapes should be normalized at the adapter boundary.
        """
        if self.message:
            return self.message

        # Create message from input data
        if isinstance(self.input_data, str):
            text = self.input_data
        elif isinstance(self.input_data, dict) and "text" in self.input_data:
            text = self.input_data["text"]
        else:
            # Try to convert to string or use as-is
            try:
                text = str(self.input_data)
            except Exception:
                # Use generic text if conversion fails
                text = f"Processing step {self.step_id}"

        # Add metadata
        metadata = {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "step_id": self.step_id,
            **self.context,
        }

        return Message(
            message_id=uuid4().hex,
            role=MessageRole.USER,
            parts=[Part(root=TextPart(text=text))],
            metadata=metadata,
        )


# for user
class UserInput(BaseModel):
    user_name: str
    user_input: str
    session_id: str | None = None


class InspectionCenterRequest(BaseModel):
    agent_id: str | None = None
    agent_url: str


class OrchestrationRequest(BaseModel):
    task_id: str | None = None
    room_id: str | None = None
    room_user_message_id: str | None = None
    room_agent_message_id: str | None = None
    room_related_message_id: str | None = None
    user_id: str | None = None
    is_recovery: bool = False
    reuse_processing_claim: bool = False
    client_request_id: str | None = None
    # Live routing inputs carried from the validated API request. When
    # present they are authoritative for Run creation; the persisted-message
    # reconstruction in the routing seam remains the recovery/re-entry
    # fallback when these are absent.
    mode: str | None = None
    agent_scope: dict[str, Any] | None = None


class DebatationCenterRequest(BaseModel):
    task_id: str


class AgentCenterRequest(BaseModel):
    agent_id: str | None = None
    agent_url: str | None = None
    provider_id: str | None = None
    user_id: str | None = None  # For visibility filtering (optional auth)
    query: dict[str, Any] | None = None
    limit: int = 0
    agent_card: AgentCard | None = None
    call_increment: int | None = 0
    call_success_increment: int | None = 0
    like_increment: int | None = 0
    dislike_increment: int | None = 0
    query_text: str | None = None
    agent: Agent | None = None
    agent_count: int | None = 0

    @field_validator("agent_card", mode="before")
    @classmethod
    def _coerce_agent_card(cls, value: Any) -> Any:
        return coerce_legacy_agent_card(value)


class ChatRequest(BaseModel):
    user_name: str
    user_input: str
    session_id: str | None = None


class RoomCenterRoomSettingRequest(BaseModel):
    room_id: str | None = None
    room_name: str | None = None
    room_owner_id: str | None = None
    room_owner_name: str | None = None
    room_created_at: datetime | None = None
    extend_info: dict[str, Any] | None = None
    room: Room | None = None
    requesting_user_id: str | None = None

    # Legacy fields — accepted during rollout; canonical fields take precedence.
    room_agent_set: dict[str, str] | None = None
    applied_from_group: str | None = None

    # Canonical membership write input (mutually exclusive)
    membership_seed_input: str | None = (
        None  # "manual" | "saved_group" | "all_current_agents"
    )
    room_agent_ids: list[str] | None = None
    seed_group_id: str | None = None
    seed_all_current_agents: bool | None = None

    # Active-runs query: optional trigger message for turn_completion_kind lookup
    trigger_message_id: str | None = None

    # Chat history presentation metadata
    is_pinned: bool | None = None
    pin_order: float | None = None


class UserAttachmentRequest(BaseModel):
    """Wire format from frontend. Only file_id is used server-side; all metadata
    is resolved from the room_files collection to prevent spoofing.
    """

    file_id: str
    file_url: str | None = None


class RoomCenterUserMessageRequest(BaseModel):
    room_id: str | None = None
    message_id: str | None = None
    related_message_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    user_input: str | None = None
    message_created_at: datetime | None = None
    extend_info: dict[str, Any] | None = None
    message: RoomUserMessage | None = None
    attachments: list[UserAttachmentRequest] | None = None
    inline_file_ids: list[str] | None = None
    client_request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CLIENT_REQUEST_ID_LENGTH,
    )


class RoomCenterAgentMessageRequest(BaseModel):
    room_id: str | None = None
    message_id: str | None = None
    related_message_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    agent_message_content: Task | None = None
    message_created_at: datetime | None = None
    extend_info: dict[str, Any] | None = None
    message: RoomAgentMessage | None = None
    # Dispatch-only values. These are intentionally not copied to RoomAgentMessage
    # or exposed through room/SSE response models.
    dispatch_task: str | None = None
    resolved_resource_payloads: list[dict[str, Any]] | None = None
    explicit_attachment_refs: list[str | dict[str, Any]] | None = None
    dispatch_resource_payloads: list[dict[str, Any]] | None = None
    selected_attachment_refs: list[str] | None = None
    attachment_forwarding_policy: str | None = None


class RoomCenterRoomMessageRequest(BaseModel):
    room_id: str | None = None
    limit: Any | None = None
    cursor: Any | None = None
    message_id: str | None = None
    message_type: str | None = None
    message_content: str | None = None
    message_created_at: datetime | None = None
    extend_info: dict[str, Any] | None = None
    message: RoomMessage | None = None


# Agent Group Requests
class AgentGroupRequest(BaseModel):
    group_id: str | None = None
    name: str | None = None
    description: str | None = None
    owner_id: str | None = None
    agents: list[str] | None = None  # List of agent IDs


class AgentGroupCreateRequest(BaseModel):
    name: str
    description: str | None = None
    owner_id: str
    agents: list[str] = []


class AgentGroupUpdateRequest(BaseModel):
    group_id: str
    name: str | None = None
    description: str | None = None
    agents: list[str] | None = None
