"""``conversation_id`` stays a code and wire name; Mongo keeps ``room_id``.

Writes through the main repositories and model dumps must store ``room_id`` and
never a ``conversation_id`` key, and existing ``room_id`` documents must load.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agent.capability_issue import AgentCapabilityIssueService
from agent.repository.capability_issue_mongo import (
    AgentCapabilityIssueMongoRepository,
)
from common.dto import MessageCommitted, RunStateChanged, UserMessageInput
from conversation.repository import MessageMongoRepository, RoomMongoRepository
from conversation.repository.quote_mongo import RoomQuoteMongoRepository
from conversation.translators import create_room_doc, user_message_doc_from_input
from execution.orchestrator.a2a_runtime.models import (
    AgentCallLedgerRecord,
    HITLRouteSnapshotV2,
)
from execution.orchestrator.models import OrchestratorRunState
from execution.repository.orchestrator.run_store import MongoOrchestratorRunStore
from execution.repository.orchestrator.stores import (
    MongoAgentCallLedgerStore,
    MongoRoomEpochStore,
)
from models.agent import AgentCapabilityIssue
from models.hitl import HITLRequest
from models.quote import QuotedSnippet
from models.room import MessageContent, Room, RoomAgentMessage, RoomUserMessage
from tests.test_orchestrator_a2a_mongo_parity import (
    FakeCollection as OrchestratorFakeCollection,
)
from tests.test_room_repository import FakeMongo, _ConversationStores

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import make_run

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = {str(key) for key in value}
        for item in value.values():
            keys |= _keys(item)
        return keys
    if isinstance(value, list | tuple):
        return set().union(*(_keys(item) for item in value)) if value else set()
    if hasattr(value, "model_dump"):
        return _keys(value.model_dump(mode="python"))
    return set()


def _assert_stored(documents: list[dict[str, Any]]) -> None:
    assert documents
    for document in documents:
        assert "conversation_id" not in _keys(document)
        assert document["room_id"] == "room-1"


def _stores(mongo: FakeMongo) -> _ConversationStores:
    return _ConversationStores(
        mongo=mongo,
        room_repository=RoomMongoRepository(mongo),
        message_repository=MessageMongoRepository(mongo),
        agent_repository=object(),
    )


@pytest.mark.asyncio
async def test_conversation_writes_store_room_id_key() -> None:
    mongo = FakeMongo()
    stores = _stores(mongo)

    await RoomMongoRepository(mongo).create(
        create_room_doc(
            conversation_id="room-1",
            owner_id="user-1",
            owner_name="User",
            room_name="Room",
            agent_set={},
            created_at=NOW,
            membership_origin="manual",
            membership_origin_status="manual",
        )
    )
    await MessageMongoRepository(mongo).save_user_message(
        user_message_doc_from_input(
            conversation_id="room-1",
            message_id="user-1",
            message=UserMessageInput(
                conversation_id="room-1", message_text="hello", sender_id="user-1"
            ),
            created_at=NOW,
        )
    )
    assert await stores.update_room_user_message_by_message_id(
        "user-1",
        RoomUserMessage(
            conversation_id="room-1",
            message_id="user-1",
            message_type="user",
            message_content=MessageContent(message_text="edited"),
        ),
    )
    agent_message = RoomAgentMessage(
        conversation_id="room-1",
        message_id="agent-1",
        message_type="agent",
        agent_id="agent-a",
        message_created_at=NOW,
        message_content=MessageContent(message_text="hi"),
    )
    assert await stores.add_room_agent_message(agent_message)
    await RoomQuoteMongoRepository(mongo).insert(
        QuotedSnippet(
            conversation_id="room-1",
            created_by_user_id="user-1",
            text="quoted",
            source_message_id="agent-1",
            source_kind="agent",
        )
    )
    await AgentCapabilityIssueService(
        repository=AgentCapabilityIssueMongoRepository(mongo), threshold=3
    ).record_issue("agent-a", "boom", "query", conversation_id="room-1")

    for name in (
        "rooms",
        "room_user_messages",
        "room_agent_messages",
        "room_quotes",
        "agent_capability_issues",
    ):
        _assert_stored(mongo.collections[name].docs)
    for _query, update, _kwargs in mongo.collections[
        "room_user_messages"
    ].update_one_calls:
        assert "conversation_id" not in _keys(update)


@pytest.mark.asyncio
async def test_orchestrator_writes_store_room_id_key() -> None:
    runs = OrchestratorFakeCollection()
    calls = OrchestratorFakeCollection()
    epochs = OrchestratorFakeCollection()
    run = make_run().model_copy(update={"conversation_id": "room-1"})

    assert (
        await MongoOrchestratorRunStore(runs).create(run, command_id="create:run-1")
    ).outcome == "accepted"
    await MongoAgentCallLedgerStore(calls).insert(
        ledger_record().model_copy(update={"conversation_id": "room-1"})
    )
    await MongoRoomEpochStore(epochs).activate("room-1", "creation-1", activated_at=NOW)

    for collection in (runs, calls, epochs):
        _assert_stored(collection.values)
    assert calls.values[0]["dispatch_snapshot"]["room_id"] == "room-1"


def test_persisted_model_dumps_use_room_id_and_load_legacy_documents() -> None:
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="call-1",
        invocation_id="invocation-1",
        conversation_id="room-1",
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        interaction_revision=1,
        interaction_fingerprint="fingerprint-1",
    )
    request = HITLRequest(
        schema_version=3,
        interaction_id="interaction-1",
        question_index=0,
        question_count=1,
        conversation_id="room-1",
        user_message_id="user-1",
        application_route="supervisor_run",
        public_source="supervisor",
        evidence_origin="supervisor",
        prompt="Which one?",
    )
    for model in (route, request):
        dumped = model.model_dump(mode="python")
        assert "conversation_id" not in dumped
        assert type(model).model_validate(dumped) == model

    run = make_run()
    assert OrchestratorRunState.model_validate(run.model_dump()) == run
    record = ledger_record()
    assert AgentCallLedgerRecord.model_validate(record.model_dump()) == record


def test_wire_and_stored_models_load_room_id_documents() -> None:
    stored_room = {
        "room_id": "room-1",
        "room_name": "Room",
        "room_owner_id": "user-1",
        "room_owner_name": "User",
    }
    room = Room.model_validate(stored_room)
    assert room.conversation_id == "room-1"
    assert room.model_dump()["conversation_id"] == "room-1"
    assert "room_id" not in room.model_dump(mode="json")

    message = RoomUserMessage.model_validate(
        {
            "room_id": "room-1",
            "message_id": "user-1",
            "message_type": "user",
            "message_content": {"message_text": "hello"},
        }
    )
    assert message.conversation_id == "room-1"
    issue = AgentCapabilityIssue.model_validate(
        {
            "issue_id": "issue-1",
            "agent_id": "agent-a",
            "error_message": "boom",
            "query_text": "query",
            "room_id": "room-1",
            "created_at": NOW,
        }
    )
    assert issue.conversation_id == "room-1"


def test_internal_events_emit_room_id_and_accept_both_keys() -> None:
    committed = MessageCommitted(
        timestamp=NOW,
        conversation_id="room-1",
        message_id="message-1",
        message_type="user",
    )
    payload = committed.model_dump(mode="json")
    assert payload["room_id"] == "room-1"
    assert "conversation_id" not in payload
    assert MessageCommitted.model_validate(payload) == committed

    changed = {
        "timestamp": NOW.isoformat(),
        "run_id": "run-1",
        "old_state": "queued",
        "new_state": "processing",
    }
    for key in ("room_id", "conversation_id"):
        event = RunStateChanged.model_validate({**changed, key: "room-1"})
        assert event.conversation_id == "room-1"
