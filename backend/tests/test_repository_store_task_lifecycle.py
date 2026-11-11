from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from approval.repository import HITLLifecycleStore, HITLRequestStore
from common.a2a_constants import TERMINAL_STATES
from conversation.repository import ConversationMessageStore


class FakeMongo:
    def __init__(self, collections: dict[str, RecordingCollection] | None = None):
        self.collections = collections or {}

    def collection(self, name: str):
        self.collections.setdefault(name, RecordingCollection())
        return self.collections[name]


class RecordingCollection:
    def __init__(
        self,
        results: list[Any] | None = None,
        *,
        side_effect: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.side_effect = side_effect
        self.update_one_calls: list[tuple[dict, dict | list, dict]] = []
        self.insert_one_calls: list[dict] = []
        self.find_one_calls: list[dict] = []
        self.find_calls: list[tuple[dict, dict]] = []
        self.find_one_and_update_calls: list[tuple[dict, dict, dict]] = []
        self.count_calls: list[dict] = []
        self.create_index_calls: list[tuple[list[tuple[str, int]], dict]] = []

    async def update_one(self, query: dict, update: dict | list, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        self.update_one_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(modified_count=1)

    async def insert_one(self, document: dict):
        if self.side_effect is not None:
            raise self.side_effect
        self.insert_one_calls.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id", "inserted-1"))

    async def find_one(self, query: dict):
        if self.side_effect is not None:
            raise self.side_effect
        self.find_one_calls.append(deepcopy(query))
        if self.results:
            return self.results.pop(0)
        return None

    async def find(self, query: dict, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        self.find_calls.append((deepcopy(query), deepcopy(kwargs)))
        if self.results:
            return self.results.pop(0)
        return []

    async def find_one_and_update(self, query: dict, update: dict, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        self.find_one_and_update_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        if self.results:
            return self.results.pop(0)
        return None

    async def count(self, query: dict):
        if self.side_effect is not None:
            raise self.side_effect
        self.count_calls.append(deepcopy(query))
        if self.results:
            return self.results.pop(0)
        return 0

    async def create_index(self, keys: list[tuple[str, int]], **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        self.create_index_calls.append((deepcopy(keys), deepcopy(kwargs)))
        return "index-name"

    async def drop_index(self, name: str):
        if self.side_effect is not None:
            raise self.side_effect


def _result(modified_count: int):
    return SimpleNamespace(modified_count=modified_count)


class _Stores:
    """Message calls go to the conversation store, HITL calls to the approval stores."""

    def __init__(self, mongo: FakeMongo) -> None:
        message_store = ConversationMessageStore(
            room_agent_messages=mongo.collection("room_agent_messages"),
            room_user_messages=mongo.collection("room_user_messages"),
            message_repository=object(),
        )
        self._targets = (
            message_store,
            HITLRequestStore(
                hitl_requests=mongo.collection("hitl_requests"),
                message_writer=message_store,
            ),
            HITLLifecycleStore(
                interactions=mongo.collection("hitl_interactions"),
                resume_commands=mongo.collection("hitl_resume_commands"),
                hitl_requests=mongo.collection("hitl_requests"),
            ),
        )

    def __getattr__(self, name: str):
        for target in self._targets:
            if hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)


def _store(
    collection: RecordingCollection | None = None,
    *,
    hitl_collection: RecordingCollection | None = None,
) -> _Stores:
    return _Stores(
        FakeMongo(
            {
                "room_agent_messages": collection or RecordingCollection(),
                "hitl_requests": hitl_collection or RecordingCollection(),
            }
        )
    )


def _set_stage(update_doc: dict | list) -> dict:
    if isinstance(update_doc, list):
        return update_doc[0]["$set"]
    return update_doc["$set"]


def _assert_terminal_state_filter(query: dict) -> None:
    state_filter = query["message_content.message_task.status.state"]
    assert set(state_filter["$nin"]) == {state.value for state in TERMINAL_STATES}


@pytest.mark.asyncio
async def test_terminal_finalization_matches_exact_completed_journal_recovery():
    collection = RecordingCollection([{"message_id": "msg-1"}])
    store = _store(collection)

    assert await store.terminal_finalization_matches(
        "msg-1",
        "completed",
        recovery_source="journal",
        recovery_id="journal-1",
    )
    assert collection.find_one_calls == [
        {
            "message_id": "msg-1",
            "message_content.message_task.status.state": "completed",
            "terminal_finalization.state": "terminal",
            "terminal_finalization.target_state": "completed",
            "terminal_finalization.recovery_source": "journal",
            "terminal_finalization.recovery_id": "journal-1",
        }
    ]


@pytest.mark.asyncio
async def test_terminal_finalization_without_recovery_id_never_matches():
    collection = RecordingCollection()
    store = _store(collection)

    assert not await store.terminal_finalization_matches(
        "msg-1",
        "completed",
        recovery_source="journal",
        recovery_id=None,
    )
    assert collection.find_one_calls == []


class TestRepositoryStoreAccumulateArtifact:
    @pytest.mark.asyncio
    async def test_missing_artifact_id_pushes_new_artifact(self):
        collection = RecordingCollection([_result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"parts": [{"kind": "text", "text": "hello"}]},
        )

        assert result is True
        query, update_doc, _ = collection.update_one_calls[0]
        assert query["message_id"] == "msg-1"
        _assert_terminal_state_filter(query)
        assert update_doc["$push"]["message_content.message_task.artifacts"] == {
            "parts": [{"kind": "text", "text": "hello"}]
        }
        assert _set_stage(update_doc)["message_content.message_text"] == "hello"

    @pytest.mark.asyncio
    async def test_append_false_replaces_existing_artifact_atomically(self):
        collection = RecordingCollection([_result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-1", "parts": [{"kind": "text", "text": "new"}]},
            append=False,
        )

        assert result is True
        query, update_doc, _ = collection.update_one_calls[0]
        assert query["message_content.message_task.artifacts"] == {
            "$elemMatch": {"$or": [{"artifactId": "art-1"}, {"artifact_id": "art-1"}]}
        }
        set_stage = _set_stage(update_doc)
        assert "$map" in set_stage["message_content.message_task.artifacts"]
        assert set_stage["message_content.message_text"] == "new"
        assert set_stage["message_content.message_task.status.state"] == "working"

    @pytest.mark.asyncio
    async def test_append_false_inserts_when_artifact_id_not_found(self):
        collection = RecordingCollection([_result(0), _result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-new", "parts": [{"kind": "text", "text": "content"}]},
            append=False,
        )

        assert result is True
        assert len(collection.update_one_calls) == 2
        _, insert_update, _ = collection.update_one_calls[1]
        assert insert_update["$push"]["message_content.message_task.artifacts"] == {
            "artifactId": "art-new",
            "parts": [{"kind": "text", "text": "content"}],
        }
        assert insert_update["$set"]["message_content.message_text"] == "content"

    @pytest.mark.asyncio
    async def test_append_true_extends_parts_and_concats_text_atomically(self):
        collection = RecordingCollection([_result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-1", "parts": [{"kind": "text", "text": " more"}]},
            append=True,
        )

        assert result is True
        query, update_doc, _ = collection.update_one_calls[0]
        assert "$elemMatch" in query["message_content.message_task.artifacts"]
        set_stage = _set_stage(update_doc)
        assert "$map" in set_stage["message_content.message_task.artifacts"]
        assert set_stage["message_content.message_text"] == {
            "$concat": [{"$ifNull": ["$message_content.message_text", ""]}, " more"]
        }

    @pytest.mark.asyncio
    async def test_append_true_inserts_when_artifact_id_not_found(self):
        collection = RecordingCollection([_result(0), _result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {
                "artifactId": "art-new",
                "parts": [{"kind": "text", "text": "first chunk"}],
            },
            append=True,
        )

        assert result is True
        assert len(collection.update_one_calls) == 2
        _, insert_update, _ = collection.update_one_calls[1]
        assert "$push" in insert_update
        assert insert_update["$set"]["message_content.message_text"] == "first chunk"

    @pytest.mark.asyncio
    async def test_append_true_returns_false_when_sanitizer_drops_all_parts(self):
        collection = RecordingCollection()
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-1", "parts": [{"kind": "text"}]},
            append=True,
        )

        assert result is False


class TestRepositoryStoreHITL:
    @pytest.mark.asyncio
    async def test_creates_and_reads_pending_hitl_requests_by_shape(self):
        hitl_requests = RecordingCollection([[{"request_id": "h1"}]])
        store = _store(hitl_collection=hitl_requests)

        assert await store.create_hitl_request({"request_id": "h1"})
        pending = await store.get_pending_hitl_requests_for_message("u1")

        assert pending == [{"request_id": "h1"}]
        assert hitl_requests.insert_one_calls == [{"request_id": "h1"}]
        assert len(hitl_requests.find_calls) == 1
        query, options = hitl_requests.find_calls[0]
        assert query["$and"][0] == {
            "user_message_id": "u1",
            "status": "pending",
        }
        assert {"expires_at": None} in query["$and"][1]["$or"]
        assert options == {}

    @pytest.mark.asyncio
    async def test_persists_and_clears_hitl_request_id_on_display_message(self):
        with_request = {
            "message_content": {
                "message_task": {"metadata": {"hitl_request_id": "hitl-supervisor-1"}}
            }
        }
        without_request = {"message_content": {"message_task": {"metadata": {}}}}
        messages = RecordingCollection(
            [_result(1), with_request, _result(1), without_request]
        )
        store = _store(messages)

        assert await store.persist_hitl_request_id_on_message(
            "display-msg-1", "hitl-supervisor-1"
        )
        assert await store.persist_hitl_request_id_on_message("display-msg-1", None)

        assert messages.find_one_and_update_calls[0][0] == {
            "message_id": "display-msg-1"
        }
        assert messages.find_one_and_update_calls[0][1] == {
            "$set": {
                "message_content.message_task.metadata.hitl_request_id": (
                    "hitl-supervisor-1"
                )
            }
        }
        assert messages.find_one_and_update_calls[1][1] == {
            "$unset": {"message_content.message_task.metadata.hitl_request_id": ""}
        }

    @pytest.mark.asyncio
    async def test_cas_and_fenced_updates_preserve_concurrency_guards(self):
        hitl_requests = RecordingCollection([_result(1), _result(1)])
        store = _store(hitl_collection=hitl_requests)

        assert await store.cas_update_hitl_request(
            "h1",
            expected_status="processing",
            status="responded",
        )
        assert await store.fenced_update_hitl_request(
            "h1",
            claim_id="claim-1",
            status="responded",
        )

        assert hitl_requests.update_one_calls[0] == (
            {"request_id": "h1", "status": "processing"},
            {"$set": {"status": "responded"}},
            {},
        )
        assert hitl_requests.update_one_calls[1] == (
            {"request_id": "h1", "claim_id": "claim-1"},
            {"$set": {"status": "responded"}},
            {},
        )

    @pytest.mark.asyncio
    async def test_indexes_use_hitl_collection(self):
        hitl_requests = RecordingCollection([])
        store = _store(hitl_collection=hitl_requests)

        await store.ensure_indexes()

        assert hitl_requests.create_index_calls == [
            ([("request_id", 1)], {"unique": True}),
            ([("room_id", 1), ("status", 1)], {}),
            ([("expires_at", 1), ("status", 1)], {}),
            ([("user_message_id", 1), ("status", 1)], {}),
            (
                [("interaction_id", 1), ("question_index", 1)],
                {
                    "unique": True,
                    "name": "uq_hitl_interaction_question",
                    "partialFilterExpression": {
                        "interaction_id": {"$type": "string"},
                    },
                },
            ),
            ([("continuation_message_id", 1)], {}),
            (
                [("room_id", 1), ("display_message_id", 1)],
                {
                    "unique": True,
                    "name": "uq_pending_hitl_display_message",
                    "partialFilterExpression": {
                        "status": "pending",
                        "public_source": "agent",
                        "display_message_id": {"$type": "string"},
                    },
                },
            ),
            (
                [("room_id", 1), ("continuation_message_id", 1)],
                {
                    "unique": True,
                    "name": "uq_pending_hitl_continuation_message",
                    "partialFilterExpression": {
                        "status": "pending",
                        "public_source": "agent",
                        "continuation_message_id": {"$type": "string"},
                    },
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_terminal_state_filter_applies_to_replace_and_insert_paths(self):
        collection = RecordingCollection([_result(0), _result(1)])
        store = _store(collection)

        await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-1", "parts": [{"kind": "text", "text": "x"}]},
            append=False,
        )

        for query, _, _ in collection.update_one_calls:
            _assert_terminal_state_filter(query)

    @pytest.mark.asyncio
    async def test_handles_artifact_id_snake_case(self):
        collection = RecordingCollection([_result(1)])
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifact_id": "art-snake", "parts": [{"kind": "text", "text": "x"}]},
        )

        assert result is True
        query, _, _ = collection.update_one_calls[0]
        elem_match = query["message_content.message_task.artifacts"]["$elemMatch"]
        assert {"artifact_id": "art-snake"} in elem_match["$or"]

    @pytest.mark.asyncio
    async def test_extracts_text_from_nested_root_text(self):
        collection = RecordingCollection([_result(1)])
        store = _store(collection)

        await store.accumulate_artifact_on_message(
            "msg-1",
            {
                "artifactId": "art-1",
                "parts": [{"root": {"kind": "text", "text": "nested text"}}],
            },
            append=False,
        )

        _, update_doc, _ = collection.update_one_calls[0]
        assert _set_stage(update_doc)["message_content.message_text"] == "nested text"

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        collection = RecordingCollection(side_effect=RuntimeError("connection lost"))
        store = _store(collection)

        result = await store.accumulate_artifact_on_message(
            "msg-1",
            {"artifactId": "art-1", "parts": [{"kind": "text", "text": "x"}]},
        )

        assert result is False
