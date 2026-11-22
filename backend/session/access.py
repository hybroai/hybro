"""Per-request binding of the session facade to its caller.

``BoundSessionAccess`` serves a Client API request: one owner, one client and
the owner's agent resolver. ``BoundOwnerSessionAccess`` serves the owner in
the Panel, who reaches every session.
"""

from __future__ import annotations

from client.protocols import ClientIdentity
from common.protocols import AgentNetworkTargetResolver
from models.session import (
    SessionClosed,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionQuestionAnswer,
    SessionQuestionAnswered,
    SessionStatus,
)
from session.protocols import SessionOwner, SessionService


class BoundSessionAccess:
    """``SessionAccess`` for one client acting on one owner's agents."""

    def __init__(
        self,
        service: SessionService,
        *,
        owner: SessionOwner,
        client: ClientIdentity,
        resolver: AgentNetworkTargetResolver,
    ) -> None:
        self._service = service
        self._owner = owner
        self._client = client
        self._resolver = resolver

    async def send(self, request: SessionMessageRequest) -> SessionMessageResponse:
        return await self._service.send(
            request, owner=self._owner, client=self._client, resolver=self._resolver
        )

    async def check(self, session_id: str) -> SessionStatus:
        return await self._service.check(
            session_id, owner=self._owner, client=self._client
        )

    async def close(self, session_id: str) -> SessionClosed:
        return await self._service.close(
            session_id, owner=self._owner, client=self._client
        )

    async def answer(
        self, session_id: str, question_id: str, request: SessionQuestionAnswer
    ) -> SessionQuestionAnswered:
        return await self._service.answer(
            session_id, question_id, request, owner=self._owner, client=self._client
        )


class BoundOwnerSessionAccess:
    """``OwnerSessionAccess`` for the owner."""

    def __init__(self, service: SessionService, *, owner: SessionOwner) -> None:
        self._service = service
        self._owner = owner

    async def list_sessions(self) -> SessionListResponse:
        return await self._service.list_for_owner(owner=self._owner)

    async def get_record(self, session_id: str) -> SessionOwnerRecord:
        return await self._service.record_for_owner(session_id, owner=self._owner)

    async def close(self, session_id: str) -> SessionClosed:
        return await self._service.close_for_owner(session_id, owner=self._owner)


__all__ = ["BoundOwnerSessionAccess", "BoundSessionAccess"]
