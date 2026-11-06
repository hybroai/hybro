import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.observability import MetricsCollector, NoopMetricsCollector, get_logger
from common.observability import events as E
from delivery.config import DeliveryConfig
from delivery.sse.connection import SSEConnection
from delivery.types import RoomSubscriptionLimitExceeded, TaskRunner

logger = get_logger(__name__)


def _log_refusal(
    conversation_id: str, reason: str, *, exc: BaseException | None = None
) -> None:
    """A refused subscription; draining is expected during shutdown."""
    logger.emit(
        E.SSE_SUBSCRIPTION_REFUSED,
        level=logging.INFO if reason == "draining" else logging.WARNING,
        exc=exc,
        conversation_id=conversation_id,
        reason=reason,
    )


@dataclass(slots=True)
class _AdmissionLockState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SSETransportImpl:
    def __init__(
        self,
        *,
        event_bus: Any,
        config: DeliveryConfig,
        now: Callable[[], datetime],
        id_factory: Callable[[], str],
        instance_id: str,
        task_runner: TaskRunner,
        metrics: MetricsCollector | None = None,
        room_seq_reader: Callable[[str], Awaitable[int | None]] | None = None,
        snapshot_provider: Callable[[str], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.config = config
        self._now = now
        self._id_factory = id_factory
        self.instance_id = instance_id
        self._task_runner = task_runner
        self._metrics = metrics or NoopMetricsCollector()
        self._room_seq_reader = room_seq_reader
        self._snapshot_provider = snapshot_provider
        self.room_connections: dict[str, dict[str, SSEConnection]] = {}
        self.connection_rooms: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._admission_locks: dict[str, _AdmissionLockState] = {}
        self._admission_locks_lock = asyncio.Lock()
        self._room_cleanup_tasks: dict[str, asyncio.Task] = {}
        self._draining = False
        self._record_connection_gauge()

    def connect(self, conversation_id: str, connection_id: str) -> AsyncIterator[dict]:
        async def iterator() -> AsyncIterator[dict]:
            connection = await self._admit_connection(conversation_id, connection_id)
            try:
                while connection.is_active:
                    yield await connection.next_frame(
                        timeout=self.config.heartbeat_interval_seconds
                    )
            finally:
                await self.disconnect(connection_id)

        return iterator()

    async def disconnect(self, connection_id: str) -> None:
        conversation_id = self.connection_rooms.get(connection_id)
        if conversation_id is None:
            return
        await self.remove_connection(conversation_id, connection_id)

    async def open_connection(self, conversation_id: str) -> SSEConnection:
        return await self._admit_connection(conversation_id, self._id_factory())

    async def remove_connection(self, conversation_id: str, connection_id: str) -> None:
        room_became_empty = await self._remove_connections_local(
            conversation_id, [connection_id]
        )
        if room_became_empty:
            self._schedule_room_cleanup(conversation_id)

    async def broadcast_frame_to_room(
        self, conversation_id: str, frame: dict[str, Any]
    ) -> int:
        async with self._lock:
            snapshot = list(self.room_connections.get(conversation_id, {}).items())

        disconnected: list[str] = []
        delivered = 0
        for connection_id, connection in snapshot:
            if await connection.send_frame(frame):
                delivered += 1
            else:
                disconnected.append(connection_id)

        if disconnected:
            room_became_empty = await self._remove_connections_local(
                conversation_id, disconnected
            )
            if room_became_empty:
                self._schedule_room_cleanup(conversation_id)

        # Boundary checkpoint (Room Stream Snapshot plan §7): a settled
        # terminal frame forces an immediate snapshot so slow/reconnecting
        # consumers converge on the final room state without waiting for
        # their own gap recovery.
        if self._snapshot_provider is not None and self._is_terminal_frame(frame):
            self._task_runner(
                self._enqueue_room_snapshot(conversation_id),
                name=f"delivery-snapshot-boundary-{conversation_id}",
            )
        return delivered

    async def _enqueue_room_snapshot(self, conversation_id: str) -> None:
        try:
            data = await self._snapshot_provider(conversation_id)
        except Exception:
            return
        if data is None:
            return
        frame = {
            "type": "snapshot",
            "timestamp": self._now().isoformat(),
            "conversation_id": conversation_id,
            "data": data,
        }
        await self.broadcast_frame_to_room(conversation_id, frame)

    def _is_terminal_frame(self, frame: dict[str, Any]) -> bool:
        frame_type = frame.get("type")
        data = frame.get("data")
        if not isinstance(data, dict):
            return False
        if frame_type in {"processing_status", "task_update"}:
            return data.get("status") in self.config.terminal_processing_statuses
        if frame_type == "run_event":
            # Terminal run facts carry a delivery_id; public trace kinds do not.
            return bool(data.get("delivery_id"))
        return False

    async def close_all_connections(self) -> None:
        async with self._lock:
            rooms = list(self.room_connections)
            connections = [
                connection
                for room_connections in self.room_connections.values()
                for connection in room_connections.values()
            ]
            self.room_connections.clear()
            self.connection_rooms.clear()

        for connection in connections:
            connection.close()
        for conversation_id in rooms:
            self._schedule_room_cleanup(conversation_id)
        await self._drain_room_cleanup_tasks()
        self._record_connection_gauge()

    def get_room_status(self, conversation_id: str) -> dict[str, Any]:
        room_connections = self.room_connections.get(conversation_id, {})
        return {
            "conversation_id": conversation_id,
            "connection_count": len(room_connections),
            "connections": list(room_connections),
        }

    def set_draining(self, draining: bool) -> None:
        self._draining = draining

    async def _admit_connection(
        self,
        conversation_id: str,
        connection_id: str,
    ) -> SSEConnection:
        if self._draining:
            _log_refusal(conversation_id, "draining")
            raise ConnectionRefusedError(
                "Server is draining - rejecting new SSE connections"
            )

        first_admission_started = False
        connection_admitted = False
        try:
            async with self._admission(conversation_id):
                if self._draining:
                    _log_refusal(conversation_id, "draining")
                    raise ConnectionRefusedError(
                        "Server is draining - rejecting new SSE connections"
                    )

                async with self._lock:
                    first_for_room = not self.room_connections.get(conversation_id)

                if first_for_room:
                    first_admission_started = True
                    try:
                        await self.event_bus.subscribe_room(conversation_id)
                    except Exception as exc:
                        _log_refusal(
                            conversation_id,
                            "subscription_limit"
                            if isinstance(exc, RoomSubscriptionLimitExceeded)
                            else "subscribe_failed",
                            exc=exc,
                        )
                        raise ConnectionRefusedError(
                            f"Unable to subscribe room {conversation_id}"
                        ) from exc

                connection = SSEConnection(
                    conversation_id=conversation_id,
                    connection_id=connection_id,
                    heartbeat_interval=self.config.heartbeat_interval_seconds,
                    queue_maxsize=self.config.sse_connection_queue_maxsize,
                    now=self._now,
                    room_seq_reader=self._room_seq_reader,
                    snapshot_provider=self._snapshot_provider,
                )
                async with self._lock:
                    room_connections = self.room_connections.setdefault(
                        conversation_id, {}
                    )
                    room_connections[connection_id] = connection
                    self.connection_rooms[connection_id] = conversation_id
                    connection_admitted = True
                    self._record_connection_gauge()
                return connection
        except BaseException:
            if first_admission_started and not connection_admitted:
                self._schedule_room_cleanup(conversation_id)
            raise

    @asynccontextmanager
    async def _admission(self, conversation_id: str):
        async with self._admission_locks_lock:
            state = self._admission_locks.get(conversation_id)
            if state is None:
                state = _AdmissionLockState()
                self._admission_locks[conversation_id] = state
            state.users += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._admission_locks_lock:
                state.users -= 1
                cleanup = self._room_cleanup_tasks.get(conversation_id)
                cleanup_active = cleanup is not None and not cleanup.done()
                cleanup_is_current = cleanup is asyncio.current_task()
                if (
                    state.users == 0
                    and not self.room_connections.get(conversation_id)
                    and (not cleanup_active or cleanup_is_current)
                    and self._admission_locks.get(conversation_id) is state
                ):
                    self._admission_locks.pop(conversation_id, None)

    async def _remove_connections_local(
        self,
        conversation_id: str,
        connection_ids: list[str],
    ) -> bool:
        room_empty = False
        async with self._lock:
            room_connections = self.room_connections.get(conversation_id)
            if not room_connections:
                return False

            for connection_id in connection_ids:
                connection = room_connections.pop(connection_id, None)
                if self.connection_rooms.get(connection_id) == conversation_id:
                    self.connection_rooms.pop(connection_id, None)
                if connection is not None:
                    connection.close()

            if not room_connections:
                self.room_connections.pop(conversation_id, None)
                room_empty = True

            self._record_connection_gauge()

        return room_empty

    def _schedule_room_cleanup(self, conversation_id: str) -> None:
        existing = self._room_cleanup_tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return
        task = self._task_runner(
            self._unsubscribe_if_room_still_empty(conversation_id),
            name=f"delivery-sse-room-cleanup-{conversation_id}",
        )
        self._room_cleanup_tasks[conversation_id] = task
        task.add_done_callback(
            lambda completed, cleanup_conversation_id=conversation_id: (
                self._finish_room_cleanup(cleanup_conversation_id, completed)
            )
        )

    def _finish_room_cleanup(
        self,
        conversation_id: str,
        task: asyncio.Task,
    ) -> None:
        if self._room_cleanup_tasks.get(conversation_id) is task:
            self._room_cleanup_tasks.pop(conversation_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _drain_room_cleanup_tasks(self) -> None:
        while self._room_cleanup_tasks:
            tasks = tuple(self._room_cleanup_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            for conversation_id, task in tuple(self._room_cleanup_tasks.items()):
                if task.done():
                    self._finish_room_cleanup(conversation_id, task)

    async def _unsubscribe_if_room_still_empty(self, conversation_id: str) -> None:
        async with self._admission(conversation_id):
            async with self._lock:
                if self.room_connections.get(conversation_id):
                    return
            await self.event_bus.unsubscribe_room(conversation_id)

    def active_connection_count(self) -> int:
        return sum(len(connections) for connections in self.room_connections.values())

    def _record_connection_gauge(self) -> None:
        try:
            self._metrics.gauge(
                "hybro_delivery_sse_connections",
                self.active_connection_count(),
                {"worker_id": self.instance_id},
            )
        except Exception:
            return


__all__ = ["SSETransportImpl"]
