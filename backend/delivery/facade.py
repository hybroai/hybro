import asyncio
import time
from enum import Enum
from typing import Any

from cachetools import TTLCache

from common.dto import (
    DeliveryEmitStatus,
    DeliveryEvent,
    TaskSubmittedEvent,
    TaskUpdateEvent,
)
from common.observability import events as E
from common.observability import get_logger
from common.protocols import EventPublisher, RedisKV
from delivery.config import DeliveryConfig
from delivery.types import SSETransport

logger = get_logger(__name__)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class DeliveryFacade:
    def __init__(
        self,
        *,
        event_publisher: EventPublisher,
        sse_transport: SSETransport,
        event_bus: Any,
        redis_kv: RedisKV | None,
        config: DeliveryConfig,
        instance_id: str,
    ) -> None:
        self.event_publisher = event_publisher
        self.sse_transport = sse_transport
        self._event_publisher = event_publisher
        self._sse_transport = sse_transport
        self._event_bus = event_bus
        self._redis_kv = redis_kv
        self.config = config
        self.instance_id = instance_id
        self._delivery_kv_connected = False
        self._delivery_pubsub_connected = False
        self._started = False
        self._kv_closed = False
        self._delivery_started_at: TTLCache[tuple[str, str], float] = TTLCache(
            maxsize=config.delivery_started_cache_maxsize,
            ttl=config.delivery_started_ttl_seconds,
        )
        self._terminal_delivery_logged: TTLCache[tuple[str, str], bool] = TTLCache(
            maxsize=config.terminal_dedup_cache_maxsize,
            ttl=config.terminal_dedup_ttl_seconds,
        )

    @property
    def delivery_kv_connected(self) -> bool:
        return self._delivery_kv_connected

    @property
    def delivery_pubsub_connected(self) -> bool:
        return self._delivery_pubsub_connected

    @property
    def redis_connected(self) -> bool:
        return self.delivery_kv_connected

    @property
    def broker_connected(self) -> bool:
        return self.delivery_pubsub_connected

    async def emit(self, event: DeliveryEvent) -> bool:
        result = await self._event_publisher.emit(event)
        return result is not False

    async def open_connection(self, conversation_id: str) -> Any:
        return await self._sse_transport.open_connection(conversation_id)

    async def add_connection(self, conversation_id: str) -> Any:
        return await self.open_connection(conversation_id)

    async def remove_connection(self, conversation_id: str, connection_id: str) -> None:
        await self._sse_transport.remove_connection(conversation_id, connection_id)

    def get_room_status(self, conversation_id: str) -> dict:
        return self._sse_transport.get_room_status(conversation_id)

    @property
    def room_connections(self) -> dict:
        return self._sse_transport.room_connections

    async def refresh_health(self) -> None:
        if self._redis_kv is None:
            self._delivery_kv_connected = False
        else:
            try:
                self._delivery_kv_connected = await self._redis_kv.ping()
            except Exception:
                self._delivery_kv_connected = False

        try:
            await self._event_bus.refresh_health()
            self._delivery_pubsub_connected = bool(self._event_bus.is_connected)
        except Exception:
            self._delivery_pubsub_connected = False

    async def start(self) -> None:
        started: list[str] = []
        try:
            await self._event_bus.start()
            started.append("bus")
            await self.refresh_health()
            self._started = True
        except Exception:
            await self._rollback_start(started)
            raise

    async def stop(self) -> None:
        if not self._started:
            await self._close_kv_once()
            return
        await self._sse_transport.close_all_connections()
        await self._event_bus.stop()
        await self._close_kv_once()
        self._started = False

    async def send_task_submitted(
        self,
        conversation_id: str,
        message_id: str,
        task_id: str,
        agent_name: str,
        agent_id: str | None = None,
        run_id: str | None = None,
        opaque_public_call_id: str | None = None,
        status: Any = "working",
        related_message_id: str | None = None,
        created_at: str | None = None,
        step_number: int | None = None,
        total_steps: int | None = None,
        task_content: str | None = None,
        client_request_id: str | None = None,
    ) -> None:
        self._delivery_started_at.setdefault(
            (conversation_id, message_id),
            time.perf_counter(),
        )
        await self.emit(
            TaskSubmittedEvent(
                conversation_id=conversation_id,
                message_id=message_id,
                task_id=task_id,
                agent_name=agent_name,
                run_id=run_id,
                opaque_public_call_id=opaque_public_call_id,
                agent_id=agent_id,
                status=_enum_value(status),
                related_message_id=related_message_id,
                created_at=created_at,
                step_number=step_number,
                total_steps=total_steps,
                task_content=task_content,
                client_request_id=client_request_id,
            )
        )

    async def send_task_update(
        self,
        conversation_id: str,
        message_id: str,
        status: Any,
        content: str | None = None,
        run_id: str | None = None,
        opaque_public_call_id: str | None = None,
        error: str | None = None,
        requires_input: bool = False,
        requires_auth: bool = False,
        status_message: str | None = None,
        agent_name: str | None = None,
        agent_id: str | None = None,
        related_message_id: str | None = None,
        created_at: str | None = None,
        step_number: int | None = None,
        total_steps: int | None = None,
        task_content: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        client_request_id: str | None = None,
        delivery_id: str | None = None,
    ) -> bool:
        event = TaskUpdateEvent(
            conversation_id=conversation_id,
            message_id=message_id,
            status=_enum_value(status),
            run_id=run_id,
            opaque_public_call_id=opaque_public_call_id,
            content=content,
            error=error,
            requires_input=requires_input,
            requires_auth=requires_auth,
            status_message=status_message,
            agent_name=agent_name,
            agent_id=agent_id,
            related_message_id=related_message_id,
            created_at=created_at,
            step_number=step_number,
            total_steps=total_steps,
            task_content=task_content,
            parts=parts,
            client_request_id=client_request_id,
            delivery_id=delivery_id,
        )
        checked = getattr(self._event_publisher, "emit_checked", None)
        if delivery_id and callable(checked):
            outcome = await checked(event)
            delivered = outcome in {
                DeliveryEmitStatus.DELIVERED,
                DeliveryEmitStatus.ALREADY_DELIVERED,
                DeliveryEmitStatus.DELIVERED.value,
                DeliveryEmitStatus.ALREADY_DELIVERED.value,
            }
        else:
            delivered = await self.emit(event)
        normalized_status = str(_enum_value(status)).lower()
        if normalized_status in self.config.terminal_processing_statuses:
            self._record_terminal_delivery(
                conversation_id=conversation_id,
                message_id=message_id,
                outcome=normalized_status if delivered else "delivery_failed",
                terminal_kind="task_update",
                agent_id=agent_id,
            )
        return delivered

    def set_draining(self, draining: bool) -> None:
        self._sse_transport.set_draining(draining)

    async def drain(self, timeout: float) -> None:
        """Refuse new streams, then wait up to ``timeout`` for open ones to end."""
        self.set_draining(True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (
            self._sse_transport.active_connection_count() > 0 and loop.time() < deadline
        ):
            await asyncio.sleep(0.1)

    async def _rollback_start(self, started: list[str]) -> None:
        if "bus" in started:
            await self._event_bus.stop()
        self._started = False

    async def _close_kv_once(self) -> None:
        if self._redis_kv is not None and not self._kv_closed:
            await self._redis_kv.close()
            self._kv_closed = True

    def _record_terminal_delivery(
        self,
        *,
        conversation_id: str,
        message_id: str,
        outcome: str,
        terminal_kind: str,
        agent_id: str | None = None,
    ) -> None:
        delivery_key = (conversation_id, message_id)
        if outcome != "delivery_failed":
            if delivery_key in self._terminal_delivery_logged:
                self._delivery_started_at.pop(delivery_key, None)
                return
            self._terminal_delivery_logged[delivery_key] = True
        started_at = self._delivery_started_at.pop(
            delivery_key,
            time.perf_counter(),
        )
        logger.emit(
            E.DELIVERY_COMPLETED,
            conversation_id=conversation_id,
            message_id=message_id,
            agent_id=agent_id,
            outcome=outcome,
            terminal_kind=terminal_kind,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
        )


__all__ = ["DeliveryFacade"]
