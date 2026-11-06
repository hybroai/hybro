import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SSETransport(Protocol):
    def connect(
        self, conversation_id: str, connection_id: str
    ) -> AsyncIterator[dict]: ...
    async def disconnect(self, connection_id: str) -> None: ...
    async def broadcast_frame_to_room(
        self, conversation_id: str, frame: dict
    ) -> int: ...
    def set_draining(self, draining: bool) -> None: ...
    def active_connection_count(self) -> int: ...


class TaskRunner(Protocol):
    def __call__(
        self,
        coro: Awaitable[Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task: ...


InternalEnvelopeCallback = Callable[[dict[str, Any]], Awaitable[None]]


class RoomSubscriptionLimitExceeded(RuntimeError):
    pass


__all__ = [
    "InternalEnvelopeCallback",
    "RoomSubscriptionLimitExceeded",
    "SSETransport",
    "TaskRunner",
]
