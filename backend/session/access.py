"""Per-request binding of the session facade to its caller.

``BoundSessionAccess`` serves a Source API request: one owner, one source and
the owner's agent resolver. ``BoundOwnerSessionAccess`` serves the owner in
the Panel, who reaches every session.
"""

from __future__ import annotations

from common.protocols import AgentNetworkTargetResolver
from models.session import (
    SessionClosed,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionStatus,
)
from session.protocols import SessionOwner, SessionService
from source.protocols import SourceIdentity


class BoundSessionAccess:
    """``SessionAccess`` for one source acting on one owner's agents."""

    def __init__(
        self,
        service: SessionService,
        *,
        owner: SessionOwner,
        source: SourceIdentity,
        resolver: AgentNetworkTargetResolver,
    ) -> None:
        self._service = service
        self._owner = owner
        self._source = source
        self._resolver = resolver

    async def send(self, request: SessionMessageRequest) -> SessionMessageResponse:
        return await self._service.send(
            request, owner=self._owner, source=self._source, resolver=self._resolver
        )

    async def check(self, session_id: str) -> SessionStatus:
        return await self._service.check(
            session_id, owner=self._owner, source=self._source
        )

    async def close(self, session_id: str) -> SessionClosed:
        return await self._service.close(
            session_id, owner=self._owner, source=self._source
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
