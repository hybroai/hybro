from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from context_memory import compaction
from context_memory.config import CompactionConfig
from context_memory.repository import (
    ContentStorageMongoRepository,
    MemoryMongoRepository,
)
from context_memory.translators import normalize_room_memory


class FakeMongoCollection:
    def __init__(self):
        self.documents: list[dict] = []
        self.find_one_and_update_calls: list[tuple[dict, dict | list[dict], dict]] = []
        self.update_one_calls: list[tuple[dict, dict | list[dict], dict]] = []
        self.raise_on_update_one = False
        self.update_one_exception: Exception | None = None

    async def find_one(self, query: dict, **kwargs):
        for doc in self.documents:
            if _matches(doc, query):
                return deepcopy(doc)
        return None

    async def find(self, query: dict, **kwargs):
        limit = kwargs.get("limit")
        docs = [deepcopy(doc) for doc in self.documents if _matches(doc, query)]
        return docs[:limit] if limit is not None else docs

    async def find_one_and_update(
        self, query: dict, update: dict | list[dict], **kwargs
    ):
        self.find_one_and_update_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        for doc in self.documents:
            if _matches(doc, query):
                _apply_update(doc, update)
                return deepcopy(doc) if kwargs.get("return_document") else None
        if kwargs.get("upsert"):
            doc = {}
            if "$setOnInsert" in update:
                doc.update(deepcopy(update["$setOnInsert"]))
            else:
                doc.update({k: v for k, v in query.items() if not isinstance(v, dict)})
            if isinstance(update, dict):
                _apply_update(doc, update, is_insert=True)
            self.documents.append(doc)
            return deepcopy(doc) if kwargs.get("return_document") else None
        return None

    async def insert_one(self, document: dict) -> str:
        doc = deepcopy(document)
        doc.setdefault("_id", f"id-{len(self.documents) + 1}")
        self.documents.append(doc)
        return str(doc["_id"])

    async def update_one(
        self, query: dict, update: dict | list[dict], **kwargs
    ) -> bool:
        self.update_one_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        if self.raise_on_update_one:
            raise RuntimeError("write failed")
        if self.update_one_exception is not None:
            raise self.update_one_exception
        if kwargs.get("upsert"):
            _assert_no_update_path_conflicts(update)
        for doc in self.documents:
            if _matches(doc, query):
                before = deepcopy(doc)
                _apply_update(doc, update, array_filters=kwargs.get("array_filters"))
                return doc != before
        if kwargs.get("upsert"):
            doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            _apply_update(doc, update, is_insert=True)
            self.documents.append(doc)
            return True
        return False

    async def delete_one(self, query: dict) -> bool:
        for index, doc in enumerate(self.documents):
            if _matches(doc, query):
                self.documents.pop(index)
                return True
        return False

    async def delete_many(self, query: dict) -> int:
        before = len(self.documents)
        self.documents = [doc for doc in self.documents if not _matches(doc, query)]
        return before - len(self.documents)


class FakeMongo:
    def __init__(self):
        self.collections = {
            "room_memories": FakeMongoCollection(),
            "conversation_content": FakeMongoCollection(),
        }

    def collection(self, name: str):
        return self.collections[name]


@pytest.fixture
def mongo():
    return FakeMongo()


@pytest.fixture
def memory_repo(mongo):
    return MemoryMongoRepository(mongo=mongo)


@pytest.fixture
def content_repo(mongo):
    return ContentStorageMongoRepository(mongo=mongo)


async def _push_turn(
    memory_repo: MemoryMongoRepository,
    conversation_id: str,
    turn: dict,
    **kwargs,
) -> tuple[bool, bool]:
    (
        modified,
        matched,
        already_exists,
    ) = await memory_repo.push_and_trim_conversation_turn_if_absent(
        conversation_id, turn, turn_id=turn["turn_id"], **kwargs
    )
    assert already_exists is False
    return modified, matched


@pytest.mark.asyncio
async def test_get_room_memory_returns_none_for_missing(memory_repo):
    assert await memory_repo.get_room_memory("missing") is None


@pytest.mark.asyncio
async def test_create_and_get_room_memory(memory_repo):
    memory_id = await memory_repo.create_room_memory(
        {"room_id": "r1", "memory_id": "m1"}
    )

    assert memory_id == "m1"
    assert await memory_repo.get_room_memory("r1") == {
        "room_id": "r1",
        "memory_id": "m1",
        "_id": "id-1",
    }


@pytest.mark.asyncio
async def test_ensure_room_memory_creates_on_first_call(memory_repo):
    doc = await memory_repo.ensure_room_memory("r1", {"memory_id": "m1", "value": 1})

    assert doc["room_id"] == "r1"
    assert doc["memory_id"] == "m1"


@pytest.mark.asyncio
async def test_ensure_room_memory_idempotent(memory_repo):
    await memory_repo.ensure_room_memory("r1", {"memory_id": "first", "value": 1})
    doc = await memory_repo.ensure_room_memory(
        "r1", {"memory_id": "second", "value": 2}
    )

    assert doc["memory_id"] == "first"
    assert doc["value"] == 1


@pytest.mark.asyncio
async def test_upsert_room_memory_insert_preserves_memory_id(memory_repo):
    await memory_repo.upsert_room_memory(
        "r1",
        {"memory_id": "m1", "memory_content": {"conversation_history": []}},
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["room_id"] == "r1"
    assert doc["memory_id"] == "m1"


@pytest.mark.asyncio
async def test_upsert_room_memory_insert_has_no_mongo_path_conflicts(memory_repo):
    await memory_repo.upsert_room_memory(
        "r1",
        {
            "memory_id": "m1",
            "memory_content": {"conversation_history": []},
            "conversation_history": [],
        },
    )

    _query, update, _kwargs = memory_repo._memories.update_one_calls[-1]
    assert set(update["$set"]).isdisjoint(update["$setOnInsert"])


@pytest.mark.asyncio
async def test_push_and_trim_conversation_turn_appends_only_top_level(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {"summary": None},
            "conversation_history": [],
        }
    )

    modified, matched = await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "t1", "role": "user", "content": "hello"},
        max_turns=5,
        summary_stub="[User] hello",
        max_summary_chars=100,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert (modified, matched) == (True, True)
    assert doc["conversation_history"][0]["turn_id"] == "t1"
    assert "conversation_history" not in doc["memory_content"]


@pytest.mark.asyncio
async def test_push_literalizes_complete_turn_with_mongo_expression_strings(
    memory_repo, mongo
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {"summary": None},
            "conversation_history": [],
        }
    )
    turn = {
        "turn_id": "$private-turn",
        "role": "user",
        "content": "$$NOW is private content",
        "brief_summary": "$private summary",
        "content_ref": {"document_id": "$$NOW"},
    }

    await _push_turn(
        memory_repo,
        "r1",
        turn,
        max_turns=5,
        summary_stub="$private caller summary",
        max_summary_chars=100,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["conversation_history"] == [turn]
    pipeline = mongo.collections["room_memories"].find_one_and_update_calls[-1][1]
    append_expression = pipeline[0]["$set"]["conversation_history"]
    assert append_expression["$concatArrays"][1] == {"$literal": [turn]}


@pytest.mark.asyncio
async def test_create_room_memory_strips_nested_history(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "summary": "keep",
                "conversation_history": [
                    {"turn_id": "legacy", "role": "user", "content": "old"}
                ],
            },
            "conversation_history": [
                {"turn_id": "direct", "role": "agent", "content": "new"}
            ],
        }
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["memory_content"] == {"summary": "keep"}
    assert [turn["turn_id"] for turn in doc["conversation_history"]] == ["direct"]


def test_normalize_room_memory_reads_only_canonical_direct_history():
    state = normalize_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "conversation_history": [],
            "memory_content": {
                "summary": "keep",
                "conversation_history": [
                    {"turn_id": "legacy", "role": "user", "content": "legacy"}
                ],
            },
        }
    )

    assert state.conversation_history == []
    assert state.summary == "keep"


@pytest.mark.asyncio
async def test_push_uses_only_canonical_direct_history(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "summary": "",
                "conversation_history": [
                    {"turn_id": "legacy", "role": "user", "content": "legacy"}
                ],
            },
            "conversation_history": [
                {"turn_id": "direct", "role": "agent", "content": "direct"}
            ],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "new", "role": "user", "content": "new"},
        max_turns=5,
        summary_stub="unused",
        max_summary_chars=100,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert [turn["turn_id"] for turn in doc["conversation_history"]] == [
        "direct",
        "new",
    ]
    assert "conversation_history" not in doc["memory_content"]


@pytest.mark.asyncio
async def test_push_and_trim_does_not_append_summary_without_trim(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {"conversation_history": [], "summary": "existing"},
            "conversation_history": [],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "t1", "role": "user", "content": "hello"},
        max_turns=5,
        summary_stub="[User] hello",
        max_summary_chars=100,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["memory_content"]["summary"] == "existing"


@pytest.mark.asyncio
async def test_push_and_trim_summary_uses_evicted_turn_preview(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {"turn_id": "old", "role": "user", "content": "old content"}
                ],
                "summary": "",
            },
            "conversation_history": [
                {"turn_id": "old", "role": "user", "content": "old content"}
            ],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "new", "role": "user", "content": "new content"},
        max_turns=1,
        summary_stub="[User] new content",
        max_summary_chars=500,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert [turn["turn_id"] for turn in doc["conversation_history"]] == [
        "old",
        "new",
    ]
    assert "conversation_history" not in doc["memory_content"]
    assert doc["memory_content"]["summary"] == "[User] old content..."


@pytest.mark.asyncio
async def test_push_and_trim_summary_ignores_non_string_legacy_content(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {"turn_id": "old", "role": "user", "content": {"text": "old"}}
                ],
                "summary": "",
            },
            "conversation_history": [
                {"turn_id": "old", "role": "user", "content": {"text": "old"}}
            ],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "new", "role": "user", "content": "new content"},
        max_turns=1,
        summary_stub="[User] new content",
        max_summary_chars=500,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["memory_content"]["summary"] == "[User] [compact turn]..."


@pytest.mark.asyncio
async def test_summary_tracks_duplicate_turns_without_ids_by_occurrence(memory_repo):
    duplicate = {"role": "user", "content": "duplicate legacy content"}
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [deepcopy(duplicate), deepcopy(duplicate)],
                "summary": "",
            },
            "conversation_history": [deepcopy(duplicate)],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "new", "role": "user", "content": "new"},
        max_turns=2,
        summary_stub="new preview",
        max_summary_chars=500,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["memory_content"]["summary"] == ""


@pytest.mark.asyncio
async def test_summary_matches_turn_id_across_representation_changes(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {
                        "turn_id": "same",
                        "role": "user",
                        "representation": "full",
                        "content": "already summarized content",
                    }
                ],
                "summary": "existing summary",
            },
            "conversation_history": [
                {
                    "turn_id": "same",
                    "role": "user",
                    "representation": "compact",
                    "content": None,
                    "content_ref": {"document_id": "same"},
                }
            ],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "new", "role": "user", "content": "new"},
        max_turns=2,
        summary_stub="new preview",
        max_summary_chars=500,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert doc["memory_content"]["summary"] == "existing summary"
    assert doc["conversation_history"][0]["representation"] == "compact"
    assert "conversation_history" not in doc["memory_content"]


@pytest.mark.asyncio
async def test_push_and_trim_appends_summary_only_when_trimmed_and_keeps_tail(
    memory_repo,
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {"turn_id": "t1"},
                    {"turn_id": "t2"},
                ],
                "summary": "oldest summary text",
            },
            "conversation_history": [
                {"turn_id": "t1"},
                {"turn_id": "t2"},
            ],
        }
    )

    await _push_turn(
        memory_repo,
        "r1",
        {"turn_id": "t3", "role": "user", "content": "new"},
        max_turns=2,
        summary_stub="[User] latest important summary",
        max_summary_chars=24,
    )

    doc = await memory_repo.get_room_memory("r1")
    assert [turn["turn_id"] for turn in doc["conversation_history"]] == [
        "t1",
        "t2",
        "t3",
    ]
    assert "conversation_history" not in doc["memory_content"]
    assert doc["memory_content"]["summary"].startswith("...")
    assert "compact turn" in doc["memory_content"]["summary"]
    assert "latest important summary" not in doc["memory_content"]["summary"]


@pytest.mark.asyncio
async def test_twenty_first_short_turn_survives_and_triggers_default_compaction(
    memory_repo, content_repo
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {"conversation_history": [], "summary": ""},
            "conversation_history": [],
        }
    )

    for index in range(1, 22):
        await _push_turn(
            memory_repo,
            "r1",
            {
                "turn_id": f"t{index}",
                "role": "user",
                "representation": "full",
                "content": f"short message {index}",
                "content_type": "text",
                "estimated_tokens_full": 3,
                "estimated_tokens_compact": 20,
            },
            max_turns=20,
            summary_stub=f"new turn preview {index}",
            max_summary_chars=1000,
        )

    doc = await memory_repo.get_room_memory("r1")
    assert len(doc["conversation_history"]) == 21
    assert "conversation_history" not in doc["memory_content"]
    assert doc["conversation_history"][0]["turn_id"] == "t1"
    assert "short message 1" in doc["memory_content"]["summary"]
    assert "short message 2" not in doc["memory_content"]["summary"]
    assert "short message 21" not in doc["memory_content"]["summary"]
    assert "new turn preview 21" not in doc["memory_content"]["summary"]
    assert len(normalize_room_memory(doc).conversation_history) == 21

    default_config = CompactionConfig(
        enabled=True,
        max_total_tokens=80_000,
        preserve_recent_turns=10,
        content_ttl_days=0,
        concurrency=2,
    )
    assert default_config.max_full_turns == 20
    result = await compaction.compact_if_needed(
        repository=memory_repo,
        content_repository=content_repo,
        conversation_id="r1",
        config=default_config,
        now=lambda: datetime.now(UTC),
    )

    assert result is not None
    assert result.compacted_count == 11
    compacted_doc = await memory_repo.get_room_memory("r1")
    assert len(compacted_doc["conversation_history"]) == 21
    assert "conversation_history" not in compacted_doc["memory_content"]
    assert all(
        turn["representation"] == "compact"
        for turn in compacted_doc["conversation_history"][:11]
    )
    assert [
        turn["turn_id"]
        for turn in compacted_doc["conversation_history"][-10:]
        if turn["representation"] == "full"
    ] == [f"t{index}" for index in range(12, 22)]
    assert (
        compacted_doc["conversation_history"][1]["brief_summary"] == "short message 2"
    )

    await _push_turn(
        memory_repo,
        "r1",
        {
            "turn_id": "t22",
            "role": "user",
            "representation": "full",
            "content": "short message 22",
            "content_type": "text",
            "estimated_tokens_full": 3,
            "estimated_tokens_compact": 20,
        },
        max_turns=20,
        summary_stub="new turn preview 22",
        max_summary_chars=1000,
    )

    appended_doc = await memory_repo.get_room_memory("r1")
    assert "[User] short message 2..." in appended_doc["memory_content"]["summary"]
    assert "[User] [compact turn]..." not in appended_doc["memory_content"]["summary"]


@pytest.mark.asyncio
async def test_push_and_trim_if_absent_rejects_duplicate(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {"summary": None},
            "conversation_history": [{"turn_id": "t1"}],
        }
    )

    result = await memory_repo.push_and_trim_conversation_turn_if_absent(
        "r1",
        {"turn_id": "t1", "role": "user", "content": "duplicate"},
        turn_id="t1",
        max_turns=5,
        summary_stub="dup",
        max_summary_chars=100,
    )

    assert result == (False, True, True)


@pytest.mark.asyncio
async def test_push_and_trim_if_absent_duplicate_check_ignores_malformed_entries(
    memory_repo,
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [None, "bad", {"turn_id": "t1"}]
            },
            "conversation_history": [
                None,
                {"content": "missing id"},
                {"turn_id": "t1"},
            ],
        }
    )

    result = await memory_repo.push_and_trim_conversation_turn_if_absent(
        "r1",
        {"turn_id": "t1", "role": "user", "content": "duplicate"},
        turn_id="t1",
        max_turns=5,
        summary_stub="dup",
        max_summary_chars=100,
    )

    assert result == (False, True, True)


@pytest.mark.asyncio
async def test_push_and_trim_if_absent_handles_concurrent_duplicate_race(
    memory_repo, monkeypatch
):
    doc = {
        "room_id": "r1",
        "memory_id": "m1",
        "memory_content": {"conversation_history": []},
        "conversation_history": [],
    }
    pushes = []

    async def fake_push(
        conversation_id, turn, max_turns, summary_stub, max_summary_chars, *, query=None
    ):
        pushes.append(turn)
        if len(pushes) == 2:
            doc["conversation_history"].append({"turn_id": "t1"})
        return None

    async def fake_get(conversation_id):
        return deepcopy(doc)

    monkeypatch.setattr(memory_repo, "_push_turn", fake_push)
    monkeypatch.setattr(memory_repo, "get_room_memory", fake_get)

    result = await memory_repo.push_and_trim_conversation_turn_if_absent(
        "r1",
        {"turn_id": "t1", "role": "user", "content": "hello"},
        turn_id="t1",
        max_turns=5,
        summary_stub="[User] hello",
        max_summary_chars=500,
    )

    assert result == (False, True, True)


@pytest.mark.asyncio
async def test_push_and_trim_if_absent_missing_room(memory_repo):
    result = await memory_repo.push_and_trim_conversation_turn_if_absent(
        "missing",
        {"turn_id": "t1"},
        turn_id="t1",
        max_turns=5,
        summary_stub="stub",
        max_summary_chars=100,
    )

    assert result == (False, False, False)


@pytest.mark.asyncio
async def test_compact_turns_bulk(memory_repo, mongo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {"turn_id": "t1", "representation": "full", "content": "one"},
                    {"turn_id": "t2", "representation": "full", "content": "two"},
                ]
            },
            "conversation_history": [
                {"turn_id": "t1", "representation": "full", "content": "one"},
                {"turn_id": "t2", "representation": "full", "content": "two"},
            ],
            "total_compactions": 0,
        }
    )

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [
            {
                "turn_id": "t1",
                "content_ref": {"document_id": "d1"},
                "estimated_tokens_compact": 7,
            },
            {
                "turn_id": "t2",
                "content_ref": {"document_id": "d2"},
                "estimated_tokens_compact": 8,
            },
        ],
    )

    doc = await memory_repo.get_room_memory("r1")
    assert ok is True
    assert len(mongo.collections["room_memories"].update_one_calls) == 1
    update = mongo.collections["room_memories"].update_one_calls[0][1]
    assert isinstance(update, list)
    assert update[0]["$set"]["last_activity_at"] == "$$NOW"
    assert doc["total_compactions"] == 1
    assert [t["representation"] for t in doc["conversation_history"]] == [
        "compact",
        "compact",
    ]
    assert [t["content_ref"]["document_id"] for t in doc["conversation_history"]] == [
        "d1",
        "d2",
    ]


@pytest.mark.asyncio
async def test_compact_turns_bulk_literalizes_caller_values(memory_repo, mongo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "conversation_history": [
                {
                    "turn_id": "$private-turn",
                    "representation": "full",
                    "content": "secret",
                }
            ],
        }
    )
    entry = {
        "turn_id": "$private-turn",
        "content_ref": {"document_id": "$$NOW", "label": "$private"},
        "estimated_tokens_compact": 7,
        "brief_summary": "$$NOW remains summary text",
    }

    assert await memory_repo.compact_turns_bulk("r1", [entry]) is True

    doc = await memory_repo.get_room_memory("r1")
    compacted = doc["conversation_history"][0]
    assert compacted["turn_id"] == "$private-turn"
    assert compacted["content_ref"] == entry["content_ref"]
    assert compacted["brief_summary"] == "$$NOW remains summary text"
    assert compacted["estimated_tokens_compact"] == 7

    pipeline = mongo.collections["room_memories"].update_one_calls[-1][1]
    mapped = pipeline[0]["$set"]["conversation_history"]["$cond"][1]["$map"]
    condition, patch = mapped["in"]["$cond"][:2]
    assert condition["$and"][0]["$in"][1] == {"$literal": ["$private-turn"]}
    switches = patch["$mergeObjects"][1]
    for field in (
        "content_ref",
        "estimated_tokens_compact",
        "brief_summary",
    ):
        switch = switches[field]["$switch"]
        assert switch["branches"][0]["case"]["$eq"][1] == {"$literal": "$private-turn"}
        assert switch["branches"][0]["then"] == {"$literal": entry[field]}
        assert switch["default"] == f"$$turn.{field}"


@pytest.mark.asyncio
async def test_compact_turns_bulk_skips_already_compact_db_turn(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {
                        "turn_id": "t1",
                        "representation": "compact",
                        "content": None,
                        "content_ref": {"document_id": "existing"},
                    }
                ]
            },
            "conversation_history": [
                {
                    "turn_id": "t1",
                    "representation": "compact",
                    "content": None,
                    "content_ref": {"document_id": "existing"},
                }
            ],
            "total_compactions": 0,
        }
    )

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [
            {
                "turn_id": "t1",
                "content_ref": {"document_id": "stale"},
                "estimated_tokens_compact": 5,
            }
        ],
    )

    doc = await memory_repo.get_room_memory("r1")
    assert ok is False
    assert doc["conversation_history"][0]["content_ref"]["document_id"] == "existing"
    assert doc["total_compactions"] == 0


@pytest.mark.asyncio
async def test_compact_turns_bulk_does_not_fallback_to_legacy_history(
    memory_repo, mongo
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "memory_content": {
                "conversation_history": [
                    {"turn_id": "t1", "representation": "full", "content": "one"},
                ]
            },
            "total_compactions": 0,
        }
    )

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [{"turn_id": "t1", "content_ref": {"document_id": "d1"}}],
    )

    doc = await memory_repo.get_room_memory("r1")
    assert ok is False
    assert "conversation_history" not in doc
    assert "conversation_history" not in doc["memory_content"]
    assert len(mongo.collections["room_memories"].update_one_calls) == 1


@pytest.mark.asyncio
async def test_compact_turns_bulk_allows_missing_turn_ids(memory_repo):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "conversation_history": [
                {"turn_id": "t1", "representation": "full", "content": "one"},
            ],
            "total_compactions": 0,
        }
    )

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [
            {"turn_id": "t1", "content_ref": {"document_id": "d1"}},
            {"turn_id": "missing", "content_ref": {"document_id": "d-missing"}},
        ],
    )

    doc = await memory_repo.get_room_memory("r1")
    assert ok is True
    assert doc["total_compactions"] == 1
    assert doc["conversation_history"][0]["representation"] == "compact"
    assert doc["conversation_history"][0]["content_ref"]["document_id"] == "d1"


@pytest.mark.asyncio
async def test_compact_turns_bulk_returns_false_on_atomic_write_failure(
    memory_repo, mongo
):
    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "conversation_history": [
                {"turn_id": "t1", "representation": "full", "content": "one"},
            ],
        }
    )
    mongo.collections["room_memories"].raise_on_update_one = True

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [{"turn_id": "t1", "content_ref": {"document_id": "d1"}}],
    )

    assert ok is False


@pytest.mark.asyncio
async def test_compact_turns_bulk_returns_false_on_mongo_write_failure(
    memory_repo, mongo
):
    class MongoWriteFailure(Exception):
        pass

    await memory_repo.create_room_memory(
        {
            "room_id": "r1",
            "memory_id": "m1",
            "conversation_history": [
                {"turn_id": "t1", "representation": "full", "content": "one"},
            ],
        }
    )
    mongo.collections["room_memories"].update_one_exception = MongoWriteFailure(
        "write failed"
    )

    ok = await memory_repo.compact_turns_bulk(
        "r1",
        [{"turn_id": "t1", "content_ref": {"document_id": "d1"}}],
    )

    assert ok is False


@pytest.mark.asyncio
async def test_content_upsert_and_get(content_repo):
    stored = await content_repo.upsert_full_content(
        document_id="doc1",
        conversation_id="r1",
        turn_id="t1",
        content="hello",
        content_type="text",
        content_hash="hash",
        stored_at=datetime.now(UTC),
    )

    assert stored == "doc1"
    assert (await content_repo.get_content_by_turn_id("r1", "t1"))["content"] == "hello"


@pytest.mark.asyncio
async def test_content_upsert_replaces_existing_content_and_expiry(content_repo):
    first_expiry = datetime(2026, 1, 1, tzinfo=UTC)
    second_expiry = datetime(2026, 1, 2, tzinfo=UTC)

    await content_repo.upsert_full_content(
        document_id="doc-old",
        conversation_id="r1",
        turn_id="t1",
        content="old",
        content_type="text",
        content_hash="old-hash",
        stored_at=first_expiry,
        expires_at=first_expiry,
    )
    stored = await content_repo.upsert_full_content(
        document_id="doc-new",
        conversation_id="r1",
        turn_id="t1",
        content="new",
        content_type="text/markdown",
        content_hash="new-hash",
        stored_at=second_expiry,
        expires_at=second_expiry,
    )

    doc = await content_repo.get_content_by_turn_id("r1", "t1")
    assert stored == "doc-new"
    assert doc["document_id"] == "doc-new"
    assert doc["content"] == "new"
    assert doc["content_type"] == "text/markdown"
    assert doc["content_hash"] == "new-hash"
    assert doc["stored_at"] == second_expiry
    assert doc["expires_at"] == second_expiry


@pytest.mark.asyncio
async def test_content_delete_by_room(content_repo, mongo):
    coll = mongo.collections["conversation_content"]
    await coll.insert_one({"room_id": "r1", "turn_id": "t1"})
    await coll.insert_one({"room_id": "r1", "turn_id": "t2"})
    await coll.insert_one({"room_id": "r2", "turn_id": "t3"})

    assert await content_repo.delete_content_by_room_id("r1") == 2
    assert len(coll.documents) == 1


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(doc, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(doc, item) for item in expected):
                return False
            continue
        actual = _get_path(doc, key)
        if isinstance(expected, dict):
            if "$elemMatch" in expected:
                if not isinstance(actual, list):
                    return False
                if not any(
                    isinstance(item, dict) and _matches(item, expected["$elemMatch"])
                    for item in actual
                ):
                    return False
            elif "$ne" in expected:
                values = _values_for_path(doc, key)
                if any(value == expected["$ne"] for value in values):
                    return False
            elif "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif "$gt" in expected:
                if actual is None or actual <= expected["$gt"]:
                    return False
            elif "$exists" in expected:
                exists = actual is not None
                if exists is not bool(expected["$exists"]):
                    return False
            else:
                return False
        elif actual != expected:
            values = _values_for_path(doc, key)
            if expected not in values:
                return False
    return True


def _get_path(doc: dict, path: str):
    current = doc
    for part in path.split("."):
        if isinstance(current, list):
            return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _values_for_path(doc: dict, path: str) -> list:
    parts = path.split(".")
    values = [doc]
    for part in parts:
        next_values = []
        for value in values:
            if isinstance(value, list):
                next_values.extend(
                    item.get(part) for item in value if isinstance(item, dict)
                )
            elif isinstance(value, dict):
                found = value.get(part)
                if isinstance(found, list):
                    next_values.extend(found)
                else:
                    next_values.append(found)
        values = next_values
    return values


def _apply_update(
    doc: dict, update: dict | list[dict], *, array_filters=None, is_insert=False
) -> None:
    if isinstance(update, list):
        for stage in update:
            stage_source = deepcopy(doc)
            for path, value in stage.get("$set", {}).items():
                if path.endswith("conversation_history"):
                    _set_path(
                        doc,
                        path,
                        _eval_history_expression(stage_source, value),
                    )
                elif path == "memory_content.summary":
                    if "$cond" in value or "$let" in value:
                        _set_path(
                            doc, path, _eval_summary_expression(stage_source, value)
                        )
                    else:
                        current = _get_path(doc, path) or ""
                        stub = value["$substrCP"][0]["$concat"][2]
                        _set_path(
                            doc, path, (current + "\n" + stub)[: value["$substrCP"][2]]
                        )
                elif path == "last_activity_at":
                    _set_path(doc, path, "$$NOW")
                elif path == "total_messages":
                    _set_path(doc, path, (_get_path(doc, path) or 0) + 1)
                elif path == "total_compactions":
                    _set_path(doc, path, (_get_path(doc, path) or 0) + 1)
        return
    if is_insert:
        for key, value in update.get("$setOnInsert", {}).items():
            _set_path(doc, key, deepcopy(value))
    for key, value in update.get("$set", {}).items():
        if "$[turn]" in key:
            _set_array_filtered(doc, key, value, array_filters)
        elif ".$." in key:
            array_path, _, field_path = key.partition(".$.")
            turn_id = query_turn_id(doc, array_path)
            for turn in _get_path(doc, array_path) or []:
                if turn.get("turn_id") == turn_id:
                    _set_path(turn, field_path, deepcopy(value))
        else:
            _set_path(doc, key, deepcopy(value))
    for key, value in update.get("$inc", {}).items():
        _set_path(doc, key, (_get_path(doc, key) or 0) + value)
    for key, value in update.get("$push", {}).items():
        existing = _get_path(doc, key) or []
        if isinstance(value, dict) and "$each" in value:
            existing.extend(deepcopy(value["$each"]))
            if "$slice" in value:
                existing = existing[value["$slice"] :]
        else:
            existing.append(deepcopy(value))
        _set_path(doc, key, existing)


def _assert_no_update_path_conflicts(update: dict | list[dict]) -> None:
    if not isinstance(update, dict):
        return
    paths: list[str] = []
    for operator in ("$set", "$setOnInsert"):
        paths.extend(update.get(operator, {}).keys())
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if (
                left == right
                or left.startswith(f"{right}.")
                or right.startswith(f"{left}.")
            ):
                raise RuntimeError(
                    f"Mongo update path conflict: {left} conflicts with {right}"
                )


def _eval_summary_expression(doc: dict, expression: dict) -> str:
    outer_vars = expression["$let"]["vars"]
    addition_cond = outer_vars["addition"]["$cond"]
    max_turns = addition_cond[0]["$gte"][1]
    cap_cond = expression["$let"]["in"]["$let"]["in"]["$cond"]
    max_summary_chars = cap_cond[0]["$gt"][1]
    existing = _get_path(doc, "memory_content.summary") or ""
    history = _get_path(doc, "conversation_history")
    canonical = history if isinstance(history, list) else []
    evicted = (
        canonical[len(canonical) - max_turns] if len(canonical) >= max_turns else None
    )
    addition = _turn_summary_preview(evicted) if evicted is not None else ""
    concatenated = (
        existing
        if not addition
        else addition
        if not existing
        else f"{existing}\n{addition}"
    )
    if len(concatenated) > max_summary_chars:
        return "..." + concatenated[-(max_summary_chars - 3) :]
    return concatenated


def _turn_summary_preview(turn: dict) -> str:
    role = turn.get("role")
    if role == "user":
        label = "User"
    elif role == "agent":
        label = turn.get("agent_name") or "Agent"
    elif role == "supervisor":
        label = "Supervisor"
    else:
        label = role or "Unknown"
    content = turn.get("content")
    if not isinstance(content, str):
        brief_summary = turn.get("brief_summary")
        content = brief_summary if isinstance(brief_summary, str) else "[compact turn]"
    return f"[{label}] {content[:200]}..."


def _eval_history_expression(doc: dict, expression) -> list[dict]:
    if isinstance(expression, dict) and set(expression) == {"$literal"}:
        return deepcopy(expression["$literal"])
    if _contains_operator(expression, "$map"):
        entries = _compact_entries_from_expression(expression)
        direct = _get_path(doc, "conversation_history")
        turns = deepcopy(direct) if isinstance(direct, list) else []
        for turn in turns:
            if turn.get("turn_id") in entries and turn.get("representation") == "full":
                entry = entries[turn["turn_id"]]
                turn["representation"] = "compact"
                turn["content"] = None
                turn["content_ref"] = deepcopy(entry["content_ref"])
                turn["estimated_tokens_compact"] = entry["estimated_tokens_compact"]
                turn["brief_summary"] = entry["brief_summary"]
        if "$slice" in expression:
            return turns[expression["$slice"][1] :]
        return turns
    if "$concatArrays" in expression:
        base = _eval_history_expression(doc, expression["$concatArrays"][0])
        addition = _eval_history_expression(doc, expression["$concatArrays"][1])
        return base + addition
    if "$slice" in expression:
        source = _eval_history_expression(doc, expression["$slice"][0])
        limit_expr = expression["$slice"][1]
        limit = (
            limit_expr["$multiply"][0] * limit_expr["$multiply"][1]
            if isinstance(limit_expr, dict) and "$multiply" in limit_expr
            else limit_expr
        )
        return source[limit:]
    if "$cond" in expression:
        direct = _get_path(doc, "conversation_history")
        return deepcopy(direct) if isinstance(direct, list) else []
    if isinstance(expression, str):
        return deepcopy(_get_path(doc, expression.lstrip("$")) or [])
    raise AssertionError(f"Unsupported history expression: {expression}")


def _contains_operator(expression, operator: str) -> bool:
    if isinstance(expression, dict):
        if "$literal" in expression:
            return operator == "$literal"
        return operator in expression or any(
            _contains_operator(value, operator) for value in expression.values()
        )
    if isinstance(expression, list):
        return any(_contains_operator(value, operator) for value in expression)
    return False


def _set_array_filtered(doc: dict, path: str, value, array_filters) -> None:
    filter_turn_id = array_filters[0]["turn.turn_id"]
    array_path, _, field_path = path.partition(".$[turn].")
    turns = _get_path(doc, array_path) or []
    for turn in turns:
        if turn.get("turn_id") == filter_turn_id:
            _set_path(turn, field_path, deepcopy(value))


def _set_path(doc: dict, path: str, value) -> None:
    current = doc
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def query_turn_id(doc: dict, array_path: str) -> str | None:
    for turn in _get_path(doc, array_path) or []:
        return turn.get("turn_id")
    return None


def _compact_entries_from_expression(expression: dict) -> dict[str, dict]:
    map_expression = _find_operator(expression, "$map")
    patch = map_expression["$map"]["in"]["$cond"][1]["$mergeObjects"][1]
    ref_branches = patch["content_ref"]["$switch"]["branches"]
    token_branches = patch["estimated_tokens_compact"]["$switch"]["branches"]
    summary_branches = patch["brief_summary"]["$switch"]["branches"]
    entries: dict[str, dict] = {}
    for branch in ref_branches:
        turn_id = _eval_literal(branch["case"]["$eq"][1])
        entries.setdefault(turn_id, {})["content_ref"] = _eval_literal(branch["then"])
    for branch in token_branches:
        turn_id = _eval_literal(branch["case"]["$eq"][1])
        entries.setdefault(turn_id, {})["estimated_tokens_compact"] = _eval_literal(
            branch["then"]
        )
    for branch in summary_branches:
        turn_id = _eval_literal(branch["case"]["$eq"][1])
        entries.setdefault(turn_id, {})["brief_summary"] = _eval_literal(branch["then"])
    return entries


def _eval_literal(expression):
    if isinstance(expression, dict) and set(expression) == {"$literal"}:
        return deepcopy(expression["$literal"])
    return deepcopy(expression)


def _find_operator(expression: dict, operator: str) -> dict:
    if operator in expression:
        return expression
    for value in expression.values():
        if isinstance(value, dict):
            try:
                return _find_operator(value, operator)
            except KeyError:
                pass
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    try:
                        return _find_operator(item, operator)
                    except KeyError:
                        pass
    raise KeyError(operator)


class _IndexedCollection:
    def __init__(self, indexes: dict, *, fail_drop: bool = False):
        self.indexes = dict(indexes)
        self.fail_drop = fail_drop
        self.dropped: list[str] = []
        self.created: list[str] = []

    async def index_information(self):
        return dict(self.indexes)

    async def drop_index(self, name: str):
        if self.fail_drop:
            raise RuntimeError("drop failed")
        self.dropped.append(name)
        self.indexes.pop(name, None)

    async def create_index(self, keys, **kwargs):
        self.created.append(kwargs["name"])
        return kwargs["name"]


class _IndexedMongo:
    def __init__(self, content):
        self.content = content

    def collection(self, name: str):
        return self.content


async def test_ensure_indexes_drops_obsolete_turn_notes_text_index():
    content = _IndexedCollection({"_id_": {}, "turn_notes_text": {}})

    await ContentStorageMongoRepository(mongo=_IndexedMongo(content)).ensure_indexes()

    assert content.dropped == ["turn_notes_text"]
    assert "turn_notes_text" not in content.created
    assert "room_turn_unique" in content.created


async def test_ensure_indexes_skips_drop_when_obsolete_index_is_absent():
    content = _IndexedCollection({"_id_": {}})

    await ContentStorageMongoRepository(mongo=_IndexedMongo(content)).ensure_indexes()

    assert content.dropped == []


async def test_obsolete_index_drop_failure_does_not_block_startup(caplog):
    content = _IndexedCollection({"turn_notes_text": {}}, fail_drop=True)

    await ContentStorageMongoRepository(mongo=_IndexedMongo(content)).ensure_indexes()

    assert "room_turn_unique" in content.created
    [record] = [
        r for r in caplog.records if r.getMessage() == "obsolete_index_drop_failed"
    ]
    assert record.levelno == logging.WARNING
    assert record.collection_name == "conversation_content"
    assert record.index_name == "turn_notes_text"
    assert record.error_type == "RuntimeError"


async def test_compact_turns_bulk_write_failure_logs_turn_count(caplog):
    class _FailingMemories:
        async def update_one(self, *_args, **_kwargs):
            raise RuntimeError("write failed")

    class _Mongo:
        def collection(self, _name):
            return _FailingMemories()

    repo = MemoryMongoRepository(mongo=_Mongo())
    entries = [{"turn_id": "t1"}, {"turn_id": "t2"}]

    with caplog.at_level(logging.ERROR, logger="context_memory.repository.mongo"):
        assert await repo.compact_turns_bulk("r1", entries) is False

    [record] = [
        r for r in caplog.records if r.getMessage() == "turn_compaction_write_failed"
    ]
    assert record.conversation_id == "r1"
    assert record.compacted_turn_count == 2
    assert record.error_type == "RuntimeError"
