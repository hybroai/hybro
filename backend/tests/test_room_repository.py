from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from common.dto.base import to_stored_conversation_keys
from conversation.dto import TimelinePosition
from conversation.idempotency import (
    IdempotencyConflictError,
    UnexpectedUserMessageDuplicateError,
)
from conversation.repository import MessageMongoRepository, RoomMongoRepository
from models.room import MessageContent, RoomAgentMessage, RoomUserMessage


class _ConversationStores:
    """Serve message and room calls from the conversation stores, agent calls
    from the agent store, and HITL calls from the approval stores."""

    def __init__(
        self, *, mongo, room_repository, message_repository, agent_repository
    ) -> None:
        from agent.repository import AgentRuntimeStore
        from approval.repository import HITLLifecycleStore, HITLRequestStore
        from conversation.repository import (
            ConversationMessageStore,
            ConversationRoomStore,
        )

        message_store = ConversationMessageStore(
            room_agent_messages=mongo.collection("room_agent_messages"),
            room_user_messages=mongo.collection("room_user_messages"),
            message_repository=message_repository,
        )
        self._targets = (
            message_store,
            ConversationRoomStore(room_repository=room_repository),
            AgentRuntimeStore(
                agent_groups=mongo.collection("agent_groups"),
                agent_repository=agent_repository,
            ),
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


class FakeMongo:
    def __init__(self, collections: dict[str, FakeCollection] | None = None) -> None:
        self.collections = collections or {}
        self.collection_calls: list[str] = []

    def collection(self, name: str) -> FakeCollection:
        self.collection_calls.append(name)
        self.collections.setdefault(name, FakeCollection())
        return self.collections[name]


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = [deepcopy(doc) for doc in docs or []]
        self.find_one_calls: list[dict] = []
        self.find_calls: list[tuple[dict, dict]] = []
        self.insert_one_calls: list[dict] = []
        self.update_one_calls: list[tuple[dict, dict, dict]] = []
        self.replace_one_calls: list[tuple[dict, dict, dict]] = []
        self.find_one_and_update_calls: list[tuple[dict, dict, dict]] = []
        self.delete_one_calls: list[dict] = []
        self.delete_many_calls: list[dict] = []

    async def find_one(self, query: dict, **kwargs) -> dict | None:
        self.find_one_calls.append(deepcopy(query))
        for doc in self.docs:
            if _matches(doc, query):
                return deepcopy(doc)
        return None

    async def find(self, query: dict, **kwargs) -> list[dict]:
        self.find_calls.append((deepcopy(query), deepcopy(kwargs)))
        matches = [deepcopy(doc) for doc in self.docs if _matches(doc, query)]
        sort = kwargs.get("sort")
        if sort:
            for field, direction in reversed(sort):
                matches.sort(
                    key=lambda doc: _get_dotted(doc, field),
                    reverse=direction < 0,
                )
        limit = kwargs.get("limit")
        return matches[:limit] if limit else matches

    async def insert_one(self, document: dict) -> str:
        self.insert_one_calls.append(deepcopy(document))
        self.docs.append(deepcopy(document))
        return str(document.get("_id") or f"inserted-{len(self.docs)}")

    async def update_one(self, query: dict, update: dict, **kwargs) -> bool:
        self.update_one_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        for doc in self.docs:
            if _matches(doc, query):
                _apply_update(doc, update)
                return True
        return False

    async def replace_one(self, query: dict, replacement: dict, **kwargs) -> bool:
        self.replace_one_calls.append(
            (deepcopy(query), deepcopy(replacement), deepcopy(kwargs))
        )
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs[index] = deepcopy(replacement)
                return True
        if kwargs.get("upsert"):
            self.docs.append(deepcopy(replacement))
            return True
        return False

    async def update_many(self, query: dict, update: dict) -> int:
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                _apply_update(doc, update)
                count += 1
        return count

    async def find_one_and_update(
        self, query: dict, update: dict, **kwargs
    ) -> dict | None:
        self.find_one_and_update_calls.append(
            (deepcopy(query), deepcopy(update), deepcopy(kwargs))
        )
        for doc in self.docs:
            if _matches(doc, query):
                _apply_update(doc, update)
                return deepcopy(doc)
        return None

    async def delete_one(self, query: dict) -> bool:
        self.delete_one_calls.append(deepcopy(query))
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs.pop(index)
                return True
        return False

    async def delete_many(self, query: dict) -> int:
        self.delete_many_calls.append(deepcopy(query))
        kept = []
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                count += 1
            else:
                kept.append(doc)
        self.docs = kept
        return count


class AtomicUniqueUserMessageCollection(FakeCollection):
    """Fake Mongo collection that atomically enforces both user-message indexes."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        super().__init__(docs)
        self._insert_lock = asyncio.Lock()

    async def insert_one(self, document: dict) -> str:
        async with self._insert_lock:
            await asyncio.sleep(0)
            message_id = document.get("message_id")
            conversation_id = document.get("room_id")
            client_request_id = document.get("client_request_id")
            if any(doc.get("message_id") == message_id for doc in self.docs):
                raise DuplicateKeyError("duplicate message_id")
            if (
                isinstance(conversation_id, str)
                and isinstance(client_request_id, str)
                and any(
                    doc.get("room_id") == conversation_id
                    and doc.get("client_request_id") == client_request_id
                    for doc in self.docs
                )
            ):
                raise DuplicateKeyError("duplicate room/client_request_id")
            return await super().insert_one(document)


def _room_repo(docs: list[dict] | None = None):
    rooms = FakeCollection(docs)
    mongo = FakeMongo({"rooms": rooms})
    return RoomMongoRepository(mongo=mongo), mongo, rooms


def _message_repo(
    user_docs: list[dict] | None = None,
    agent_docs: list[dict] | None = None,
):
    user_messages = FakeCollection(user_docs)
    agent_messages = FakeCollection(agent_docs)
    mongo = FakeMongo(
        {
            "room_user_messages": user_messages,
            "room_agent_messages": agent_messages,
        }
    )
    return MessageMongoRepository(mongo=mongo), mongo, user_messages, agent_messages


@pytest.mark.asyncio
async def test_room_repository_uses_rooms_collection_and_query_shapes():
    repo, mongo, rooms = _room_repo(
        [{"room_id": "r1", "room_owner_id": "u1", "room_name": "Room"}]
    )

    assert await repo.get_by_id("r1") == {
        "room_id": "r1",
        "room_owner_id": "u1",
        "room_name": "Room",
    }
    assert await repo.get_by_owner("u1") == [
        {"room_id": "r1", "room_owner_id": "u1", "room_name": "Room"}
    ]

    assert mongo.collection_calls == ["rooms"]
    assert rooms.find_one_calls == [{"room_id": "r1"}]
    assert rooms.find_calls == [
        (
            {
                "room_owner_id": "u1",
                "$or": [
                    {"lifecycle_state": "active"},
                    {"lifecycle_state": {"$exists": False}},
                ],
            },
            {},
        )
    ]


@pytest.mark.asyncio
async def test_room_repository_create_update_update_fields_set_membership_and_delete():
    repo, _, rooms = _room_repo([{"room_id": "r1", "room_name": "Old"}])

    created_id = await repo.create({"room_id": "r2", "room_name": "New"})
    updated = await repo.update("r1", {"room_name": "Changed"})
    updated_doc = await repo.update_fields("r1", {"pin_order": 2.0})
    membership_doc = await repo.set_membership(
        "r1",
        agent_set={"a1": "Agent One"},
        membership_origin="saved_group",
        membership_origin_status="seeded_never_edited",
        source_group_id="g1",
        source_group_name="Group One",
    )
    deleted = await repo.delete("r2")

    assert created_id == "r2"
    assert updated is True
    assert updated_doc["pin_order"] == 2.0
    assert membership_doc["room_agent_set"] == {"a1": "Agent One"}
    assert membership_doc["membership_origin"] == "saved_group"
    assert membership_doc["membership_origin_status"] == "seeded_never_edited"
    assert membership_doc["source_group_id"] == "g1"
    assert membership_doc["source_group_name"] == "Group One"
    assert deleted is True
    assert rooms.insert_one_calls == [{"room_id": "r2", "room_name": "New"}]
    assert rooms.update_one_calls[0] == (
        {
            "room_id": "r1",
            "$or": [
                {"lifecycle_state": "active"},
                {"lifecycle_state": {"$exists": False}},
            ],
        },
        {"$set": {"room_name": "Changed"}},
        {},
    )
    assert rooms.find_one_and_update_calls[-1][0] == {
        "room_id": "r1",
        "$or": [
            {"lifecycle_state": "active"},
            {"lifecycle_state": {"$exists": False}},
        ],
    }
    assert rooms.delete_one_calls == [{"room_id": "r2"}]
    assert all(isinstance(doc, dict) for doc in rooms.docs)


@pytest.mark.asyncio
async def test_message_repository_uses_room_message_collections_and_saves_raw_dicts():
    repo, mongo, user_messages, agent_messages = _message_repo()

    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    user_id = await repo.save_user_message(
        {"message_id": "u1", "room_id": "r1", "message_created_at": created_at}
    )
    agent_id = await repo.save_agent_message(
        {"message_id": "a1", "room_id": "r1", "message_created_at": created_at}
    )

    assert mongo.collection_calls == ["room_user_messages", "room_agent_messages"]
    assert user_id == "u1"
    assert agent_id == "a1"
    assert user_messages.insert_one_calls == [
        {
            "message_id": "u1",
            "room_id": "r1",
            "message_created_at": created_at,
            "timeline_sort_us": 1767225600000000,
        }
    ]
    assert agent_messages.insert_one_calls == [
        {
            "message_id": "a1",
            "room_id": "r1",
            "message_created_at": created_at,
            "timeline_sort_us": 1767225600000000,
        }
    ]


@pytest.mark.asyncio
async def test_legacy_user_message_save_normalizes_request_key_without_mutating_input():
    repo, _, user_messages, _ = _message_repo()
    message = {
        "message_id": "message-1",
        "room_id": "room-1",
        "client_request_id": "  request-1  ",
        "message_created_at": "2026-01-01T00:00:00Z",
    }

    message_id = await repo.save_user_message(message)

    assert message_id == "message-1"
    assert message["client_request_id"] == "  request-1  "
    assert user_messages.insert_one_calls == [
        {
            "message_id": "message-1",
            "room_id": "room-1",
            "client_request_id": "request-1",
            "message_created_at": "2026-01-01T00:00:00Z",
            "timeline_sort_us": 1767225600000000,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"room_id": "room-1"},
        {"room_id": "room-1", "message_id": " "},
        {"room_id": " ", "message_id": "message-1"},
        {
            "room_id": "room-1",
            "message_id": "message-1",
            "client_request_id": " ",
        },
        {
            "room_id": "room-1",
            "message_id": "message-1",
            "client_request_id": 123,
        },
        {
            "room_id": "room-1",
            "message_id": "message-1",
            "client_request_id": "x" * 129,
        },
    ],
)
async def test_legacy_user_message_save_rejects_invalid_identity(message):
    repo, _, user_messages, _ = _message_repo()

    with pytest.raises(ValueError):
        await repo.save_user_message(message)

    assert user_messages.insert_one_calls == []


def _idempotent_message(
    *,
    conversation_id: str = "r1",
    client_request_id: str = "request-1",
    message_id: str = "message-1",
    fingerprint: str = "fingerprint-1",
) -> dict:
    return {
        "room_id": conversation_id,
        "message_id": message_id,
        "client_request_id": client_request_id,
        "idempotency_fingerprint": fingerprint,
        "idempotency_fingerprint_version": 1,
        "message_type": "user",
        "message_content": {"message_text": "hello"},
        "message_created_at": "2026-01-01T00:00:00Z",
    }


def _idempotent_message_repo(
    docs: list[dict] | None = None,
) -> tuple[MessageMongoRepository, AtomicUniqueUserMessageCollection]:
    user_messages = AtomicUniqueUserMessageCollection(docs)
    mongo = FakeMongo(
        {
            "room_user_messages": user_messages,
            "room_agent_messages": FakeCollection(),
        }
    )
    return MessageMongoRepository(mongo=mongo), user_messages


@pytest.mark.asyncio
async def test_unfingerprinted_row_conflicts_on_shared_normalized_unique_key():
    repo, user_messages = _idempotent_message_repo()
    unfingerprinted = _idempotent_message(client_request_id=" request-1 ")
    unfingerprinted.pop("idempotency_fingerprint")
    unfingerprinted.pop("idempotency_fingerprint_version")

    await repo.save_user_message(unfingerprinted)
    with pytest.raises(IdempotencyConflictError):
        await repo.insert_user_message_idempotently(
            _idempotent_message(message_id="message-2")
        )

    assert len(user_messages.docs) == 1
    assert user_messages.docs[0]["client_request_id"] == "request-1"


@pytest.mark.asyncio
async def test_idempotent_user_message_insert_creates_then_replays_stable_winner():
    repo, user_messages = _idempotent_message_repo()

    created = await repo.insert_user_message_idempotently(_idempotent_message())
    replayed = await repo.insert_user_message_idempotently(
        _idempotent_message(message_id="message-loser")
    )

    assert created.created is True
    assert created.message_id == "message-1"
    assert replayed.created is False
    assert replayed.message_id == "message-1"
    assert replayed.document["message_id"] == "message-1"
    assert len(user_messages.docs) == 1
    assert user_messages.find_one_calls[-1] == {
        "room_id": "r1",
        "client_request_id": "request-1",
    }


@pytest.mark.asyncio
async def test_idempotent_user_message_insert_rejects_payload_conflict():
    repo, user_messages = _idempotent_message_repo([_idempotent_message()])

    with pytest.raises(IdempotencyConflictError):
        await repo.insert_user_message_idempotently(
            _idempotent_message(
                message_id="message-2",
                fingerprint="different-fingerprint",
            )
        )

    assert len(user_messages.docs) == 1


@pytest.mark.asyncio
async def test_message_id_unique_collision_is_not_misclassified_as_replay():
    repo, user_messages = _idempotent_message_repo([_idempotent_message()])

    with pytest.raises(
        UnexpectedUserMessageDuplicateError,
        match="Unexpected user-message unique-index collision",
    ):
        await repo.insert_user_message_idempotently(
            _idempotent_message(
                conversation_id="r2",
                client_request_id="request-2",
                message_id="message-1",
            )
        )

    assert user_messages.find_one_calls[-1] == {
        "room_id": "r2",
        "client_request_id": "request-2",
    }


@pytest.mark.asyncio
async def test_same_client_request_id_is_allowed_in_different_rooms():
    repo, user_messages = _idempotent_message_repo()

    first = await repo.insert_user_message_idempotently(_idempotent_message())
    second = await repo.insert_user_message_idempotently(
        _idempotent_message(conversation_id="r2", message_id="message-2")
    )

    assert first.created is True
    assert second.created is True
    assert len(user_messages.docs) == 2


@pytest.mark.asyncio
async def test_idempotent_repository_enforces_normalized_request_id_length_boundary():
    repo, user_messages = _idempotent_message_repo()

    accepted = await repo.insert_user_message_idempotently(
        _idempotent_message(client_request_id="x" * 128)
    )
    normalized = await repo.insert_user_message_idempotently(
        _idempotent_message(
            client_request_id=" request-with-padding ",
            message_id="message-2",
        )
    )
    with pytest.raises(ValueError, match="valid client_request_id"):
        await repo.insert_user_message_idempotently(
            _idempotent_message(
                client_request_id="y" * 129,
                message_id="message-3",
            )
        )

    assert accepted.created is True
    assert normalized.created is True
    assert user_messages.docs[1]["client_request_id"] == "request-with-padding"
    assert len(user_messages.docs) == 2


@pytest.mark.asyncio
async def test_idempotent_user_message_insert_requires_persisted_message_id():
    repo, user_messages = _idempotent_message_repo()
    message = _idempotent_message()
    del message["message_id"]

    with pytest.raises(ValueError, match="non-empty message_id"):
        await repo.insert_user_message_idempotently(message)

    assert user_messages.docs == []


@pytest.mark.asyncio
async def test_concurrent_duplicate_key_loser_reads_and_returns_winner_message_id():
    repo, user_messages = _idempotent_message_repo()

    first, second = await asyncio.gather(
        repo.insert_user_message_idempotently(
            _idempotent_message(message_id="message-a")
        ),
        repo.insert_user_message_idempotently(
            _idempotent_message(message_id="message-b")
        ),
    )

    assert sorted((first.created, second.created)) == [False, True]
    assert first.message_id == second.message_id
    assert first.message_id in {"message-a", "message-b"}
    assert len(user_messages.docs) == 1


@pytest.mark.asyncio
async def test_user_message_update_cannot_rewrite_persistent_identity_fields():
    repo, _, user_messages, _ = _message_repo(
        user_docs=[
            {
                "room_id": "room-1",
                "message_id": "message-1",
                "client_request_id": "request-1",
                "idempotency_fingerprint": "fingerprint-1",
                "idempotency_fingerprint_version": 1,
            }
        ]
    )

    updated = await repo.update_user_message(
        "message-1",
        {
            "room_id": "other-room",
            "message_id": "other-message",
            "client_request_id": " padded-request ",
            "idempotency_fingerprint": "other-fingerprint",
            "idempotency_fingerprint_version": 2,
            "message_content": {"message_text": "updated"},
        },
    )

    assert updated is True
    assert user_messages.update_one_calls == [
        (
            {"message_id": "message-1"},
            {"$set": {"message_content": {"message_text": "updated"}}},
            {},
        )
    ]
    assert user_messages.docs[0]["room_id"] == "room-1"
    assert user_messages.docs[0]["message_id"] == "message-1"
    assert user_messages.docs[0]["client_request_id"] == "request-1"
    assert user_messages.docs[0]["idempotency_fingerprint"] == "fingerprint-1"


@pytest.mark.asyncio
async def test_message_updates_strip_immutable_timeline_identity():
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    repo, _, users, agents = _message_repo(
        user_docs=[{"message_id": "u1", "room_id": "r1"}],
        agent_docs=[{"message_id": "a1", "room_id": "r1"}],
    )
    updates = {
        "message_id": "rewritten",
        "room_id": "other",
        "message_created_at": created_at,
        "timeline_sort_us": 1,
        "message_content": {"message_text": "allowed"},
    }

    assert await repo.update_user_message("u1", updates) is True
    assert await repo.update_agent_message("a1", updates) is True
    assert users.update_one_calls[-1][1]["$set"] == {
        "message_content": {"message_text": "allowed"}
    }
    assert agents.update_one_calls[-1][1]["$set"] == {
        "message_content": {"message_text": "allowed"}
    }


@pytest.mark.asyncio
async def test_task_state_update_cannot_overwrite_durable_terminal_projection_winner():
    terminal = {
        "message_id": "a1",
        "room_id": "r1",
        "terminal_projection_event_id": "evt-cancel",
        "message_content": {"message_task": {"status": {"state": "canceled"}}},
    }
    repo, _, _, agents = _message_repo(agent_docs=[terminal])

    updated = await repo.update_agent_message(
        "a1",
        {"message_content": {"message_task": {"status": {"state": "completed"}}}},
    )

    assert updated is False
    assert agents.docs[0] == terminal
    query = agents.update_one_calls[-1][0]
    assert "canceled" in query["message_content.message_task.status.state"]["$nin"]


@pytest.mark.asyncio
async def test_same_terminal_state_can_backfill_final_agent_response():
    terminal = {
        "message_id": "a1",
        "room_id": "r1",
        "terminal_projection_event_id": "winner-1",
        "message_content": {
            "message_text": None,
            "message_task": {
                "id": "durable-task",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "durable-artifact"}],
            },
        },
    }
    repo, _, _, agents = _message_repo(agent_docs=[terminal])

    updated = await repo.update_agent_message(
        "a1",
        {
            "message_content": {
                "message_text": "Final travel plan",
                "message_task": {
                    "id": "stale-task",
                    "status": {"state": "completed"},
                    "artifacts": [{"artifactId": "stale-artifact"}],
                },
            }
        },
    )

    assert updated is True
    durable = agents.docs[0]
    assert durable["message_content"]["message_text"] == "Final travel plan"
    assert durable["message_content"]["message_task"]["id"] == "durable-task"
    assert durable["message_content"]["message_task"]["artifacts"] == [
        {"artifactId": "durable-artifact"}
    ]
    assert durable["terminal_projection_event_id"] == "winner-1"
    query, update, _ = agents.update_one_calls[-1]
    assert query == {
        "message_id": "a1",
        "message_content.message_task.status.state": "completed",
        "message_content.message_text": None,
    }
    assert update == {"$set": {"message_content.message_text": "Final travel plan"}}


@pytest.mark.asyncio
async def test_message_repository_get_by_id_searches_user_first_then_agent():
    repo, _, user_messages, agent_messages = _message_repo(
        user_docs=[{"message_id": "u1", "message_type": "user"}],
        agent_docs=[{"message_id": "a1", "message_type": "agent"}],
    )

    assert await repo.get_by_id("u1") == {"message_id": "u1", "message_type": "user"}
    assert await repo.get_by_id("a1") == {"message_id": "a1", "message_type": "agent"}
    assert user_messages.find_one_calls == [
        {"message_id": "u1"},
        {"message_id": "a1"},
    ]
    assert agent_messages.find_one_calls == [{"message_id": "a1"}]


@pytest.mark.asyncio
async def test_message_repository_get_by_ids_combines_collections_and_preserves_order():
    repo, _, _, _ = _message_repo(
        user_docs=[{"message_id": "u1"}, {"message_id": "u2"}],
        agent_docs=[{"message_id": "a1"}],
    )

    assert await repo.get_by_ids(["a1", "missing", "u2", "u1"]) == [
        {"message_id": "a1"},
        {"message_id": "u2"},
        {"message_id": "u1"},
    ]


@pytest.mark.asyncio
async def test_message_repository_direct_room_history_methods_pass_limits():
    repo, _, user_messages, agent_messages = _message_repo(
        user_docs=[{"message_id": "u1", "room_id": "r1"}],
        agent_docs=[{"message_id": "a1", "room_id": "r1"}],
    )

    assert await repo.get_user_messages_for_room("r1", limit=3) == [
        {"message_id": "u1", "room_id": "r1"}
    ]
    assert await repo.get_agent_messages_for_room("r1", limit=4) == [
        {"message_id": "a1", "room_id": "r1"}
    ]
    expected_sort = [("timeline_sort_us", -1), ("message_id", -1)]
    assert user_messages.find_calls[-1] == (
        {"room_id": "r1"},
        {"sort": expected_sort, "limit": 3},
    )
    assert agent_messages.find_calls[-1] == (
        {"room_id": "r1"},
        {"sort": expected_sort, "limit": 4},
    )


@pytest.mark.asyncio
async def test_runtime_store_generates_agent_message_id_when_empty():

    class RecordingMessageRepository:
        def __init__(self) -> None:
            self.saved_docs: list[dict] = []

        async def save_agent_message(self, doc):
            self.saved_docs.append(doc)
            return "saved"

    message_repository = RecordingMessageRepository()
    store = _ConversationStores(
        mongo=FakeMongo(),
        room_repository=object(),
        message_repository=message_repository,
        agent_repository=object(),
    )
    message = RoomAgentMessage(
        conversation_id="r1",
        message_id="",
        agent_id="agent-1",
        message_created_at=datetime(2026, 5, 11, tzinfo=UTC),
        message_content=MessageContent(message_text="created"),
    )

    assert await store.add_room_agent_message(message)
    assert message.message_id
    assert message_repository.saved_docs[0]["message_id"] == message.message_id


@pytest.mark.asyncio
async def test_runtime_store_room_runtime_methods_use_repositories_and_dal():

    room_repo, mongo, rooms = _room_repo(
        [
            {
                "room_id": "r1",
                "room_owner_id": "owner-1",
                "room_owner_name": "Owner",
                "room_name": "Room",
            }
        ]
    )
    message_repo = MessageMongoRepository(mongo=mongo)
    agents = FakeCollection(
        [
            {
                "agent_id": "agent-1",
                "agent_card": {
                    "name": "Agent",
                    "url": "https://agent.example/.well-known/agent.json",
                    "version": "1.0",
                    "capabilities": {},
                    "skills": [],
                },
                "is_public": True,
            }
        ]
    )
    runs = FakeCollection(
        [
            {
                "run_id": "old",
                "room_id": "r1",
                "state": "processing",
                "updated_at": 1,
            },
            {"run_id": "new", "room_id": "r1", "state": "queued", "updated_at": 2},
            {"run_id": "done", "room_id": "r1", "state": "completed", "updated_at": 3},
        ]
    )
    mongo.collections.update(
        {
            "agents": agents,
            "runs": runs,
        }
    )
    store = _ConversationStores(
        mongo=mongo,
        room_repository=room_repo,
        message_repository=message_repo,
        agent_repository=object(),
    )

    assert [
        room.conversation_id
        for room in await store.get_rooms_by_room_owner_id("owner-1")
    ] == ["r1"]
    user_message = RoomUserMessage(
        conversation_id="r1",
        message_id="u1",
        user_id="owner-1",
        message_created_at=datetime(2026, 5, 11, tzinfo=UTC),
        message_content=MessageContent(message_text="hello"),
    )

    await message_repo.save_user_message(
        to_stored_conversation_keys(user_message.model_dump(mode="json"))
    )
    updated_user_message = RoomUserMessage(
        conversation_id="r1",
        message_id="u1",
        user_id="owner-1",
        message_created_at=datetime(2026, 5, 11, tzinfo=UTC),
        message_content=MessageContent(message_text="updated"),
    )
    assert await store.update_room_user_message_by_message_id(
        "u1", updated_user_message
    )
    assert (
        mongo.collections["room_user_messages"].docs[0]["message_content"][
            "message_text"
        ]
        == "updated"
    )


@pytest.mark.asyncio
async def test_message_repository_combines_history_sorted_with_before_filter():
    older = datetime(2026, 5, 10, tzinfo=UTC)
    newer = datetime(2026, 5, 11, tzinfo=UTC)
    older_us = int(older.timestamp() * 1_000_000)
    newer_us = int(newer.timestamp() * 1_000_000)
    repo, _, user_messages, agent_messages = _message_repo(
        user_docs=[
            {
                "message_id": "u2",
                "room_id": "r1",
                "message_created_at": newer,
                "timeline_sort_us": newer_us,
            },
            {
                "message_id": "u1",
                "room_id": "r1",
                "message_created_at": older,
                "timeline_sort_us": older_us,
            },
        ],
        agent_docs=[
            {
                "message_id": "a1",
                "room_id": "r1",
                "message_created_at": older,
                "timeline_sort_us": older_us,
                "step_number": 2,
            },
            {
                "message_id": "a0",
                "room_id": "r1",
                "message_created_at": older,
                "timeline_sort_us": older_us,
                "step_number": 1,
            },
        ],
    )

    history = await repo.get_for_room("r1", limit=3, before=newer)

    assert [doc["message_id"] for doc in history] == ["u1", "a0", "a1"]
    assert [doc["message_type"] for doc in history] == ["user", "agent", "agent"]
    expected_query = {"room_id": "r1", "timeline_sort_us": {"$lt": newer_us}}
    expected_options = {
        "sort": [("timeline_sort_us", -1), ("message_id", -1)],
        "limit": 3,
    }
    assert user_messages.find_calls[-1] == (expected_query, expected_options)
    assert agent_messages.find_calls[-1] == (expected_query, expected_options)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 2, 5, 37])
async def test_timeline_cursor_traversal_is_complete_stable_and_bounded(page_size):
    same_time = 1785700000000000
    user_docs = [
        {"room_id": "r1", "message_id": f"u-{index:03d}", "timeline_sort_us": same_time}
        for index in range(126)
    ]
    agent_docs = [
        {"room_id": "r1", "message_id": f"a-{index:03d}", "timeline_sort_us": same_time}
        for index in range(125)
    ]
    # A message id is only collection-local; both typed messages must survive.
    user_docs[0]["message_id"] = "same-id"
    agent_docs[0]["message_id"] = "same-id"
    repo, _, user_messages, agent_messages = _message_repo(user_docs, agent_docs)

    before = None
    pages = []
    while True:
        page = await repo.get_timeline_page("r1", limit=page_size, before=before)
        pages.insert(
            0, [(entry.source, entry.message["message_id"]) for entry in page.entries]
        )
        if not page.has_more:
            assert page.next_position is None
            break
        assert page.next_position is not None
        before = page.next_position

    flattened = [item for page in pages for item in page]
    expected = sorted(
        [("user", row["message_id"]) for row in user_docs]
        + [("agent", row["message_id"]) for row in agent_docs],
        key=lambda item: (same_time, 0 if item[0] == "user" else 1, item[1]),
    )
    assert flattened == expected
    assert len(flattened) == len(set(flattened)) == 251
    expected_options = {
        "sort": [("timeline_sort_us", -1), ("message_id", -1)],
        "limit": page_size + 1,
    }
    assert all(options == expected_options for _, options in user_messages.find_calls)
    assert all(options == expected_options for _, options in agent_messages.find_calls)
    assert "skip" not in repr(user_messages.find_calls + agent_messages.find_calls)


@pytest.mark.asyncio
async def test_timeline_first_page_returns_latest_over_one_hundred_single_source():
    docs = [
        {"room_id": "r1", "message_id": f"u-{index:03d}", "timeline_sort_us": index}
        for index in range(251)
    ]
    repo, _, _, _ = _message_repo(user_docs=docs)

    page = await repo.get_timeline_page("r1", limit=200, before=None)

    assert [entry.message["timeline_sort_us"] for entry in page.entries] == list(
        range(51, 251)
    )
    assert page.has_more is True
    assert page.next_position == TimelinePosition(
        timeline_sort_us=51, source="user", message_id="u-051"
    )


@pytest.mark.asyncio
async def test_timeline_continuation_ignores_later_concurrent_append():
    docs = [
        {"room_id": "r1", "message_id": f"u-{index}", "timeline_sort_us": index}
        for index in range(5)
    ]
    repo, _, users, _ = _message_repo(user_docs=docs)
    first = await repo.get_timeline_page("r1", limit=2, before=None)
    users.docs.extend(
        [
            {"room_id": "r1", "message_id": "new-later", "timeline_sort_us": 10},
            {"room_id": "r1", "message_id": "new-backdated", "timeline_sort_us": 1},
        ]
    )

    older_ids = set()
    before = first.next_position
    while before is not None:
        page = await repo.get_timeline_page("r1", limit=2, before=before)
        older_ids.update(entry.message["message_id"] for entry in page.entries)
        before = page.next_position

    first_ids = {entry.message["message_id"] for entry in first.entries}
    assert first_ids.isdisjoint(older_ids)
    assert "new-later" not in older_ids
    # No cross-request snapshot is promised: a backdated insert can join an older page.
    assert "new-backdated" in older_ids


@pytest.mark.asyncio
async def test_message_repository_get_thread_walks_descendants_and_stops_cycles():
    repo, _, _, _ = _message_repo(
        agent_docs=[
            {"message_id": "a1", "related_message_id": "u1"},
            {"message_id": "a2", "parent_message_id": "a1"},
            {
                "message_id": "cycle",
                "related_message_id": "a2",
                "parent_message_id": "cycle",
            },
            {"message_id": "other", "related_message_id": "not-u1"},
        ]
    )

    thread = await repo.get_thread("u1")

    assert [doc["message_id"] for doc in thread] == ["a1", "a2", "cycle"]


@pytest.mark.asyncio
async def test_message_repository_update_status_sets_task_state_and_extra_fields():
    repo, _, _, agent_messages = _message_repo(
        agent_docs=[
            {
                "message_id": "a1",
                "room_id": "room-1",
                "message_created_at": "2026-01-01T00:00:00Z",
                "timeline_sort_us": 1767225600000000,
                "message_content": {"message_task": {"status": {"state": "working"}}},
            }
        ]
    )

    assert (
        await repo.update_status(
            "a1",
            "completed",
            task_updated_at="now",
            room_id="other-room",
            message_id="rewritten",
            message_created_at="2030-01-01T00:00:00Z",
            timeline_sort_us=1,
        )
        is True
    )

    assert (
        agent_messages.docs[0]["message_content"]["message_task"]["status"]["state"]
        == "completed"
    )
    assert agent_messages.docs[0]["task_updated_at"] == "now"
    assert agent_messages.docs[0]["room_id"] == "room-1"
    assert agent_messages.docs[0]["message_id"] == "a1"
    assert agent_messages.docs[0]["message_created_at"] == "2026-01-01T00:00:00Z"
    assert agent_messages.docs[0]["timeline_sort_us"] == 1767225600000000
    assert agent_messages.update_one_calls == [
        (
            {
                "message_id": "a1",
                "message_content.message_task.status.state": {
                    "$nin": [
                        "canceled",
                        "completed",
                        "expired",
                        "failed",
                        "rejected",
                    ]
                },
            },
            {
                "$set": {
                    "message_content.message_task.status.state": "completed",
                    "task_updated_at": "now",
                }
            },
            {},
        )
    ]


@pytest.mark.asyncio
async def test_message_repository_delete_for_room_deletes_both_collections():
    repo, _, user_messages, agent_messages = _message_repo(
        user_docs=[
            {"message_id": "u1", "room_id": "r1"},
            {"message_id": "u2", "room_id": "r1"},
            {"message_id": "u3", "room_id": "other"},
        ],
        agent_docs=[
            {"message_id": "a1", "room_id": "r1"},
            {"message_id": "a2", "room_id": "r1"},
            {"message_id": "a3", "room_id": "r1"},
        ],
    )

    assert await repo.delete_for_room("r1") == {
        "user_messages": 2,
        "agent_messages": 3,
    }
    assert user_messages.delete_many_calls == [{"room_id": "r1"}]
    assert agent_messages.delete_many_calls == [{"room_id": "r1"}]


def _matches(doc: dict, query: dict) -> bool:  # noqa: C901
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, item) for item in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, item) for item in expected):
                return False
            continue

        actual = _get_dotted(doc, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$exists" in expected and (actual is not None) != expected["$exists"]:
                return False
            if "$lt" in expected and not (
                actual is not None and actual < expected["$lt"]
            ):
                return False
            if "$lte" in expected and not (
                actual is not None and actual <= expected["$lte"]
            ):
                return False
        elif actual != expected:
            return False
    return True


def _apply_update(doc: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.get("$set", {}).items():
        _set_dotted(doc, key, deepcopy(value))
    for key in update.get("$unset", {}):
        _unset_dotted(doc, key)


def _get_dotted(doc: dict[str, Any], key: str) -> Any:
    current: Any = doc
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_dotted(doc: dict[str, Any], key: str, value: Any) -> None:
    current = doc
    parts = key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _unset_dotted(doc: dict[str, Any], key: str) -> None:
    current = doc
    parts = key.split(".")
    for part in parts[:-1]:
        current = current.get(part)
        if not isinstance(current, dict):
            return
    current.pop(parts[-1], None)
