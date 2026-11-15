from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from approval.repository import HITLLifecycleStore, HITLRequestStore
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
