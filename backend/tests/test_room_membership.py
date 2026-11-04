from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from common.dto import AgentInfo, MembershipSeed, UserMessageInput
from common.observability import events as E
from conversation.dto import SavedAgentGroupSnapshot
from conversation.membership import normalize_room_agent_set, resolve_membership_seed
from conversation.membership_source import RepositoryRoomMembershipSeedSource
from conversation.message_graph import (
    select_thread,
    sort_messages,
    status_update_payload,
)
from conversation.translators import (
    create_room_doc,
    message_info_from_doc,
    room_info_from_doc,
    saved_user_message_from_doc,
    user_message_doc_from_input,
)


def test_room_translator_maps_legacy_fields_and_defaults_provenance():
    created = datetime(2026, 5, 11, tzinfo=UTC)
    info = room_info_from_doc(
        {
            "room_id": "r1",
            "room_name": "Room",
            "room_owner_id": "u1",
            "room_owner_name": "Owner",
            "room_created_at": created,
            "room_agent_set": {"a1": "Agent One"},
        }
    )

    assert info.conversation_id == "r1"
    assert info.owner_id == "u1"
    assert info.owner_name == "Owner"
    assert info.created_at == created
    assert list(info.agent_ids) == ["a1"]
    assert dict(info.agent_set) == {"a1": "Agent One"}
    assert info.membership_origin == "manual"
    assert info.membership_origin_status == "manual"


def test_create_room_doc_preserves_room_fields_without_legacy_model_imports():
    now = datetime(2026, 5, 11, tzinfo=UTC)

    doc = create_room_doc(
        conversation_id="r1",
        owner_id="u1",
        owner_name="Owner",
        room_name="Room",
        agent_set={"a1": "Agent One"},
        created_at=now,
        membership_origin="saved_group",
        membership_origin_status="seeded_never_edited",
        source_group_id="g1",
        source_group_name="Group",
    )

    assert doc == {
        "room_id": "r1",
        "room_name": "Room",
        "room_owner_id": "u1",
        "room_owner_name": "Owner",
        "room_agent_set": {"a1": "Agent One"},
        "room_created_at": now,
        "last_activity_at": now,
        "is_pinned": False,
        "pin_order": None,
        "membership_origin": "saved_group",
        "membership_origin_status": "seeded_never_edited",
        "source_group_id": "g1",
        "source_group_name": "Group",
        "lifecycle_state": "active",
        "write_leases": [],
        "owner_product": "chat",
    }


def test_message_translators_map_user_agent_and_saved_user_shapes():
    now = datetime(2026, 5, 11, tzinfo=UTC)
    user = message_info_from_doc(
        {
            "room_id": "r1",
            "message_id": "u1",
            "message_type": "user",
            "user_id": "u1",
            "user_name": "User",
            "message_content": {"message_text": "hello"},
            "message_created_at": now,
            "client_request_id": "client-1",
        }
    )
    agent = message_info_from_doc(
        {
            "room_id": "r1",
            "message_id": "a1",
            "message_type": "agent",
            "agent_id": "agent-1",
            "related_message_id": "u1",
            "message_content": {"message_text": "hi"},
            "message_created_at": now,
        }
    )
    saved = saved_user_message_from_doc(
        {
            "room_id": "r1",
            "message_id": "u1",
            "user_id": "user-1",
            "user_name": "User",
            "message_content": {"message_text": "hello"},
            "scope_resolution_error": {"code": "empty_scope"},
        }
    )

    assert user.content == {"message_text": "hello"}
    assert user.sender_id == "u1"
    assert user.metadata["client_request_id"] == "client-1"
    assert agent.agent_id == "agent-1"
    assert agent.parent_message_id == "u1"
    assert saved.dispatch_root_message_id == saved.message_id
    assert saved.message["message_content"] == {"message_text": "hello"}
    assert saved.scope_resolution_error == {"code": "empty_scope"}


def test_user_message_doc_from_input_preserves_metadata():
    now = datetime(2026, 5, 11, tzinfo=UTC)

    doc = user_message_doc_from_input(
        conversation_id="r1",
        message_id="m1",
        message=UserMessageInput(
            conversation_id="r1",
            message_text="hello",
            sender_id="u1",
            sender_name="User",
            client_request_id="client-1",
            metadata={"scope_resolution_error": {"code": "empty_scope"}},
        ),
        created_at=now,
    )

    assert doc["message_id"] == "m1"
    assert doc["room_id"] == "r1"
    assert doc["message_type"] == "user"
    assert doc["user_id"] == "u1"
    assert doc["message_content"]["message_text"] == "hello"
    assert doc["client_request_id"] == "client-1"
    assert doc["scope_resolution_error"] == {"code": "empty_scope"}


@pytest.mark.asyncio
async def test_membership_source_logs_agent_registry_fallback():
    agent = SimpleNamespace(
        agent_id="a1",
        agent_card=SimpleNamespace(name="Agent One", description=None, url=None),
        provider_id="owner",
        agent_status="active",
        source="cloud",
        hub_id=None,
        is_public=True,
        public_url=None,
    )
    source = RepositoryRoomMembershipSeedSource(
        membership_store=SimpleNamespace(
            get_all_active_agents=AsyncMock(return_value=[agent])
        ),
        agent_registry=SimpleNamespace(
            list_visible_agents=AsyncMock(side_effect=RuntimeError("boom"))
        ),
    )

    with patch("conversation.membership_source.logger", create=True) as logger:
        agents = await source.list_current_agents("owner")

    assert [agent.agent_id for agent in agents] == ["a1"]
    logger.emit.assert_called_once()
    assert logger.emit.call_args.args == (E.MEMBERSHIP_AGENTS_FALLBACK_SELECTED,)


def test_membership_source_warns_for_missing_critical_agent_fields():
    agent = SimpleNamespace(agent_id="a1", agent_card=None, is_public=True)

    with patch("conversation.membership_source.logger", create=True) as logger:
        info = RepositoryRoomMembershipSeedSource.agent_info_from_record(agent)

    assert info.agent_id == "a1"
    logger.emit.assert_called_once()
    assert logger.emit.call_args.args == (E.MEMBERSHIP_AGENT_FIELDS_NOT_FOUND,)


def test_membership_source_default_constructor_does_not_import_database_service():
    with patch(
        "importlib.import_module", side_effect=AssertionError("legacy import attempted")
    ):
        source = RepositoryRoomMembershipSeedSource()

    assert source._store is not None


def test_message_graph_sort_thread_and_status_payload_helpers():
    older = datetime(2026, 5, 10, tzinfo=UTC)
    newer = datetime(2026, 5, 11, tzinfo=UTC)
    rows = [
        {"message_id": "missing"},
        {"message_id": "a2", "message_created_at": older, "step_number": 2},
        {"message_id": "a1", "message_created_at": older, "step_number": 1},
        {"message_id": "u2", "message_created_at": newer},
    ]

    assert [row["message_id"] for row in sort_messages(rows)] == [
        "a1",
        "a2",
        "u2",
        "missing",
    ]
    assert [row["message_id"] for row in select_thread(rows, "a1")] == []
    thread_rows = [
        {"message_id": "a1", "related_message_id": "u1"},
        {"message_id": "a2", "parent_message_id": "a1"},
        {
            "message_id": "cycle",
            "related_message_id": "a2",
            "parent_message_id": "cycle",
        },
    ]
    assert [row["message_id"] for row in select_thread(thread_rows, "u1")] == [
        "a1",
        "a2",
        "cycle",
    ]
    now = newer
    assert status_update_payload("completed", {"task_updated_at": now}) == {
        "message_content.message_task.status.state": "completed",
        "task_updated_at": now,
    }


def test_saved_group_snapshot_dto_fields_and_runtime_protocol_export():
    from conversation.membership import RoomMembershipSeedSource

    snapshot = SavedAgentGroupSnapshot(
        group_id="g1",
        name="Group",
        owner_id="u1",
        type="custom",
        agent_ids=["a1"],
    )

    assert snapshot.group_id == "g1"
    assert snapshot.name == "Group"
    assert snapshot.owner_id == "u1"
    assert snapshot.type == "custom"
    assert list(snapshot.agent_ids) == ["a1"]
    assert isinstance(_FakeMembershipSource(), RoomMembershipSeedSource)


def test_normalize_legacy_inverted_room_agent_set():
    inverted = {"Agent One": "550e8400-e29b-41d4-a716-446655440000"}

    assert normalize_room_agent_set(inverted) == {
        "550e8400-e29b-41d4-a716-446655440000": "Agent One"
    }


@pytest.mark.asyncio
async def test_manual_seed_resolves_names_and_provenance():
    registry = _registry(
        AgentInfo(agent_id="a1", name="Agent One"),
        AgentInfo(agent_id="a2", name=None),
    )

    resolved = await resolve_membership_seed(
        seed=MembershipSeed(mode="manual", agent_ids=["a2", "a1"]),
        owner_id="owner",
        agent_registry=registry,
        membership_source=_FakeMembershipSource(),
    )

    assert resolved.agent_set == {"a2": "a2", "a1": "Agent One"}
    assert resolved.membership_origin == "manual"
    assert resolved.membership_origin_status == "manual"


@pytest.mark.asyncio
async def test_manual_seed_rejects_unknown_and_inaccessible_private_agents():
    registry = _registry(
        AgentInfo(agent_id="private", name="Private", is_public=False, provider_id="u2")
    )

    with pytest.raises(ValueError, match="Unknown or deleted agent IDs: missing"):
        await resolve_membership_seed(
            seed=MembershipSeed(mode="manual", agent_ids=["missing"]),
            owner_id="u1",
            agent_registry=registry,
            membership_source=_FakeMembershipSource(),
        )

    with pytest.raises(ValueError, match="Access denied to private agents: private"):
        await resolve_membership_seed(
            seed=MembershipSeed(mode="manual", agent_ids=["private"]),
            owner_id="u1",
            agent_registry=registry,
            membership_source=_FakeMembershipSource(),
        )


@pytest.mark.asyncio
async def test_saved_group_seed_validation_filtering_and_provenance():
    source = _FakeMembershipSource(
        saved_groups={
            "builtin": SavedAgentGroupSnapshot(
                group_id="builtin",
                name="Built In",
                type="builtin",
                owner_id="someone-else",
                agent_ids=["a1", "inactive"],
            ),
            "private": SavedAgentGroupSnapshot(
                group_id="private",
                name="Private Group",
                type="custom",
                owner_id="u2",
                agent_ids=["a1"],
            ),
        }
    )
    registry = _registry(
        AgentInfo(agent_id="a1", name="Agent One", status="active"),
        AgentInfo(agent_id="inactive", name="Inactive", status="inactive"),
    )

    with pytest.raises(ValueError, match="group_id is required"):
        await resolve_membership_seed(
            seed=MembershipSeed(mode="saved_group"),
            owner_id="u1",
            agent_registry=registry,
            membership_source=source,
        )
    with pytest.raises(ValueError, match="Saved group missing not found"):
        await resolve_membership_seed(
            seed=MembershipSeed(mode="saved_group", group_id="missing"),
            owner_id="u1",
            agent_registry=registry,
            membership_source=source,
        )
    with pytest.raises(ValueError, match="permission"):
        await resolve_membership_seed(
            seed=MembershipSeed(mode="saved_group", group_id="private"),
            owner_id="u1",
            agent_registry=registry,
            membership_source=source,
        )

    resolved = await resolve_membership_seed(
        seed=MembershipSeed(mode="saved_group", group_id="builtin"),
        owner_id="u1",
        agent_registry=registry,
        membership_source=source,
    )

    assert resolved.agent_set == {"a1": "Agent One"}
    assert resolved.membership_origin == "saved_group"
    assert resolved.membership_origin_status == "seeded_never_edited"
    assert resolved.source_group_id == "builtin"
    assert resolved.source_group_name == "Built In"


@pytest.mark.asyncio
async def test_all_current_agents_seed_uses_source_active_visible_agents():
    source = _FakeMembershipSource(
        current_agents=[
            AgentInfo(agent_id="a1", name="Agent One", status="active"),
            AgentInfo(agent_id="inactive", name="Inactive", status="inactive"),
        ]
    )

    resolved = await resolve_membership_seed(
        seed=MembershipSeed(mode="all_current_agents", requesting_user_id="u1"),
        owner_id="owner",
        agent_registry=_registry(),
        membership_source=source,
    )

    assert source.list_current_agents_calls == ["u1"]
    assert resolved.agent_set == {"a1": "Agent One"}
    assert resolved.membership_origin == "all_current_agents"
    assert resolved.membership_origin_status == "seeded_never_edited"


def _registry(*agents: AgentInfo):
    by_id = {agent.agent_id: agent for agent in agents}
    registry = AsyncMock()

    async def get_agents_by_ids(agent_ids: list[str]) -> list[AgentInfo]:
        return [by_id[agent_id] for agent_id in agent_ids if agent_id in by_id]

    registry.get_agents_by_ids.side_effect = get_agents_by_ids
    return registry


class _FakeMembershipSource:
    def __init__(
        self,
        *,
        saved_groups: dict[str, SavedAgentGroupSnapshot] | None = None,
        current_agents: list[AgentInfo] | None = None,
    ) -> None:
        self.saved_groups = saved_groups or {}
        self.current_agents = current_agents or []
        self.list_current_agents_calls: list[str | None] = []

    async def get_saved_group(self, group_id: str) -> SavedAgentGroupSnapshot | None:
        return self.saved_groups.get(group_id)

    async def list_current_agents(self, user_id: str | None) -> list[AgentInfo]:
        self.list_current_agents_calls.append(user_id)
        return list(self.current_agents)


@pytest.mark.asyncio
async def test_membership_source_lists_active_visible_agents_from_registry():
    info = AgentInfo(agent_id="a1", name="Agent One")
    registry = SimpleNamespace(list_visible_agents=AsyncMock(return_value=[info]))
    source = RepositoryRoomMembershipSeedSource(agent_registry=registry)

    assert await source.list_current_agents("owner") == [info]
    registry.list_visible_agents.assert_awaited_once_with(
        user_id="owner", active_only=True
    )
