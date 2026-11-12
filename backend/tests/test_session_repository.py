"""MongoSessionRepository against a mocked collection adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from session.models import SessionRecord
from session.repository.mongo import SESSION_INDEXES, MongoSessionRepository

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _record(**overrides) -> SessionRecord:
    values = {
        "session_id": "sess-1",
        "owner_user_id": "owner-1",
        "client_label": "Buyer agent",
        "creation_request_id": "cr-1",
        "opened_at": NOW,
        "last_activity_at": NOW,
    }
    values.update(overrides)
    return SessionRecord(**values)


def _repository() -> tuple[MongoSessionRepository, MagicMock]:
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    collection.find_one = AsyncMock(return_value=None)
    collection.find_one_and_update = AsyncMock(return_value=None)
    collection.find = AsyncMock(return_value=[])
    collection.create_index = AsyncMock(return_value="idx")
    mongo = SimpleNamespace(collection=lambda name: collection)
    return MongoSessionRepository(mongo), collection


@pytest.mark.asyncio
async def test_ensure_indexes_creates_partial_unique_creation_index():
    repository, collection = _repository()

    await repository.ensure_indexes()

    calls = {
        call.kwargs["name"]: (call.args[0], call.kwargs)
        for call in collection.create_index.await_args_list
    }
    assert calls["session_id_unique"] == (
        [("session_id", 1)],
        {"name": "session_id_unique", "unique": True},
    )
    keys, options = calls["session_creation_request"]
    assert keys == [("owner_user_id", 1), ("creation_request_id", 1)]
    assert options["unique"] is True
    assert options["partialFilterExpression"] == {
        "creation_request_id": {"$type": "string"}
    }
    assert calls["session_owner_status_activity"][0] == [
        ("owner_user_id", 1),
        ("status", 1),
        ("last_activity_at", -1),
    ]
    assert len(SESSION_INDEXES) == 3


@pytest.mark.asyncio
async def test_reserve_inserts_a_new_record():
    repository, collection = _repository()
    record = _record()

    stored = await repository.reserve(record)

    assert stored == record
    inserted = collection.insert_one.await_args.args[0]
    assert inserted["session_id"] == "sess-1"
    assert inserted["conversation_id"] is None
    assert inserted["status"] == "open"


@pytest.mark.asyncio
async def test_reserve_returns_existing_record_on_duplicate_creation_request():
    repository, collection = _repository()
    existing = _record(session_id="sess-existing", conversation_id="conv-1")
    collection.insert_one.side_effect = DuplicateKeyError("dup")
    collection.find_one.return_value = existing.model_dump()

    stored = await repository.reserve(_record(session_id="sess-new"))

    assert stored.session_id == "sess-existing"
    assert collection.find_one.await_args.args[0] == {
        "owner_user_id": "owner-1",
        "creation_request_id": "cr-1",
    }


@pytest.mark.asyncio
async def test_reserve_reraises_duplicate_without_a_matching_record():
    repository, collection = _repository()
    collection.insert_one.side_effect = DuplicateKeyError("dup")

    with pytest.raises(DuplicateKeyError):
        await repository.reserve(_record())


@pytest.mark.asyncio
async def test_set_conversation_writes_only_when_unset():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = _record(
        conversation_id="conv-1"
    ).model_dump()

    stored = await repository.set_conversation_if_unset("sess-1", "conv-1")

    assert stored.conversation_id == "conv-1"
    query, update = collection.find_one_and_update.await_args.args
    assert query == {"session_id": "sess-1", "conversation_id": None}
    assert update == {"$set": {"conversation_id": "conv-1"}}
    assert (
        collection.find_one_and_update.await_args.kwargs["return_document"]
        is ReturnDocument.AFTER
    )


@pytest.mark.asyncio
async def test_set_conversation_loser_reads_the_winner():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = None
    collection.find_one.return_value = _record(
        conversation_id="conv-winner"
    ).model_dump()

    stored = await repository.set_conversation_if_unset("sess-1", "conv-loser")

    assert stored.conversation_id == "conv-winner"


@pytest.mark.asyncio
async def test_record_send_and_close_update_fields():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = _record().model_dump()

    await repository.record_send(
        "sess-1",
        client_request_id="cr-2",
        user_message_id="msg-2",
        event_seq=7,
        at=NOW,
    )
    query, update = collection.find_one_and_update.await_args.args
    # The request is appended only if it is not recorded yet.
    assert query == {
        "session_id": "sess-1",
        "requests.client_request_id": {"$ne": "cr-2"},
    }
    assert update == {
        "$set": {
            "last_client_request_id": "cr-2",
            "last_user_message_id": "msg-2",
            "last_send_event_seq": 7,
            "last_activity_at": NOW,
        },
        # Every sent request is kept for retries and the client's history.
        "$push": {
            "requests": {
                "client_request_id": "cr-2",
                "user_message_id": "msg-2",
                "sent_at": NOW,
            }
        },
    }

    await repository.mark_closed("sess-1", NOW)
    _query, update = collection.find_one_and_update.await_args.args
    assert update == {
        "$set": {"status": "closed", "closed_at": NOW, "last_activity_at": NOW}
    }


@pytest.mark.asyncio
async def test_record_send_of_a_recorded_request_does_not_append_it_again():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = None
    collection.find_one.return_value = _record(
        last_client_request_id="cr-3"
    ).model_dump()

    stored = await repository.record_send(
        "sess-1",
        client_request_id="cr-2",
        user_message_id="msg-2",
        event_seq=7,
        at=NOW,
    )

    # A concurrent send already recorded it; a later request must not be undone.
    assert stored is not None
    assert stored.last_client_request_id == "cr-3"
    assert collection.find_one_and_update.await_count == 1
    assert collection.find_one.await_args.args[0] == {"session_id": "sess-1"}


@pytest.mark.asyncio
async def test_list_for_owner_sorts_by_activity():
    repository, collection = _repository()
    collection.find.return_value = [_record().model_dump()]

    records = await repository.list_for_owner("owner-1", 50)

    assert [r.session_id for r in records] == ["sess-1"]
    assert collection.find.await_args.args[0] == {"owner_user_id": "owner-1"}
    assert collection.find.await_args.kwargs["sort"] == [("last_activity_at", -1)]
    assert collection.find.await_args.kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_list_open_conversations_reads_every_open_session():
    repository, collection = _repository()
    collection.find.return_value = [
        {"session_id": "sess-1", "conversation_id": "conv-1"},
        {"session_id": "sess-2", "conversation_id": "conv-2"},
    ]

    rooms = await repository.list_open_conversations("owner-1")

    assert rooms == {"conv-1": "sess-1", "conv-2": "sess-2"}
    assert collection.find.await_args.args[0] == {
        "owner_user_id": "owner-1",
        "status": "open",
        "conversation_id": {"$type": "string"},
    }
    # Not capped: a waiting session must never fall off this read.
    assert "limit" not in collection.find.await_args.kwargs


@pytest.mark.asyncio
async def test_records_read_back_from_mongo_are_utc_aware():
    # Mongo returns naive datetimes; serialized without a zone, browsers read
    # them as local time.
    repository, collection = _repository()
    naive = datetime(2026, 10, 15, 5, 3, 19)
    collection.find.return_value = [
        {
            **_record().model_dump(),
            "opened_at": naive,
            "last_activity_at": naive,
            "closed_at": naive,
        }
    ]

    [record] = await repository.list_for_owner("owner-1", 50)

    assert record.opened_at == naive.replace(tzinfo=UTC)
    assert record.last_activity_at.tzinfo is UTC
    assert record.closed_at is not None and record.closed_at.tzinfo is UTC
    assert record.model_dump(mode="json")["opened_at"] == "2026-10-15T05:03:19Z"
