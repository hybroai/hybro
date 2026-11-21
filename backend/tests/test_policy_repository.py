"""MongoRuleRepository against a mocked collection adapter."""

from __future__ import annotations

import asyncio
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
    assert call.args == (
        {"rule_id": "rule_1", "kind": "check"},
        {"$set": {"answer": "allow"}},
    )
    assert call.kwargs["return_document"] is ReturnDocument.AFTER


async def test_update_of_a_missing_rule_returns_none():
    repository, _ = _repository()
    assert await repository.update("rule_1", {"answer": "allow"}) is None


async def test_delete_reports_whether_a_rule_was_removed():
    repository, collection = _repository()
    assert await repository.delete("rule_1") is True
    collection.delete_one.assert_awaited_once_with(
        {"rule_id": "rule_1", "kind": "check"}
    )
    collection.delete_one.return_value = False
    assert await repository.delete("rule_1") is False


async def test_set_positions_claims_a_revision_then_writes_each_position():
    repository, collection = _repository()
    collection.find_one_and_update.return_value = {"revision": 7}

    await repository.set_positions(["rule_b", "rule_a"], NOW)

    claim = collection.find_one_and_update.await_args
    assert claim.args == (
        {"rule_id": "order_revision"},
        {"$inc": {"revision": 1}, "$setOnInsert": {"kind": "order_revision"}},
    )
    assert claim.kwargs["upsert"] is True
    assert claim.kwargs["return_document"] is ReturnDocument.AFTER
    (operations,), kwargs = collection.bulk_write.await_args
    older = {"$not": {"$gte": 7}}
    assert operations == [
        UpdateOne(
            {"rule_id": "rule_b", "kind": "check", "order_revision": older},
            {"$set": {"position": 0, "order_revision": 7, "updated_at": NOW}},
        ),
        UpdateOne(
            {"rule_id": "rule_a", "kind": "check", "order_revision": older},
            {"$set": {"position": 1, "order_revision": 7, "updated_at": NOW}},
        ),
    ]
    assert kwargs == {"ordered": True}


async def test_set_positions_with_no_rules_writes_nothing():
    repository, collection = _repository()
    await repository.set_positions([], NOW)
    collection.bulk_write.assert_not_awaited()


class _OrderCollection:
    """Enough of a collection to run two reorders at once.

    Each write yields, so the two batches interleave rule by rule, as two
    requests can on a real server.
    """

    def __init__(self, rule_ids: list[str]) -> None:
        self.docs = {
            rule_id: {**_record(rule_id=rule_id, position=p).model_dump()}
            for p, rule_id in enumerate(rule_ids)
        }

    async def find_one_and_update(self, query, update, **kwargs):
        await asyncio.sleep(0)
        doc = self.docs.get(query["rule_id"])
        if doc is None:
            assert kwargs.get("upsert") is True
            doc = {"rule_id": query["rule_id"], **update.get("$setOnInsert", {})}
            self.docs[query["rule_id"]] = doc
        for field, step in update.get("$inc", {}).items():
            doc[field] = doc.get(field, 0) + step
        assert kwargs["return_document"] is ReturnDocument.AFTER
        return {k: v for k, v in doc.items() if k != "_id"}

    async def bulk_write(self, operations, **kwargs):
        for operation in operations:
            await asyncio.sleep(0)
            doc = self.docs.get(operation._filter["rule_id"])
            if doc is None or not _matches(doc, operation._filter):
                continue
            doc.update(operation._doc["$set"])

    async def find(self, query, **kwargs):
        rules = [d for d in self.docs.values() if d.get("kind") == "check"]
        return sorted(rules, key=lambda d: (d["position"], d["created_at"]))


def _matches(doc, query) -> bool:
    for field, condition in query.items():
        if isinstance(condition, dict):
            ((operator, operand),) = condition.items()
            assert operator == "$not" and set(operand) == {"$gte"}
            value = doc.get(field)
            if value is not None and value >= operand["$gte"]:
                return False
        elif doc.get(field) != condition:
            return False
    return True


async def test_two_reorders_at_once_store_one_callers_order():
    ids = [f"rule_{n}" for n in range(6)]
    collection = _OrderCollection(ids)
    repository = MongoRuleRepository(SimpleNamespace(collection=lambda _: collection))
    first = list(reversed(ids))
    second = ids[3:] + ids[:3]

    await asyncio.gather(
        repository.set_positions(first, NOW),
        repository.set_positions(second, NOW),
    )

    stored = [rule.rule_id for rule in await repository.list_all()]
    # The later claim wins whole; the order is never a mix of the two.
    assert stored == second


async def test_the_order_revision_is_not_a_rule():
    collection = _OrderCollection(["rule_a", "rule_b"])
    repository = MongoRuleRepository(SimpleNamespace(collection=lambda _: collection))

    await repository.set_positions(["rule_b", "rule_a"], NOW)

    assert [rule.rule_id for rule in await repository.list_all()] == [
        "rule_b",
        "rule_a",
    ]
