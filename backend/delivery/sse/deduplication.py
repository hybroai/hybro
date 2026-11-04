import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

from cachetools import TTLCache

from common.observability import events as E
from common.observability import traced_create_task
from common.protocols import RedisKV
from common.utils.logger import get_logger
from delivery.config import DeliveryConfig

logger = get_logger(__name__)


class DeliveryReservationStatus(StrEnum):
    RESERVED = "reserved"
    IN_FLIGHT = "in_flight"
    ALREADY_DELIVERED = "already_delivered"


@dataclass(frozen=True)
class DeliveryReservation:
    status: DeliveryReservationStatus
    dedup_key: str
    claim_id: str | None = None
    l2_owned: bool = False


@dataclass(frozen=True)
class _CacheEntry:
    state: str
    status: str
    claim_id: str | None = None
    l2_owned: bool = False


class TerminalStatusDeduplicator:
    """Two-phase terminal delivery reservation and confirmation.

    Reservations use a short lease and are distinct from confirmed delivery
    markers. A Redis failure creates an explicitly L1-only reservation that can
    be confirmed locally without later mistaking a missing Redis key for lost
    ownership.
    """

    def __init__(
        self,
        *,
        config: DeliveryConfig,
        redis_kv: RedisKV | None = None,
        timer: Callable[[], float] | None = None,
        claim_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.redis_kv = redis_kv
        self._claim_id_factory = claim_id_factory or (lambda: uuid4().hex)
        self.reservation_ttl_seconds = min(
            config.terminal_reservation_ttl_seconds,
            max(1, config.terminal_dedup_ttl_seconds - 1),
        )
        confirmed_kwargs = {
            "maxsize": config.terminal_dedup_cache_maxsize,
            "ttl": config.terminal_dedup_ttl_seconds,
        }
        reservation_kwargs = {
            "maxsize": config.terminal_dedup_cache_maxsize,
            "ttl": self.reservation_ttl_seconds,
        }
        if timer is not None:
            confirmed_kwargs["timer"] = timer
            reservation_kwargs["timer"] = timer
        self.cache: TTLCache[str, _CacheEntry] = TTLCache(**confirmed_kwargs)
        self.reservations: TTLCache[str, _CacheEntry] = TTLCache(**reservation_kwargs)
        self._reservation_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._redis_tasks: dict[asyncio.Task[Any], str] = {}

    def _redis_task_done(self, task: asyncio.Task[Any]) -> None:
        if self._redis_tasks.pop(task, None) is None or task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _await_terminal_redis(
        self,
        awaitable: Awaitable[Any],
        *,
        operation: str,
    ) -> Any:
        task = traced_create_task(
            awaitable,
            name=f"terminal-dedup-redis:{operation}",
        )
        self._redis_tasks[task] = operation
        task.add_done_callback(self._redis_task_done)
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=self.config.terminal_redis_io_timeout_seconds,
            )
        except BaseException:
            task.cancel()
            raise
        if not done:
            task.cancel()
            logger.emit(
                E.TERMINAL_DEDUP_REDIS_OPERATION_TIMED_OUT,
                operation=operation,
                timeout_seconds=self.config.terminal_redis_io_timeout_seconds,
            )
            raise TimeoutError(
                f"terminal delivery Redis operation timed out: {operation}"
            )
        return task.result()

    @staticmethod
    def _dedup_key(
        conversation_id: str,
        message_id: str | None,
        delivery_id: str | None,
    ) -> str:
        return (
            f"delivery:{delivery_id}"
            if delivery_id
            else f"{conversation_id}:{message_id}"
        )

    async def reserve(
        self,
        *,
        conversation_id: str,
        message_id: str | None,
        status: str,
        delivery_id: str | None = None,
    ) -> DeliveryReservation:
        dedup_key = self._dedup_key(conversation_id, message_id, delivery_id)
        # Lock creation and acquisition happen without an intervening await.
        # Keeping the Redis call inside the per-key critical section also makes
        # the L1 fallback a real provisional owner when Redis raises.
        lock = self._reservation_locks.setdefault(dedup_key, asyncio.Lock())
        async with lock:
            return await self._reserve_locked(
                dedup_key=dedup_key,
                status=status,
            )

    async def _reserve_locked(
        self,
        *,
        dedup_key: str,
        status: str,
    ) -> DeliveryReservation:
        if self.cache.get(dedup_key) is not None:
            return DeliveryReservation(
                DeliveryReservationStatus.ALREADY_DELIVERED, dedup_key
            )
        if self.reservations.get(dedup_key) is not None:
            return DeliveryReservation(DeliveryReservationStatus.IN_FLIGHT, dedup_key)

        normalized_status = status.strip().lower()
        claim_id = self._claim_id_factory()
        if self.redis_kv is not None:
            redis_key = f"{self.config.redis_terminal_key_prefix}{dedup_key}"
            try:
                was_first = await self.redis_kv.setnx(
                    redis_key,
                    claim_id,
                    ttl=self.reservation_ttl_seconds,
                )
            except Exception:
                self.reservations[dedup_key] = _CacheEntry(
                    "reserved", normalized_status, claim_id, l2_owned=False
                )
                return DeliveryReservation(
                    DeliveryReservationStatus.RESERVED,
                    dedup_key,
                    claim_id,
                    l2_owned=False,
                )
            if not was_first:
                try:
                    existing = await self.redis_kv.get(redis_key)
                except Exception:
                    existing = None
                if isinstance(existing, str) and existing.startswith("delivered:"):
                    self.cache[dedup_key] = _CacheEntry("delivered", normalized_status)
                    return DeliveryReservation(
                        DeliveryReservationStatus.ALREADY_DELIVERED, dedup_key
                    )
                return DeliveryReservation(
                    DeliveryReservationStatus.IN_FLIGHT, dedup_key
                )

        l2_owned = self.redis_kv is not None
        self.reservations[dedup_key] = _CacheEntry(
            "reserved", normalized_status, claim_id, l2_owned=l2_owned
        )
        return DeliveryReservation(
            DeliveryReservationStatus.RESERVED,
            dedup_key,
            claim_id,
            l2_owned=l2_owned,
        )

    async def renew(self, reservation: DeliveryReservation) -> bool:
        """Renew an owned reservation while transport fanout is in progress."""
        if (
            reservation.status != DeliveryReservationStatus.RESERVED
            or reservation.claim_id is None
        ):
            return False
        cached = self.reservations.get(reservation.dedup_key)
        if cached is None or cached.claim_id != reservation.claim_id:
            return False
        if cached.l2_owned:
            if self.redis_kv is None:
                return False
            redis_key = (
                f"{self.config.redis_terminal_key_prefix}{reservation.dedup_key}"
            )
            try:
                renewed = await self._await_terminal_redis(
                    self.redis_kv.compare_set(
                        redis_key,
                        reservation.claim_id,
                        reservation.claim_id,
                        ttl=self.reservation_ttl_seconds,
                    ),
                    operation="renew",
                )
            except Exception:
                return False
            if not renewed:
                return False
        self.reservations[reservation.dedup_key] = cached
        return True

    async def confirm(self, reservation: DeliveryReservation) -> bool:
        if (
            reservation.status != DeliveryReservationStatus.RESERVED
            or reservation.claim_id is None
        ):
            return reservation.status == DeliveryReservationStatus.ALREADY_DELIVERED
        cached = self.reservations.get(reservation.dedup_key)
        if (
            cached is None
            or cached.state != "reserved"
            or cached.claim_id != reservation.claim_id
        ):
            return False

        if cached.l2_owned and reservation.l2_owned and self.redis_kv is not None:
            redis_key = (
                f"{self.config.redis_terminal_key_prefix}{reservation.dedup_key}"
            )
            delivered_value = f"delivered:{cached.status}"
            try:
                confirmed = await self._await_terminal_redis(
                    self.redis_kv.compare_set(
                        redis_key,
                        reservation.claim_id,
                        delivered_value,
                        ttl=self.config.terminal_dedup_ttl_seconds,
                    ),
                    operation="confirm",
                )
            except Exception:
                return False
            if not confirmed:
                return False

        self.reservations.pop(reservation.dedup_key, None)
        self.cache[reservation.dedup_key] = _CacheEntry("delivered", cached.status)
        return True

    async def mark_delivered_after_acceptance(
        self,
        reservation: DeliveryReservation,
        *,
        status: str,
    ) -> None:
        """Best-effort persist the transport-accepted delivery boundary.

        Fanout acceptance is irreversible, so a lost reservation or failed CAS
        must not allow another worker to redeliver after the short lease expires.
        L1 is updated before Redis I/O, and Redis failure never escapes to change
        the already-accepted result.
        """
        normalized_status = status.strip().lower()
        cached = self.reservations.get(reservation.dedup_key)
        if cached is not None and cached.claim_id == reservation.claim_id:
            self.reservations.pop(reservation.dedup_key, None)
        self.cache[reservation.dedup_key] = _CacheEntry("delivered", normalized_status)

        if self.redis_kv is None:
            return
        redis_key = f"{self.config.redis_terminal_key_prefix}{reservation.dedup_key}"
        try:
            await self._await_terminal_redis(
                self.redis_kv.set(
                    redis_key,
                    f"delivered:{normalized_status}",
                    ttl=self.config.terminal_dedup_ttl_seconds,
                ),
                operation="mark-delivered",
            )
        except Exception:
            return

    async def release(
        self,
        *,
        conversation_id: str,
        message_id: str | None,
        status: str,
        delivery_id: str | None = None,
        reservation: DeliveryReservation | None = None,
    ) -> None:
        """Release only the caller's unconfirmed reservation."""
        normalized_status = status.strip().lower()
        dedup_key = self._dedup_key(conversation_id, message_id, delivery_id)
        claim = self.reservations.get(dedup_key)
        claim_id = reservation.claim_id if reservation is not None else None
        if (
            claim is None
            or claim.state != "reserved"
            or claim.status != normalized_status
            or (claim_id is not None and claim.claim_id != claim_id)
        ):
            return
        self.reservations.pop(dedup_key, None)

        if self.redis_kv is None or claim.claim_id is None or not claim.l2_owned:
            return
        redis_key = f"{self.config.redis_terminal_key_prefix}{dedup_key}"
        try:
            await self.redis_kv.compare_delete(redis_key, claim.claim_id)
        except Exception:
            return


__all__ = [
    "DeliveryReservation",
    "DeliveryReservationStatus",
    "TerminalStatusDeduplicator",
]
