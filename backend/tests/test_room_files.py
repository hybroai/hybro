from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from common.errors import (
    FileStoragePlatformError,
    RetryableFileStoragePlatformError,
)
from conversation.repository import RoomMongoRepository
from room_files import RoomFiles
from room_files.errors import FileOperationError
from tests.fakes.room_files import MemoryFileContentStore


class InMemoryCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["file_id"])

    async def find_one(self, query, **kwargs):
        for doc in self.docs:
            if _matches(doc, query):
                return deepcopy(doc)
        return None

    async def update_one(self, query, update, *, array_filters=None):
        for doc in self.docs:
            if not _matches(doc, query):
                continue
            for key, value in update.get("$set", {}).items():
                if key == "reference_claims.$[claim].state":
                    claim_filter = (array_filters or [{}])[0]
                    for claim in doc.get("reference_claims", []):
                        if _matches(
                            claim,
                            {
                                filter_key.removeprefix("claim."): filter_value
                                for filter_key, filter_value in claim_filter.items()
                            },
                        ):
                            claim["state"] = value
                else:
                    doc[key] = value
            for key, value in update.get("$pull", {}).items():
                doc[key] = [
                    item for item in doc.get(key, []) if not _matches(item, value)
                ]
            for key, value in update.get("$inc", {}).items():
                doc[key] = doc.get(key, 0) + value
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def delete_one(self, query):
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not _matches(doc, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def find(self, query):
        return AsyncCursor([deepcopy(doc) for doc in self.docs if _matches(doc, query)])


def room_lifecycle(rooms):
    """Conversation's room lifecycle store over an in-memory ``rooms``."""
    return RoomMongoRepository(SimpleNamespace(collection=lambda _name: rooms))


class AlwaysValidLeases:
    @asynccontextmanager
    async def hold(self, _conversation_id, _owner):
        yield "test-lease"

    async def assert_valid(self, _conversation_id, _lease_id):
        return None


class UncertainFinalizeCollection(InMemoryCollection):
    def __init__(self):
        super().__init__()
        self._raise_after_ready = True

    async def update_one(self, query, update):
        result = await super().update_one(query, update)
        if (
            self._raise_after_ready
            and query.get("status") == "pending"
            and update.get("$set", {}).get("status") == "ready"
        ):
            self._raise_after_ready = False
            raise RuntimeError("simulated lost finalize acknowledgement")
        return result


class AsyncCursor:
    def __init__(self, docs):
        self._docs = iter(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._docs)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _matches(doc, query):
    for key, value in query.items():
        actual = doc.get(key)
        if isinstance(value, dict) and "$lt" in value:
            if actual is None or _as_utc(actual) >= _as_utc(value["$lt"]):
                return False
        elif isinstance(value, dict) and "$elemMatch" in value:
            if not any(_matches(item, value["$elemMatch"]) for item in actual or []):
                return False
        elif isinstance(value, dict) and "$size" in value:
            if actual is None or len(actual) != value["$size"]:
                return False
        elif actual != value:
            return False
    return True


def _as_utc(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def test_room_files_upload_persists_ready_metadata_and_content():
    now = datetime(2026, 7, 23, tzinfo=UTC)
    file_id = uuid4().hex
    collection = InMemoryCollection()
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        file_id_factory=lambda: file_id,
        now=lambda: now,
        max_upload_bytes=5 * 1024 * 1024,
    )

    uploaded = await files.upload(
        file_bytes=b"hello",
        filename="../../report.txt",
        owner_id="user-1",
        conversation_id="room-1",
        content_type="text/plain",
    )

    assert uploaded.file_id == file_id
    assert uploaded.url == f"/api/v1/files/{file_id}/content"
    assert await files.get_bytes(file_id, max_bytes=5) == b"hello"
    assert collection.docs == [
        {
            "file_id": file_id,
            "room_id": "room-1",
            "owner_id": "user-1",
            "source": "user_upload",
            "file_name": "report.txt",
            "mime_type": "text/plain",
            "size_bytes": 5,
            "sha256": (
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            ),
            "status": "ready",
            "version": 2,
            "reference_claims": [],
            "created_at": now,
            "updated_at": now,
        }
    ]


async def test_reconcile_content_uses_collection_projection_keyword():
    collection = InMemoryCollection()
    collection.docs.append({"file_id": "known-file"})
    content_store = SimpleNamespace(
        list_file_ids=AsyncMock(return_value=["known-file", "orphan-file"]),
        delete=AsyncMock(return_value=True),
    )
    files = RoomFiles(metadata=collection, content=content_store)

    assert await files._reconcile_content() == 1
    content_store.delete.assert_awaited_once_with("orphan-file")


async def test_reference_recovery_uses_message_projection_keyword():
    messages = SimpleNamespace(find_one=AsyncMock(return_value={"_id": "message"}))
    files = RoomFiles(
        metadata=InMemoryCollection(),
        content=MemoryFileContentStore(),
        messages=messages,
    )

    assert await files._message_exists("message-1") is True
    assert await files._has_message_reference("file-1") is True
    assert messages.find_one.await_args_list[0].args == ({"message_id": "message-1"},)
    assert messages.find_one.await_args_list[0].kwargs == {"projection": {"_id": 1}}
    assert messages.find_one.await_args_list[1].args == (
        {"message_content.attachments.file_id": "file-1"},
    )
    assert messages.find_one.await_args_list[1].kwargs == {"projection": {"_id": 1}}


async def test_user_upload_survives_lost_finalize_acknowledgement():
    file_id = uuid4().hex
    collection = UncertainFinalizeCollection()
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        file_id_factory=lambda: file_id,
    )

    uploaded = await files.upload(
        file_bytes=b"hello",
        filename="report.txt",
        owner_id="user-1",
        conversation_id="room-1",
        content_type="text/plain",
    )

    assert uploaded.file_id == file_id
    assert collection.docs[0]["status"] == "ready"
    assert await files.get_bytes(file_id, max_bytes=5) == b"hello"


async def test_agent_artifact_rejects_configured_room_without_write_leases():
    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        room_lifecycle=room_lifecycle(rooms),
        lease_writes=False,
    )

    with pytest.raises(FileStoragePlatformError) as exc_info:
        await files.store_agent_artifact(
            conversation_id="room-1",
            source_message_id="message-1",
            origin_key="origin-1",
            content=b"result",
            file_name="result.txt",
            mime_type="text/plain",
        )

    assert exc_info.value.status_code == 409
    assert collection.docs == []
    assert content_store._contents == {}


async def test_agent_artifact_translates_transient_content_backend_failure():
    class FailingContentStore(MemoryFileContentStore):
        async def write(self, file_id, content, content_type):
            raise FileOperationError("temporary filesystem failure")

    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    files = RoomFiles(
        metadata=collection,
        content=FailingContentStore(),
        room_lifecycle=room_lifecycle(rooms),
    )
    files._leases = AlwaysValidLeases()

    with pytest.raises(RetryableFileStoragePlatformError) as exc_info:
        await files.store_agent_artifact(
            conversation_id="room-1",
            source_message_id="message-1",
            origin_key="origin-1",
            content=b"result",
            file_name="result.txt",
            mime_type="text/plain",
        )

    assert exc_info.value.status_code == 503


async def test_agent_artifact_survives_lost_finalize_acknowledgement():
    file_id = uuid4().hex
    collection = UncertainFinalizeCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        room_lifecycle=room_lifecycle(rooms),
        file_id_factory=lambda: file_id,
    )
    files._leases = AlwaysValidLeases()

    stored = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-1",
        content=b"result",
        file_name="result.txt",
        mime_type="text/plain",
    )

    assert stored["file_id"] == file_id
    assert stored["status"] == "ready"
    assert await files.get_bytes(file_id, max_bytes=6) == b"result"


async def test_room_files_normalizes_mime_types_before_persistence():
    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    files = RoomFiles(
        metadata=collection,
        content=MemoryFileContentStore(),
        room_lifecycle=room_lifecycle(rooms),
    )
    files._leases = AlwaysValidLeases()

    uploaded = await files.upload(
        file_bytes=b"user",
        filename="user.txt",
        owner_id="user-1",
        conversation_id="room-1",
        content_type="Text/Plain; charset=UTF-8",
    )
    agent = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-unsafe-mime",
        content=b"agent",
        file_name="agent.txt",
        mime_type="text/plain\r\nX-Injected: yes",
    )

    assert uploaded.mime_type == "text/plain"
    assert agent["mime_type"] == "application/octet-stream"
    assert [doc["mime_type"] for doc in collection.docs] == [
        "text/plain",
        "application/octet-stream",
    ]


async def test_room_files_rejects_upload_before_writing_metadata_or_content():
    collection = InMemoryCollection()
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        max_upload_bytes=5,
    )

    with pytest.raises(FileStoragePlatformError) as exc_info:
        await files.upload(
            file_bytes=b"123456",
            filename="large.txt",
            owner_id="user-1",
            conversation_id="room-1",
            content_type="text/plain",
        )

    assert exc_info.value.status_code == 413
    assert collection.docs == []


async def test_missing_local_content_does_not_tombstone_shared_metadata():
    now = datetime(2026, 7, 23, tzinfo=UTC)
    file_id = "d" * 32
    collection = InMemoryCollection()
    collection.docs.append(
        {
            "file_id": file_id,
            "room_id": "room-1",
            "owner_id": "user-1",
            "source": "user_upload",
            "file_name": "remote.txt",
            "mime_type": "text/plain",
            "size_bytes": 6,
            "sha256": "missing-locally",
            "status": "ready",
            "version": 2,
            "reference_claims": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    files = RoomFiles(
        metadata=collection,
        content=MemoryFileContentStore(),
        now=lambda: now,
    )

    assert await files.get_ready_file(file_id, owner_id="user-1") is None
    assert (
        await files.prepare_download(
            file_id,
            owner_id="user-1",
            chunk_size=1024,
        )
        is None
    )
    assert await files.recover() == 0
    assert collection.docs[0]["status"] == "ready"
    assert collection.docs[0]["version"] == 2
    assert "delete_reason" not in collection.docs[0]


async def test_room_files_materializes_agent_artifact_idempotently():
    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        room_lifecycle=room_lifecycle(rooms),
    )
    files._leases = AlwaysValidLeases()

    first = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-1",
        content=b"result",
        file_name="result.txt",
        mime_type="text/plain",
    )
    second = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-1",
        content=b"result",
        file_name="result.txt",
        mime_type="text/plain",
    )

    assert first["file_id"] == second["file_id"]
    assert first["source"] == "agent_artifact"
    assert first["owner_id"] == "user-1"
    assert len(collection.docs) == 1


async def test_room_files_resumes_pending_agent_artifact_without_duplicate_origin():
    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    content_store = MemoryFileContentStore()
    file_id = "a" * 32
    digest = "f6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68"
    collection.docs.append(
        {
            "file_id": file_id,
            "room_id": "room-1",
            "owner_id": "user-1",
            "source": "agent_artifact",
            "source_message_id": "message-1",
            "origin_key": "origin-1",
            "file_name": "result.txt",
            "mime_type": "text/plain",
            "size_bytes": 6,
            "sha256": digest,
            "status": "pending",
            "version": 1,
        }
    )
    await content_store.write(file_id, b"result", "text/plain")
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        room_lifecycle=room_lifecycle(rooms),
    )
    files._leases = AlwaysValidLeases()

    stored = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-1",
        content=b"result",
        content_sha256=digest,
        file_name="result.txt",
        mime_type="text/plain",
    )

    assert stored["file_id"] == file_id
    assert stored["status"] == "ready"
    assert len(collection.docs) == 1
    assert collection.docs[0]["status"] == "ready"


async def test_room_files_deletes_only_superseded_agent_artifacts():
    ids = iter(["a" * 32, "b" * 32])
    collection = InMemoryCollection()
    rooms = InMemoryCollection()
    rooms.docs.append({"room_id": "room-1", "room_owner_id": "user-1"})
    content_store = MemoryFileContentStore()
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        room_lifecycle=room_lifecycle(rooms),
        file_id_factory=lambda: next(ids),
    )
    files._leases = AlwaysValidLeases()
    old = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-old",
        content=b"old",
        file_name="old.txt",
        mime_type="text/plain",
    )
    new = await files.store_agent_artifact(
        conversation_id="room-1",
        source_message_id="message-1",
        origin_key="origin-new",
        content=b"new",
        file_name="new.txt",
        mime_type="text/plain",
    )

    deleted = await files.delete_superseded_agent_artifacts(
        conversation_id="room-1",
        source_message_id="message-1",
        file_ids={old["file_id"]},
    )

    assert deleted == 1
    assert await files.get_bytes(old["file_id"], max_bytes=3) is None
    assert await files.get_bytes(new["file_id"], max_bytes=3) == b"new"


async def test_reference_recovery_commits_stale_naive_claim_when_message_exists():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    collection = InMemoryCollection()
    collection.docs.append(
        {
            "file_id": "stale-claim",
            "source": "user_upload",
            "status": "ready",
            "version": 1,
            "reference_claims": [
                {
                    "message_id": "message-1",
                    "state": "pending",
                    "claimed_at": datetime(2026, 7, 28),
                }
            ],
            "created_at": now - timedelta(days=4),
            "updated_at": now - timedelta(days=4),
        }
    )
    messages = SimpleNamespace(find_one=AsyncMock(return_value={"_id": "message-1"}))
    files = RoomFiles(
        metadata=collection,
        content=MemoryFileContentStore(),
        messages=messages,
        now=lambda: now,
    )

    recovered = await files._recover_reference_claims(now - timedelta(hours=24))

    assert recovered == 1
    assert collection.docs[0]["reference_claims"][0]["state"] == "committed"


async def test_recovery_removes_stale_naive_claim_and_deletes_orphan():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    file_id = "e" * 32
    collection = InMemoryCollection()
    collection.docs.append(
        {
            "file_id": file_id,
            "source": "user_upload",
            "status": "ready",
            "version": 1,
            "reference_claims": [
                {
                    "message_id": "missing-message",
                    "state": "pending",
                    "claimed_at": datetime(2026, 7, 28),
                }
            ],
            "created_at": now - timedelta(days=4),
            "updated_at": now - timedelta(days=4),
        }
    )
    content_store = MemoryFileContentStore()
    await content_store.write(file_id, b"orphan", "text/plain")
    messages = SimpleNamespace(find_one=AsyncMock(return_value=None))
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        messages=messages,
        now=lambda: now,
    )

    recovered = await files.recover()

    assert recovered == 2
    assert collection.docs == []
    assert await content_store.read(file_id, max_bytes=6) is None


async def test_reference_recovery_only_updates_stale_claims():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    collection = InMemoryCollection()
    collection.docs.append(
        {
            "file_id": "mixed-claims",
            "source": "user_upload",
            "status": "ready",
            "version": 1,
            "reference_claims": [
                {
                    "message_id": "old-message",
                    "state": "pending",
                    "claimed_at": datetime(2026, 7, 28),
                },
                {
                    "message_id": "new-message",
                    "state": "pending",
                    "claimed_at": datetime(2026, 7, 31, 12),
                },
            ],
            "created_at": now - timedelta(days=4),
            "updated_at": now - timedelta(days=4),
        }
    )
    messages = SimpleNamespace(find_one=AsyncMock(return_value={"_id": "message"}))
    files = RoomFiles(
        metadata=collection,
        content=MemoryFileContentStore(),
        messages=messages,
        now=lambda: now,
    )

    recovered = await files._recover_reference_claims(now - timedelta(hours=24))

    assert recovered == 1
    assert [claim["state"] for claim in collection.docs[0]["reference_claims"]] == [
        "committed",
        "pending",
    ]


async def test_recovery_removes_crash_orphaned_agent_artifact():
    now = datetime(2026, 7, 23, tzinfo=UTC)
    orphan_file_id = "c" * 32
    collection = InMemoryCollection()
    collection.docs.append(
        {
            "file_id": orphan_file_id,
            "room_id": "room-1",
            "owner_id": "user-1",
            "source": "agent_artifact",
            "source_message_id": "message-1",
            "origin_key": "origin-orphan",
            "file_name": "orphan.txt",
            "mime_type": "text/plain",
            "size_bytes": 6,
            "sha256": "sha",
            "status": "ready",
            "version": 2,
            "reference_claims": [],
            "created_at": now - timedelta(hours=25),
            "updated_at": now - timedelta(hours=25),
        }
    )
    content_store = MemoryFileContentStore()
    await content_store.write(orphan_file_id, b"orphan", "text/plain")
    agent_messages = SimpleNamespace(find_one=AsyncMock(return_value=None))
    files = RoomFiles(
        metadata=collection,
        content=content_store,
        agent_messages=agent_messages,
        now=lambda: now,
    )

    recovered = await files._recover_superseded_agent_artifacts(
        now - timedelta(hours=24)
    )

    assert recovered == 1
    assert collection.docs == []
    assert await content_store.read(orphan_file_id, max_bytes=6) is None
