from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from common.idempotency import MAX_CLIENT_REQUEST_ID_LENGTH
from models.room import RoomMessage, RoomUserMessage

MAX_AGENT_URL_LENGTH = 2048

AgentUrl = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_AGENT_URL_LENGTH
    ),
]


class InspectionCenterRequest(BaseModel):
    """An owner-supplied agent URL to fetch a card from or inspect."""

    agent_id: str | None = None
    agent_url: AgentUrl


class ChatRequest(BaseModel):
    user_name: str
    user_input: str
    session_id: str | None = None


class UserAttachmentRequest(BaseModel):
    """Wire format from frontend. Only file_id is used server-side; all metadata
    is resolved from the room_files collection to prevent spoofing.
    """

    file_id: str
    file_url: str | None = None


class RoomCenterUserMessageRequest(BaseModel):
    conversation_id: str | None = None
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


class RoomCenterRoomMessageRequest(BaseModel):
    conversation_id: str | None = None
    limit: Any | None = None
    cursor: Any | None = None
    message_id: str | None = None
    message_type: str | None = None
    message_content: str | None = None
    message_created_at: datetime | None = None
    extend_info: dict[str, Any] | None = None
    message: RoomMessage | None = None
