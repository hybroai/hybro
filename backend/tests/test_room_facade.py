from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.dto import (
    AgentInfo,
    AgentMessageInput,
    CreateRoomRequest,
    MembershipSeed,
    MembershipUpdateRequest,
    SavedAgentGroupSnapshot,
    UserMessageInput,
    UserMessageInsertResult,
)
from common.types import Task, TaskState, TaskStatus
from execution.orchestrator.a2a_runtime.in_memory import InMemoryRoomEpochStore
from models.quote import QuoteSourceKind
from models.request import RoomCenterUserMessageRequest
from models.room import (
    MessageContent,
    Room,
    RoomAgentMessage,
    RoomUserMessage,
    UserAttachment,
)
from room import RoomFacade
from room.idempotency import IdempotencyConflictError

NOW = datetime(2026, 5, 11, tzinfo=UTC)


@pytest.mark.asyncio
async def test_update_room_default_mode_uses_atomic_nested_field_write():
    facade, rooms, _, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "extend_info": {"preserved": "value", "use_supervisor": False},
            }
        ]
    )

    updated = await facade.update_room_default_mode("r1", use_supervisor=True)

    assert updated is True
    assert rooms.update_field_calls == [("r1", {"extend_info.use_supervisor": True})]


@pytest.mark.asyncio
async def test_registry_and_ownership_methods_use_repository_and_agent_registry():
    facade, rooms, _, registry, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {"a1": "Agent One", "a2": "Agent Two"},
                "room_created_at": NOW,
            }
        ],
        agents=[
            AgentInfo(agent_id="a1", name="Agent One"),
            AgentInfo(agent_id="a2", name="Agent Two"),
        ],
    )

    room = await facade.get_room("r1")

    assert room is not None
    assert room.room_id == "r1"
    assert list(room.agent_ids) == ["a1", "a2"]
    assert await facade.get_room("missing") is None
    assert await facade.get_room_agents("r1") == ["a1", "a2"]
    assert await facade.get_room_agents("missing") == []
    assert await facade.get_room_owner("r1") == "owner"
    assert await facade.get_room_owner("missing") is None
    assert await facade.verify_room_agent_membership("r1", "a1") is True
    assert await facade.verify_room_agent_membership("r1", "missing") is False
    assert rooms.get_by_id_calls[:2] == ["r1", "missing"]
    assert rooms.get_by_id_calls.count("missing") == 3
    assert rooms.get_by_id_calls.count("r1") >= 5


@pytest.mark.asyncio
async def test_create_room_validates_required_fields_and_manual_seed():
    facade, rooms, _, _, _ = _facade(
        agents=[
            AgentInfo(agent_id="a1", name="Agent One"),
            AgentInfo(agent_id="a2", name="Agent Two"),
        ],
        ids=["room-created"],
    )

    with pytest.raises(ValueError, match="owner_id is required"):
        await facade.create_room(
            CreateRoomRequest(
                owner_id="",
                owner_name="Owner",
                room_name="Room",
                membership_seed=MembershipSeed(mode="manual"),
            )
        )

    room = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Room",
            membership_seed=MembershipSeed(mode="manual", agent_ids=["a2", "a1"]),
        )
    )

    assert room.room_id == "room-created"
    assert list(room.agent_ids) == ["a2", "a1"]
    assert dict(room.agent_set) == {"a2": "Agent Two", "a1": "Agent One"}
    assert room.membership_origin == "manual"
    assert room.membership_origin_status == "manual"
    assert rooms.created_docs[-1] == {
        "room_id": "room-created",
        "room_name": "Room",
        "room_owner_id": "owner",
        "room_owner_name": "Owner",
        "room_agent_set": {"a2": "Agent Two", "a1": "Agent One"},
        "room_created_at": NOW,
        "last_activity_at": NOW,
        "is_pinned": False,
        "pin_order": None,
        "membership_origin": "manual",
        "membership_origin_status": "manual",
        "source_group_id": None,
        "source_group_name": None,
        "processing_message_id": None,
        "lifecycle_state": "active",
        "write_leases": [],
    }


@pytest.mark.asyncio
async def test_create_room_preserves_initial_extend_info():
    facade, rooms, _, _, _ = _facade(ids=["room-created"])

    room = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Room",
            membership_seed=MembershipSeed(mode="manual"),
            extend_info={"debateMode": True, "use_supervisor": True},
        )
    )

    assert room.extend_info == {"debateMode": True, "use_supervisor": True}
    assert rooms.created_docs[-1]["extend_info"] == {
        "debateMode": True,
        "use_supervisor": True,
    }


@pytest.mark.asyncio
async def test_create_room_supports_saved_group_all_current_and_empty_manual():
    facade, _, _, _, source = _facade(
        agents=[
            AgentInfo(agent_id="a1", name="Agent One", status="active"),
            AgentInfo(agent_id="a2", name="Agent Two", status="active"),
        ],
        saved_groups={
            "g1": SavedAgentGroupSnapshot(
                group_id="g1",
                name="Group One",
                owner_id="owner",
                type="custom",
                agent_ids=["a1"],
            )
        },
        current_agents=[
            AgentInfo(agent_id="a2", name="Agent Two", status="active"),
            AgentInfo(agent_id="inactive", name="Inactive", status="inactive"),
        ],
        ids=["saved-room", "current-room", "empty-room"],
    )

    saved = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Saved",
            membership_seed=MembershipSeed(mode="saved_group", group_id="g1"),
        )
    )
    current = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Current",
            membership_seed=MembershipSeed(mode="all_current_agents"),
        )
    )
    empty = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Empty",
            membership_seed=MembershipSeed(mode="manual"),
        )
    )

    assert saved.source_group_id == "g1"
    assert saved.source_group_name == "Group One"
    assert saved.membership_origin == "saved_group"
    assert saved.membership_origin_status == "seeded_never_edited"
    assert list(current.agent_ids) == ["a2"]
    assert source.list_current_agents_calls == ["owner"]
    assert list(empty.agent_ids) == []
    assert empty.membership_origin == "manual"


@pytest.mark.asyncio
async def test_update_and_delete_room_lifecycle_rules():
    facade, rooms, messages, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Old",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {},
                "room_created_at": NOW,
            }
        ]
    )

    updated = await facade.update_room("r1", {"room_name": "New"})
    extended = await facade.update_room("r1", {"extend_info": {"x": 1}})

    assert updated is not None
    assert updated.room_name == "New"
    assert extended is not None
    assert extended.extend_info == {"x": 1}
    assert await facade.update_room("missing", {"room_name": "Nope"}) is None
    with pytest.raises(ValueError, match="Unknown room update keys"):
        await facade.update_room("r1", {"not_allowed": True})

    assert await facade.delete_room("missing", "owner") is False
    assert await facade.delete_room("r1", "not-owner") is False
    assert await facade.delete_room("r1", "owner") is True
    assert messages.deleted_rooms == ["r1"]
    assert rooms.deleted_ids == ["r1"]


@pytest.mark.asyncio
async def test_delete_room_uses_normalized_owner_id_from_repository_doc():
    class OwnerId:
        def __str__(self) -> str:
            return "owner"

    facade, rooms, messages, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": OwnerId(),
                "room_owner_name": "Owner",
                "room_agent_set": {},
            }
        ]
    )

    assert await facade.get_room_owner("r1") == "owner"
    assert await facade.delete_room("r1", "owner") is True
    assert messages.deleted_rooms == ["r1"]
    assert rooms.deleted_ids == ["r1"]


@pytest.mark.asyncio
async def test_update_membership_add_remove_validation_and_provenance():
    facade, rooms, _, _, _ = _facade(
        room_docs=[
            {
                "room_id": "seeded",
                "room_name": "Seeded",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {"a1": "Agent One"},
                "membership_origin": "saved_group",
                "membership_origin_status": "seeded_never_edited",
            },
            {
                "room_id": "manual",
                "room_name": "Manual",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {"a1": "Agent One"},
                "membership_origin": "manual",
                "membership_origin_status": "manual",
            },
        ],
        agents=[
            AgentInfo(agent_id="a1", name="Agent One", status="active"),
            AgentInfo(agent_id="a2", name="Agent Two", status="active"),
            AgentInfo(agent_id="inactive", name="Inactive", status="inactive"),
            AgentInfo(
                agent_id="private",
                name="Private",
                status="active",
                is_public=False,
                provider_id="someone-else",
            ),
        ],
    )

    seeded = await facade.update_membership(
        "seeded",
        MembershipUpdateRequest(add_agent_ids=["a2"]),
    )
    manual = await facade.update_membership(
        "manual",
        MembershipUpdateRequest(remove_agent_ids=["a1"]),
    )

    assert dict(seeded.agent_set) == {"a1": "Agent One", "a2": "Agent Two"}
    assert seeded.membership_origin_status == "seeded_edited"
    assert dict(manual.agent_set) == {}
    assert manual.membership_origin_status == "manual"
    with pytest.raises(ValueError, match="Unknown or deleted agent IDs"):
        await facade.update_membership(
            "manual",
            MembershipUpdateRequest(add_agent_ids=["missing"]),
        )
    with pytest.raises(ValueError, match="Access denied to private agents"):
        await facade.update_membership(
            "manual",
            MembershipUpdateRequest(add_agent_ids=["private"]),
        )
    with pytest.raises(ValueError, match="Inactive agent IDs"):
        await facade.update_membership(
            "manual",
            MembershipUpdateRequest(add_agent_ids=["inactive"]),
        )
    assert rooms.membership_updates[-1]["membership_origin_status"] == "manual"


@pytest.mark.asyncio
async def test_replace_membership_compatibility_helper_replaces_full_seed():
    facade, _, _, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {"old": "Old Agent"},
                "membership_origin": "manual",
                "membership_origin_status": "manual",
            }
        ],
        agents=[AgentInfo(agent_id="a1", name="Agent One")],
    )

    room = await facade.replace_membership(
        "r1",
        MembershipSeed(mode="manual", agent_ids=["a1"]),
        requesting_user_id="owner",
    )

    assert list(room.agent_ids) == ["a1"]
    assert dict(room.agent_set) == {"a1": "Agent One"}


@pytest.mark.asyncio
async def test_save_user_message_verifies_room_persists_raw_doc_and_returns_saved_dto():
    facade, _, messages, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {},
            }
        ],
        ids=["msg-user"],
    )

    with pytest.raises(ValueError, match="Room not found"):
        await facade.save_user_message(
            "missing",
            UserMessageInput(room_id="missing", message_text="hi", sender_id="u1"),
        )

    saved = await facade.save_user_message(
        "r1",
        UserMessageInput(
            room_id="r1",
            message_text="hello",
            sender_id="u1",
            sender_name="User",
            client_request_id="client-1",
            metadata={"scope_resolution_error": {"code": "empty_scope"}},
        ),
    )

    assert saved.message_id == "msg-user"
    assert saved.dispatch_root_message_id == "msg-user"
    assert saved.scope_resolution_error == {"code": "empty_scope"}
    assert messages.user_messages["msg-user"]["room_id"] == "r1"
    assert messages.user_messages["msg-user"]["message_type"] == "user"
    assert messages.user_messages["msg-user"]["user_id"] == "u1"
    assert (
        messages.user_messages["msg-user"]["message_content"]["message_text"] == "hello"
    )
    assert messages.user_messages["msg-user"]["client_request_id"] == "client-1"
    assert messages.user_messages["msg-user"]["message_created_at"] == NOW


@pytest.mark.asyncio
async def test_save_agent_message_verifies_room_and_preserves_metadata():
    facade, _, messages, _, _ = _facade(
        room_docs=[
            {
                "room_id": "r1",
                "room_name": "Room",
                "room_owner_id": "owner",
                "room_owner_name": "Owner",
                "room_agent_set": {"a1": "Agent One"},
            }
        ],
        ids=["msg-agent"],
    )

    with pytest.raises(ValueError, match="Room not found"):
        await facade.save_agent_message(
            "missing",
            AgentMessageInput(room_id="missing", agent_id="a1"),
        )

    message_id = await facade.save_agent_message(
        "r1",
        AgentMessageInput(
            room_id="r1",
            agent_id="a1",
            content={"message_text": "working"},
            parent_message_id="msg-user",
            metadata={
                "step_number": 1,
                "total_steps": 2,
                "has_task_tracking": True,
                "turn_id": "msg-user",
            },
        ),
    )

    assert message_id == "msg-agent"
    stored = messages.agent_messages["msg-agent"]
    assert stored["room_id"] == "r1"
    assert stored["message_type"] == "agent"
    assert stored["agent_id"] == "a1"
    assert stored["related_message_id"] == "msg-user"
    assert stored["parent_message_id"] == "msg-user"
    assert stored["message_content"] == {"message_text": "working"}
    assert stored["step_number"] == 1
    assert stored["has_task_tracking"] is True


@pytest.mark.asyncio
async def test_status_and_history_methods_delegate_and_translate_results():
    facade, _, messages, _, _ = _facade()
    messages.user_messages["u1"] = {
        "room_id": "r1",
        "message_id": "u1",
        "message_type": "user",
        "user_id": "user",
        "message_content": {"message_text": "hello"},
        "message_created_at": NOW,
    }
    messages.agent_messages["a1"] = {
        "room_id": "r1",
        "message_id": "a1",
        "message_type": "agent",
        "agent_id": "agent",
        "related_message_id": "u1",
        "message_content": {"message_text": "hi"},
        "message_created_at": NOW,
    }

    assert await facade.update_agent_message_status(
        "a1", "completed", task_updated_at=NOW
    )
    assert messages.status_updates == [("a1", "completed", {"task_updated_at": NOW})]

    user = await facade.get_message("u1")
    history = await facade.get_messages_for_room("r1")
    by_ids = await facade.get_messages_by_ids(["a1", "missing", "u1"])
    thread = await facade.get_message_thread("u1")

    assert user is not None
    assert user.message_id == "u1"
    assert [message.message_id for message in history] == ["u1", "a1"]
    assert [message.message_id for message in by_ids] == ["a1", "u1"]
    assert [message.message_id for message in thread] == ["a1"]


@pytest.mark.asyncio
async def test_update_agent_message_does_not_rehydrate_existing_task_metadata():
    private_sentinel = "PRIVATE_SENTINEL_facade_existing_task_metadata"
    facade, _, messages, _, _ = _facade()
    messages.agent_messages["a1"] = {
        "room_id": "r1",
        "message_id": "a1",
        "message_type": "agent",
        "agent_id": "agent",
        "related_message_id": "u1",
        "message_content": {
            "message_text": "old",
            "message_task": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "working"},
                "metadata": {"remote_private": private_sentinel},
            },
        },
        "message_created_at": NOW,
    }
    incoming = RoomAgentMessage(
        room_id="r1",
        message_id="a1",
        agent_id="agent",
        related_message_id="u1",
        message_content=MessageContent(
            message_text="updated",
            message_task=Task(
                id="task-1",
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.completed),
                metadata=None,
            ),
        ),
    )

    assert await facade.update_agent_message("a1", incoming)

    stored_task = messages.agent_messages["a1"]["message_content"]["message_task"]
    assert stored_task.get("metadata") is None
    assert private_sentinel not in json.dumps(
        messages.agent_messages["a1"], default=str
    )


@pytest.mark.asyncio
async def test_auto_fail_stale_agent_message_rebuilds_public_task_before_persistence():
    private_sentinel = "PRIVATE_SENTINEL_stale_auto_fail_private_task"
    facade, _, messages, _, _ = _facade(ids=["failed-status-message"])
    messages.agent_messages["a1"] = {
        "room_id": "r1",
        "message_id": "a1",
        "message_type": "agent",
        "agent_id": "agent",
        "related_message_id": "u1",
        "message_content": {
            "message_task": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {
                    "state": "working",
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": private_sentinel}],
                    },
                },
                "history": [
                    {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": private_sentinel}],
                    }
                ],
                "artifacts": [
                    {
                        "artifactId": "artifact-1",
                        "name": "partial",
                        "parts": [{"kind": "text", "text": private_sentinel}],
                    }
                ],
                "metadata": {"private": private_sentinel},
            },
        },
        "has_task_tracking": True,
        "task_created_at": datetime(2026, 5, 10, tzinfo=UTC),
        "task_updated_at": datetime(2026, 5, 10, tzinfo=UTC),
        "message_created_at": NOW,
    }

    returned = await facade.get_agent_messages_for_room("r1")

    assert len(returned) == 1
    returned_json = returned[0].model_dump_json()
    stored_json = json.dumps(messages.agent_messages["a1"], default=str)
    stored_task = messages.agent_messages["a1"]["message_content"]["message_task"]
    assert stored_task["status"]["state"] == "failed"
    assert stored_task["status"]["message"]["parts"][0]["text"].startswith(
        "Task did not complete"
    )
    assert stored_task["metadata"] is None
    assert stored_task["history"] is None
    assert stored_task["artifacts"] is None
    assert private_sentinel not in returned_json
    assert private_sentinel not in stored_json


@pytest.mark.asyncio
async def test_auto_fail_stale_skips_orchestrator_managed_working_messages():
    facade, _, messages, _, _ = _facade(ids=["orchestrator-working-message"])
    messages.agent_messages["a1"] = {
        "room_id": "r1",
        "message_id": "orchestrator:run-test:call-1",
        "message_type": "agent",
        "agent_id": "agent",
        "related_message_id": "u1",
        "message_content": {
            "message_text": "Generate a travel plan",
            "message_task": {
                "id": "orchestrator-task-call-1",
                "kind": "task",
                "status": {"state": "working"},
            },
        },
        "extend_info": {"orchestrator_run_id": "run-test"},
        "message_created_at": NOW,
    }

    returned = await facade.get_agent_messages_for_room("r1")

    assert len(returned) == 1
    stored_task = messages.agent_messages["a1"]["message_content"]["message_task"]
    assert stored_task["status"]["state"] == "working"


@pytest.mark.asyncio
async def test_auto_fail_stale_does_not_fail_working_messages_without_task_timestamps():
    facade, _, messages, _, _ = _facade(ids=["working-without-timestamps"])
    messages.agent_messages["a1"] = {
        "room_id": "r1",
        "message_id": "a1",
        "message_type": "agent",
        "agent_id": "agent",
        "related_message_id": "u1",
        "message_content": {
            "message_task": {
                "id": "task-1",
                "status": {"state": "working"},
            },
        },
        "has_task_tracking": True,
        "message_created_at": NOW,
    }

    returned = await facade.get_agent_messages_for_room("r1")

    assert len(returned) == 1
    stored_task = messages.agent_messages["a1"]["message_content"]["message_task"]
    assert stored_task["status"]["state"] == "working"


@pytest.mark.asyncio
async def test_auto_fail_ignores_durable_orchestrator_projection():
    facade, _, messages, _, _ = _facade(ids=["unused-id"])
    messages.agent_messages["orchestrator-card"] = {
        "room_id": "r1",
        "message_id": "orchestrator-card",
        "message_type": "agent",
        "agent_id": "weather-agent",
        "related_message_id": "u1",
        "message_created_at": NOW,
        "message_content": {
            "message_text": "Checking weather",
            "message_task": {
                "id": "orchestrator-task-call-1",
                "kind": "task",
                "status": {"state": "working"},
                "artifacts": [],
            },
        },
        "has_task_tracking": False,
        "extend_info": {"orchestrator_run_id": "run-1"},
    }

    returned = await facade.get_agent_messages_for_room("r1")

    assert len(returned) == 1
    assert returned[0].message_content.message_task.status.state == TaskState.working
    stored_task = messages.agent_messages["orchestrator-card"]["message_content"][
        "message_task"
    ]
    assert stored_task["status"]["state"] == "working"


@pytest.mark.asyncio
async def test_legacy_user_message_persistence_strips_ephemeral_fields():
    facade, _, messages, _, _ = _facade(ids=["unused-id"])
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="legacy-user",
        user_id="user",
        message_content=MessageContent(
            message_text="hello",
            attachments=[
                UserAttachment(
                    file_id="file-1",
                    s3_key="rooms/r1/file-1",
                    mime_type="text/plain",
                    file_name="note.txt",
                    size_bytes=10,
                    file_url="https://presigned.example/file-1",
                )
            ],
        ),
    )

    result = await facade.persist_user_message(user_message)

    assert result.created is True
    assert result.message_id == "legacy-user"
    stored = messages.user_messages["legacy-user"]
    assert "quote" not in stored
    assert "file_url" not in stored["message_content"]["attachments"][0]


@pytest.mark.asyncio
async def test_facade_persists_internal_fingerprint_and_returns_typed_outcome():
    facade, _, messages, _, _ = _facade(ids=["generated-message-id"])
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="",
        user_id="user",
        client_request_id="request-1",
        message_content=MessageContent(message_text="hello"),
    )

    result = await facade.persist_user_message(
        user_message,
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert result == UserMessageInsertResult(
        message_id="generated-message-id",
        created=True,
        document=messages.user_messages["generated-message-id"],
    )
    stored = messages.user_messages["generated-message-id"]
    assert stored["idempotency_fingerprint"] == "fingerprint-1"
    assert stored["idempotency_fingerprint_version"] == 1
    assert "idempotency_fingerprint" not in user_message.model_dump()


@pytest.mark.asyncio
async def test_internal_idempotency_fields_are_not_exposed_by_user_message_models():
    facade, _, messages, _, _ = _facade()
    messages.user_messages["message-1"] = {
        "room_id": "r1",
        "message_id": "message-1",
        "message_type": "user",
        "user_id": "user",
        "client_request_id": "request-1",
        "message_content": {"message_text": "hello"},
        "message_created_at": NOW,
        "idempotency_fingerprint": "private-fingerprint",
        "idempotency_fingerprint_version": 1,
    }

    message = await facade.get_user_message_model("message-1")

    assert message is not None
    serialized = message.model_dump(mode="json")
    assert "idempotency_fingerprint" not in serialized
    assert "idempotency_fingerprint_version" not in serialized


def test_ensure_user_message_id_assigns_once_and_is_idempotent():
    facade, _, _, _, _ = _facade(ids=["generated-message-id"])
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="",
        user_id="user",
        message_content=MessageContent(message_text="hello"),
    )

    assert facade.ensure_user_message_id(user_message) == "generated-message-id"
    assert user_message.message_id == "generated-message-id"
    assert facade.ensure_user_message_id(user_message) == "generated-message-id"


@pytest.mark.asyncio
async def test_quote_materialization_validates_source_and_dual_writes_extend_info():
    quote_repo = FakeQuoteRepository()
    facade, _, messages, _, _ = _facade(quote_repository=quote_repo)
    messages.agent_messages["agent-source"] = {
        "room_id": "r1",
        "message_id": "agent-source",
        "message_type": "agent",
        "agent_id": "a1",
        "message_content": {"message_text": "source"},
        "message_created_at": NOW,
    }
    room = Room(
        room_id="r1",
        room_name="Room",
        room_owner_id="owner",
        room_owner_name="Owner",
    )
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        user_id="user",
        message_content=MessageContent(message_text="reply"),
        quote={
            "text": " quoted text ",
            "source_message_id": "agent-source",
            "source_kind": QuoteSourceKind.AGENT,
            "sender_display_name": "Agent One",
        },
    )

    response = await facade.materialize_quote(
        room=room,
        request=RoomCenterUserMessageRequest(user_id="user"),
        user_message=user_message,
    )

    assert response is None
    assert user_message.quote is None
    assert user_message.quote_id == "quote-1"
    assert user_message.extend_info == {
        "quoted_text": "quoted text",
        "quoted_sender_name": "Agent One",
        "quote_id": "quote-1",
    }
    assert quote_repo.inserted[0].room_id == "r1"
    assert quote_repo.inserted[0].text == "quoted text"


@pytest.mark.asyncio
async def test_quote_materialization_rejects_invalid_source_room():
    quote_repo = FakeQuoteRepository()
    facade, _, messages, _, _ = _facade(quote_repository=quote_repo)
    messages.user_messages["other-room-source"] = {
        "room_id": "other",
        "message_id": "other-room-source",
        "message_type": "user",
        "message_content": {"message_text": "source"},
        "message_created_at": NOW,
    }
    room = Room(
        room_id="r1",
        room_name="Room",
        room_owner_id="owner",
        room_owner_name="Owner",
    )
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="reply"),
        quote={
            "text": "quoted text",
            "source_message_id": "other-room-source",
            "source_kind": QuoteSourceKind.USER_TURN,
        },
    )

    response = await facade.materialize_quote(
        room=room,
        request=RoomCenterUserMessageRequest(user_id="user"),
        user_message=user_message,
    )

    assert response is not None
    assert response.success is False
    assert response.error == "Invalid quote source"
    assert quote_repo.inserted == []


async def test_create_room_activates_epoch_after_persist():
    epoch_store = InMemoryRoomEpochStore()
    facade, rooms, _, _, _ = _facade(ids=["room-created"], epoch_store=epoch_store)

    room = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Room",
            membership_seed=MembershipSeed(mode="manual"),
        )
    )

    active = await epoch_store.read_active("room-created")
    assert active is not None
    assert active.epoch == 1
    assert active.creation_id
    assert room.room_id == "room-created"
    assert await facade.get_room("room-created") is not None
    assert rooms.deleted_ids == []


@pytest.mark.asyncio
async def test_create_room_conflict_compensates_and_room_is_not_routable():
    epoch_store = SimpleNamespace(activate=AsyncMock(return_value=("conflict", None)))
    facade, rooms, _, _, _ = _facade(ids=["room-created"], epoch_store=epoch_store)

    with pytest.raises(ValueError, match="Room epoch activation conflict"):
        await facade.create_room(
            CreateRoomRequest(
                owner_id="owner",
                owner_name="Owner",
                room_name="Room",
                membership_seed=MembershipSeed(mode="manual"),
            )
        )

    assert "room-created" in rooms.deleted_ids
    assert await facade.get_room("room-created") is None


@pytest.mark.asyncio
async def test_create_room_tolerates_epoch_replay():
    epoch_store = SimpleNamespace(activate=AsyncMock(return_value=("replayed", None)))
    facade, rooms, _, _, _ = _facade(ids=["room-created"], epoch_store=epoch_store)

    room = await facade.create_room(
        CreateRoomRequest(
            owner_id="owner",
            owner_name="Owner",
            room_name="Room",
            membership_seed=MembershipSeed(mode="manual"),
        )
    )

    assert room.room_id == "room-created"
    assert rooms.deleted_ids == []
    assert await facade.get_room("room-created") is not None


@pytest.mark.asyncio
async def test_create_room_activation_error_compensates():
    async def activate(*args, **kwargs):
        raise RuntimeError("mongo down")

    epoch_store = SimpleNamespace(activate=activate)
    facade, rooms, _, _, _ = _facade(ids=["room-created"], epoch_store=epoch_store)

    with pytest.raises(ValueError, match="Room epoch activation failed"):
        await facade.create_room(
            CreateRoomRequest(
                owner_id="owner",
                owner_name="Owner",
                room_name="Room",
                membership_seed=MembershipSeed(mode="manual"),
            )
        )

    assert "room-created" in rooms.deleted_ids
    assert await facade.get_room("room-created") is None


def _facade(
    *,
    room_docs: list[dict] | None = None,
    agents: list[AgentInfo] | None = None,
    saved_groups: dict[str, SavedAgentGroupSnapshot] | None = None,
    current_agents: list[AgentInfo] | None = None,
    ids: list[str] | None = None,
    quote_repository=None,
    epoch_store=None,
):
    rooms = FakeRoomRepository(room_docs or [])
    messages = FakeMessageRepository()
    registry = _registry(*(agents or []))
    source = FakeMembershipSource(
        saved_groups=saved_groups or {},
        current_agents=current_agents or [],
    )
    id_iter = iter(ids or ["generated-id"])
    epoch_store = epoch_store or InMemoryRoomEpochStore()
    facade = RoomFacade(
        repository=rooms,
        message_repository=messages,
        agent_registry=registry,
        membership_source=source,
        quote_repository=quote_repository,
        id_factory=lambda: next(id_iter),
        now=lambda: NOW,
        epoch_store=epoch_store,
    )
    return facade, rooms, messages, registry, source


def _registry(*agents: AgentInfo):
    by_id = {agent.agent_id: agent for agent in agents}
    registry = AsyncMock()

    async def get_agents_by_ids(agent_ids: list[str]) -> list[AgentInfo]:
        return [by_id[agent_id] for agent_id in agent_ids if agent_id in by_id]

    registry.get_agents_by_ids.side_effect = get_agents_by_ids
    return registry


class FakeMembershipSource:
    def __init__(
        self,
        *,
        saved_groups: dict[str, SavedAgentGroupSnapshot],
        current_agents: list[AgentInfo],
    ) -> None:
        self.saved_groups = saved_groups
        self.current_agents = current_agents
        self.list_current_agents_calls: list[str | None] = []

    async def get_saved_group(self, group_id: str) -> SavedAgentGroupSnapshot | None:
        return self.saved_groups.get(group_id)

    async def list_current_agents(self, user_id: str | None) -> list[AgentInfo]:
        self.list_current_agents_calls.append(user_id)
        return list(self.current_agents)


class FakeRoomRepository:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = {doc["room_id"]: deepcopy(doc) for doc in docs}
        self.get_by_id_calls: list[str] = []
        self.created_docs: list[dict] = []
        self.deleted_ids: list[str] = []
        self.membership_updates: list[dict] = []
        self.update_field_calls: list[tuple[str, dict]] = []

    async def get_by_id(self, room_id: str) -> dict | None:
        self.get_by_id_calls.append(room_id)
        doc = self.docs.get(room_id)
        return deepcopy(doc) if doc is not None else None

    async def get_by_owner(self, owner_id: str) -> list[dict]:
        return [
            deepcopy(doc)
            for doc in self.docs.values()
            if doc.get("room_owner_id") == owner_id
        ]

    async def create(self, room: dict) -> str:
        self.created_docs.append(deepcopy(room))
        self.docs[room["room_id"]] = deepcopy(room)
        return room["room_id"]

    async def update(self, room_id: str, updates: dict) -> bool:
        if room_id not in self.docs:
            return False
        self.docs[room_id].update(deepcopy(updates))
        return True

    async def update_fields(self, room_id: str, updates: dict) -> dict | None:
        self.update_field_calls.append((room_id, deepcopy(updates)))
        if room_id not in self.docs:
            return None
        self.docs[room_id].update(deepcopy(updates))
        return deepcopy(self.docs[room_id])

    async def set_membership(
        self,
        room_id: str,
        *,
        agent_set: dict[str, str],
        membership_origin: str,
        membership_origin_status: str,
        source_group_id: str | None = None,
        source_group_name: str | None = None,
    ) -> dict | None:
        updates = {
            "room_agent_set": deepcopy(agent_set),
            "membership_origin": membership_origin,
            "membership_origin_status": membership_origin_status,
            "source_group_id": source_group_id,
            "source_group_name": source_group_name,
        }
        self.membership_updates.append(deepcopy(updates))
        return await self.update_fields(room_id, updates)

    async def delete(self, room_id: str) -> bool:
        if room_id not in self.docs:
            return False
        self.deleted_ids.append(room_id)
        del self.docs[room_id]
        return True


class FakeMessageRepository:
    def __init__(self) -> None:
        self.deleted_rooms: list[str] = []
        self.user_messages: dict[str, dict] = {}
        self.agent_messages: dict[str, dict] = {}
        self.status_updates: list[tuple[str, str, dict]] = []

    async def save_user_message(self, message: dict) -> str:
        self.user_messages[message["message_id"]] = deepcopy(message)
        return message["message_id"]

    async def get_user_message_by_idempotency_key(
        self, room_id: str, client_request_id: str
    ) -> dict | None:
        for message in self.user_messages.values():
            if (
                message.get("room_id") == room_id
                and message.get("client_request_id") == client_request_id
            ):
                return deepcopy(message)
        return None

    async def insert_user_message_idempotently(
        self, message: dict
    ) -> UserMessageInsertResult:
        existing = await self.get_user_message_by_idempotency_key(
            message["room_id"], message["client_request_id"]
        )
        if existing is not None:
            if existing.get("idempotency_fingerprint") != message.get(
                "idempotency_fingerprint"
            ):
                raise IdempotencyConflictError(
                    message["room_id"], message["client_request_id"]
                )
            return UserMessageInsertResult(
                message_id=existing["message_id"],
                created=False,
                document=existing,
            )
        self.user_messages[message["message_id"]] = deepcopy(message)
        return UserMessageInsertResult(
            message_id=message["message_id"],
            created=True,
            document=deepcopy(message),
        )

    async def save_agent_message(self, message: dict) -> str:
        self.agent_messages[message["message_id"]] = deepcopy(message)
        return message["message_id"]

    async def get_by_id(self, message_id: str) -> dict | None:
        doc = self.user_messages.get(message_id) or self.agent_messages.get(message_id)
        return deepcopy(doc) if doc is not None else None

    async def get_user_message_by_id(self, message_id: str) -> dict | None:
        doc = self.user_messages.get(message_id)
        return deepcopy(doc) if doc is not None else None

    async def get_agent_message_by_id(self, message_id: str) -> dict | None:
        doc = self.agent_messages.get(message_id)
        return deepcopy(doc) if doc is not None else None

    async def get_by_ids(self, message_ids: list[str]) -> list[dict]:
        out = []
        for message_id in message_ids:
            doc = self.user_messages.get(message_id) or self.agent_messages.get(
                message_id
            )
            if doc is not None:
                out.append(deepcopy(doc))
        return out

    async def get_for_room(
        self, room_id: str, limit: int, before: datetime | None = None
    ) -> list[dict]:
        docs = [
            *[
                deepcopy(doc)
                for doc in self.user_messages.values()
                if doc.get("room_id") == room_id
            ],
            *[
                deepcopy(doc)
                for doc in self.agent_messages.values()
                if doc.get("room_id") == room_id
            ],
        ]
        return docs[:limit]

    async def get_thread(self, parent_message_id: str) -> list[dict]:
        return [
            deepcopy(doc)
            for doc in self.agent_messages.values()
            if doc.get("related_message_id") == parent_message_id
            or doc.get("parent_message_id") == parent_message_id
        ]

    async def update_status(self, message_id: str, status: str, **fields) -> bool:
        self.status_updates.append((message_id, status, deepcopy(fields)))
        return message_id in self.agent_messages

    async def update_agent_message(self, message_id: str, updates: dict) -> bool:
        if message_id not in self.agent_messages:
            return False
        self.agent_messages[message_id].update(deepcopy(updates))
        return True

    async def delete_for_room(self, room_id: str) -> dict[str, int]:
        self.deleted_rooms.append(room_id)
        return {"user_messages": 0, "agent_messages": 0}

    async def get_user_messages_for_room(
        self, room_id: str, limit: int = 100, before: datetime | None = None
    ) -> list[dict]:
        return [
            deepcopy(doc)
            for doc in self.user_messages.values()
            if doc.get("room_id") == room_id
        ][:limit]

    async def get_agent_messages_for_room(
        self, room_id: str, limit: int = 100, before: datetime | None = None
    ) -> list[dict]:
        return [
            deepcopy(doc)
            for doc in self.agent_messages.values()
            if doc.get("room_id") == room_id
        ][:limit]

    async def get_agent_messages_by_related_message_id(
        self, related_message_id: str
    ) -> list[dict]:
        return [
            deepcopy(doc)
            for doc in self.agent_messages.values()
            if doc.get("related_message_id") == related_message_id
        ]


class FakeQuoteRepository:
    def __init__(self) -> None:
        self.inserted = []
        self.deleted_ids: list[str] = []

    async def insert(self, snippet) -> str:
        self.inserted.append(snippet)
        return f"quote-{len(self.inserted)}"

    async def delete_by_id(self, quote_id: str) -> bool:
        self.deleted_ids.append(quote_id)
        return True
