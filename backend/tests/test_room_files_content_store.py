from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from room_files import FileConflictError, FileTooLargeError, LocalFileContentStore
from tests.fakes.room_files import MemoryFileContentStore


@pytest.fixture(params=["memory", "local"])
def content_store(request, tmp_path):
    if request.param == "memory":
        return MemoryFileContentStore()
    return LocalFileContentStore(tmp_path)


async def test_content_store_round_trips_bytes_through_public_interface(content_store):
    file_id = uuid4().hex

    await content_store.write(file_id, b"hello room", "text/plain")

    assert await content_store.read(file_id, max_bytes=32) == b"hello room"
    assert b"".join([chunk async for chunk in content_store.stream(file_id, 3)]) == (
        b"hello room"
    )


async def test_content_store_create_is_conflict_safe(content_store):
    file_id = uuid4().hex
    await content_store.write(file_id, b"first", "text/plain")

    with pytest.raises(FileConflictError):
        await content_store.write(file_id, b"replacement", "text/plain")

    assert await content_store.read(file_id, max_bytes=32) == b"first"


async def test_content_store_enforces_read_limit(content_store):
    file_id = uuid4().hex
    await content_store.write(file_id, b"12345", "application/octet-stream")

    with pytest.raises(FileTooLargeError):
        await content_store.read(file_id, max_bytes=4)


async def test_content_store_delete_is_idempotent(content_store):
    file_id = uuid4().hex
    await content_store.write(file_id, b"x", "application/octet-stream")

    assert await content_store.delete(file_id) is True
    assert await content_store.delete(file_id) is False
    assert await content_store.read(file_id, max_bytes=1) is None


async def test_prepared_stream_survives_concurrent_delete(content_store):
    file_id = uuid4().hex
    await content_store.write(file_id, b"durable", "application/octet-stream")

    prepared = await content_store.prepare_stream(
        file_id,
        3,
        expected_size=7,
    )
    assert prepared is not None
    assert await content_store.delete(file_id) is True

    assert b"".join([chunk async for chunk in prepared]) == b"durable"


async def test_prepared_stream_can_close_before_first_iteration(content_store):
    file_id = uuid4().hex
    await content_store.write(file_id, b"durable", "application/octet-stream")

    prepared = await content_store.prepare_stream(
        file_id,
        3,
        expected_size=7,
    )
    assert prepared is not None

    await prepared.aclose()
    await prepared.aclose()

    assert b"".join([chunk async for chunk in prepared]) == b""


async def test_outer_stream_early_close_closes_prepared_stream(
    content_store,
    monkeypatch,
):
    file_id = uuid4().hex
    await content_store.write(file_id, b"durable", "application/octet-stream")
    prepared = await content_store.prepare_stream(file_id, 3, expected_size=7)
    assert prepared is not None

    async def prepare_stream(*args, **kwargs):
        del args, kwargs
        return prepared

    monkeypatch.setattr(content_store, "prepare_stream", prepare_stream)
    outer = content_store.stream(file_id, 3)

    assert await anext(outer) == b"dur"
    await outer.aclose()

    with pytest.raises(StopAsyncIteration):
        await anext(prepared)


async def test_content_store_rejects_noncanonical_file_ids(content_store):
    with pytest.raises(ValueError):
        await content_store.write("../escape", b"x", "text/plain")


async def test_local_store_concurrent_publish_has_one_winner(tmp_path):
    store = LocalFileContentStore(tmp_path)
    file_id = uuid4().hex
    results = await asyncio.gather(
        store.write(file_id, b"one", "text/plain"),
        store.write(file_id, b"two", "text/plain"),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, FileConflictError) for result in results) == 1
    assert await store.read(file_id, max_bytes=3) in {b"one", b"two"}
