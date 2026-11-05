import asyncio
import inspect
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.errors import TransientError
from common.protocols import MongoChangeStream


class FakeMongoChangeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_mongo_collection_adapter_maps_basic_operations():
    from dal.mongo.client import MongoCollectionAdapter

    collection = MagicMock()
    collection.find_one = AsyncMock(return_value={"_id": "1"})
    collection.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id="id1"))
    collection.insert_many = AsyncMock(
        return_value=SimpleNamespace(inserted_ids=["id1", "id2"])
    )
    collection.update_one = AsyncMock(
        return_value=SimpleNamespace(modified_count=0, upserted_id="up1")
    )
    collection.update_many = AsyncMock(return_value=SimpleNamespace(modified_count=2))
    collection.delete_one = AsyncMock(return_value=SimpleNamespace(deleted_count=1))
    collection.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=3))
    collection.count_documents = AsyncMock(return_value=4)
    collection.create_index = AsyncMock(return_value="idx")
    collection.index_information = AsyncMock(return_value={"idx": {}})
    collection.drop_index = AsyncMock()
    watcher = FakeMongoChangeStream()
    collection.watch.return_value = watcher

    adapter = MongoCollectionAdapter(collection)

    assert await adapter.find_one({"a": 1}) == {"_id": "1"}
    assert await adapter.insert_one({"a": 1}) == "id1"
    assert await adapter.insert_many([{"a": 1}, {"a": 2}]) == ["id1", "id2"]
    assert await adapter.update_one({"a": 1}, {"$set": {"b": 2}}, upsert=True) is True
    assert await adapter.update_many({"a": 1}, {"$set": {"b": 2}}) == 2
    assert await adapter.delete_one({"a": 1}) is True
    assert await adapter.delete_many({"a": 1}) == 3
    assert await adapter.count({"a": 1}) == 4
    assert await adapter.create_index([("a", 1)], unique=True) == "idx"
    assert await adapter.index_information() == {"idx": {}}
    await adapter.drop_index("idx")
    async with adapter.watch() as stream:
        assert stream is watcher

    hints = get_type_hints(MongoCollectionAdapter.watch)
    assert hints["return"] is MongoChangeStream


@pytest.mark.asyncio
async def test_mongo_collection_adapter_replace_reports_matched_identical_document():
    from dal.mongo.client import MongoCollectionAdapter

    collection = MagicMock()
    collection.replace_one = AsyncMock(
        return_value=SimpleNamespace(
            matched_count=1,
            modified_count=0,
            upserted_id=None,
        )
    )
    adapter = MongoCollectionAdapter(collection)

    assert await adapter.replace_one({"_id": "same"}, {"_id": "same"}) is True


@pytest.mark.asyncio
async def test_mongo_collection_adapter_replace_reports_unmatched_document():
    from dal.mongo.client import MongoCollectionAdapter

    collection = MagicMock()
    collection.replace_one = AsyncMock(
        return_value=SimpleNamespace(
            matched_count=0,
            modified_count=0,
            upserted_id=None,
        )
    )
    adapter = MongoCollectionAdapter(collection)

    assert await adapter.replace_one({"_id": "missing"}, {"_id": "missing"}) is False


@pytest.mark.asyncio
async def test_mongo_collection_adapter_materializes_find_and_aggregate():
    from dal.mongo.client import MongoCollectionAdapter

    find_cursor = MagicMock()
    find_cursor.sort.return_value = find_cursor
    find_cursor.skip.return_value = find_cursor
    find_cursor.limit.return_value = find_cursor
    find_cursor.to_list = AsyncMock(return_value=[{"a": 1}])

    aggregate_cursor = MagicMock()
    aggregate_cursor.to_list = AsyncMock(return_value=[{"total": 2}])

    collection = MagicMock()
    collection.find.return_value = find_cursor
    collection.aggregate.return_value = aggregate_cursor

    adapter = MongoCollectionAdapter(collection)

    result = await adapter.find(
        {"a": 1},
        projection={"a": 1},
        sort=[("a", -1)],
        skip=5,
        limit=10,
    )
    aggregate = await adapter.aggregate([{"$match": {"a": 1}}])

    assert result == [{"a": 1}]
    assert aggregate == [{"total": 2}]
    collection.find.assert_called_once_with({"a": 1}, projection={"a": 1})
    find_cursor.sort.assert_called_once_with([("a", -1)])
    find_cursor.skip.assert_called_once_with(5)
    find_cursor.limit.assert_called_once_with(10)
    find_cursor.to_list.assert_awaited_once_with(length=10)
    aggregate_cursor.to_list.assert_awaited_once_with(length=1000)


@pytest.mark.asyncio
async def test_ensure_runtime_indexes_uses_mongo_dal_specs():
    from container import ensure_runtime_indexes

    collections: dict[str, MagicMock] = {}

    def _collection(name: str):
        if name not in collections:
            collection = MagicMock()
            collection.create_index = AsyncMock(return_value=f"{name}_idx")
            collection.index_information = AsyncMock(return_value={})
            collection.drop_index = AsyncMock()
            collection.aggregate = AsyncMock(return_value=[])
            collection.find_one = AsyncMock(return_value=None)
            collection.update_one = AsyncMock(return_value=True)
            collections[name] = collection
        return collections[name]

    mongo = MagicMock()
    mongo.collection.side_effect = _collection
    collections["agents"] = _collection("agents")
    collections["agents"].index_information.return_value = {
        "unique_normalized_url": {
            "partialFilterExpression": {"normalized_url": {"$exists": True}}
        }
    }

    await ensure_runtime_indexes(mongo=mongo)

    assert set(collections) >= {
        "agent_capability_issues",
        "agent_groups",
        "agents",
        "conversation_content",
        "migration_markers",
        "room_agent_messages",
        "room_user_messages",
        "room_memories",
        "room_quotes",
    }
    assert not {"runs", "run_events"} & set(collections)
    assert not {"orchestration_runs", "orchestration_run_events"} & set(collections)
    collections["agents"].drop_index.assert_awaited_once_with("unique_normalized_url")
    assert _has_create_index(
        collections["agents"],
        [("normalized_url", 1)],
        unique=True,
        name="unique_normalized_url",
        partialFilterExpression={"normalized_url": {"$type": "string"}},
    )
    assert _has_create_index(
        collections["agents"],
        [
            ("agent_card.name", "text"),
            ("agent_card.skills.name", "text"),
            ("agent_card.skills.tags", "text"),
            ("agent_card.description", "text"),
            ("agent_card.skills.description", "text"),
        ],
        unique=False,
        name="agent_lexical_text",
        weights={
            "agent_card.name": 10,
            "agent_card.skills.name": 8,
            "agent_card.skills.tags": 6,
            "agent_card.description": 3,
            "agent_card.skills.description": 3,
        },
    )
    assert _has_create_index(
        collections["agent_groups"],
        [("group_id", 1)],
        unique=True,
        name="agent_group_id_unique",
    )
    assert _has_create_index(
        collections["conversation_content"],
        [("room_id", 1), ("turn_id", 1)],
        unique=True,
        name="room_turn_unique",
    )
    content_index_names = {
        call.kwargs.get("name")
        for call in collections["conversation_content"].create_index.call_args_list
    }
    assert "turn_notes_text" not in content_index_names
    assert "cancelled_messages" not in collections
    task_index_names = {
        call.kwargs.get("name")
        for call in collections["room_agent_messages"].create_index.call_args_list
    }
    assert not task_index_names & {
        "has_task_tracking_sparse",
        "task_updated_state_sparse",
        "task_created_state_sparse",
        "user_task_state_sparse",
        "room_task_created_sparse",
    }
    assert _has_create_index(
        collections["room_agent_messages"],
        [("message_id", 1)],
        unique=True,
        name="room_agent_message_id_unique",
    )
    assert _has_create_index(
        collections["room_user_messages"],
        [("message_id", 1)],
        unique=True,
        name="room_user_message_id_unique",
    )
    assert _has_create_index(
        collections["room_user_messages"],
        [("room_id", 1), ("client_request_id", 1)],
        unique=True,
        name="room_user_client_request_id_unique",
        partialFilterExpression={
            "room_id": {"$type": "string"},
            "client_request_id": {"$type": "string"},
        },
    )
    assert collections["room_user_messages"].aggregate.await_count == 7
    assert collections["room_agent_messages"].aggregate.await_count == 2
    assert _has_create_index(
        collections["room_user_messages"],
        [("room_id", 1), ("timeline_sort_us", -1), ("message_id", -1)],
        unique=False,
        name="room_user_timeline_desc",
    )
    assert _has_create_index(
        collections["room_agent_messages"],
        [("room_id", 1), ("timeline_sort_us", -1), ("message_id", -1)],
        unique=False,
        name="room_agent_timeline_desc",
    )
    marker_update = collections["migration_markers"].update_one.await_args
    assert marker_update.args[0] == {"_id": "room_timeline_sort_keys_v1"}
    assert marker_update.kwargs == {"upsert": True}
    assert all(
        call.args[0][-1] == {"$limit": 5}
        for call in collections["room_user_messages"].aggregate.await_args_list
    )
    invalid_client_pipeline = (
        collections["room_user_messages"].aggregate.await_args_list[3].args[0]
    )
    assert "$strLenCP" in repr(invalid_client_pipeline)
    assert "128" in repr(invalid_client_pipeline)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "critical_index_name",
    [
        "room_user_message_id_unique",
        "room_user_client_request_id_unique",
        "room_agent_message_id_unique",
        "room_user_timeline_desc",
        "room_agent_timeline_desc",
    ],
)
async def test_ensure_runtime_indexes_raises_for_critical_unique_index_failures(
    critical_index_name: str,
):
    from container import ensure_runtime_indexes

    collections: dict[str, MagicMock] = {}

    def _collection(name: str):
        if name not in collections:
            collection = MagicMock()

            async def create_index(_keys, **kwargs):
                if kwargs.get("name") == critical_index_name:
                    raise ValueError("index failure")
                return f"{name}_idx"

            collection.create_index = AsyncMock(side_effect=create_index)
            collection.index_information = AsyncMock(return_value={})
            collection.drop_index = AsyncMock()
            collection.aggregate = AsyncMock(return_value=[])
            collection.find_one = AsyncMock(return_value=None)
            collection.update_one = AsyncMock(return_value=True)
            collections[name] = collection
        return collections[name]

    mongo = MagicMock()
    mongo.collection.side_effect = _collection

    with pytest.raises(RuntimeError, match="Critical index creation failed"):
        await ensure_runtime_indexes(mongo=mongo)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_check_index", "expected_error"),
    [
        (0, "duplicate non-empty message_id"),
        (1, "missing, null, non-string, or empty message_id"),
        (2, "duplicate \\(room_id, normalized client_request_id\\)"),
        (3, "invalid or non-normalized client_request_id"),
        (4, "missing, null, non-string, or empty room_id"),
    ],
)
async def test_user_message_index_readiness_blocks_historical_conflicts(
    failed_check_index: int,
    expected_error: str,
):
    from conversation.repository.indexes import (
        ensure_user_message_indexes as _ensure_user_message_indexes,
    )

    collection = MagicMock()
    responses = [[], [], [], [], []]
    responses[failed_check_index] = [
        {
            "message_id": "message-duplicate",
            "room_id": "room-1",
            "client_request_id": "request-1",
            "occurrences": 2,
        }
    ]
    collection.aggregate = AsyncMock(side_effect=responses)
    collection.create_index = AsyncMock()
    mongo = MagicMock()
    mongo.collection.return_value = collection

    with pytest.raises(RuntimeError, match=expected_error):
        await _ensure_user_message_indexes(mongo)

    collection.create_index.assert_not_awaited()
    assert collection.aggregate.await_count == 5


@pytest.mark.asyncio
async def test_user_message_index_readiness_passes_then_creates_both_indexes():
    from conversation.repository.indexes import (
        ensure_user_message_indexes as _ensure_user_message_indexes,
    )

    collection = MagicMock()
    collection.aggregate = AsyncMock(return_value=[])
    collection.create_index = AsyncMock(return_value="index")
    mongo = MagicMock()
    mongo.collection.return_value = collection

    await _ensure_user_message_indexes(mongo)

    assert collection.aggregate.await_count == 5
    assert _has_create_index(
        collection,
        [("message_id", 1)],
        unique=True,
        name="room_user_message_id_unique",
    )
    assert _has_create_index(
        collection,
        [("room_id", 1), ("client_request_id", 1)],
        unique=True,
        name="room_user_client_request_id_unique",
        partialFilterExpression={
            "room_id": {"$type": "string"},
            "client_request_id": {"$type": "string"},
        },
    )


@pytest.mark.asyncio
async def test_room_timeline_readiness_blocks_invalid_rows_before_indexes():
    from conversation.repository.indexes import (
        ensure_room_timeline_indexes as _ensure_room_timeline_indexes,
    )

    user_collection = MagicMock()
    user_collection.aggregate = AsyncMock(
        side_effect=[[], [{"message_id": "missing-timeline"}]]
    )
    user_collection.find_one = AsyncMock(return_value={"_id": "user-row"})
    user_collection.create_index = AsyncMock()
    agent_collection = MagicMock()
    agent_collection.aggregate = AsyncMock(return_value=[])
    agent_collection.find_one = AsyncMock(return_value=None)
    agent_collection.create_index = AsyncMock()
    mongo = MagicMock()
    mongo.collection.side_effect = lambda name: {
        "room_user_messages": user_collection,
        "room_agent_messages": agent_collection,
    }[name]

    with pytest.raises(RuntimeError, match="migrate_room_timeline_sort_keys"):
        await _ensure_room_timeline_indexes(mongo)

    pipeline = user_collection.aggregate.await_args.args[0]
    assert pipeline[-2] == {"$project": {"_id": 0, "message_id": 1}}
    assert pipeline[-1] == {"$limit": 5}
    user_collection.create_index.assert_not_awaited()
    agent_collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_timeline_readiness_requires_final_audit_marker_for_existing_rows():
    from conversation.repository.indexes import (
        ensure_room_timeline_indexes as _ensure_room_timeline_indexes,
    )

    user_collection = MagicMock()
    user_collection.aggregate = AsyncMock(return_value=[])
    user_collection.find_one = AsyncMock(return_value={"_id": "user-row"})
    user_collection.create_index = AsyncMock()
    agent_collection = MagicMock()
    agent_collection.aggregate = AsyncMock(return_value=[])
    agent_collection.find_one = AsyncMock(return_value=None)
    agent_collection.create_index = AsyncMock()
    markers = MagicMock()
    markers.find_one = AsyncMock(return_value=None)
    mongo = MagicMock()
    mongo.collection.side_effect = lambda name: {
        "room_user_messages": user_collection,
        "room_agent_messages": agent_collection,
        "migration_markers": markers,
    }[name]

    with pytest.raises(RuntimeError, match="final migration audit"):
        await _ensure_room_timeline_indexes(mongo)

    markers.find_one.assert_awaited_once_with({"_id": "room_timeline_sort_keys_v1"})
    user_collection.create_index.assert_not_awaited()
    agent_collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_timeline_readiness_rejects_legacy_marker_id_only_row():
    from conversation.repository.indexes import (
        ensure_room_timeline_indexes as _ensure_room_timeline_indexes,
    )

    user_collection = MagicMock()
    user_collection.aggregate = AsyncMock(return_value=[])
    user_collection.find_one = AsyncMock(return_value={"_id": "user-row"})
    user_collection.create_index = AsyncMock()
    agent_collection = MagicMock()
    agent_collection.aggregate = AsyncMock(return_value=[])
    agent_collection.find_one = AsyncMock(return_value=None)
    agent_collection.create_index = AsyncMock()
    markers = MagicMock()
    markers.find_one = AsyncMock(
        return_value={
            "marker_id": "room_timeline_sort_keys_v1",
            "version": 1,
            "status": "complete",
            "collections": {
                "room_user_messages": {"scanned": 1, "correct": 1},
                "room_agent_messages": {"scanned": 0, "correct": 0},
            },
        }
    )
    mongo = MagicMock()
    mongo.collection.side_effect = lambda name: {
        "room_user_messages": user_collection,
        "room_agent_messages": agent_collection,
        "migration_markers": markers,
    }[name]

    with pytest.raises(RuntimeError, match="final migration audit"):
        await _ensure_room_timeline_indexes(mongo)

    markers.find_one.assert_awaited_once_with({"_id": "room_timeline_sort_keys_v1"})
    user_collection.create_index.assert_not_awaited()
    agent_collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_timeline_readiness_accepts_valid_final_audit_marker():
    from conversation.repository.indexes import (
        ensure_room_timeline_indexes as _ensure_room_timeline_indexes,
    )

    user_collection = MagicMock()
    user_collection.aggregate = AsyncMock(return_value=[])
    user_collection.find_one = AsyncMock(return_value={"_id": "user-row"})
    user_collection.create_index = AsyncMock(return_value="user-index")
    agent_collection = MagicMock()
    agent_collection.aggregate = AsyncMock(return_value=[])
    agent_collection.find_one = AsyncMock(return_value={"_id": "agent-row"})
    agent_collection.create_index = AsyncMock(return_value="agent-index")
    markers = MagicMock()
    markers.find_one = AsyncMock(
        return_value={
            "_id": "room_timeline_sort_keys_v1",
            "marker_id": "room_timeline_sort_keys_v1",
            "version": 1,
            "status": "complete",
            "collections": {
                "room_user_messages": {"scanned": 2, "correct": 2},
                "room_agent_messages": {"scanned": 1, "correct": 1},
            },
        }
    )
    mongo = MagicMock()
    mongo.collection.side_effect = lambda name: {
        "room_user_messages": user_collection,
        "room_agent_messages": agent_collection,
        "migration_markers": markers,
    }[name]

    await _ensure_room_timeline_indexes(mongo)

    assert _has_create_index(
        user_collection,
        [("room_id", 1), ("timeline_sort_us", -1), ("message_id", -1)],
        unique=False,
        name="room_user_timeline_desc",
    )
    assert _has_create_index(
        agent_collection,
        [("room_id", 1), ("timeline_sort_us", -1), ("message_id", -1)],
        unique=False,
        name="room_agent_timeline_desc",
    )


@pytest.mark.asyncio
async def test_room_timeline_readiness_requires_manual_identity_repair():
    from conversation.repository.indexes import (
        ensure_room_timeline_indexes as _ensure_room_timeline_indexes,
    )

    user_collection = MagicMock()
    user_collection.aggregate = AsyncMock(side_effect=[[{"message_id": None}], []])
    user_collection.find_one = AsyncMock(return_value={"_id": "user-row"})
    user_collection.create_index = AsyncMock()
    agent_collection = MagicMock()
    agent_collection.aggregate = AsyncMock(return_value=[])
    agent_collection.find_one = AsyncMock(return_value=None)
    agent_collection.create_index = AsyncMock()
    mongo = MagicMock()
    mongo.collection.side_effect = lambda name: {
        "room_user_messages": user_collection,
        "room_agent_messages": agent_collection,
    }[name]

    with pytest.raises(RuntimeError, match="manually repair"):
        await _ensure_room_timeline_indexes(mongo)

    user_collection.create_index.assert_not_awaited()
    agent_collection.create_index.assert_not_awaited()


def _has_create_index(collection: MagicMock, keys, **kwargs) -> bool:
    return any(
        call.args == (keys,) and call.kwargs == kwargs
        for call in collection.create_index.call_args_list
    )


@pytest.mark.asyncio
async def test_agent_text_index_is_ensured_when_normalized_url_index_is_current():
    from agent.repository import AgentMongoRepository

    collection = MagicMock()
    collection.index_information = AsyncMock(
        return_value={
            "unique_normalized_url": {
                "partialFilterExpression": {"normalized_url": {"$type": "string"}}
            }
        }
    )
    collection.create_index = AsyncMock()
    collection.drop_index = AsyncMock()
    mongo = MagicMock()
    mongo.collection.return_value = collection

    assert await AgentMongoRepository(mongo).ensure_indexes() is True
    collection.drop_index.assert_not_awaited()
    assert any(
        call.kwargs.get("name") == "agent_lexical_text"
        for call in collection.create_index.call_args_list
    )


@pytest.mark.asyncio
async def test_text_index_with_scalar_prefix_is_rebuilt_even_when_weights_match():
    from common.utils.mongo_indexes import ensure_text_index as _ensure_text_index

    collection = MagicMock()
    collection.index_information = AsyncMock(
        return_value={
            "turn_notes_text": {
                "key": [
                    ("room_id", 1),
                    ("_fts", "text"),
                    ("_ftsx", 1),
                ],
                "weights": {"content": 1},
            }
        }
    )
    collection.drop_index = AsyncMock()
    collection.create_index = AsyncMock()

    assert await _ensure_text_index(
        collection,
        name="turn_notes_text",
        weights={"content": 1},
    )

    collection.drop_index.assert_awaited_once_with("turn_notes_text")
    collection.create_index.assert_awaited_once_with(
        [("content", "text")],
        name="turn_notes_text",
        unique=False,
        weights={"content": 1},
    )


@pytest.mark.asyncio
async def test_agent_search_index_failure_is_reported():
    failed_name = "agent_lexical_text"
    from container import ensure_runtime_indexes

    collections: dict[str, MagicMock] = {}

    def _collection(name):
        if name not in collections:
            collection = MagicMock()
            collection.index_information = AsyncMock(return_value={})
            collection.drop_index = AsyncMock()

            async def create_index(_keys, **kwargs):
                if kwargs.get("name") == failed_name:
                    raise RuntimeError("index unavailable")
                return kwargs.get("name")

            collection.create_index = AsyncMock(side_effect=create_index)
            collection.aggregate = AsyncMock(return_value=[])
            collection.find_one = AsyncMock(return_value=None)
            collection.update_one = AsyncMock(return_value=True)
            collections[name] = collection
        return collections[name]

    mongo = MagicMock()
    mongo.collection.side_effect = _collection

    assert await ensure_runtime_indexes(mongo=mongo) == {
        "agent_search_index_ready": False,
        "diagnostics_store_ready": False,
    }


@pytest.mark.asyncio
async def test_mongo_collection_adapter_preserves_zero_limit():
    from dal.mongo.client import MongoCollectionAdapter

    find_cursor = MagicMock()
    find_cursor.limit.return_value = find_cursor
    find_cursor.to_list = AsyncMock(return_value=[])

    collection = MagicMock()
    collection.find.return_value = find_cursor

    adapter = MongoCollectionAdapter(collection)

    assert await adapter.find({}, limit=0) == []
    find_cursor.limit.assert_called_once_with(0)
    find_cursor.to_list.assert_awaited_once_with(length=0)


@pytest.mark.asyncio
async def test_mongo_collection_adapter_delegates_bulk_write():
    from dal.mongo.client import MongoCollectionAdapter

    collection = MagicMock()
    collection.bulk_write = AsyncMock(return_value="bulk-result")

    adapter = MongoCollectionAdapter(collection)

    result = await adapter.bulk_write(["op"], ordered=False)

    assert result == "bulk-result"
    collection.bulk_write.assert_awaited_once_with(["op"], ordered=False)


@pytest.mark.asyncio
async def test_mongo_collection_adapter_delegates_distinct():
    from dal.mongo.client import MongoCollectionAdapter

    collection = MagicMock()
    collection.distinct = AsyncMock(return_value=["room-1"])

    adapter = MongoCollectionAdapter(collection)

    result = await adapter.distinct("room_id", {"state": "running"})

    assert result == ["room-1"]
    collection.distinct.assert_awaited_once_with("room_id", {"state": "running"})


@pytest.mark.asyncio
async def test_redis_kv_impl_uses_direct_redis_client():
    from dal.redis.kv import RedisKVImpl

    client = MagicMock()
    client.get = AsyncMock(return_value="value")
    client.set = AsyncMock(return_value=True)
    client.delete = AsyncMock(return_value=1)
    client.incrby = AsyncMock(return_value=3)
    client.exists = AsyncMock(return_value=1)
    client.ping = AsyncMock(return_value=True)
    client.aclose = AsyncMock()

    kv = RedisKVImpl(client=client)

    assert await kv.get("k") == "value"
    await kv.set("k", "v", ttl=10)
    assert await kv.delete("k") is True
    assert await kv.increment("k", amount=2) == 3
    assert await kv.setnx("k", "v", ttl=5) is True
    assert await kv.exists("k") is True
    assert await kv.ping() is True
    await kv.close()

    client.set.assert_any_await("k", "v", ex=10)
    client.incrby.assert_awaited_once_with("k", 2)
    client.set.assert_any_await("k", "v", nx=True, ex=5)
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_kv_impl_gracefully_degrades_without_url(monkeypatch):
    from dal.redis import kv as kv_module

    monkeypatch.setattr(kv_module.settings, "redis_url", "")

    kv = kv_module.RedisKVImpl()

    assert await kv.get("k") is None
    await kv.set("k", "v")
    assert await kv.delete("k") is False
    assert await kv.increment("k") == 0
    assert await kv.setnx("k", "v", ttl=1) is False
    assert await kv.exists("k") is False
    assert await kv.ping() is False


def test_redis_kv_impl_constructs_client_with_bounded_timeout(monkeypatch):
    from dal.redis import kv as kv_module

    calls = []
    client = MagicMock()

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(kv_module.aioredis, "from_url", from_url)
    monkeypatch.setattr(kv_module.settings, "redis_url", "redis://localhost:6379/0")
    monkeypatch.setattr(kv_module.settings, "redis_max_connections", 17)

    kv = kv_module.RedisKVImpl()

    assert kv._ensure_client() is client
    assert calls == [
        (
            "redis://localhost:6379/0",
            {
                "decode_responses": True,
                "socket_connect_timeout": 5,
                "max_connections": 17,
            },
        )
    ]


@pytest.mark.asyncio
async def test_redis_kv_impl_raises_transient_error_for_configured_driver_failures():
    from dal.redis.kv import RedisKVImpl

    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("get failed"))
    client.set = AsyncMock(side_effect=RuntimeError("set failed"))
    client.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client.incrby = AsyncMock(side_effect=RuntimeError("increment failed"))
    client.exists = AsyncMock(side_effect=RuntimeError("exists failed"))

    kv = RedisKVImpl(client=client)

    with pytest.raises(TransientError):
        await kv.get("k")
    with pytest.raises(TransientError):
        await kv.set("k", "v")
    with pytest.raises(TransientError):
        await kv.delete("k")
    with pytest.raises(TransientError):
        await kv.increment("k")
    with pytest.raises(TransientError):
        await kv.setnx("k", "v", ttl=1)
    with pytest.raises(TransientError):
        await kv.exists("k")


@pytest.mark.asyncio
async def test_redis_streams_impl_normalizes_xread():
    from dal.redis.streams import RedisStreamsImpl

    client = MagicMock()
    client.xadd = AsyncMock(return_value="1-0")
    client.xread = AsyncMock(return_value=[("stream-a", [("1-0", {"payload": "one"})])])

    streams = RedisStreamsImpl(client=client)

    assert await streams.xadd("stream-a", {"payload": "one"}, maxlen=100) == "1-0"
    assert await streams.xread({"stream-a": "0-0"}, block=5, count=10) == [
        {"stream": "stream-a", "id": "1-0", "fields": {"payload": "one"}}
    ]
    client.xadd.assert_awaited_once_with("stream-a", {"payload": "one"}, maxlen=100)
    client.xread.assert_awaited_once_with({"stream-a": "0-0"}, block=5, count=10)


@pytest.mark.asyncio
async def test_redis_streams_impl_gracefully_degrades_without_url(monkeypatch):
    from dal.redis import streams as streams_module

    monkeypatch.setattr(streams_module.settings, "redis_url", "")

    streams = streams_module.RedisStreamsImpl()

    assert await streams.xadd("stream-a", {"payload": "one"}) == ""
    assert await streams.xread({"stream-a": "0-0"}) == []
    assert await streams.ping() is False


def test_redis_streams_impl_constructs_client_with_bounded_timeout(monkeypatch):
    from dal.redis import streams as streams_module

    calls = []
    client = MagicMock()

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(streams_module.aioredis, "from_url", from_url)
    monkeypatch.setattr(
        streams_module.settings, "redis_url", "redis://localhost:6379/0"
    )
    monkeypatch.setattr(streams_module.settings, "redis_max_connections", 17)

    streams = streams_module.RedisStreamsImpl()

    assert streams._ensure_client() is client
    assert calls == [
        (
            "redis://localhost:6379/0",
            {
                "decode_responses": True,
                "socket_connect_timeout": 5,
                "max_connections": 17,
            },
        )
    ]


@pytest.mark.asyncio
async def test_redis_streams_impl_raises_transient_error_for_configured_failures():
    from dal.redis.streams import RedisStreamsImpl

    client = MagicMock()
    client.xadd = AsyncMock(side_effect=RuntimeError("xadd failed"))
    client.xread = AsyncMock(side_effect=RuntimeError("xread failed"))

    streams = RedisStreamsImpl(client=client)

    with pytest.raises(TransientError):
        await streams.xadd("stream-a", {"payload": "one"})
    with pytest.raises(TransientError):
        await streams.xread({"stream-a": "0-0"})


@pytest.mark.asyncio
async def test_redis_pubsub_impl_yields_only_messages():
    from dal.redis.pubsub import RedisPubSubImpl

    assert inspect.iscoroutinefunction(RedisPubSubImpl.subscribe)

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.get_message = AsyncMock(
        return_value={"type": "subscribe", "channel": "events", "data": 1}
    )
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    async def listen():
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message"}
        yield {"type": "message", "data": "payload"}

    pubsub.listen = listen

    client = MagicMock()
    client.pubsub.return_value = pubsub

    pubsub_impl = RedisPubSubImpl(client=client)

    iterator = await pubsub_impl.subscribe("events")
    assert await anext(iterator) == "payload"
    await iterator.aclose()

    pubsub.subscribe.assert_awaited_once_with("events")
    pubsub.unsubscribe.assert_awaited_once_with("events")
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_pubsub_iterator_bounds_blocked_unsubscribe_cleanup(monkeypatch):
    from dal.redis import pubsub as pubsub_module

    monkeypatch.setattr(pubsub_module, "_PUBSUB_CLEANUP_TIMEOUT_SECONDS", 0.01)
    unsubscribe_started = asyncio.Event()

    async def blocked_unsubscribe(_channel):
        unsubscribe_started.set()
        await asyncio.Event().wait()

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.get_message = AsyncMock(
        return_value={"type": "subscribe", "channel": "events", "data": 1}
    )
    pubsub.unsubscribe = AsyncMock(side_effect=blocked_unsubscribe)
    pubsub.aclose = AsyncMock()

    async def listen():
        yield {"type": "message", "data": "payload"}
        await asyncio.Future()

    pubsub.listen = listen
    client = MagicMock()
    client.pubsub.return_value = pubsub
    pubsub_impl = pubsub_module.RedisPubSubImpl(client=client)
    iterator = await pubsub_impl.subscribe("events")
    assert await anext(iterator) == "payload"

    await asyncio.wait_for(iterator.aclose(), timeout=0.1)

    assert unsubscribe_started.is_set()
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_pubsub_close_bounds_blocked_client_cleanup(monkeypatch):
    from dal.redis import pubsub as pubsub_module

    monkeypatch.setattr(pubsub_module, "_PUBSUB_CLEANUP_TIMEOUT_SECONDS", 0.01)
    close_started = asyncio.Event()

    async def blocked_close():
        close_started.set()
        await asyncio.Event().wait()

    client = MagicMock()
    client.aclose = AsyncMock(side_effect=blocked_close)
    pubsub_impl = pubsub_module.RedisPubSubImpl(client=client)

    await asyncio.wait_for(pubsub_impl.close(), timeout=0.1)

    assert close_started.is_set()
    assert pubsub_impl._client is None


@pytest.mark.asyncio
async def test_redis_pubsub_impl_subscribe_returns_only_after_ready():
    from dal.redis.pubsub import RedisPubSubImpl

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.get_message = AsyncMock(
        return_value={"type": "subscribe", "channel": "events", "data": 1}
    )
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    async def listen():
        yield {"type": "message", "data": "payload"}

    pubsub.listen = listen

    client = MagicMock()
    client.pubsub.return_value = pubsub

    pubsub_impl = RedisPubSubImpl(client=client)

    iterator = await pubsub_impl.subscribe("events")
    client.pubsub.assert_called_once_with()
    pubsub.subscribe.assert_awaited_once_with("events")
    pubsub.get_message.assert_awaited_once_with(
        ignore_subscribe_messages=False,
        timeout=None,
    )

    assert await anext(iterator) == "payload"
    await iterator.aclose()


@pytest.mark.asyncio
async def test_redis_pubsub_impl_publishes_with_direct_client():
    from dal.redis.pubsub import RedisPubSubImpl

    client = MagicMock()
    client.publish = AsyncMock(return_value=1)

    pubsub_impl = RedisPubSubImpl(client=client)

    await pubsub_impl.publish("events", "payload")

    client.publish.assert_awaited_once_with("events", "payload")


@pytest.mark.asyncio
async def test_redis_pubsub_impl_raises_transient_error_for_publish_failure():
    from dal.redis.pubsub import RedisPubSubImpl

    client = MagicMock()
    client.publish = AsyncMock(side_effect=RuntimeError("publish failed"))

    pubsub_impl = RedisPubSubImpl(client=client)

    with pytest.raises(TransientError):
        await pubsub_impl.publish("events", "payload")


@pytest.mark.asyncio
async def test_redis_pubsub_impl_surfaces_subscribe_setup_failure():
    from dal.redis.pubsub import RedisPubSubImpl

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock(side_effect=RuntimeError("subscribe failed"))
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    client = MagicMock()
    client.pubsub.return_value = pubsub

    pubsub_impl = RedisPubSubImpl(client=client)

    with pytest.raises(TransientError):
        await pubsub_impl.subscribe("events")
    pubsub.aclose.assert_awaited_once()


def test_redis_pubsub_impl_accepts_explicit_max_connections(monkeypatch):
    from dal.redis import pubsub as pubsub_module

    captured = {}

    def from_url(url, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pubsub_module.aioredis, "from_url", from_url)

    pubsub_impl = pubsub_module.RedisPubSubImpl(
        url="redis://localhost:6379/0",
        max_connections=120,
    )
    pubsub_impl._ensure_client()

    assert pubsub_impl.max_connections == 120
    assert captured["max_connections"] == 120


@pytest.mark.asyncio
async def test_redis_pubsub_impl_gracefully_degrades_without_url(monkeypatch):
    from dal.redis import pubsub as pubsub_module

    monkeypatch.setattr(pubsub_module.settings, "redis_url", "")

    pubsub_impl = pubsub_module.RedisPubSubImpl()

    await pubsub_impl.publish("events", "payload")
    iterator = await pubsub_impl.subscribe("events")
    next_message = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)

    assert not next_message.done()
    next_message.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_message
    assert await pubsub_impl.ping() is False


@pytest.mark.asyncio
async def test_distributed_lock_impl_uses_owner_checked_lua():
    from dal.redis.lock import DistributedLockImpl

    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    client.eval = AsyncMock(return_value=1)

    lock = DistributedLockImpl(client=client)

    assert await lock.acquire("resource", "owner", ttl=30) is True
    assert await lock.release("resource", "owner") is True
    assert await lock.renew("resource", "owner", ttl=45) is True

    client.set.assert_awaited_once_with("lock:resource", "owner", nx=True, ex=30)
    assert client.eval.await_count == 2
    release_args = client.eval.await_args_list[0].args
    renew_args = client.eval.await_args_list[1].args
    assert release_args[1:] == (1, "lock:resource", "owner")
    assert renew_args[1:] == (1, "lock:resource", "owner", "45")


@pytest.mark.asyncio
async def test_distributed_lock_impl_gracefully_degrades_without_url(monkeypatch):
    from dal.redis import lock as lock_module

    monkeypatch.setattr(lock_module.settings, "redis_url", "")

    lock = lock_module.DistributedLockImpl()

    assert await lock.acquire("resource", "owner") is False
    assert await lock.release("resource", "owner") is False
    assert await lock.renew("resource", "owner") is False


@pytest.mark.asyncio
async def test_distributed_lock_impl_close_closes_client():
    from dal.redis.lock import DistributedLockImpl

    client = MagicMock()
    client.aclose = AsyncMock()

    lock = DistributedLockImpl(client=client)

    await lock.close()

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_leader_elector_impl_uses_instance_id_owner_checks():
    from dal.redis.lock import LeaderElectorImpl

    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    client.eval = AsyncMock(return_value=1)

    elector = LeaderElectorImpl(client, instance_id="inst")

    assert await elector.try_acquire("job", ttl=30) is True
    assert await elector.renew("job", ttl=45) is True
    await elector.release("job")
    await elector.release_all(["job2"])

    client.set.assert_awaited_once_with("leader:job", "inst", nx=True, ex=30)
    assert client.eval.await_count == 3


@pytest.mark.asyncio
async def test_leader_elector_impl_accepts_ttl_seconds_alias():
    from dal.redis.lock import LeaderElectorImpl

    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    client.eval = AsyncMock(return_value=1)

    elector = LeaderElectorImpl(client, instance_id="inst")

    assert await elector.try_acquire("job", ttl=30, ttl_seconds=120) is True
    assert await elector.renew("job", ttl=45, ttl_seconds=180) is True

    client.set.assert_awaited_once_with("leader:job", "inst", nx=True, ex=120)
    renew_args = client.eval.await_args.args
    assert renew_args[1:] == (1, "leader:job", "inst", "180")


@pytest.mark.asyncio
async def test_leader_elector_impl_gracefully_degrades_without_url(monkeypatch):
    from dal.redis import lock as lock_module

    monkeypatch.setattr(lock_module.settings, "redis_url", "")

    elector = lock_module.LeaderElectorImpl(instance_id="inst")

    assert await elector.try_acquire("job") is False
    assert await elector.renew("job") is False
    assert await elector.release("job") is None


@pytest.mark.asyncio
async def test_leader_elector_impl_close_closes_client():
    from dal.redis.lock import LeaderElectorImpl

    client = MagicMock()
    client.aclose = AsyncMock()

    elector = LeaderElectorImpl(client, instance_id="inst")

    await elector.close()

    client.aclose.assert_awaited_once()
