from __future__ import annotations

import os
import socket
from typing import Any

import redis.asyncio as aioredis

from common.config import settings
from common.observability import events as E
from common.observability import get_logger

log = get_logger(__name__)

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class _RedisOwnerClient:
    def __init__(self, *, client: Any | None = None, url: str | None = None) -> None:
        self._client = client
        self._url = settings.redis_url if url is None else url

    def _ensure_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self._url:
            return None
        kwargs: dict[str, Any] = {"decode_responses": True}
        max_connections = getattr(settings, "redis_max_connections", None)
        if max_connections is not None:
            kwargs["max_connections"] = max_connections
        self._client = aioredis.from_url(self._url, **kwargs)
        return self._client

    @staticmethod
    def _log_failure(
        operation: str,
        lock_kind: str,
        exc: Exception,
        lock_name: str | None = None,
    ) -> None:
        fields = {} if lock_name is None else {"lock_name": lock_name}
        log.emit(
            E.REDIS_LOCK_OPERATION_FAILED,
            exc=exc,
            operation=operation,
            lock_kind=lock_kind,
            **fields,
        )

    async def _owner_eval(
        self,
        script: str,
        key: str,
        owner: str,
        *args: str,
        operation: str,
        lock_kind: str,
        lock_name: str,
    ) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            result = await client.eval(script, 1, key, owner, *args)
            return result == 1
        except Exception as exc:
            self._log_failure(operation, lock_kind, exc, lock_name)
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                self._log_failure("close", "client", exc)
        self._client = None


class DistributedLockImpl(_RedisOwnerClient):
    """Short-lived Redis lock with owner-checked release and renew."""

    async def acquire(self, key: str, owner: str, ttl: int = 60) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            return bool(await client.set(f"lock:{key}", owner, nx=True, ex=ttl))
        except Exception as exc:
            self._log_failure("acquire", "lock", exc, key)
            return False

    async def release(self, key: str, owner: str) -> bool:
        return await self._owner_eval(
            _RELEASE_SCRIPT,
            f"lock:{key}",
            owner,
            operation="release",
            lock_kind="lock",
            lock_name=key,
        )

    async def renew(self, key: str, owner: str, ttl: int = 60) -> bool:
        return await self._owner_eval(
            _RENEW_SCRIPT,
            f"lock:{key}",
            owner,
            str(ttl),
            operation="renew",
            lock_kind="lock",
            lock_name=key,
        )


class RoomRedisDistributedLock(_RedisOwnerClient):
    """Room-scoped Redis lock with tri-state acquire for graceful fallback."""

    _ROOM_LOCK_PREFIX = "room:lock:"

    def __init__(
        self,
        client: Any | None = None,
        *,
        url: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(client=client, url=url)
        self._enabled = enabled

    async def acquire(self, conversation_id: str, owner: str, ttl: int) -> bool | None:
        if not self._enabled:
            return None
        client = self._ensure_client()
        if client is None:
            return None
        try:
            result = await client.set(
                f"{self._ROOM_LOCK_PREFIX}{conversation_id}",
                owner,
                nx=True,
                ex=ttl,
            )
            return bool(result)
        except Exception as exc:
            self._log_failure("acquire", "room", exc, conversation_id)
            return None

    async def renew(self, conversation_id: str, owner: str, ttl: int) -> bool | None:
        if not self._enabled:
            return None
        client = self._ensure_client()
        if client is None:
            return None
        try:
            result = await client.eval(
                _RENEW_SCRIPT,
                1,
                f"{self._ROOM_LOCK_PREFIX}{conversation_id}",
                owner,
                str(ttl),
            )
            return result == 1
        except Exception as exc:
            self._log_failure("renew", "room", exc, conversation_id)
            return None

    async def release(self, conversation_id: str, owner: str) -> None:
        if not self._enabled:
            return
        await self._owner_eval(
            _RELEASE_SCRIPT,
            f"{self._ROOM_LOCK_PREFIX}{conversation_id}",
            owner,
            operation="release",
            lock_kind="room",
            lock_name=conversation_id,
        )


class LeaderElectorImpl(_RedisOwnerClient):
    """Long-lived Redis leader election with owner-checked renewal."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        instance_id: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(client=client, url=url)
        self._instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}"

    async def try_acquire(
        self, job_name: str, ttl: int = 60, *, ttl_seconds: int | None = None
    ) -> bool:
        resolved_ttl = ttl if ttl_seconds is None else ttl_seconds
        client = self._ensure_client()
        if client is None:
            return False
        try:
            return bool(
                await client.set(
                    f"leader:{job_name}",
                    self._instance_id,
                    nx=True,
                    ex=resolved_ttl,
                )
            )
        except Exception as exc:
            self._log_failure("try_acquire", "leader", exc, job_name)
            return False

    async def renew(
        self, job_name: str, ttl: int = 60, *, ttl_seconds: int | None = None
    ) -> bool:
        resolved_ttl = ttl if ttl_seconds is None else ttl_seconds
        return await self._owner_eval(
            _RENEW_SCRIPT,
            f"leader:{job_name}",
            self._instance_id,
            str(resolved_ttl),
            operation="renew",
            lock_kind="leader",
            lock_name=job_name,
        )

    async def release(self, job_name: str) -> None:
        await self._owner_eval(
            _RELEASE_SCRIPT,
            f"leader:{job_name}",
            self._instance_id,
            operation="release",
            lock_kind="leader",
            lock_name=job_name,
        )

    async def release_all(self, job_names: list[str]) -> None:
        for job_name in job_names:
            await self.release(job_name)
