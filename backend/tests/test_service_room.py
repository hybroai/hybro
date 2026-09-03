"""
Unit tests for RoomCenter (room_runtime.py) -- pure logic methods.

Tests cover:
- _looks_like_agent_id heuristic
- _normalize_room_agent_set canonical shape detection
- parse_agent_mentions extraction
- extract_agent_message_content per-agent routing
- _validate_send_message_request input validation
"""

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.dto import (
    MessageCommitted,
    RoomInfo,
    UserMessageInsertResult,
)
from models.request import RoomCenterRoomSettingRequest, RoomCenterUserMessageRequest
from models.room import MessageContent, Room, RoomUserMessage, UserAttachment
from room.compat.runtime import RoomServices
from room.deletion import RoomDeletionService
from room.idempotency import IdempotencyConflictError, UserMessagePersistenceError
from room.user_message_persistence import (
    UserMessageCommitCommand,
    UserMessageCommitService,
)


@pytest.fixture
def room_center():
    """Create a RoomCenter with mocked dependencies."""
    rc = object.__new__(RoomServices)
    rc._store = MagicMock()
    # Backwards compatibility alias
    rc.database_service = rc._store
    rc.openai_service = MagicMock()
    return rc


_ROOT = Path(__file__).resolve().parents[1]


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.internal_events = []
        self.wait_flags = []

    async def publish(
        self,
        event,
        *,
        wait_for_handlers: bool = False,
        fanout: bool = True,
    ) -> None:
        self.internal_events.append(event)
        self.wait_flags.append(wait_for_handlers)


def _bind_user_message_commit(
    svc: RoomServices,
    *,
    facade,
    publisher,
    room_files=None,
) -> UserMessageCommitService:
    service = UserMessageCommitService(
        writer=facade,
        files=room_files,
        internal_event_publisher=publisher,
    )
    svc.bind_user_message_commit(service)
    return service


@pytest.mark.asyncio
async def test_preflight_token_lifecycle_always_releases_identity_owner():
    svc = object.__new__(RoomServices)
    svc.cancellation_control = MagicMock()
    token = object()
    context = SimpleNamespace(
        user_message=SimpleNamespace(message_id="msg-1"), token=token
    )

    for outcome in ("failed", "canceled", "completed"):
        svc.cancellation_control.release_token.reset_mock()
        response = SimpleNamespace(preflight_outcome=outcome)
        svc._run_message_preflight_to_room = AsyncMock(return_value=response)

        assert await svc.run_message_preflight_to_room(context) is response
        svc.cancellation_control.release_token.assert_called_once_with("msg-1", token)

    svc.cancellation_control.release_token.reset_mock()
    ready_response = SimpleNamespace(preflight_outcome="ready")
    svc._run_message_preflight_to_room = AsyncMock(return_value=ready_response)

    assert await svc.run_message_preflight_to_room(context) is ready_response
    svc.cancellation_control.release_token.assert_called_once_with("msg-1", token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("parse failed"), asyncio.CancelledError()]
)
async def test_preflight_token_lifecycle_removes_on_exception_or_cancellation(failure):
    svc = object.__new__(RoomServices)
    svc.cancellation_control = MagicMock()
    token = object()
    context = SimpleNamespace(
        user_message=SimpleNamespace(message_id="msg-1"), token=token
    )
    svc._run_message_preflight_to_room = AsyncMock(side_effect=failure)

    with pytest.raises(type(failure)):
        await svc.run_message_preflight_to_room(context)

    svc.cancellation_control.release_token.assert_called_once_with("msg-1", token)


def test_room_services_excludes_dead_compat_helpers_and_stale_wiring():
    dead_helpers = {
        "_build_supervisor_conversation_context",
        "_active_run_payloads_from_raw",
        "_resolve_membership_input",
        "_validate_agents_access",
        "_fetch_agents_from_set",
        "_require_memory_search",
        "_search_context_memory_results",
        "_require_context_assembly",
    }

    assert dead_helpers.isdisjoint(RoomServices.__dict__)
    assert (
        "memory_search"
        not in inspect.signature(RoomServices.bind_context_memory).parameters
    )

    container_tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "container.py").read_text()
    )
    context_memory_binding = next(
        node
        for node in ast.walk(container_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "room_runtime"
        and node.func.attr == "bind_context_memory"
    )
    assert "memory_search" not in {
        keyword.arg for keyword in context_memory_binding.keywords
    }


def test_room_services_bind_store_sets_runtime_store():
    svc = object.__new__(RoomServices)
    store = object()

    svc.bind_store(store)

    assert svc._store is store


@pytest.mark.asyncio
async def test_room_services_delegated_methods_fail_before_bind():
    svc = object.__new__(RoomServices)
    svc._facade = None
    svc._bound = False

    with pytest.raises(
        RuntimeError,
        match=r"RoomServices\.bind_facade\(\) not called - startup incomplete",
    ):
        await svc.create_new_room(RoomCenterRoomSettingRequest(room_name="Room"))


@pytest.mark.asyncio
async def test_room_services_bind_facade_delegates_room_lifecycle_methods():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.create_room.return_value = RoomInfo(
        room_id="r1",
        room_name="Room",
        owner_id="owner",
        owner_name="Owner",
        agent_ids=["a1"],
        agent_set={"a1": "Agent One"},
    )
    facade.get_room.return_value = facade.create_room.return_value
    facade.list_rooms_for_owner.return_value = [facade.create_room.return_value]
    facade.list_room_history_for_owner.return_value = [facade.create_room.return_value]
    facade.replace_membership.return_value = facade.create_room.return_value
    facade.update_room.return_value = facade.create_room.return_value
    facade.get_room_owner.return_value = "owner"
    facade.delete_room.return_value = True

    svc.bind_facade(facade)
    memory_cleanup = SimpleNamespace(delete_room_memory=AsyncMock(return_value=True))
    svc.bind_context_memory(room_memory_cleanup=memory_cleanup)
    svc.bind_room_deletion(
        RoomDeletionService(
            room_lifecycle=facade,
            memory_cleanup=memory_cleanup,
        )
    )
    svc._s3_service = SimpleNamespace(delete_prefix=AsyncMock())

    create_response = await svc.create_new_room(
        RoomCenterRoomSettingRequest(
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={"a1": "Agent One"},
            extend_info={"debateMode": True, "use_supervisor": True},
            requesting_user_id="owner",
        )
    )
    inquiry_response = await svc.inquiry_room_setting(
        RoomCenterRoomSettingRequest(room_id="r1", requesting_user_id="owner")
    )
    list_response = await svc.inquiry_rooms_by_room_owner_id(
        RoomCenterRoomSettingRequest(room_owner_id="owner")
    )
    history_response = await svc.inquiry_room_history_by_owner_id(
        RoomCenterRoomSettingRequest(room_owner_id="owner")
    )
    replace_response = await svc.update_room_agent_set(
        RoomCenterRoomSettingRequest(
            room_id="r1",
            room_agent_set={"a1": "Agent One"},
            requesting_user_id="owner",
        )
    )
    rename_response = await svc.update_room_name(
        RoomCenterRoomSettingRequest(room_id="r1", room_name="Renamed")
    )
    delete_response = await svc.delete_room_by_room_id(
        RoomCenterRoomSettingRequest(room_id="r1", requesting_user_id="owner")
    )

    assert create_response.success is True
    assert create_response.room.room_id == "r1"
    assert inquiry_response.room.room_agent_set == {"a1": "Agent One"}
    assert list_response.room_list[0].room_id == "r1"
    assert history_response.room_list[0].room_id == "r1"
    assert replace_response.success is True
    assert rename_response.success is True
    assert delete_response.success is True
    facade.create_room.assert_awaited_once()
    create_request = facade.create_room.await_args.args[0]
    assert create_request.extend_info == {"debateMode": True, "use_supervisor": True}
    facade.get_room.assert_awaited()
    facade.list_rooms_for_owner.assert_awaited_once_with("owner")
    facade.list_room_history_for_owner.assert_awaited_once_with("owner", limit=100)
    facade.replace_membership.assert_awaited_once()
    facade.update_room.assert_awaited_once_with("r1", {"room_name": "Renamed"})
    facade.delete_room.assert_awaited_once_with("r1", "owner")


@pytest.mark.asyncio
async def test_room_services_active_runs_response_is_room_metadata_only():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        side_effect=AssertionError("legacy room store should not be used")
    )
    svc._store.get_room_user_message_by_message_id = AsyncMock(
        side_effect=AssertionError("legacy message store should not be used")
    )
    svc._store.get_active_runs_by_room_id = AsyncMock(
        side_effect=AssertionError("legacy active-run store should not be used")
    )
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_room.return_value = RoomInfo(
        room_id="r1",
        room_name="Room",
        owner_id="owner",
        owner_name="Owner",
    )
    facade.get_turn_completion_kind.return_value = "synthesis"

    svc.bind_facade(facade)

    response = await svc.inquiry_active_runs(
        RoomCenterRoomSettingRequest(
            room_id="r1",
            trigger_message_id="trigger-1",
        )
    )

    assert response.success is True
    assert response.room_id == "r1"
    assert response.active_runs == []
    assert response.turn_completion_kind == "synthesis"
    facade.get_room.assert_awaited_once_with("r1")
    facade.get_turn_completion_kind.assert_awaited_once_with("trigger-1")
    svc._store.get_room_by_room_id.assert_not_awaited()
    svc._store.get_room_user_message_by_message_id.assert_not_awaited()
    svc._store.get_active_runs_by_room_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_services_room_setting_returns_room_metadata_only():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_active_runs_by_room_id = AsyncMock(
        side_effect=AssertionError("legacy active-run store should not be used")
    )
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_room.return_value = RoomInfo(
        room_id="r1",
        room_name="Room",
        owner_id="owner",
        owner_name="Owner",
    )
    svc.bind_facade(facade)

    response = await svc.inquiry_room_setting(
        RoomCenterRoomSettingRequest(room_id="r1")
    )

    assert response.success is True
    assert response.active_runs is None
    facade.get_room.assert_awaited_once_with("r1")
    svc._store.get_active_runs_by_room_id.assert_not_awaited()


def test_room_services_migrated_crud_methods_do_not_keep_legacy_store_branches():
    forbidden_by_method = {
        "inquiry_room_setting": {"get_room_by_room_id", "update_room_by_room_id"},
        "inquiry_active_runs": {
            "get_room_by_room_id",
            "get_room_user_message_by_message_id",
        },
        "inquiry_rooms_by_room_owner_id": {"get_rooms_by_room_owner_id"},
        "inquiry_room_history_by_owner_id": {"get_rooms_by_room_owner_id"},
        "update_room_agent_set": {"get_room_by_room_id", "update_room_by_room_id"},
        "update_room_name": {"get_room_by_room_id", "update_room_by_room_id"},
    }
    source = _ROOT / "room" / "compat" / "runtime.py"
    tree = ast.parse(source.read_text())
    methods = {
        item.name: item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RoomServices"
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    violations: list[str] = []
    for method_name, forbidden_attrs in forbidden_by_method.items():
        method = methods[method_name]
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                violations.append(f"{method_name}:{node.lineno}: {node.attr}")

    assert not violations, (
        "Migrated methods still use legacy store branches:\n" + "\n".join(violations)
    )


@pytest.mark.asyncio
async def test_room_services_persist_user_message_emits_message_committed_event():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.add_room_user_message = AsyncMock(
        side_effect=AssertionError("legacy message store should not be used")
    )
    svc._bound = False
    svc._facade = None
    publisher = RecordingEventPublisher()
    facade = AsyncMock()
    facade.persist_user_message.return_value = UserMessageInsertResult(
        message_id="u1",
        created=True,
        document={},
    )
    svc.bind_facade(facade)
    _bind_user_message_commit(svc, facade=facade, publisher=publisher)
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )

    result = await svc._persist_user_message(
        user_message,
        room_agent_set={"a1": "Agent One"},
    )

    assert result.created is True
    assert result.message_id == "u1"
    facade.persist_user_message.assert_awaited_once_with(
        user_message,
        idempotency_fingerprint=None,
        idempotency_fingerprint_version=None,
    )
    svc._store.add_room_user_message.assert_not_awaited()
    assert len(publisher.internal_events) == 1
    event = publisher.internal_events[0]
    assert isinstance(event, MessageCommitted)
    assert event.room_id == "r1"
    assert event.message_id == "u1"
    assert event.message_type == "user"
    assert event.agent_id is None
    assert event.room_agent_set == {"a1": "Agent One"}
    assert publisher.wait_flags == [True]


@pytest.mark.asyncio
async def test_room_services_persist_user_message_does_not_emit_on_failure():
    svc = object.__new__(RoomServices)
    svc._bound = False
    svc._facade = None
    publisher = RecordingEventPublisher()
    facade = AsyncMock()
    facade.persist_user_message.side_effect = UserMessagePersistenceError(
        "insert failed"
    )
    svc.bind_facade(facade)
    _bind_user_message_commit(svc, facade=facade, publisher=publisher)
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )

    with pytest.raises(UserMessagePersistenceError, match="insert failed"):
        await svc._persist_user_message(user_message, room_agent_set={})

    assert publisher.internal_events == []
    assert publisher.wait_flags == []


@pytest.mark.asyncio
async def test_room_services_assigns_message_id_before_claiming_file_references():
    svc = object.__new__(RoomServices)
    svc._bound = False
    svc._facade = None
    publisher = RecordingEventPublisher()
    facade = MagicMock()

    def ensure_user_message_id(user_message):
        if user_message.message_id == "":
            user_message.message_id = "generated-message-id"
        return user_message.message_id

    async def persist_user_message(user_message, **_kwargs):
        if user_message.message_id == "":
            user_message.message_id = "generated-message-id"
        return UserMessageInsertResult(
            message_id=user_message.message_id,
            created=True,
            document={},
        )

    facade.ensure_user_message_id.side_effect = ensure_user_message_id
    facade.persist_user_message = AsyncMock(side_effect=persist_user_message)
    room_files = MagicMock()
    room_files.claim_references = AsyncMock()
    room_files.commit_references = AsyncMock()
    room_files.release_references = AsyncMock()
    svc.bind_facade(facade)
    svc.bind_room_files(room_files)
    commit_service = _bind_user_message_commit(
        svc,
        facade=facade,
        publisher=publisher,
        room_files=room_files,
    )
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="",
        user_id="user-1",
        message_content=MessageContent(
            message_text="What content in this pdf?",
            attachments=[
                UserAttachment(
                    file_id="pdf-1",
                    mime_type="application/pdf",
                    file_name="document.pdf",
                    size_bytes=1024,
                )
            ],
        ),
    )

    result = await commit_service.commit(
        UserMessageCommitCommand(message=user_message, room_agent_set={})
    )

    assert result.created is True
    facade.ensure_user_message_id.assert_called_once_with(user_message)
    room_files.claim_references.assert_awaited_once_with(
        room_id="r1",
        owner_id="user-1",
        message_id="generated-message-id",
        file_ids=["pdf-1"],
    )
    room_files.commit_references.assert_awaited_once_with(
        message_id="generated-message-id",
        file_ids=["pdf-1"],
    )
    assert publisher.internal_events[0].message_id == "generated-message-id"


@pytest.mark.asyncio
async def test_room_services_persist_message_to_room_passes_room_agent_set_to_user_commit_event():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            room_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={"a1": "Canonical Agent"},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    svc.cancellation_control = MagicMock()
    svc.cancellation_control.create_token.return_value = object()
    svc.cancellation_control.check_cancelled = AsyncMock(return_value=False)
    svc._validate_send_message_request = MagicMock(return_value=None)
    svc._resolve_and_apply_attachments = AsyncMock(return_value=None)
    svc._resolve_explicit_target_scope = AsyncMock()
    svc._materialize_room_quote = AsyncMock(return_value=None)
    publisher = RecordingEventPublisher()
    facade = AsyncMock()
    facade.persist_user_message.return_value = UserMessageInsertResult(
        message_id="u1",
        created=True,
        document={},
    )
    svc.bind_facade(facade)
    _bind_user_message_commit(svc, facade=facade, publisher=publisher)
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        user_id="user-1",
        message_content=MessageContent(
            message_text="Please ask <@a1|Stale Name> for help"
        ),
    )

    response, context = await svc.persist_message_to_room(
        RoomCenterUserMessageRequest(
            room_id="r1",
            user_id="user-1",
            message=user_message,
        ),
        target_group="all_agents",
    )

    assert response.success is True
    assert context is not None
    svc.cancellation_control.check_cancelled.assert_awaited_once_with("u1")
    event = publisher.internal_events[0]
    assert isinstance(event, MessageCommitted)
    assert event.room_agent_set == {"a1": "Canonical Agent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_structured_quote", [False, True])
async def test_canonical_send_sanitizes_server_owned_user_message_fields(
    with_structured_quote,
):
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            room_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    svc.cancellation_control = MagicMock()
    svc.cancellation_control.create_token.return_value = object()
    svc.cancellation_control.check_cancelled = AsyncMock(return_value=False)
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = None
    svc.bind_facade(facade)
    svc._resolve_and_apply_attachments = AsyncMock(return_value=None)
    svc._materialize_room_quote = AsyncMock(return_value=None)

    async def persist(user_message, **_kwargs):
        assert user_message.room_id == "r1"
        assert user_message.user_id == "trusted-user"
        assert user_message.message_id == ""
        assert user_message.message_type == "user"
        assert user_message.agent_id is None
        assert user_message.run_id is None
        assert user_message.step_number is None
        assert user_message.total_steps is None
        assert user_message.task_content is None
        assert user_message.processing_claimed_at is None
        assert user_message.quote_id is None
        expected_legacy_quote = (
            {
                "execution_mode": "direct",
                "agent_scope": {"source": "room_default"},
            }
            if with_structured_quote
            else {
                "quoted_text": "allowed quote",
                "quoted_sender_name": "Allowed Sender",
                "execution_mode": "direct",
                "agent_scope": {"source": "room_default"},
            }
        )
        assert user_message.extend_info == expected_legacy_quote
        user_message.message_id = "server-message"
        return UserMessageInsertResult(
            message_id="server-message",
            created=True,
            document={},
        )

    svc._persist_user_message = AsyncMock(side_effect=persist)
    request = RoomCenterUserMessageRequest(
        room_id="r1",
        user_id="trusted-user",
        client_request_id="request-1",
        message=RoomUserMessage(
            room_id="spoofed-room",
            message_id="client-message",
            message_type="agent",
            user_id="spoofed-user",
            agent_id="spoofed-agent",
            run_id="spoofed-run",
            step_number=9,
            total_steps=9,
            task_content="spoofed task",
            processing_claimed_at="2026-08-01T00:00:00Z",
            quote_id="spoofed-quote-id",
            extend_info={
                "quoted_text": "allowed quote",
                "quoted_sender_name": "Allowed Sender",
                "quote_id": "spoofed-quote-id",
                "turn_completion_kind": "synthesis",
                "orchestration_status": "completed",
                "custom_internal": "spoofed",
            },
            message_content=MessageContent(message_text="hello"),
            quote=(
                {
                    "text": "structured quote",
                    "source_message_id": "source-1",
                    "source_kind": "agent",
                    "sender_display_name": None,
                }
                if with_structured_quote
                else None
            ),
        ),
    )

    response, context = await svc.persist_message_to_room(
        request,
        target_group="all_agents",
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert response.success is True
    assert response.message_id == "server-message"
    assert context is not None


@pytest.mark.asyncio
async def test_sequential_replay_skips_quote_attachment_claim_event_and_token():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._bound = False
    svc._facade = None
    svc.cancellation_control = MagicMock()
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = {
        "room_id": "r1",
        "message_id": "winner-message",
        "client_request_id": "request-1",
        "idempotency_fingerprint": "fingerprint-1",
        "idempotency_fingerprint_version": 1,
    }
    svc.bind_facade(facade)
    svc._validate_send_message_request = MagicMock(
        side_effect=AssertionError("replay must return before validation")
    )
    svc._resolve_and_apply_attachments = AsyncMock(
        side_effect=AssertionError("replay must not resolve attachments")
    )
    svc._materialize_room_quote = AsyncMock(
        side_effect=AssertionError("replay must not create quote")
    )
    publisher = RecordingEventPublisher()

    response, context = await svc.persist_message_to_room(
        RoomCenterUserMessageRequest(
            room_id="r1",
            user_id="user-1",
            client_request_id=" request-1 ",
            message=RoomUserMessage(
                room_id="r1",
                message_id="loser-message",
                user_id="user-1",
                message_content=MessageContent(message_text="hello"),
            ),
        ),
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert response.success is True
    assert response.message_id == "winner-message"
    assert response.dispatch_root_message_id is None
    assert context is None
    facade.get_user_message_by_idempotency_key.assert_awaited_once_with(
        "r1", "request-1"
    )
    facade.persist_user_message.assert_not_awaited()
    svc.cancellation_control.create_token.assert_not_called()
    assert publisher.internal_events == []


@pytest.mark.asyncio
async def test_legacy_idempotency_row_replays_without_backfilling_fingerprint(caplog):
    svc = object.__new__(RoomServices)
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = {
        "room_id": "r1",
        "message_id": "legacy-message",
        "client_request_id": "request-legacy",
    }
    svc.bind_facade(facade)

    response = await svc.get_idempotent_user_message(
        room_id="r1",
        client_request_id="request-legacy",
        idempotency_fingerprint="new-fingerprint-cannot-be-proven",
        idempotency_fingerprint_version=1,
    )

    assert response is not None
    assert response.success is True
    assert response.message_id == "legacy-message"
    assert response.dispatch_root_message_id is None
    assert "Legacy idempotency replay without fingerprint" in caplog.text
    facade.update_user_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["conflict", "replay"])
async def test_claim_release_failure_preserves_determined_idempotency_outcome(
    outcome,
    caplog,
):
    class Lease:
        async def __aenter__(self):
            return "lease-1"

        async def __aexit__(self, *_args):
            return False

    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            room_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    svc.cancellation_control = MagicMock()
    facade = MagicMock()
    facade.get_user_message_by_idempotency_key = AsyncMock(return_value=None)

    def ensure_user_message_id(user_message):
        user_message.message_id = user_message.message_id or "loser-message"
        return user_message.message_id

    facade.ensure_user_message_id.side_effect = ensure_user_message_id
    if outcome == "conflict":
        facade.persist_user_message = AsyncMock(
            side_effect=IdempotencyConflictError("r1", "request-1")
        )
    else:
        facade.persist_user_message = AsyncMock(
            return_value=UserMessageInsertResult(
                message_id="winner-message",
                created=False,
                document={"message_id": "winner-message"},
            )
        )
    svc.bind_facade(facade)
    room_files = MagicMock()
    room_files.write_lease.return_value = Lease()
    room_files.claim_references = AsyncMock()
    room_files.release_references = AsyncMock(
        side_effect=RuntimeError("release unavailable")
    )
    room_files.commit_references = AsyncMock()
    svc.bind_room_files(room_files)
    publisher = RecordingEventPublisher()
    _bind_user_message_commit(
        svc,
        facade=facade,
        publisher=publisher,
        room_files=room_files,
    )

    async def resolve_attachments(_request, user_message):
        user_message.message_content.attachments = [
            UserAttachment(
                file_id="file-1",
                mime_type="text/plain",
                file_name="note.txt",
                size_bytes=10,
            )
        ]
        return None

    svc._resolve_and_apply_attachments = AsyncMock(side_effect=resolve_attachments)
    svc._materialize_room_quote = AsyncMock(return_value=None)
    svc.run_message_preflight_to_room = AsyncMock()
    request = RoomCenterUserMessageRequest(
        room_id="r1",
        user_id="user-1",
        client_request_id="request-1",
        message=RoomUserMessage(
            room_id="r1",
            message_id="client-message",
            user_id="user-1",
            message_content=MessageContent(message_text="hello"),
        ),
    )

    response, context = await svc.persist_message_to_room(
        request,
        target_group="all_agents",
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert context is None
    if outcome == "conflict":
        assert response.success is False
        assert response.status_code == 409
        assert response.message_id is None
        assert "preserving persistence error" in caplog.text
    else:
        assert response.success is True
        assert response.message_id == "winner-message"
        assert response.dispatch_root_message_id is None
        assert "returning replay" in caplog.text
    room_files.release_references.assert_awaited_once_with(
        message_id="loser-message",
        file_ids=["file-1"],
    )
    room_files.commit_references.assert_not_awaited()
    assert publisher.internal_events == []
    svc.cancellation_control.create_token.assert_not_called()
    svc.run_message_preflight_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_loser_releases_own_claim_without_touching_winner_commit():
    svc = object.__new__(RoomServices)
    svc._bound = False
    svc._facade = None
    facade = MagicMock()
    facade.ensure_user_message_id.side_effect = lambda message: (
        message.message_id or "loser-message"
    )
    facade.persist_user_message = AsyncMock(
        return_value=UserMessageInsertResult(
            message_id="winner-message",
            created=False,
            document={"message_id": "winner-message"},
        )
    )
    room_files = MagicMock()
    room_files.claim_references = AsyncMock()
    room_files.commit_references = AsyncMock()
    room_files.release_references = AsyncMock()
    svc.bind_facade(facade)
    svc.bind_room_files(room_files)
    publisher = RecordingEventPublisher()
    commit_service = _bind_user_message_commit(
        svc,
        facade=facade,
        publisher=publisher,
        room_files=room_files,
    )
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="loser-message",
        user_id="user-1",
        message_content=MessageContent(
            message_text="hello",
            attachments=[
                UserAttachment(
                    file_id="file-1",
                    mime_type="text/plain",
                    file_name="note.txt",
                    size_bytes=10,
                )
            ],
        ),
    )

    result = await commit_service.commit(
        UserMessageCommitCommand(
            message=user_message,
            room_agent_set={},
            idempotency_fingerprint="fingerprint-1",
            idempotency_fingerprint_version=1,
        )
    )

    assert result.created is False
    assert result.message_id == "winner-message"
    room_files.claim_references.assert_awaited_once_with(
        room_id="r1",
        owner_id="user-1",
        message_id="loser-message",
        file_ids=["file-1"],
    )
    room_files.release_references.assert_awaited_once_with(
        message_id="loser-message",
        file_ids=["file-1"],
    )
    room_files.commit_references.assert_not_awaited()
    assert publisher.internal_events == []


@pytest.mark.asyncio
async def test_concurrent_loser_deletes_only_its_new_quote_and_returns_winner():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            room_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    svc.cancellation_control = MagicMock()
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = None
    svc.bind_facade(facade)
    svc._validate_send_message_request = MagicMock(return_value=None)
    svc._resolve_and_apply_attachments = AsyncMock(return_value=None)

    async def materialize(_room, _request, user_message):
        user_message.quote_id = "loser-quote"
        return None

    svc._materialize_room_quote = AsyncMock(side_effect=materialize)
    svc._persist_user_message = AsyncMock(
        return_value=UserMessageInsertResult(
            message_id="winner-message",
            created=False,
            document={"message_id": "winner-message", "quote_id": "winner-quote"},
        )
    )
    request = RoomCenterUserMessageRequest(
        room_id="r1",
        user_id="user-1",
        client_request_id="request-1",
        message=RoomUserMessage(
            room_id="r1",
            message_id="loser-message",
            user_id="user-1",
            message_content=MessageContent(message_text="hello"),
        ),
    )

    response, context = await svc.persist_message_to_room(
        request,
        target_group="all_agents",
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert response.success is True
    assert response.message_id == "winner-message"
    assert response.dispatch_root_message_id is None
    assert context is None
    facade.delete_room_quote.assert_awaited_once_with("loser-quote")
    assert "winner-quote" not in {
        call.args[0] for call in facade.delete_room_quote.await_args_list
    }
    svc.cancellation_control.create_token.assert_not_called()


@pytest.mark.asyncio
async def test_room_services_quote_materialization_delegates_to_room_facade():
    svc = object.__new__(RoomServices)
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.materialize_quote.return_value = None
    svc.bind_facade(facade)
    room = Room(
        room_id="r1",
        room_name="Room",
        room_owner_id="owner",
        room_owner_name="Owner",
    )
    user_message = RoomUserMessage(
        room_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )
    request = MagicMock()

    assert await svc._materialize_room_quote(room, request, user_message) is None
    facade.materialize_quote.assert_awaited_once_with(
        room=room,
        request=request,
        user_message=user_message,
    )


def test_room_services_migrated_message_methods_do_not_call_legacy_store():
    forbidden_by_method = {
        "_persist_user_message": {"add_room_user_message"},
        "update_agent_message_by_message_id": {
            "get_room_agent_message_by_message_id",
            "update_room_agent_message_by_message_id",
        },
        "inquiry_user_messages_by_room_id": {"get_room_user_messages_by_room_id"},
        "inquiry_agent_messages_by_room_id": {
            "get_room_agent_messages_by_room_id",
            "update_room_agent_message_by_message_id",
        },
        "inquiry_agent_message_by_message_id": {
            "get_room_agent_message_by_message_id",
        },
        "inquiry_user_message_by_message_id": {
            "get_room_user_message_by_message_id",
        },
        "inquiry_agent_messages_by_related_message_id": {
            "get_room_agent_messages_by_related_message_id",
        },
    }
    source = _ROOT / "room" / "compat" / "runtime.py"
    tree = ast.parse(source.read_text())
    methods = {
        item.name: item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RoomServices"
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    violations: list[str] = []
    for method_name, forbidden_attrs in forbidden_by_method.items():
        method = methods[method_name]
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                violations.append(f"{method_name}:{node.lineno}: {node.attr}")

    assert not violations, (
        "Migrated message methods still use legacy store:\n" + "\n".join(violations)
    )


@pytest.mark.asyncio
async def test_delete_room_does_not_cleanup_when_requester_is_not_owner():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_active_runs_by_room_id = AsyncMock(return_value=[])
    svc._bound = False
    svc._facade = None
    svc._s3_service = SimpleNamespace(delete_prefix=AsyncMock())
    facade = AsyncMock()
    facade.get_room_owner.return_value = "owner"
    facade.delete_room.return_value = True
    svc.bind_facade(facade)
    memory_cleanup = SimpleNamespace(delete_room_memory=AsyncMock(return_value=True))
    svc.bind_context_memory(room_memory_cleanup=memory_cleanup)
    svc.bind_room_deletion(
        RoomDeletionService(
            room_lifecycle=facade,
            memory_cleanup=memory_cleanup,
        )
    )

    response = await svc.delete_room_by_room_id(
        RoomCenterRoomSettingRequest(room_id="r1", requesting_user_id="intruder")
    )

    assert response.success is False
    assert response.status_code == 403
    assert response.error == "Forbidden"
    facade.delete_room.assert_not_awaited()
    memory_cleanup.delete_room_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_room_success_when_post_delete_context_memory_cleanup_fails():
    svc = object.__new__(RoomServices)
    svc._store = MagicMock()
    svc._store.get_active_runs_by_room_id = AsyncMock(return_value=[])
    svc._bound = False
    svc._facade = None
    svc._s3_service = SimpleNamespace(delete_prefix=AsyncMock())
    facade = AsyncMock()
    facade.get_room_owner.return_value = "owner"
    facade.delete_room.return_value = True
    svc.bind_facade(facade)
    memory_cleanup = SimpleNamespace(delete_room_memory=AsyncMock(return_value=False))
    svc.bind_context_memory(room_memory_cleanup=memory_cleanup)
    svc.bind_room_deletion(
        RoomDeletionService(
            room_lifecycle=facade,
            memory_cleanup=memory_cleanup,
        )
    )

    response = await svc.delete_room_by_room_id(
        RoomCenterRoomSettingRequest(room_id="r1", requesting_user_id="owner")
    )

    assert response.success is True
    assert response.status_code == 200
    assert response.error is None
    facade.delete_room.assert_awaited_once_with("r1", "owner")


# =============================================================================
# _looks_like_agent_id Tests
# =============================================================================


class TestLooksLikeAgentId:
    """Tests for UUID-style agent ID detection."""

    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "550e8400e29b41d4a716446655440000",
        ],
    )
    def test_recognizes_valid_uuids(self, value):
        assert RoomServices._looks_like_agent_id(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "MyAgent",
            "agent-name",
            "",
            "not-a-uuid-at-all",
        ],
    )
    def test_rejects_non_uuid_strings(self, value):
        assert RoomServices._looks_like_agent_id(value) is False

    def test_rejects_non_string(self):
        assert RoomServices._looks_like_agent_id(123) is False
        assert RoomServices._looks_like_agent_id(None) is False


# =============================================================================
# _normalize_room_agent_set Tests
# =============================================================================


class TestNormalizeRoomAgentSet:
    """Tests for room_agent_set normalization."""

    def test_returns_empty_for_none(self, room_center):
        assert room_center._normalize_room_agent_set(None) == {}

    def test_returns_empty_for_empty(self, room_center):
        assert room_center._normalize_room_agent_set({}) == {}

    def test_preserves_correct_shape(self, room_center):
        """Keys are UUIDs, values are names -- already canonical."""
        data = {"550e8400e29b41d4a716446655440000": "MyAgent"}
        result = room_center._normalize_room_agent_set(data)
        assert result == data

    def test_flips_inverted_shape(self, room_center):
        """Keys are names, values are UUIDs -- needs flipping."""
        data = {"MyAgent": "550e8400e29b41d4a716446655440000"}
        result = room_center._normalize_room_agent_set(data)
        assert "550e8400e29b41d4a716446655440000" in result
        assert result["550e8400e29b41d4a716446655440000"] == "MyAgent"

    def test_handles_ambiguous_data(self, room_center):
        """When keys and values both look like IDs, preserves original."""
        data = {"550e8400e29b41d4a716446655440000": "660e8400e29b41d4a716446655440000"}
        result = room_center._normalize_room_agent_set(data)
        assert result == data


# =============================================================================
# parse_agent_mentions Tests
# =============================================================================


class TestParseAgentMentions:
    """Tests for @agent mention parsing."""

    def test_parses_single_mention(self, room_center):
        text = "Hello <@agent-1|AgentOne> please help"
        agent_set = {"agent-1": "AgentOne"}
        result = room_center.parse_agent_mentions(text, agent_set)

        assert len(result) == 1
        assert result[0]["agent_id"] == "agent-1"
        assert result[0]["agent_name"] == "AgentOne"
        assert result[0]["mention_text"] == "<@agent-1|AgentOne>"

    def test_parses_multiple_mentions(self, room_center):
        text = "<@a1|Alpha> do X and <@a2|Beta> do Y"
        agent_set = {"a1": "Alpha", "a2": "Beta"}
        result = room_center.parse_agent_mentions(text, agent_set)

        assert len(result) == 2
        assert result[0]["agent_id"] == "a1"
        assert result[1]["agent_id"] == "a2"

    def test_ignores_unknown_agent(self, room_center):
        """Agent not in room should be silently ignored."""
        text = "<@unknown|Ghost> do something"
        agent_set = {}
        result = room_center.parse_agent_mentions(text, agent_set)

        assert len(result) == 0

    def test_returns_empty_for_no_mentions(self, room_center):
        text = "Just a normal message with no mentions"
        result = room_center.parse_agent_mentions(text, {"a1": "Alpha"})
        assert result == []

    def test_preserves_position_order(self, room_center):
        text = "<@b|Beta> then <@a|Alpha>"
        agent_set = {"a": "Alpha", "b": "Beta"}
        result = room_center.parse_agent_mentions(text, agent_set)

        assert result[0]["agent_id"] == "b"
        assert result[1]["agent_id"] == "a"


# =============================================================================
# extract_agent_message_content Tests
# =============================================================================


class TestExtractAgentMessageContent:
    """Tests for per-agent message content extraction."""

    def test_extracts_content_for_mentioned_agent(self, room_center):
        text = "<@a1|Alpha> write code. <@a2|Beta> review it."
        mentions = [
            {
                "agent_id": "a1",
                "agent_name": "Alpha",
                "mention_text": "<@a1|Alpha>",
                "position": 0,
            },
            {
                "agent_id": "a2",
                "agent_name": "Beta",
                "mention_text": "<@a2|Beta>",
                "position": 22,
            },
        ]

        result = room_center.extract_agent_message_content(
            text, "a1", "Alpha", mentions
        )
        assert "write code" in result
        assert "<@" not in result

    def test_returns_clean_text_when_agent_not_mentioned(self, room_center):
        """Agent not in mentions gets full text with all mentions stripped."""
        text = "<@a1|Alpha> do something"
        mentions = [
            {
                "agent_id": "a1",
                "agent_name": "Alpha",
                "mention_text": "<@a1|Alpha>",
                "position": 0,
            },
        ]

        result = room_center.extract_agent_message_content(text, "a2", "Beta", mentions)
        assert "<@" not in result
        assert "do something" in result


# =============================================================================
# _validate_send_message_request Tests
# =============================================================================


class TestValidateSendMessageRequest:
    """Tests for send_message input validation."""

    def test_returns_none_for_valid_request(self, room_center):
        req = MagicMock()
        req.room_id = "room-001"
        req.message = MagicMock()
        assert room_center._validate_send_message_request(req) is None

    def test_returns_error_when_room_id_missing(self, room_center):
        req = MagicMock()
        req.room_id = None
        req.message = MagicMock()
        result = room_center._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400

    def test_returns_error_when_message_missing(self, room_center):
        req = MagicMock()
        req.room_id = "room-001"
        req.message = None
        result = room_center._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400

    def test_returns_error_when_message_text_exceeds_max_length(self, room_center):
        """SDR 2.10: Messages exceeding MAX_MESSAGE_LENGTH should be rejected."""
        from models.room import MAX_MESSAGE_LENGTH, MessageContent, RoomUserMessage

        oversized_message = RoomUserMessage(
            room_id="room-001",
            message_id="msg-001",
            message_content=MessageContent(message_text="x" * (MAX_MESSAGE_LENGTH + 1)),
        )
        req = MagicMock()
        req.room_id = "room-001"
        req.message = oversized_message
        result = room_center._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400
        assert "maximum length" in result.error.lower()

    def test_accepts_message_at_max_length(self, room_center):
        """Messages exactly at MAX_MESSAGE_LENGTH should be accepted."""
        from models.room import MAX_MESSAGE_LENGTH, MessageContent, RoomUserMessage

        ok_message = RoomUserMessage(
            room_id="room-001",
            message_id="msg-002",
            message_content=MessageContent(message_text="x" * MAX_MESSAGE_LENGTH),
        )
        req = MagicMock()
        req.room_id = "room-001"
        req.message = ok_message
        result = room_center._validate_send_message_request(req)
        assert result is None
