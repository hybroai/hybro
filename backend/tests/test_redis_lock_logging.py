"""Redis lock failures are logged as ``redis_lock_operation_failed``."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from dal.redis.lock import (
    DistributedLockImpl,
    LeaderElectorImpl,
    RoomRedisDistributedLock,
)


class _RedisDown(ConnectionError):
    pass


class _FailingClient:
    async def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise _RedisDown("redis://user:secret@redis:6379 unreachable")

    async def eval(self, *_args: Any, **_kwargs: Any) -> Any:
        raise _RedisDown("redis unreachable")

    async def aclose(self) -> None:
        raise _RedisDown("redis unreachable")


def _failures(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "dal.redis.lock"
        and record.getMessage() == "redis_lock_operation_failed"
    ]


async def _leader_try_acquire() -> Any:
    return await LeaderElectorImpl(
        _FailingClient(), instance_id="instance-a"
    ).try_acquire("stale_task_checker", ttl_seconds=10)


async def _leader_renew() -> Any:
    return await LeaderElectorImpl(_FailingClient(), instance_id="instance-a").renew(
        "stale_task_checker", ttl_seconds=10
    )


async def _leader_release() -> Any:
    return await LeaderElectorImpl(_FailingClient(), instance_id="instance-a").release(
        "stale_task_checker"
    )


async def _lock_acquire() -> Any:
    return await DistributedLockImpl(client=_FailingClient()).acquire(
        "upload-42", "owner-a", ttl=10
    )


async def _lock_renew() -> Any:
    return await DistributedLockImpl(client=_FailingClient()).renew(
        "upload-42", "owner-a", ttl=10
    )


async def _lock_release() -> Any:
    return await DistributedLockImpl(client=_FailingClient()).release(
        "upload-42", "owner-a"
    )


async def _room_acquire() -> Any:
    return await RoomRedisDistributedLock(_FailingClient()).acquire(
        "room-7", "owner-a", 10
    )


async def _room_renew() -> Any:
    return await RoomRedisDistributedLock(_FailingClient()).renew(
        "room-7", "owner-a", 10
    )


async def _room_release() -> Any:
    return await RoomRedisDistributedLock(_FailingClient()).release("room-7", "owner-a")


CASES = [
    (_leader_try_acquire, "try_acquire", "leader", "stale_task_checker", False),
    (_leader_renew, "renew", "leader", "stale_task_checker", False),
    (_leader_release, "release", "leader", "stale_task_checker", None),
    (_lock_acquire, "acquire", "lock", "upload-42", False),
    (_lock_renew, "renew", "lock", "upload-42", False),
    (_lock_release, "release", "lock", "upload-42", False),
    (_room_acquire, "acquire", "room", "room-7", None),
    (_room_renew, "renew", "room", "room-7", None),
    (_room_release, "release", "room", "room-7", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "operation", "lock_kind", "lock_name", "expected"),
    CASES,
    ids=[f"{kind}-{operation}" for _, operation, kind, _, _ in CASES],
)
async def test_lock_operation_failure_is_logged_and_return_value_unchanged(
    caplog, call, operation, lock_kind, lock_name, expected
):
    caplog.set_level(logging.DEBUG)

    result = await call()

    assert result is expected
    [record] = _failures(caplog)
    assert record.levelno == logging.WARNING
    assert record.operation == operation
    assert record.lock_kind == lock_kind
    assert record.lock_name == lock_name
    assert record.error_type == "_RedisDown"
    assert "secret" not in str(record.__dict__)


@pytest.mark.asyncio
async def test_close_failure_is_logged_without_a_lock_name(caplog):
    caplog.set_level(logging.DEBUG)
    lock = DistributedLockImpl(client=_FailingClient())

    await lock.close()

    [record] = _failures(caplog)
    assert record.levelno == logging.WARNING
    assert record.operation == "close"
    assert record.lock_kind == "client"
    assert record.error_type == "_RedisDown"
    assert not hasattr(record, "lock_name")
    assert lock._client is None


@pytest.mark.asyncio
async def test_successful_operations_log_nothing(caplog):
    class _Client:
        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def eval(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

        async def aclose(self) -> None:
            return None

    caplog.set_level(logging.DEBUG)
    leader = LeaderElectorImpl(_Client(), instance_id="instance-a")

    assert await leader.try_acquire("stale_task_checker", ttl_seconds=10) is True
    assert await leader.renew("stale_task_checker", ttl_seconds=10) is True
    await leader.release("stale_task_checker")
    await leader.close()

    assert _failures(caplog) == []
