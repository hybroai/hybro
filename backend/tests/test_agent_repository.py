from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.repository import AgentMongoRepository


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = [deepcopy(doc) for doc in docs or []]
        self.find_one_calls: list[dict] = []
        self.find_calls: list[tuple[dict, dict]] = []
        self.find_one_and_update_calls: list[tuple[dict, dict, dict]] = []
        self.update_one_calls: list[tuple[dict, dict, dict]] = []
        self.update_many_calls: list[tuple[dict, dict]] = []
        self.delete_one_calls: list[dict] = []
        self.count_calls: list[dict] = []
        self.duplicate_normalized_urls: set[str] = set()
        self.return_false_for_noop = False

    async def find_one(self, query: dict, **kwargs) -> dict | None:
        self.find_one_calls.append(deepcopy(query))
        for doc in self.docs:
            if _matches(doc, query):
                return deepcopy(doc)
        return None

    async def find(self, query: dict, **kwargs) -> list[dict]:
        self.find_calls.append((deepcopy(query), deepcopy(kwargs)))
        matches = [deepcopy(doc) for doc in self.docs if _matches(doc, query)]
        limit = kwargs.get("limit")
        return matches if limit is None else matches[:limit]

    async def find_one_and_update(
        self, query: dict, update: dict, **kwargs
    ) -> dict | None:
        self.find_one_and_update_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        for doc in self.docs:
            if _matches(doc, query):
                updated = deepcopy(doc)
                _apply_update(updated, update, inserting=False)
                if _duplicates_normalized_url(updated, self.duplicate_normalized_urls):
                    raise DuplicateKeyError("duplicate normalized_url")
                doc.clear()
                doc.update(updated)
                return deepcopy(doc)
        if kwargs.get("upsert"):
            new_doc = _query_identity(query)
            _apply_update(new_doc, update, inserting=True)
            if _duplicates_normalized_url(new_doc, self.duplicate_normalized_urls):
                raise DuplicateKeyError("duplicate normalized_url")
            self.docs.append(new_doc)
            return deepcopy(new_doc)
        return None

    async def update_one(self, query: dict, update: dict, **kwargs) -> bool:
        self.update_one_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        for doc in self.docs:
            if _matches(doc, query):
                updated = deepcopy(doc)
                _apply_update(updated, update)
                if _duplicates_normalized_url(updated, self.duplicate_normalized_urls):
                    raise DuplicateKeyError("duplicate normalized_url")
                if updated == doc and self.return_false_for_noop:
                    return False
                doc.clear()
                doc.update(updated)
                return True
        if kwargs.get("upsert"):
            new_doc = _query_identity(query)
            _apply_update(new_doc, update)
            if _duplicates_normalized_url(new_doc, self.duplicate_normalized_urls):
                raise DuplicateKeyError("duplicate normalized_url")
            self.docs.append(new_doc)
            return True
        return False

    async def delete_many(self, query: dict) -> int:
        kept = [doc for doc in self.docs if not _matches(doc, query)]
        deleted = len(self.docs) - len(kept)
        self.docs = kept
        return deleted

    async def update_many(self, query: dict, update: dict) -> int:
        self.update_many_calls.append((deepcopy(query), deepcopy(update)))
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                _apply_update(doc, update)
                count += 1
        return count

    async def delete_one(self, query: dict) -> bool:
        self.delete_one_calls.append(deepcopy(query))
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs.pop(index)
                return True
        return False

    async def count(self, query: dict) -> int:
        self.count_calls.append(deepcopy(query))
        return sum(1 for doc in self.docs if _matches(doc, query))


def _repo(
    docs: list[dict] | None = None,
) -> tuple[AgentMongoRepository, FakeCollection]:
    collection = FakeCollection(docs)
    mongo = MagicMock()
    mongo.collection.return_value = collection
    return AgentMongoRepository(mongo=mongo), collection


@pytest.mark.asyncio
async def test_get_by_id_queries_agent_id():
    repo, collection = _repo([{"agent_id": "a1", "name": "A"}])

    assert await repo.get_by_id("a1") == {"agent_id": "a1", "name": "A"}
    assert collection.find_one_calls == [{"agent_id": "a1"}]


@pytest.mark.asyncio
async def test_get_by_ids_and_provider_keep_dict_outputs():
    repo, collection = _repo(
        [
            {"agent_id": "a1", "provider_id": "u1"},
            {"agent_id": "a2", "provider_id": "u2"},
        ]
    )

    assert await repo.get_by_ids(["a2", "a1"]) == [
        {"agent_id": "a1", "provider_id": "u1"},
        {"agent_id": "a2", "provider_id": "u2"},
    ]
    assert await repo.get_by_provider("u1") == [{"agent_id": "a1", "provider_id": "u1"}]
    assert collection.find_calls[0][0] == {"agent_id": {"$in": ["a2", "a1"]}}
    assert collection.find_calls[1][0] == {"provider_id": "u1"}


@pytest.mark.asyncio
async def test_get_public_requires_explicit_is_public_with_limit():
    repo, collection = _repo(
        [
            {"agent_id": "missing"},
            {"agent_id": "public", "is_public": True},
            {"agent_id": "private", "is_public": False},
        ]
    )

    assert await repo.get_public(limit=1) == [{"agent_id": "public", "is_public": True}]
    assert collection.find_calls[-1] == ({"is_public": True}, {"limit": 1})


@pytest.mark.asyncio
async def test_list_visible_filters_public_owned_and_active_agents():
    repo, _ = _repo(
        [
            {"agent_id": "public-active", "is_public": True, "agent_status": "active"},
            {"agent_id": "missing-flag-active", "agent_status": "active"},
            {
                "agent_id": "owned-private",
                "is_public": False,
                "provider_id": "u1",
                "agent_status": "active",
            },
            {
                "agent_id": "other-private",
                "is_public": False,
                "provider_id": "u2",
                "agent_status": "active",
            },
            {"agent_id": "inactive", "is_public": True, "agent_status": "inactive"},
        ]
    )

    unauthenticated = await repo.list_visible(active_only=True)
    authenticated = await repo.list_visible(user_id="u1", active_only=True)

    assert [doc["agent_id"] for doc in unauthenticated] == ["public-active"]
    assert [doc["agent_id"] for doc in authenticated] == [
        "public-active",
        "owned-private",
    ]


@pytest.mark.asyncio
async def test_list_visible_combines_query_with_visibility_filters():
    repo, _ = _repo(
        [
            {"agent_id": "matching", "is_public": True, "agent_status": "active"},
            {"agent_id": "hidden", "is_public": True, "agent_status": "inactive"},
            {"agent_id": "private", "is_public": False, "agent_status": "active"},
        ]
    )

    result = await repo.list_visible(query={"agent_status": "active"})

    assert [doc["agent_id"] for doc in result] == ["matching"]


@pytest.mark.asyncio
async def test_list_visible_returns_every_agent_without_a_limit():
    docs = [{"agent_id": f"public-{index}", "is_public": True} for index in range(1001)]
    repo, collection = _repo(docs)

    complete = await repo.list_visible()

    assert {doc["agent_id"] for doc in complete} == {doc["agent_id"] for doc in docs}
    assert collection.find_calls[-1][1] == {"limit": None}


@pytest.mark.asyncio
async def test_list_visible_honors_explicit_finite_limit():
    repo, _ = _repo(
        [{"agent_id": f"public-{index}", "is_public": True} for index in range(3)]
    )

    result = await repo.list_visible(limit=2)

    assert [doc["agent_id"] for doc in result] == ["public-0", "public-1"]


@pytest.mark.asyncio
async def test_find_by_normalized_url_reads_only_the_indexed_field():
    repo, collection = _repo(
        [
            {
                "agent_id": "legacy",
                "provider_id": "u1",
                "agent_card": {"url": "HTTP://127.0.0.1:80/.well-known/agent.json"},
            },
            {
                "agent_id": "exact",
                "provider_id": "u2",
                "normalized_url": "https://example.com",
            },
        ]
    )

    exact = await repo.find_by_normalized_url("https://example.com")
    missing = await repo.find_by_normalized_url("http://localhost", provider_id="u1")

    assert exact["agent_id"] == "exact"
    assert missing is None
    assert collection.find_one_calls == [
        {"normalized_url": "https://example.com"},
        {"normalized_url": "http://localhost", "provider_id": "u1"},
    ]
    assert collection.find_calls == []


@pytest.mark.asyncio
async def test_public_url_exists_update_delete_and_health():
    repo, collection = _repo(
        [{"agent_id": "a1", "public_url": "https://story.hybro.ai"}]
    )

    assert await repo.public_url_exists("story", "hybro.ai") is True
    assert await repo.public_url_exists("free", "hybro.ai") is False
    assert collection.count_calls[-2:] == [
        {"public_url": {"$regex": "://story\\.hybro\\.ai"}},
        {"public_url": {"$regex": "://free\\.hybro\\.ai"}},
    ]

    assert await repo.update("a1", {"agent_status": "inactive"}) == {
        "agent_id": "a1",
        "public_url": "https://story.hybro.ai",
        "agent_status": "inactive",
    }
    collection.return_false_for_noop = True
    assert await repo.update("a1", {"agent_status": "inactive"}) == {
        "agent_id": "a1",
        "public_url": "https://story.hybro.ai",
        "agent_status": "inactive",
    }
    assert await repo.update("missing", {"agent_status": "inactive"}) is None

    await repo.update_health("a1", healthy=True)
    assert (await repo.get_by_id("a1"))["agent_status"] == "active"
    assert await repo.delete("a1") is True
    assert await repo.delete("a1") is False


@pytest.mark.asyncio
def _matches(doc: dict, query: dict) -> bool:
    return all(_matches_field(doc, key, expected) for key, expected in query.items())


def _matches_field(doc: dict, key: str, expected: Any) -> bool:
    if key == "$or":
        return any(_matches(doc, branch) for branch in expected)
    if key == "$and":
        return all(_matches(doc, branch) for branch in expected)
    actual = _get_path(doc, key)
    if isinstance(expected, dict):
        exists = _path_exists(doc, key)
        return all(
            _matches_operator(actual, exists, op, value)
            for op, value in expected.items()
        )
    return actual == expected


def _matches_operator(actual: Any, exists: bool, op: str, value: Any) -> bool:
    if op == "$in":
        return actual in value
    if op == "$nin":
        return actual not in value
    if op == "$ne":
        return actual != value
    if op == "$lt":
        return actual is not None and actual < value
    if op == "$exists":
        return exists is bool(value)
    if op == "$regex":
        return value.replace("\\.", ".").strip(".*") in str(actual or "")
    return True


def _get_path(doc: dict, path: str) -> Any:
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _path_exists(doc: dict, path: str) -> bool:
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _apply_update(doc: dict, update: dict, *, inserting: bool = False) -> None:
    if inserting:
        for path, value in update.get("$setOnInsert", {}).items():
            _set_path(doc, path, value)
    for path, value in update.get("$set", {}).items():
        _set_path(doc, path, value)
    for path in update.get("$unset", {}):
        _unset_path(doc, path)


def _set_path(doc: dict, path: str, value: Any) -> None:
    current = doc
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _unset_path(doc: dict, path: str) -> None:
    current = doc
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.get(part, {})
    current.pop(parts[-1], None)


def _query_identity(query: dict) -> dict:
    return {key: value for key, value in query.items() if not isinstance(value, dict)}


def _duplicates_normalized_url(doc: dict, duplicate_normalized_urls: set[str]) -> bool:
    normalized_url = doc.get("normalized_url")
    return normalized_url is not None and normalized_url in duplicate_normalized_urls


class DuplicateKeyError(Exception):
    pass


@pytest.mark.asyncio
async def test_mark_agents_inactive_records_when_the_record_went_inactive():
    repo, collection = _repo(
        [{"agent_id": "a1", "source": "local", "agent_status": "active"}]
    )

    assert await repo.mark_agents_inactive(["a1"], source="local") == 1

    assert collection.docs[0]["agent_status"] == "inactive"
    assert isinstance(collection.docs[0]["inactive_since"], datetime)


@pytest.mark.asyncio
async def test_delete_inactive_ownerless_keeps_owned_recent_and_active_records():
    cutoff = datetime(2026, 10, 16, tzinfo=UTC)
    old = cutoff - timedelta(days=1)
    recent = cutoff + timedelta(days=1)
    base = {"source": "local", "provider_id": None, "agent_status": "inactive"}
    repo, collection = _repo(
        [
            {**base, "agent_id": "stale", "inactive_since": old},
            {**base, "agent_id": "legacy-stale", "updated_at": old},
            {**base, "agent_id": "recent", "inactive_since": recent},
            {**base, "agent_id": "owned", "provider_id": "u1", "inactive_since": old},
            {**base, "agent_id": "active", "agent_status": "active", "updated_at": old},
            {**base, "agent_id": "cloud", "source": "cloud", "inactive_since": old},
        ]
    )

    deleted = await repo.delete_inactive_ownerless(
        source="local", inactive_before=cutoff
    )

    assert deleted == 2
    assert [doc["agent_id"] for doc in collection.docs] == [
        "recent",
        "owned",
        "active",
        "cloud",
    ]
