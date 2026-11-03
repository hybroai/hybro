import asyncio
import inspect

import pytest

from container import _run_cleanup_steps, _runtime_cleanup_tasks


def test_bounded_cleanup_uses_owned_task_wait_not_wait_for():
    source = inspect.getsource(_run_cleanup_steps)
    assert "asyncio.create_task" in source
    assert "asyncio.wait(" in source
    assert "wait_for" not in source


@pytest.mark.asyncio
async def test_cleanup_steps_preserve_first_failure_and_continue_all_stages():
    calls = []
    first = RuntimeError("delivery close failed")

    async def delivery():
        calls.append("delivery")
        raise first

    async def cancellation():
        calls.append("cancellation")
        raise ValueError("cancellation close failed")

    async def redis():
        calls.append("redis")

    async def mongo():
        calls.append("mongo")

    error = await _run_cleanup_steps(
        [
            ("delivery", delivery),
            ("cancellation", cancellation),
            ("redis", redis),
            ("mongo", mongo),
        ]
    )

    assert error is first
    assert calls == ["delivery", "cancellation", "redis", "mongo"]


@pytest.mark.asyncio
async def test_cleanup_steps_continue_after_cancellation_error():
    calls = []

    async def cancelled():
        calls.append("cancelled")
        raise asyncio.CancelledError

    async def later():
        calls.append("later")

    error = await _run_cleanup_steps([("cancelled", cancelled), ("later", later)])

    assert isinstance(error, asyncio.CancelledError)
    assert calls == ["cancelled", "later"]


@pytest.mark.asyncio
async def test_cleanup_steps_detach_cancel_resistant_timeout_and_continue_order(
    caplog,
):
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def relay():
        calls.append("relay")
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        raise RuntimeError("late relay close failed")

    async def eventing():
        calls.append("eventing")

    async def delivery():
        calls.append("delivery")

    try:
        error = await _run_cleanup_steps(
            [("relay", relay), ("eventing", eventing), ("delivery", delivery)],
            timeout_seconds=0.01,
        )
        await entered.wait()

        assert isinstance(error, TimeoutError)
        assert calls == ["relay", "eventing", "delivery"]
        assert set(_runtime_cleanup_tasks.values()) == {"relay"}
    finally:
        release.set()
        owned = tuple(_runtime_cleanup_tasks)
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
        await asyncio.sleep(0)

    assert _runtime_cleanup_tasks == {}
    [failed] = [
        record
        for record in caplog.records
        if record.getMessage() == "runtime_cleanup_task_failed"
    ]
    assert failed.cleanup_step == "relay"
    assert failed.error_type == "RuntimeError"
