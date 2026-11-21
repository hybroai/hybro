"""MongoRuleRepository against a mocked collection adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pymongo import ReturnDocument, UpdateOne

from policy.models import CheckRule
from policy.repository import RULE_INDEXES, MongoRuleRepository

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _record(**overrides) -> CheckRule:
    values = {
        "rule_id": "rule_1",
        "agent_id": "coder",
        "answer": "deny",
        "enabled": True,
        "position": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CheckRule(**values)


def _repository() -> tuple[MongoRuleRepository, MagicMock]:
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value="id")
    collection.find = AsyncMock(return_value=[])
    collection.find_one_and_update = AsyncMock(return_value=None)
    collection.delete_one = AsyncMock(return_value=True)
    collection.bulk_write = AsyncMock(return_value=None)
    collection.create_index = AsyncMock(return_value="idx")
    names: list[str] = []
    mongo = SimpleNamespace(collection=lambda name: names.append(name) or collection)
    repository = MongoRuleRepository(mongo)
    assert names == ["rules"]
    return repository, collection


async def test_indexes_make_ids_unique_and_order_the_check_rules():
    repository, collection = _repository()

    await repository.ensure_indexes()

    calls = {
        call.kwargs["name"]: (call.args[0], call.kwargs)
        for call in collection.create_index.await_args_list
    }
    assert calls["rule_id_unique"][0] == [("rule_id", 1)]
    assert calls["rule_id_unique"][1]["unique"] is True
    assert calls["rule_kind_position"][0] == [("kind", 1), ("position", 1)]
    assert len(RULE_INDEXES) == 2


async def test_insert_stores_the_record():
    repository, collection = _repository()
    record = _record()

    await repository.insert(record)

    collection.insert_one.assert_awaited_once_with(record.model_dump())


async def test_list_all_reads_the_check_rules_in_order():
    repository, collection = _repository()
    collection.find.return_value = [_record().model_dump()]

    listed = await repository.list_all()

    assert listed == [_record()]
    query = collection.find.await_args
    assert query.args[0] == {"kind": "check"}
    assert query.kwargs["projection"] == {"_id": 0}
    assert query.kwargs["sort"] == [
        ("position", 1),
        ("created_at", 1),
        ("rule_id", 1),
    ]


async def test_update_sets_fields_and_returns_the_new_record():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = _record(answer="allow").model_dump()

    updated = await repository.update("rule_1", {"answer": "allow"})

    assert updated == _record(answer="allow")
    call = collection.find_one_and_update.await_args
    assert call.args == ({"rule_id": "rule_1"}, {"$set": {"answer": "allow"}})
    assert call.kwargs["return_document"] is ReturnDocument.AFTER


async def test_update_of_a_missing_rule_returns_none():
    repository, _ = _repository()
    assert await repository.update("rule_1", {"answer": "allow"}) is None


async def test_delete_reports_whether_a_rule_was_removed():
    repository, collection = _repository()
    assert await repository.delete("rule_1") is True
    collection.delete_one.assert_awaited_once_with({"rule_id": "rule_1"})
    collection.delete_one.return_value = False
    assert await repository.delete("rule_1") is False


async def test_set_positions_writes_each_position_in_one_batch():
    repository, collection = _repository()

    await repository.set_positions(["rule_b", "rule_a"], NOW)

    (operations,), kwargs = collection.bulk_write.await_args
    assert operations == [
        UpdateOne({"rule_id": "rule_b"}, {"$set": {"position": 0, "updated_at": NOW}}),
        UpdateOne({"rule_id": "rule_a"}, {"$set": {"position": 1, "updated_at": NOW}}),
    ]
    assert kwargs == {"ordered": True}


async def test_set_positions_with_no_rules_writes_nothing():
    repository, collection = _repository()
    await repository.set_positions([], NOW)
    collection.bulk_write.assert_not_awaited()
