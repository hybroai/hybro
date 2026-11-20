"""MongoSourceRepository against a mocked collection adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pymongo import ReturnDocument

from source.models import SourceRecord
from source.repository import SOURCE_INDEXES, MongoSourceRepository

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _record(**overrides) -> SourceRecord:
    values = {
        "source_id": "src_1",
        "name": "CI bot",
        "token_hash": "a" * 64,
        "token_last4": "wxyz",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SourceRecord(**values)


def _repository() -> tuple[MongoSourceRepository, MagicMock]:
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    collection.find_one = AsyncMock(return_value=None)
    collection.find_one_and_update = AsyncMock(return_value=None)
    collection.find = AsyncMock(return_value=[])
    collection.update_one = AsyncMock(return_value=True)
    collection.create_index = AsyncMock(return_value="idx")
    names: list[str] = []
    mongo = SimpleNamespace(collection=lambda name: names.append(name) or collection)
    repository = MongoSourceRepository(mongo)
    assert names == ["sources"]
    return repository, collection


async def test_indexes_make_ids_and_token_hashes_unique():
    repository, collection = _repository()

    await repository.ensure_indexes()

    calls = {
        call.kwargs["name"]: (call.args[0], call.kwargs)
        for call in collection.create_index.await_args_list
    }
    assert calls["source_id_unique"][0] == [("source_id", 1)]
    assert calls["source_id_unique"][1]["unique"] is True
    keys, options = calls["source_token_hash_unique"]
    assert keys == [("token_hash", 1)]
    assert options["unique"] is True
    assert options["partialFilterExpression"] == {"token_hash": {"$type": "string"}}
    assert len(SOURCE_INDEXES) == 2


async def test_insert_get_and_lookup_by_hash():
    repository, collection = _repository()
    record = _record()

    await repository.insert(record)
    collection.find_one.return_value = record.model_dump()
    by_hash = await repository.get_by_token_hash("a" * 64)
    by_id = await repository.get("src_1")

    assert collection.insert_one.await_args.args[0]["source_id"] == "src_1"
    assert by_hash == by_id == record
    queries = [call.args[0] for call in collection.find_one.await_args_list]
    assert queries == [{"token_hash": "a" * 64}, {"source_id": "src_1"}]


async def test_update_live_only_skips_a_revoked_source():
    repository, collection = _repository()

    assert await repository.update("src_1", {"name": "x"}, live_only=True) is None

    query, update = collection.find_one_and_update.await_args.args
    assert query == {"source_id": "src_1", "revoked_at": None}
    assert update == {"$set": {"name": "x"}}
    assert (
        collection.find_one_and_update.await_args.kwargs["return_document"]
        == ReturnDocument.AFTER
    )


async def test_list_by_ids_queries_only_those_ids():
    repository, collection = _repository()
    collection.find.return_value = [_record().model_dump()]

    found = await repository.list_by_ids(["src_1", "src_2"])

    assert [r.source_id for r in found] == ["src_1"]
    assert collection.find.await_args.args[0] == {
        "source_id": {"$in": ["src_1", "src_2"]}
    }


async def test_touch_sets_last_use():
    repository, collection = _repository()
    await repository.touch("src_1", NOW)
    collection.update_one.assert_awaited_once_with(
        {"source_id": "src_1"}, {"$set": {"last_used_at": NOW}}
    )
