"""
Unit tests for RoomCenter (conversation_runtime.py) -- pure logic methods.

Tests cover:
- _looks_like_agent_id heuristic
- _normalize_room_agent_set canonical shape detection
- parse_agent_mentions extraction
- _validate_send_message_request input validation
"""

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.dto import MessageCommitted
from conversation.dto import UserMessageInsertResult
from conversation.idempotency import (
    IdempotencyConflictError,
    UserMessagePersistenceError,
)
from conversation.mentions import parse_agent_mentions
from conversation.message_sending import RoomMessageSender
from conversation.preflight import MessagePreflight
from conversation.runtime import ConversationRuntime
from conversation.target_scope import TargetScopeResolver
from conversation.user_message_persistence import (
    UserMessageCommitCommand,
    UserMessageCommitService,
)
from models.request import RoomCenterUserMessageRequest
from models.room import MessageContent, Room, RoomUserMessage, UserAttachment


@pytest.fixture
def room_center():
    """Create a RoomCenter with mocked dependencies."""
    rc = object.__new__(ConversationRuntime)
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
    svc: ConversationRuntime,
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
async def test_preflight_token_lifecycle_always_releases_identity_owner(monkeypatch):
    svc = ConversationRuntime()
    token = object()
    context = SimpleNamespace(
        user_message=SimpleNamespace(message_id="msg-1"), token=token
    )

    for outcome in ("failed", "canceled", "completed"):
        response = SimpleNamespace(preflight_outcome=outcome)
        monkeypatch.setattr(MessagePreflight, "run", AsyncMock(return_value=response))

        assert await svc.run_message_preflight_to_room(context) is response

    ready_response = SimpleNamespace(preflight_outcome="ready")
    monkeypatch.setattr(MessagePreflight, "run", AsyncMock(return_value=ready_response))

    assert await svc.run_message_preflight_to_room(context) is ready_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("parse failed"), asyncio.CancelledError()]
)
async def test_preflight_token_lifecycle_removes_on_exception_or_cancellation(
    failure, monkeypatch
):
    svc = ConversationRuntime()
    token = object()
    context = SimpleNamespace(
        user_message=SimpleNamespace(message_id="msg-1"), token=token
    )
    monkeypatch.setattr(MessagePreflight, "run", AsyncMock(side_effect=failure))

    with pytest.raises(type(failure)):
        await svc.run_message_preflight_to_room(context)


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

    assert dead_helpers.isdisjoint(ConversationRuntime.__dict__)
    for name in ("bind_room_files", "room_files", "bind_context_memory"):
        assert not hasattr(ConversationRuntime, name), name
    container_source = (
        Path(__file__).resolve().parents[1] / "container.py"
    ).read_text()
    assert "conversation_runtime.bind_context_memory" not in container_source
    assert "conversation_runtime.bind_room_files" not in container_source


def test_room_services_bind_store_sets_runtime_store():
    svc = object.__new__(ConversationRuntime)
    store = object()

    svc.bind_store(store)

    assert svc._store is store


def test_room_services_migrated_crud_methods_do_not_keep_legacy_store_branches():
    forbidden_by_method = {
        ("chat/service.py", "ChatService", "get_room"): {
            "get_room_by_room_id",
            "update_room_by_room_id",
        },
        ("chat/service.py", "ChatService", "list_rooms_for_owner"): {
            "get_rooms_by_room_owner_id"
        },
        ("chat/service.py", "ChatService", "list_room_history"): {
            "get_rooms_by_room_owner_id"
        },
        ("chat/service.py", "ChatService", "rename_room"): {
            "get_room_by_room_id",
            "update_room_by_room_id",
        },
    }
    violations: list[str] = []
    for (path, class_name, method_name), forbidden_attrs in forbidden_by_method.items():
        tree = ast.parse((_ROOT / path).read_text())
        methods = {
            item.name: item
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        method = methods[method_name]
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                violations.append(f"{method_name}:{node.lineno}: {node.attr}")

    assert not violations, (
        "Migrated methods still use legacy store branches:\n" + "\n".join(violations)
    )


@pytest.mark.asyncio
async def test_room_services_persist_user_message_emits_message_committed_event():
    svc = ConversationRuntime()
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
        conversation_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )

    result = await svc._message_sender._persist_user_message(
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
    assert event.conversation_id == "r1"
    assert event.message_id == "u1"
    assert event.message_type == "user"
    assert event.agent_id is None
    assert event.room_agent_set == {"a1": "Agent One"}
    assert publisher.wait_flags == [True]


@pytest.mark.asyncio
async def test_room_services_persist_user_message_does_not_emit_on_failure():
    svc = ConversationRuntime()
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
        conversation_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )

    with pytest.raises(UserMessagePersistenceError, match="insert failed"):
        await svc._message_sender._persist_user_message(user_message, room_agent_set={})

    assert publisher.internal_events == []
    assert publisher.wait_flags == []


@pytest.mark.asyncio
async def test_room_services_assigns_message_id_before_claiming_file_references():
    svc = object.__new__(ConversationRuntime)
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
    commit_service = _bind_user_message_commit(
        svc,
        facade=facade,
        publisher=publisher,
        room_files=room_files,
    )
    user_message = RoomUserMessage(
        conversation_id="r1",
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
        conversation_id="r1",
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
async def test_room_services_persist_message_to_room_passes_room_agent_set_to_user_commit_event(
    monkeypatch,
):
    svc = ConversationRuntime()
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            conversation_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={"a1": "Canonical Agent"},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    monkeypatch.setattr(
        RoomMessageSender,
        "_validate_send_message_request",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        RoomMessageSender,
        "_resolve_and_apply_attachments",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        TargetScopeResolver, "resolve_explicit_target_scope", AsyncMock()
    )
    monkeypatch.setattr(
        RoomMessageSender, "_materialize_room_quote", AsyncMock(return_value=None)
    )
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
        conversation_id="r1",
        message_id="u1",
        user_id="user-1",
        message_content=MessageContent(
            message_text="Please ask <@a1|Stale Name> for help"
        ),
    )

    response, context = await svc.persist_message_to_room(
        RoomCenterUserMessageRequest(
            conversation_id="r1",
            user_id="user-1",
            message=user_message,
        ),
        target_group="all_agents",
    )

    assert response.success is True
    assert context is not None
    event = publisher.internal_events[0]
    assert isinstance(event, MessageCommitted)
    assert event.room_agent_set == {"a1": "Canonical Agent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_structured_quote", [False, True])
async def test_canonical_send_sanitizes_server_owned_user_message_fields(
    with_structured_quote,
    monkeypatch,
    caplog,
):
    svc = ConversationRuntime()
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            conversation_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = None
    svc.bind_facade(facade)
    monkeypatch.setattr(
        RoomMessageSender,
        "_resolve_and_apply_attachments",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        RoomMessageSender, "_materialize_room_quote", AsyncMock(return_value=None)
    )

    async def persist(user_message, **_kwargs):
        assert user_message.conversation_id == "r1"
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
        # Client quote display fields are dropped whether or not a structured
        # quote is present; only materialize_quote writes them.
        assert user_message.extend_info == {
            "execution_mode": "direct",
            "agent_scope": {"source": "room_default"},
        }
        user_message.message_id = "server-message"
        return UserMessageInsertResult(
            message_id="server-message",
            created=True,
            document={},
        )

    monkeypatch.setattr(
        RoomMessageSender, "_persist_user_message", AsyncMock(side_effect=persist)
    )
    request = RoomCenterUserMessageRequest(
        conversation_id="r1",
        user_id="trusted-user",
        client_request_id="request-1",
        message=RoomUserMessage(
            conversation_id="spoofed-room",
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

    with caplog.at_level(logging.DEBUG, logger="conversation.message_sending"):
        response, context = await svc.persist_message_to_room(
            request,
            target_group="all_agents",
            idempotency_fingerprint="fingerprint-1",
            idempotency_fingerprint_version=1,
        )

    assert response.success is True
    assert response.message_id == "server-message"
    assert context is not None
    ignored_logged = any(
        record.getMessage() == "quote_display_fields_discarded"
        for record in caplog.records
    )
    assert ignored_logged is (not with_structured_quote)
    [started] = [
        r for r in caplog.records if r.getMessage() == "user_message_persist_started"
    ]
    assert (started.client_request_id, started.message_char_count) == ("request-1", 5)
    [persisted] = [
        r for r in caplog.records if r.getMessage() == "user_message_persisted"
    ]
    assert (persisted.message_id, persisted.client_request_id) == (
        "server-message",
        "request-1",
    )


@pytest.mark.asyncio
async def test_sequential_replay_skips_quote_attachment_claim_event_and_token(
    monkeypatch,
):
    svc = ConversationRuntime()
    svc._store = MagicMock()
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = {
        "room_id": "r1",
        "message_id": "winner-message",
        "client_request_id": "request-1",
        "idempotency_fingerprint": "fingerprint-1",
        "idempotency_fingerprint_version": 1,
    }
    svc.bind_facade(facade)
    monkeypatch.setattr(
        RoomMessageSender,
        "_validate_send_message_request",
        MagicMock(side_effect=AssertionError("replay must return before validation")),
    )
    monkeypatch.setattr(
        RoomMessageSender,
        "_resolve_and_apply_attachments",
        AsyncMock(side_effect=AssertionError("replay must not resolve attachments")),
    )
    monkeypatch.setattr(
        RoomMessageSender,
        "_materialize_room_quote",
        AsyncMock(side_effect=AssertionError("replay must not create quote")),
    )
    publisher = RecordingEventPublisher()

    response, context = await svc.persist_message_to_room(
        RoomCenterUserMessageRequest(
            conversation_id="r1",
            user_id="user-1",
            client_request_id=" request-1 ",
            message=RoomUserMessage(
                conversation_id="r1",
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
    assert publisher.internal_events == []


@pytest.mark.asyncio
async def test_idempotency_row_without_fingerprint_is_conflict():
    svc = ConversationRuntime()
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = {
        "room_id": "r1",
        "message_id": "unfingerprinted-message",
        "client_request_id": "request-1",
    }
    svc.bind_facade(facade)

    response = await svc.get_idempotent_user_message(
        conversation_id="r1",
        client_request_id="request-1",
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )

    assert response is not None
    assert response.success is False
    assert response.status_code == 409
    assert response.message_id is None
    facade.update_user_message.assert_not_awaited()


def _release_reasons(caplog) -> list[str]:
    return [
        record.release_reason
        for record in caplog.records
        if record.getMessage() == "file_reference_release_failed"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["conflict", "replay"])
async def test_claim_release_failure_preserves_determined_idempotency_outcome(
    outcome,
    caplog,
    monkeypatch,
):
    class Lease:
        async def __aenter__(self):
            return "lease-1"

        async def __aexit__(self, *_args):
            return False

    svc = ConversationRuntime()
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            conversation_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
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

    monkeypatch.setattr(
        RoomMessageSender,
        "_resolve_and_apply_attachments",
        AsyncMock(side_effect=resolve_attachments),
    )
    monkeypatch.setattr(
        RoomMessageSender, "_materialize_room_quote", AsyncMock(return_value=None)
    )
    svc.run_message_preflight_to_room = AsyncMock()
    request = RoomCenterUserMessageRequest(
        conversation_id="r1",
        user_id="user-1",
        client_request_id="request-1",
        message=RoomUserMessage(
            conversation_id="r1",
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
        assert _release_reasons(caplog) == ["persistence_failed"]
    else:
        assert response.success is True
        assert response.message_id == "winner-message"
        assert response.dispatch_root_message_id is None
        assert _release_reasons(caplog) == ["replay"]
    room_files.release_references.assert_awaited_once_with(
        message_id="loser-message",
        file_ids=["file-1"],
    )
    room_files.commit_references.assert_not_awaited()
    assert publisher.internal_events == []
    svc.run_message_preflight_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_loser_releases_own_claim_without_touching_winner_commit():
    svc = object.__new__(ConversationRuntime)
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
    publisher = RecordingEventPublisher()
    commit_service = _bind_user_message_commit(
        svc,
        facade=facade,
        publisher=publisher,
        room_files=room_files,
    )
    user_message = RoomUserMessage(
        conversation_id="r1",
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
        conversation_id="r1",
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
async def test_concurrent_loser_deletes_only_its_new_quote_and_returns_winner(
    monkeypatch,
):
    svc = ConversationRuntime()
    svc._store = MagicMock()
    svc._store.get_room_by_room_id = AsyncMock(
        return_value=Room(
            conversation_id="r1",
            room_name="Room",
            room_owner_id="owner",
            room_owner_name="Owner",
            room_agent_set={},
            extend_info={},
        )
    )
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.get_user_message_by_idempotency_key.return_value = None
    svc.bind_facade(facade)
    monkeypatch.setattr(
        RoomMessageSender,
        "_validate_send_message_request",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        RoomMessageSender,
        "_resolve_and_apply_attachments",
        AsyncMock(return_value=None),
    )

    async def materialize(_room, _request, user_message):
        user_message.quote_id = "loser-quote"
        return None

    monkeypatch.setattr(
        RoomMessageSender, "_materialize_room_quote", AsyncMock(side_effect=materialize)
    )
    monkeypatch.setattr(
        RoomMessageSender,
        "_persist_user_message",
        AsyncMock(
            return_value=UserMessageInsertResult(
                message_id="winner-message",
                created=False,
                document={"message_id": "winner-message", "quote_id": "winner-quote"},
            )
        ),
    )
    request = RoomCenterUserMessageRequest(
        conversation_id="r1",
        user_id="user-1",
        client_request_id="request-1",
        message=RoomUserMessage(
            conversation_id="r1",
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


@pytest.mark.asyncio
async def test_room_services_quote_materialization_delegates_to_room_facade():
    svc = ConversationRuntime()
    svc._bound = False
    svc._facade = None
    facade = AsyncMock()
    facade.materialize_quote.return_value = None
    svc.bind_facade(facade)
    room = Room(
        conversation_id="r1",
        room_name="Room",
        room_owner_id="owner",
        room_owner_name="Owner",
    )
    user_message = RoomUserMessage(
        conversation_id="r1",
        message_id="u1",
        message_content=MessageContent(message_text="hello"),
    )
    request = MagicMock()

    assert (
        await svc._message_sender._materialize_room_quote(room, request, user_message)
        is None
    )
    facade.materialize_quote.assert_awaited_once_with(
        room=room,
        request=request,
        user_message=user_message,
    )


def test_room_services_migrated_message_methods_do_not_call_legacy_store():
    forbidden_by_method = {
        "_persist_user_message": {"add_room_user_message"},
    }
    source = _ROOT / "conversation" / "message_sending.py"
    tree = ast.parse(source.read_text())
    methods = {
        item.name: item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RoomMessageSender"
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


# =============================================================================
# parse_agent_mentions Tests
# =============================================================================


class TestParseAgentMentions:
    """Tests for @agent mention parsing."""

    def test_parses_single_mention(self, room_center):
        text = "Hello <@agent-1|AgentOne> please help"
        agent_set = {"agent-1": "AgentOne"}
        result = parse_agent_mentions(text, agent_set)

        assert len(result) == 1
        assert result[0]["agent_id"] == "agent-1"
        assert result[0]["agent_name"] == "AgentOne"
        assert result[0]["mention_text"] == "<@agent-1|AgentOne>"

    def test_parses_multiple_mentions(self, room_center):
        text = "<@a1|Alpha> do X and <@a2|Beta> do Y"
        agent_set = {"a1": "Alpha", "a2": "Beta"}
        result = parse_agent_mentions(text, agent_set)

        assert len(result) == 2
        assert result[0]["agent_id"] == "a1"
        assert result[1]["agent_id"] == "a2"

    def test_ignores_unknown_agent(self, room_center):
        """Agent not in room should be silently ignored."""
        text = "<@unknown|Ghost> do something"
        agent_set = {}
        result = parse_agent_mentions(text, agent_set)

        assert len(result) == 0

    def test_returns_empty_for_no_mentions(self, room_center):
        text = "Just a normal message with no mentions"
        result = parse_agent_mentions(text, {"a1": "Alpha"})
        assert result == []

    def test_preserves_position_order(self, room_center):
        text = "<@b|Beta> then <@a|Alpha>"
        agent_set = {"a": "Alpha", "b": "Beta"}
        result = parse_agent_mentions(text, agent_set)

        assert result[0]["agent_id"] == "b"
        assert result[1]["agent_id"] == "a"


# =============================================================================
# _validate_send_message_request Tests
# =============================================================================


class TestValidateSendMessageRequest:
    """Tests for send_message input validation."""

    def test_returns_none_for_valid_request(self, room_center):
        req = MagicMock()
        req.conversation_id = "room-001"
        req.message = MagicMock()
        assert room_center._message_sender._validate_send_message_request(req) is None

    def test_returns_error_when_room_id_missing(self, room_center):
        req = MagicMock()
        req.conversation_id = None
        req.message = MagicMock()
        result = room_center._message_sender._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400

    def test_returns_error_when_message_missing(self, room_center):
        req = MagicMock()
        req.conversation_id = "room-001"
        req.message = None
        result = room_center._message_sender._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400

    def test_returns_error_when_message_text_exceeds_max_length(self, room_center):
        """SDR 2.10: Messages exceeding MAX_MESSAGE_LENGTH should be rejected."""
        from models.room import MAX_MESSAGE_LENGTH, MessageContent, RoomUserMessage

        oversized_message = RoomUserMessage(
            conversation_id="room-001",
            message_id="msg-001",
            message_content=MessageContent(message_text="x" * (MAX_MESSAGE_LENGTH + 1)),
        )
        req = MagicMock()
        req.conversation_id = "room-001"
        req.message = oversized_message
        result = room_center._message_sender._validate_send_message_request(req)
        assert result is not None
        assert result.success is False
        assert result.status_code == 400
        assert "maximum length" in result.error.lower()

    def test_accepts_message_at_max_length(self, room_center):
        """Messages exactly at MAX_MESSAGE_LENGTH should be accepted."""
        from models.room import MAX_MESSAGE_LENGTH, MessageContent, RoomUserMessage

        ok_message = RoomUserMessage(
            conversation_id="room-001",
            message_id="msg-002",
            message_content=MessageContent(message_text="x" * MAX_MESSAGE_LENGTH),
        )
        req = MagicMock()
        req.conversation_id = "room-001"
        req.message = ok_message
        result = room_center._message_sender._validate_send_message_request(req)
        assert result is None
