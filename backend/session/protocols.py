"""Session ports.

``SessionAccess`` is the Client API surface, bound to one owner and one client
per request; ``OwnerSessionAccess`` is the owner's surface in the Panel. Every
parameter and return of both is a public DTO from ``models.session``.
``SessionService`` is the facade surface the gateway holds, ``SessionStore``
the durable record, ``ClientDirectory`` the names of the clients on the
owner's payloads, and ``ConversationEventsPort`` the durable room event log
the bounded wait reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from client.protocols import ClientIdentity
from common.dto import (
    AgentMessageInput,
    CreateRoomRequest,
    MembershipUpdateRequest,
    RoomInfo,
    RoomMessageInfo,
    SavedUserMessage,
    UserMessageInput,
)
from common.protocols import AgentNetworkTargetResolver
from models.session import (
    SessionClosed,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionStatus,
)
from session.models import SessionRecord


@runtime_checkable
class RoomRegistry(Protocol):
    async def get_room(self, conversation_id: str) -> RoomInfo | None: ...
    async def get_room_agents(self, conversation_id: str) -> list[str]: ...
    async def get_room_owner(self, conversation_id: str) -> str | None: ...


@runtime_checkable
class RoomManagement(Protocol):
    async def create_room(self, request: CreateRoomRequest) -> RoomInfo: ...
    async def delete_room(self, conversation_id: str, owner_id: str) -> bool: ...
    async def update_room(
        self, conversation_id: str, updates: dict
    ) -> RoomInfo | None: ...
    async def update_membership(
        self, conversation_id: str, request: MembershipUpdateRequest
    ) -> RoomInfo: ...


@runtime_checkable
class RoomMessageStore(Protocol):
    async def save_user_message(
        self, conversation_id: str, message: UserMessageInput
    ) -> SavedUserMessage: ...
    async def save_agent_message(
        self, conversation_id: str, message: AgentMessageInput
    ) -> str: ...
    async def update_agent_message_status(
        self, message_id: str, status: str, **kwargs
    ) -> bool: ...
    async def get_message(self, message_id: str) -> RoomMessageInfo | None: ...


@dataclass(frozen=True, slots=True)
class SessionOwner:
    """The instance owner a request acts for."""

    user_id: str
    name: str


@runtime_checkable
class SessionAccess(Protocol):
    async def send(self, request: SessionMessageRequest) -> SessionMessageResponse: ...
    async def check(self, session_id: str) -> SessionStatus: ...
    async def close(self, session_id: str) -> SessionClosed: ...


@runtime_checkable
class OwnerSessionAccess(Protocol):
    async def list_sessions(self) -> SessionListResponse: ...
    async def get_record(self, session_id: str) -> SessionOwnerRecord: ...
    async def close(self, session_id: str) -> SessionClosed: ...


@runtime_checkable
class ClientDirectory(Protocol):
    async def identities(self, client_ids: list[str]) -> dict[str, ClientIdentity]: ...


@runtime_checkable
class SessionService(Protocol):
    async def send(
        self,
        request: SessionMessageRequest,
        *,
        owner: SessionOwner,
        client: ClientIdentity,
        resolver: AgentNetworkTargetResolver,
    ) -> SessionMessageResponse: ...
    async def check(
        self, session_id: str, *, owner: SessionOwner, client: ClientIdentity
    ) -> SessionStatus: ...
    async def close(
        self, session_id: str, *, owner: SessionOwner, client: ClientIdentity
    ) -> SessionClosed: ...
    async def list_for_owner(self, *, owner: SessionOwner) -> SessionListResponse: ...
    async def record_for_owner(
        self, session_id: str, *, owner: SessionOwner
    ) -> SessionOwnerRecord: ...
    async def close_for_owner(
        self, session_id: str, *, owner: SessionOwner
    ) -> SessionClosed: ...


@runtime_checkable
class SessionStore(Protocol):
    async def reserve(self, record: SessionRecord) -> SessionRecord: ...
    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def set_conversation_if_unset(
        self, session_id: str, conversation_id: str
    ) -> SessionRecord | None: ...
    async def record_send(
        self,
        session_id: str,
        *,
        client_request_id: str,
        user_message_id: str,
        client_id: str,
        event_seq: int,
        at: datetime,
    ) -> SessionRecord | None: ...
    async def record_accepted_agent(
        self, session_id: str, agent_id: str
    ) -> SessionRecord | None: ...
    async def mark_closed(
        self, session_id: str, at: datetime
    ) -> SessionRecord | None: ...
    async def list_for_owner(
        self, owner_user_id: str, limit: int = 100
    ) -> list[SessionRecord]: ...
    async def list_open_conversations(self, owner_user_id: str) -> dict[str, str]: ...


@runtime_checkable
class ConversationEventsPort(Protocol):
    async def latest_seq(self, conversation_id: str) -> int: ...
    async def read_range(
        self, conversation_id: str, *, after: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...


__all__ = [
    "ConversationEventsPort",
    "OwnerSessionAccess",
    "RoomManagement",
    "RoomMessageStore",
    "RoomRegistry",
    "SessionAccess",
    "SessionOwner",
    "SessionService",
    "SessionStore",
    "ClientDirectory",
]
