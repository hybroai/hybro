"""The conversation package emits catalog events with ids, never message text."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.dto import CreateRoomRequest, MembershipSeed
from common.observability import get_log_context
from conversation import ConversationFacade
from conversation.deletion import RoomDeletionService
from conversation.idempotency import UserMessagePersistenceError
from conversation.membership_source import RepositoryRoomMembershipSeedSource
from conversation.mentions import canonical_mentions_or_error, parse_agent_mentions
from conversation.message_reads import RoomMessageReader
from conversation.message_sending import RoomMessageSender
from conversation.preflight import MessagePreflight, RoomMessagePreflightContext
from conversation.repository.indexes import (
    ensure_room_timeline_indexes,
    ensure_user_message_indexes,
)
from conversation.repository.message_documents import (
    _safe_parse_agent_message,
    _safe_parse_room,
    _safe_parse_user_message,
)
from conversation.repository.messages import ConversationMessageStore
from conversation.repository.rooms import ConversationRoomStore
from conversation.target_scope import TargetScopeResolver
from conversation.user_message_persistence import UserMessageCommitService
from models.quote import QuoteSourceKind
from models.request import RoomCenterRoomMessageRequest, RoomCenterUserMessageRequest
from models.room import MessageContent, Room, RoomAgentMessage, RoomUserMessage
from models.room_services_models import ResolvedRoutingScope
from tests.test_room_timeline_projection import _hitl_message, _projector
from tests.test_room_user_message_persistence import (
    _command,
    _files,
    _message,
    _publisher,
    _writer,
)

SECRET = "secret-detail-must-not-be-logged"
NOW = datetime(2026, 5, 11, tzinfo=UTC)


class _Failed(RuntimeError):
    pass


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("conversation.") and record.getMessage() == event
    ]


def _one(caplog, event: str) -> logging.LogRecord:
    [record] = _events(caplog, event)
    return record


def _assert_no_secret(caplog) -> None:
    for record in caplog.records:
        assert record.exc_info is None
        assert SECRET not in repr(record.__dict__)


def _assert_failure(record: logging.LogRecord, level: int, error_type="_Failed"):
    assert record.levelno == level
    assert record.error_type == error_type


def _assert_ids(record: logging.LogRecord, ids: dict) -> None:
    for key, value in ids.items():
        assert getattr(record, key) == value, key


def _failing() -> MagicMock:
    target = MagicMock()
    for method in (
        "get_by_id",
        "get_by_owner",
        "get_user_message_by_id",
        "get_user_messages_for_room",
        "get_agent_message_by_id",
        "get_agent_messages_for_room",
        "get_agent_messages_by_related_message_id",
        "save_agent_message",
        "save_user_message",
        "update_user_message",
        "update_agent_message",
        "update_agent_message_if_not_terminal",
        "find_one",
        "find_one_and_update",
        "update_one",
        "delete_one",
        "insert_user_message_idempotently",
    ):
        setattr(target, method, AsyncMock(side_effect=_Failed(SECRET)))
    return target


def _user_message(**overrides) -> RoomUserMessage:
    values = {
        "conversation_id": "room-1",
        "message_id": "msg-1",
        "user_id": "user-1",
        "client_request_id": "request-1",
        "message_content": MessageContent(message_text=SECRET),
    }
    values.update(overrides)
    return RoomUserMessage(**values)


def _agent_message() -> RoomAgentMessage:
    return RoomAgentMessage(
        conversation_id="room-1",
        message_id="msg-1",
        agent_id="agent-1",
        message_content=MessageContent(message_text=SECRET),
    )


# ── message and room stores ──


_STORE_CASES = [
    (
        "get_room_user_message_by_message_id",
        lambda s: s.get_room_user_message_by_message_id("msg-1"),
        {"message_id": "msg-1"},
        None,
    ),
    (
        "get_room_user_messages_by_room_id",
        lambda s: s.get_room_user_messages_by_room_id("room-1"),
        {"conversation_id": "room-1"},
        [],
    ),
    (
        "get_room_agent_message_by_message_id",
        lambda s: s.get_room_agent_message_by_message_id("msg-1"),
        {"message_id": "msg-1"},
        None,
    ),
    (
        "get_room_agent_messages_by_room_id",
        lambda s: s.get_room_agent_messages_by_room_id("room-1"),
        {"conversation_id": "room-1"},
        [],
    ),
    (
        "get_room_agent_messages_by_related_message_id",
        lambda s: s.get_room_agent_messages_by_related_message_id("msg-1"),
        {"related_message_id": "msg-1"},
        [],
    ),
    (
        "add_room_agent_message",
        lambda s: s.add_room_agent_message(_agent_message()),
        {"conversation_id": "room-1", "message_id": "msg-1", "agent_id": "agent-1"},
        False,
    ),
    (
        "add_room_user_message",
        lambda s: s.add_room_user_message(_user_message()),
        {
            "conversation_id": "room-1",
            "message_id": "msg-1",
            "client_request_id": "request-1",
        },
        False,
    ),
    (
        "update_room_user_message_by_message_id",
        lambda s: s.update_room_user_message_by_message_id("msg-1", _user_message()),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "delete_room_agent_message_by_message_id",
        lambda s: s.delete_room_agent_message_by_message_id("msg-1"),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "update_room_agent_message_by_message_id",
        lambda s: s.update_room_agent_message_by_message_id("msg-1", _agent_message()),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "refresh_processing_claim",
        lambda s: s.refresh_processing_claim("msg-1"),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "update_orchestration_projection_if_status",
        lambda s: s.update_orchestration_projection_if_status(
            "msg-1", expected_status="created", status="completed"
        ),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "turn_exists",
        lambda s: s.turn_exists("room-1", "turn-1"),
        {"conversation_id": "room-1", "turn_id": "turn-1"},
        False,
    ),
    (
        "update_room_agent_message_with_new_message_content_by_message_id",
        lambda s: s.update_room_agent_message_with_new_message_content_by_message_id(
            "msg-1", MessageContent(message_text=SECRET)
        ),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "reset_last_notified_state",
        lambda s: s.reset_last_notified_state("msg-1"),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "accumulate_artifact_on_message",
        lambda s: s.accumulate_artifact_on_message(
            "msg-1", {"parts": [{"kind": "text", "text": SECRET}]}
        ),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "update_task_state_on_message_if_not_terminal",
        lambda s: s.update_task_state_on_message_if_not_terminal("msg-1", "working"),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "update_agent_message_task_state",
        lambda s: s.update_agent_message_task_state("msg-1", "working"),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "persist_hitl_request_id_on_message",
        lambda s: s.persist_hitl_request_id_on_message("msg-1", "req-1"),
        {"message_id": "msg-1", "hitl_request_id": "req-1"},
        False,
    ),
    (
        "persist_pending_hitl_on_agent_message",
        lambda s: s.persist_pending_hitl_on_agent_message(
            "msg-1",
            request_id="req-1",
            prompt=SECRET,
            prompt_type="text",
            choices=None,
            a2a_task_id="task-1",
            a2a_context_id="ctx-1",
            interaction_id="int-1",
            question_count=1,
            question_index=0,
        ),
        {"message_id": "msg-1", "hitl_request_id": "req-1", "interaction_id": "int-1"},
        False,
    ),
    (
        "persist_hitl_user_answer",
        lambda s: s.persist_hitl_user_answer("msg-1", SECRET),
        {"message_id": "msg-1"},
        False,
    ),
    (
        "persist_hitl_interaction_metadata",
        lambda s: s.persist_hitl_interaction_metadata(
            "msg-1", interaction_id="int-1", question_count=1, question_index=0
        ),
        {"message_id": "msg-1", "interaction_id": "int-1"},
        False,
    ),
]


@pytest.mark.parametrize(
    ("operation", "call", "ids", "fallback"),
    _STORE_CASES,
    ids=[case[0] for case in _STORE_CASES],
)
async def test_message_store_failure_emits_store_operation_failed(
    caplog, operation, call, ids, fallback
):
    caplog.set_level(logging.DEBUG)
    failing = _failing()
    store = ConversationMessageStore(
        room_agent_messages=failing,
        room_user_messages=failing,
        message_repository=failing,
    )

    assert await call(store) == fallback

    record = _one(caplog, "conversation_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == operation
    _assert_ids(record, ids)
    _assert_no_secret(caplog)


async def test_agent_message_upsert_failure_emits_and_reraises(caplog):
    caplog.set_level(logging.DEBUG)
    failing = _failing()
    store = ConversationMessageStore(
        room_agent_messages=failing,
        room_user_messages=failing,
        message_repository=failing,
    )
    message = _agent_message()
    message.message_created_at = NOW

    with pytest.raises(_Failed):
        await store.upsert_room_agent_message(message)

    record = _one(caplog, "conversation_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == "upsert_room_agent_message"
    _assert_ids(record, {"conversation_id": "room-1", "message_id": "msg-1"})
    _assert_no_secret(caplog)


@pytest.mark.parametrize(
    ("operation", "call", "ids", "fallback"),
    [
        (
            "get_room_by_room_id",
            lambda s: s.get_room_by_room_id("room-1"),
            {"conversation_id": "room-1"},
            None,
        ),
        (
            "get_rooms_by_room_owner_id",
            lambda s: s.get_rooms_by_room_owner_id("owner-1"),
            {"user_id": "owner-1"},
            [],
        ),
    ],
)
async def test_room_store_failure_emits_store_operation_failed(
    caplog, operation, call, ids, fallback
):
    caplog.set_level(logging.DEBUG)

    assert await call(ConversationRoomStore(room_repository=_failing())) == fallback

    record = _one(caplog, "conversation_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == operation
    _assert_ids(record, ids)
    _assert_no_secret(caplog)


async def test_artifact_append_with_no_valid_parts_emits_append_skipped(caplog):
    caplog.set_level(logging.DEBUG)
    collection = MagicMock()
    collection.update_one = AsyncMock()
    store = ConversationMessageStore(
        room_agent_messages=collection,
        room_user_messages=MagicMock(),
        message_repository=MagicMock(),
    )

    assert (
        await store.accumulate_artifact_on_message(
            "msg-1", {"artifactId": "art-1", "parts": [SECRET]}, append=True
        )
        is False
    )

    record = _one(caplog, "artifact_append_skipped")
    assert record.levelno == logging.WARNING
    _assert_ids(record, {"message_id": "msg-1", "reason": "parts_dropped"})
    collection.update_one.assert_not_awaited()
    _assert_no_secret(caplog)


async def test_artifact_append_to_missing_artifact_emits_target_not_found(caplog):
    caplog.set_level(logging.DEBUG)
    collection = MagicMock()
    collection.update_one = AsyncMock(side_effect=[False, True])
    store = ConversationMessageStore(
        room_agent_messages=collection,
        room_user_messages=MagicMock(),
        message_repository=MagicMock(),
    )

    assert await store.accumulate_artifact_on_message(
        "msg-1",
        {"artifactId": "art-1", "parts": [{"kind": "text", "text": SECRET}]},
        append=True,
    )

    record = _one(caplog, "task_artifact_append_target_not_found")
    assert record.levelno == logging.WARNING
    _assert_ids(record, {"artifact_id": "art-1", "message_id": "msg-1"})
    _assert_no_secret(caplog)


# ── document parsing ──


_INVALID_USER_DOC = {
    "room_id": "room-1",
    "message_id": "msg-1",
    "message_content": SECRET,
}
_INVALID_AGENT_DOC = {
    "room_id": "room-1",
    "message_id": "msg-1",
    "message_content": SECRET,
}


def _facade_parse_user(doc):
    from conversation.facade import _safe_parse_user_message as parse

    return parse(doc)


def _facade_parse_agent(doc):
    from conversation.facade import _safe_parse_agent_message as parse

    return parse(doc)


@pytest.mark.parametrize(
    ("parse", "doc", "document_kind", "ids"),
    [
        (
            _safe_parse_room,
            {"room_id": "room-1", "room_name": SECRET},
            "room",
            {"conversation_id": "room-1"},
        ),
        (
            _safe_parse_user_message,
            _INVALID_USER_DOC,
            "room_user_message",
            {"conversation_id": "room-1", "message_id": "msg-1"},
        ),
        (
            _safe_parse_agent_message,
            _INVALID_AGENT_DOC,
            "room_agent_message",
            {"conversation_id": "room-1", "message_id": "msg-1"},
        ),
        (
            _facade_parse_user,
            _INVALID_USER_DOC,
            "room_user_message",
            {"conversation_id": "room-1", "message_id": "msg-1"},
        ),
        (
            _facade_parse_agent,
            _INVALID_AGENT_DOC,
            "room_agent_message",
            {"conversation_id": "room-1", "message_id": "msg-1"},
        ),
    ],
)
def test_invalid_document_emits_document_parse_failed(
    caplog, parse, doc, document_kind, ids
):
    caplog.set_level(logging.DEBUG)

    assert parse(dict(doc)) is None

    record = _one(caplog, "conversation_document_parse_failed")
    _assert_failure(record, logging.WARNING, "ValidationError")
    assert record.document_kind == document_kind
    _assert_ids(record, ids)
    _assert_no_secret(caplog)


# ── facade ──


def _facade(
    *,
    repository=None,
    message_repository=None,
    quote_repository=None,
    epoch_store=None,
    ids=("room-created",),
) -> ConversationFacade:
    id_iter = iter(ids)
    return ConversationFacade(
        repository=repository or _failing(),
        message_repository=message_repository or _failing(),
        agent_registry=AsyncMock(),
        membership_source=MagicMock(),
        quote_repository=quote_repository,
        id_factory=lambda: next(id_iter),
        now=lambda: NOW,
        epoch_store=epoch_store,
    )


@pytest.mark.parametrize(
    ("operation", "call", "ids", "fallback"),
    [
        (
            "update_agent_message",
            lambda f: f.update_agent_message("msg-1", _agent_message()),
            {"message_id": "msg-1"},
            False,
        ),
        (
            "get_user_messages_for_room",
            lambda f: f.get_user_messages_for_room("room-1"),
            {"conversation_id": "room-1"},
            [],
        ),
        (
            "get_agent_messages_for_room",
            lambda f: f.get_agent_messages_for_room("room-1"),
            {"conversation_id": "room-1"},
            [],
        ),
        (
            "get_agent_messages_by_related_message_id",
            lambda f: f.get_agent_messages_by_related_message_id("msg-1"),
            {"related_message_id": "msg-1"},
            [],
        ),
    ],
)
async def test_facade_read_failure_emits_store_operation_failed(
    caplog, operation, call, ids, fallback
):
    caplog.set_level(logging.DEBUG)

    assert await call(_facade()) == fallback

    record = _one(caplog, "conversation_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == operation
    _assert_ids(record, ids)
    _assert_no_secret(caplog)


async def test_facade_persist_failure_emits_store_operation_failed(caplog):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(UserMessagePersistenceError):
        await _facade().persist_user_message(_user_message())

    record = _one(caplog, "conversation_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == "persist_user_message"
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "message_id": "msg-1",
            "client_request_id": "request-1",
        },
    )
    _assert_no_secret(caplog)


async def test_invalid_timeline_message_emits_parse_failed_and_raises(caplog):
    caplog.set_level(logging.DEBUG)
    messages = MagicMock()
    messages.get_timeline_page = AsyncMock(
        return_value=SimpleNamespace(
            entries=[SimpleNamespace(source="user", message=dict(_INVALID_USER_DOC))],
            has_more=False,
            next_position=None,
        )
    )

    with pytest.raises(ValueError, match="Invalid stored room timeline message"):
        await _facade(message_repository=messages).get_timeline_page(
            "room-1", limit=10, before=None
        )

    record = _one(caplog, "timeline_message_parse_failed")
    _assert_failure(record, logging.ERROR, "ValidationError")
    _assert_ids(
        record,
        {"conversation_id": "room-1", "message_id": "msg-1", "message_source": "user"},
    )
    _assert_no_secret(caplog)


async def test_room_epoch_activation_failure_emits_and_compensation_failure(caplog):
    caplog.set_level(logging.DEBUG)
    repository = MagicMock()
    repository.create = AsyncMock()
    repository.delete = AsyncMock(side_effect=_Failed(SECRET))
    epoch_store = SimpleNamespace(activate=AsyncMock(side_effect=_Failed(SECRET)))

    with pytest.raises(ValueError, match="Room epoch activation failed"):
        await _facade(repository=repository, epoch_store=epoch_store).create_room(
            CreateRoomRequest(
                owner_id="owner-1",
                owner_name="Owner",
                room_name=SECRET,
                membership_seed=MembershipSeed(mode="manual"),
            )
        )

    activation = _one(caplog, "room_epoch_activation_failed")
    _assert_failure(activation, logging.ERROR)
    assert activation.conversation_id == "room-created"
    compensation = _one(caplog, "room_creation_compensation_failed")
    _assert_failure(compensation, logging.WARNING)
    assert compensation.conversation_id == "room-created"
    _assert_no_secret(caplog)


async def test_room_activity_update_failure_emits_activity_update_failed(caplog):
    caplog.set_level(logging.DEBUG)
    repository = MagicMock()
    repository.touch_activity = AsyncMock(side_effect=_Failed(SECRET))

    await _facade(repository=repository)._touch_room_activity("room-1")

    record = _one(caplog, "room_activity_update_failed")
    _assert_failure(record, logging.WARNING)
    assert record.conversation_id == "room-1"
    _assert_no_secret(caplog)


_QUOTE_ROOM = Room(
    conversation_id="room-1",
    room_name=SECRET,
    room_owner_id="owner-1",
    room_owner_name="Owner",
)


async def test_quote_snippet_creation_failure_emits_creation_failed(caplog):
    caplog.set_level(logging.DEBUG)
    quotes = SimpleNamespace(insert=AsyncMock(side_effect=_Failed(SECRET)))
    message = _user_message(
        message_id="",
        quote={
            "text": SECRET,
            "source_message_id": "source-1",
            "source_kind": QuoteSourceKind.UNKNOWN,
        },
    )

    response = await _facade(quote_repository=quotes).materialize_quote(
        room=_QUOTE_ROOM,
        request=RoomCenterUserMessageRequest(user_id="user-1"),
        user_message=message,
    )

    assert response is not None and response.status_code == 500
    record = _one(caplog, "quote_snippet_creation_failed")
    _assert_failure(record, logging.ERROR)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "user_id": "user-1",
            "client_request_id": "request-1",
        },
    )
    _assert_no_secret(caplog)


async def test_canonical_quote_lookup_failure_emits_source_lookup_failed(caplog):
    caplog.set_level(logging.DEBUG)
    facade = _facade(quote_repository=SimpleNamespace(insert=AsyncMock()))
    facade.bind_agent_call_reader(
        SimpleNamespace(agent_call_exists=AsyncMock(side_effect=_Failed(SECRET)))
    )
    message = _user_message(
        message_id="",
        quote={
            "text": SECRET,
            "source_message_id": "orchestrator:run-1:inv_weather_0001",
            "source_kind": QuoteSourceKind.AGENT,
        },
    )

    response = await facade.materialize_quote(
        room=_QUOTE_ROOM,
        request=RoomCenterUserMessageRequest(user_id="user-1"),
        user_message=message,
    )

    assert response is not None and response.error == "Invalid quote source"
    record = _one(caplog, "quote_source_lookup_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(record, {"conversation_id": "room-1", "run_id": "run-1"})
    _assert_no_secret(caplog)


# ── reads and sending ──


async def test_timeline_read_failure_emits_timeline_read_failed(caplog):
    caplog.set_level(logging.DEBUG)
    facade = SimpleNamespace(get_timeline_page=AsyncMock(side_effect=_Failed(SECRET)))
    reader = RoomMessageReader(facade=facade, timeline_projector=MagicMock())

    response = await reader.inquiry_room_messages_by_room_id(
        RoomCenterRoomMessageRequest(conversation_id="room-1")
    )

    assert response.status_code == 500
    record = _one(caplog, "room_timeline_read_failed")
    _assert_failure(record, logging.ERROR)
    assert record.conversation_id == "room-1"
    _assert_no_secret(caplog)


async def test_uncommitted_quote_deletion_failure_emits_quote_deletion_failed(caplog):
    caplog.set_level(logging.DEBUG)
    facade = SimpleNamespace(delete_room_quote=AsyncMock(side_effect=_Failed(SECRET)))
    sender = RoomMessageSender(
        store=MagicMock(),
        target_scope=MagicMock(),
        facade=facade,
        user_message_commit=None,
        attachment_metadata_reader=None,
    )

    await sender._delete_uncommitted_quote(_user_message(quote_id="quote-1"))

    record = _one(caplog, "quote_deletion_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "quote_id": "quote-1",
            "client_request_id": "request-1",
        },
    )
    _assert_no_secret(caplog)


# ── deletion ──


def _deletion(
    *, epoch_store=None, memory=None, files=None, room=None, orchestrator=None
):
    return RoomDeletionService(
        room_lifecycle=room
        or SimpleNamespace(cleanup_room_owned_data=AsyncMock(return_value={})),
        memory_cleanup=memory,
        file_lifecycle=files
        or SimpleNamespace(
            delete_for_room=AsyncMock(return_value=0),
            delete_room_state=AsyncMock(return_value=True),
        ),
        epoch_store=epoch_store,
        orchestrator_epoch_cleanup=orchestrator,
    )


async def test_epoch_read_failure_emits_deletion_step_failed(caplog):
    caplog.set_level(logging.DEBUG)
    epoch_store = SimpleNamespace(read=AsyncMock(side_effect=_Failed(SECRET)))

    assert await _deletion(epoch_store=epoch_store)._deactivate_room_epoch(
        "room-1", "deletion-1"
    ) == (False, None)

    record = _one(caplog, "room_deletion_step_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "deletion_id": "deletion-1",
            "deletion_step": "epoch_read",
        },
    )
    _assert_no_secret(caplog)


async def test_epoch_deactivation_failure_emits_deletion_step_failed(caplog):
    caplog.set_level(logging.DEBUG)
    epoch_store = SimpleNamespace(
        read=AsyncMock(return_value=SimpleNamespace(epoch=3)),
        deactivate=AsyncMock(side_effect=_Failed(SECRET)),
    )

    assert await _deletion(epoch_store=epoch_store)._deactivate_room_epoch(
        "room-1", "deletion-1"
    ) == (False, 3)

    record = _one(caplog, "room_deletion_step_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "deletion_id": "deletion-1",
            "deletion_step": "epoch_deactivation",
            "room_epoch": 3,
        },
    )
    _assert_no_secret(caplog)


async def test_epoch_deactivation_conflict_emits_conflict_detected(caplog):
    caplog.set_level(logging.DEBUG)
    epoch_store = SimpleNamespace(
        read=AsyncMock(return_value=SimpleNamespace(epoch=3)),
        deactivate=AsyncMock(return_value=("conflict", None)),
    )

    assert await _deletion(epoch_store=epoch_store)._deactivate_room_epoch(
        "room-1", "deletion-1"
    ) == (False, 3)

    record = _one(caplog, "room_epoch_deactivation_conflict_detected")
    assert record.levelno == logging.WARNING
    _assert_ids(
        record,
        {"conversation_id": "room-1", "deletion_id": "deletion-1", "room_epoch": 3},
    )


@pytest.mark.parametrize(
    ("memory", "reason", "error_type"),
    [
        (None, "cleanup_unbound", None),
        (
            SimpleNamespace(delete_room_memory=AsyncMock(return_value=False)),
            "cleanup_reported_failure",
            None,
        ),
        (
            SimpleNamespace(delete_room_memory=AsyncMock(side_effect=_Failed(SECRET))),
            None,
            "_Failed",
        ),
    ],
)
async def test_context_memory_cleanup_failure_emits_deletion_step_failed(
    caplog, memory, reason, error_type
):
    caplog.set_level(logging.DEBUG)

    assert (
        await _deletion(memory=memory)._cleanup_context_memory_for_room("room-1")
        is False
    )

    record = _one(caplog, "room_deletion_step_failed")
    assert record.levelno == logging.WARNING
    _assert_ids(
        record,
        {"conversation_id": "room-1", "deletion_step": "context_memory_cleanup"},
    )
    assert getattr(record, "reason", None) == reason
    assert getattr(record, "error_type", None) == error_type
    _assert_no_secret(caplog)


@pytest.mark.parametrize(
    ("step", "kwargs"),
    [
        (
            "file_cleanup",
            {
                "files": SimpleNamespace(
                    delete_for_room=AsyncMock(side_effect=_Failed(SECRET)),
                    delete_room_state=AsyncMock(return_value=True),
                )
            },
        ),
        (
            "orchestrator_epoch_cleanup",
            {
                "orchestrator": SimpleNamespace(
                    delete_by_epoch=AsyncMock(side_effect=_Failed(SECRET))
                )
            },
        ),
        (
            "owned_data_cleanup",
            {
                "room": SimpleNamespace(
                    cleanup_room_owned_data=AsyncMock(side_effect=_Failed(SECRET))
                )
            },
        ),
    ],
)
async def test_owned_data_cleanup_failure_emits_deletion_step_failed(
    caplog, step, kwargs
):
    caplog.set_level(logging.DEBUG)

    assert (
        await _deletion(**kwargs)._delete_room_owned_data(
            "room-1", deletion_id="deletion-1", epoch=3
        )
        is False
    )

    record = _one(caplog, "room_deletion_step_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "deletion_id": "deletion-1",
            "deletion_step": step,
        },
    )
    _assert_no_secret(caplog)


# ── preflight ──


def _preflight_context(user_message: RoomUserMessage) -> RoomMessagePreflightContext:
    return RoomMessagePreflightContext(
        request=RoomCenterUserMessageRequest(
            conversation_id="room-1", user_id="user-1", client_request_id="request-1"
        ),
        target_group="room_team",
        mentioned_agent_ids=None,
        user_message=user_message,
        client_request_id="request-1",
        room=_QUOTE_ROOM,
        message_text=SECRET,
        pre_resolved_mentions=None,
        pre_resolved_scope=None,
        pre_resolved_selected_scope=ResolvedRoutingScope(
            selected_agent_set={"agent-1": "Agent One"},
            auto_assign_agents=False,
            agents=[],
        ),
    )


_PREFLIGHT_IDS = {
    "conversation_id": "room-1",
    "message_id": "msg-1",
    "client_request_id": "request-1",
}


async def test_envelope_persist_failure_emits_preflight_failed(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        update_room_user_message_by_message_id=AsyncMock(return_value=False)
    )
    preflight = MessagePreflight(store=store, target_scope=MagicMock())

    response = await preflight.run(_preflight_context(_user_message()))

    assert response.preflight_outcome == "failed"
    resolved = _one(caplog, "message_preflight_scope_resolved")
    assert resolved.levelno == logging.INFO
    _assert_ids(resolved, {**_PREFLIGHT_IDS, "candidate_count": 1})
    envelope = _one(caplog, "orchestration_envelope_persist_failed")
    assert envelope.levelno == logging.WARNING
    _assert_ids(envelope, _PREFLIGHT_IDS)
    failed = _one(caplog, "message_preflight_failed")
    assert failed.levelno == logging.WARNING
    _assert_ids(failed, {**_PREFLIGHT_IDS, "canceled": False})
    assert _events(caplog, "message_preflight_completed") == []
    _assert_no_secret(caplog)


async def test_envelope_persist_success_emits_preflight_completed(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        update_room_user_message_by_message_id=AsyncMock(return_value=True)
    )
    preflight = MessagePreflight(store=store, target_scope=MagicMock())

    response = await preflight.run(_preflight_context(_user_message()))

    assert response.preflight_outcome == "ready"
    prepared = _one(caplog, "orchestration_envelope_prepared")
    assert prepared.levelno == logging.INFO
    _assert_ids(prepared, {**_PREFLIGHT_IDS, "candidate_count": 1})
    completed = _one(caplog, "message_preflight_completed")
    assert completed.levelno == logging.INFO
    _assert_ids(completed, {**_PREFLIGHT_IDS, "preflight_outcome": "ready"})
    assert _events(caplog, "message_preflight_failed") == []
    _assert_no_secret(caplog)


# ── user message persistence ──


def _commit_service(writer, files) -> UserMessageCommitService:
    return UserMessageCommitService(
        writer=writer, files=files, internal_event_publisher=_publisher()
    )


_FILE_IDS = {
    "conversation_id": "room-1",
    "message_id": "message-1",
    "file_count": 1,
}


async def test_file_claim_failure_emits_claim_and_release_failed(caplog):
    caplog.set_level(logging.DEBUG)
    files = _files()
    files.claim_references.side_effect = _Failed(SECRET)
    files.release_references.side_effect = _Failed(SECRET)

    with pytest.raises(UserMessagePersistenceError):
        await _commit_service(_writer(), files).commit(_command(_message()))

    claim = _one(caplog, "file_reference_claim_failed")
    _assert_failure(claim, logging.WARNING)
    _assert_ids(claim, _FILE_IDS)
    release = _one(caplog, "file_reference_release_failed")
    _assert_failure(release, logging.WARNING)
    _assert_ids(release, {**_FILE_IDS, "release_reason": "claim_failed"})
    _assert_no_secret(caplog)


@pytest.mark.parametrize(
    ("created", "persist_fails", "release_reason"),
    [(True, True, "persistence_failed"), (False, False, "replay")],
)
async def test_file_release_failure_emits_release_failed(
    caplog, created, persist_fails, release_reason
):
    caplog.set_level(logging.DEBUG)
    writer = _writer(created=created)
    if persist_fails:
        writer.persist_user_message.side_effect = _Failed(SECRET)
    files = _files()
    files.release_references.side_effect = _Failed(SECRET)

    if persist_fails:
        with pytest.raises(_Failed):
            await _commit_service(writer, files).commit(_command(_message()))
    else:
        await _commit_service(writer, files).commit(_command(_message()))

    record = _one(caplog, "file_reference_release_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(record, {**_FILE_IDS, "release_reason": release_reason})
    _assert_no_secret(caplog)


async def test_file_commit_failure_emits_commit_failed(caplog):
    caplog.set_level(logging.DEBUG)
    files = _files()
    files.commit_references.side_effect = _Failed(SECRET)

    await _commit_service(_writer(), files).commit(_command(_message()))

    record = _one(caplog, "file_reference_commit_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(record, _FILE_IDS)
    _assert_no_secret(caplog)


# ── timeline projection ──


async def test_hitl_reader_failure_emits_projection_verification_failed(caplog):
    caplog.set_level(logging.DEBUG)
    message, task = _hitl_message()
    reader = SimpleNamespace(get_hitl_request=AsyncMock(side_effect=_Failed(SECRET)))

    assert await _projector(hitl=reader).trusted_hitl_projection(message, task) == (
        None,
        None,
    )

    record = _one(caplog, "hitl_projection_verification_failed")
    _assert_failure(record, logging.WARNING)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "message_id": "agent-1",
            "hitl_request_id": "hitl-1",
        },
    )
    _assert_no_secret(caplog)


# ── target scope and mentions ──


def _agent(agent_id: str):
    return SimpleNamespace(
        agent_id=agent_id, agent_card=SimpleNamespace(name=f"Name {agent_id}")
    )


def _resolver(store) -> TargetScopeResolver:
    return TargetScopeResolver(store=store)


async def test_all_agents_selection_failure_emits_resolution_failed(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        get_all_active_agents=AsyncMock(side_effect=_Failed(SECRET))
    )

    result = await _resolver(store).resolve_explicit_target_scope(
        _QUOTE_ROOM, SECRET, "all_agents", sender_user_id="user-1"
    )

    assert result.status_code == 500
    record = _one(caplog, "target_scope_resolution_failed")
    _assert_failure(record, logging.ERROR)
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "scope_source": "all_agents",
            "user_id": "user-1",
        },
    )
    _assert_no_secret(caplog)


async def test_all_agents_selection_emits_scope_resolved(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        get_all_active_agents=AsyncMock(return_value=[_agent("a1")]),
        get_agent_by_agent_id=AsyncMock(return_value=None),
    )
    resolver = _resolver(store)
    resolver.sanitize = AsyncMock(return_value=([_agent("a1")], []))

    await resolver.resolve_explicit_target_scope(
        _QUOTE_ROOM, SECRET, "all_agents", sender_user_id="user-1"
    )

    record = _one(caplog, "target_scope_resolved")
    assert record.levelno == logging.INFO
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "scope_source": "all_agents",
            "candidate_count": 1,
        },
    )


def _room(agent_set: dict) -> Room:
    return Room(
        conversation_id="room-1",
        room_name=SECRET,
        room_owner_id="owner-1",
        room_owner_name="Owner",
        room_agent_set=agent_set,
    )


async def test_room_team_scope_resolved_counts_sanitized_agents(caplog):
    caplog.set_level(logging.DEBUG)
    resolver = _resolver(SimpleNamespace())
    resolver.sanitize = AsyncMock(return_value=([_agent("a1")], ["a2"]))

    await resolver.resolve_explicit_target_scope(
        _room({"a1": "One", "a2": "Two"}), SECRET, "room_team"
    )

    record = _one(caplog, "target_scope_resolved")
    assert record.levelno == logging.INFO
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "scope_source": "room_team",
            "candidate_count": 1,
        },
    )


async def test_room_team_sanitize_failure_emits_no_scope_resolved(caplog):
    caplog.set_level(logging.DEBUG)
    resolver = _resolver(SimpleNamespace())
    resolver.sanitize = AsyncMock(side_effect=_Failed(SECRET))

    with pytest.raises(_Failed):
        await resolver.resolve_explicit_target_scope(
            _room({"a1": "One"}), SECRET, "room_team"
        )

    assert _events(caplog, "target_scope_resolved") == []


@pytest.mark.parametrize(
    ("target_group", "room_agents", "group", "ids"),
    [
        (
            "room_team",
            {},
            None,
            {"scope_source": "room_team", "reason": "empty_scope"},
        ),
        (
            "group-1",
            {},
            None,
            {
                "scope_source": "saved_group",
                "reason": "group_not_found",
                "group_id": "group-1",
            },
        ),
        (
            "group-1",
            {},
            SimpleNamespace(
                type="custom", owner_id="other", agents=["a1"], name=SECRET
            ),
            {
                "scope_source": "saved_group",
                "reason": "group_not_authorized",
                "group_id": "group-1",
                "user_id": "user-1",
            },
        ),
        (
            "group-1",
            {},
            SimpleNamespace(type="custom", owner_id="user-1", agents=[], name=SECRET),
            {
                "scope_source": "saved_group",
                "reason": "empty_scope",
                "group_id": "group-1",
            },
        ),
    ],
)
async def test_explicit_scope_rejection_emits_scope_rejected(
    caplog, target_group, room_agents, group, ids
):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(get_agent_group_by_id=AsyncMock(return_value=group))

    result = await _resolver(store).resolve_explicit_target_scope(
        _room(room_agents), SECRET, target_group, sender_user_id="user-1"
    )

    assert result.success is False
    record = _one(caplog, "target_scope_rejected")
    assert record.levelno == logging.WARNING
    _assert_ids(record, {"conversation_id": "room-1", **ids})
    _assert_no_secret(caplog)


async def test_saved_group_scope_emits_scope_resolved(caplog):
    caplog.set_level(logging.DEBUG)
    group = SimpleNamespace(
        type="custom", owner_id="user-1", agents=["a1"], name=SECRET
    )
    resolver = _resolver(
        SimpleNamespace(get_agent_group_by_id=AsyncMock(return_value=group))
    )
    resolver.sanitize = AsyncMock(return_value=([_agent("a1")], []))

    await resolver.resolve_explicit_target_scope(
        _room({}), SECRET, "group-1", sender_user_id="user-1"
    )

    record = _one(caplog, "target_scope_resolved")
    _assert_ids(
        record,
        {
            "conversation_id": "room-1",
            "scope_source": "saved_group",
            "group_id": "group-1",
            "candidate_count": 1,
        },
    )
    _assert_no_secret(caplog)


_FREE_TEXT_ID = f"{SECRET} text"
_REJECTED_IDS = ["x" * 3000, _FREE_TEXT_ID, "a9"]
_GATED_IDS = ["invalid", "invalid", "a9"]


async def test_selected_candidate_rejection_logs_gated_ids(caplog):
    caplog.set_level(logging.DEBUG)
    resolver = _resolver(SimpleNamespace())
    resolver.sanitize = AsyncMock(return_value=([], list(_REJECTED_IDS)))

    result = await resolver.resolve_selected_candidate_scope(
        list(_REJECTED_IDS), "user-1"
    )

    assert result.status_code == 400
    record = _one(caplog, "target_scope_rejected")
    assert record.levelno == logging.WARNING
    _assert_ids(
        record,
        {
            "scope_source": "selected_candidates",
            "reason": "unauthorized_targets",
            "rejected_agent_count": 3,
            "rejected_agent_ids": _GATED_IDS,
            "user_id": "user-1",
        },
    )
    _assert_no_secret(caplog)
    assert "x" * 200 not in repr(record.__dict__)


def test_canonical_mention_rejection_logs_gated_ids(caplog):
    caplog.set_level(logging.DEBUG)

    result = canonical_mentions_or_error([], list(_REJECTED_IDS))

    assert result.status_code == 400
    record = _one(caplog, "target_scope_rejected")
    assert record.levelno == logging.WARNING
    _assert_ids(
        record,
        {
            "scope_source": "mention",
            "reason": "unauthorized_targets",
            "rejected_agent_count": 3,
            "rejected_agent_ids": _GATED_IDS,
        },
    )
    _assert_no_secret(caplog)
    assert "x" * 200 not in repr(record.__dict__)


def test_rejected_agent_ids_are_capped(caplog):
    caplog.set_level(logging.DEBUG)

    canonical_mentions_or_error([], [f"a{index}" for index in range(15)])

    record = _one(caplog, "target_scope_rejected")
    assert record.rejected_agent_count == 15
    assert record.rejected_agent_ids == [f"a{index}" for index in range(10)]


async def test_missing_saved_group_logs_gated_group_id(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(get_agent_group_by_id=AsyncMock(return_value=None))

    await _resolver(store).resolve_explicit_target_scope(
        _room({}), "hello", _FREE_TEXT_ID, sender_user_id="user-1"
    )

    record = _one(caplog, "target_scope_rejected")
    _assert_ids(record, {"reason": "group_not_found", "group_id": "invalid"})
    _assert_no_secret(caplog)


def test_inline_mentions_outside_room_log_only_a_count(caplog):
    caplog.set_level(logging.DEBUG)
    text = f"<@{_FREE_TEXT_ID}|x> <@ghost|Ghost> " + " ".join(
        f"<@a{index}|A>" for index in range(12)
    )

    assert parse_agent_mentions(text, {}) == []

    record = _one(caplog, "mentions_dropped")
    assert record.levelno == logging.WARNING
    _assert_ids(record, {"reason": "not_room_member", "dropped_mention_count": 14})
    assert not hasattr(record, "dropped_agent_ids")
    assert "ghost" not in repr(record.__dict__)
    _assert_no_secret(caplog)


def test_room_member_mentions_emit_no_mentions_dropped(caplog):
    caplog.set_level(logging.DEBUG)

    assert len(parse_agent_mentions("<@a1|A>", {"a1": "Alpha"})) == 1

    assert _events(caplog, "mentions_dropped") == []


class _ContextCapture(logging.Handler):
    """Snapshot the bound log context when each conversation record is emitted."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.contexts: dict[str, dict] = {}

    def emit(self, record: logging.LogRecord) -> None:
        self.contexts[record.getMessage()] = dict(get_log_context())


@pytest.fixture
def bound_context(caplog):
    caplog.set_level(logging.DEBUG)
    handler = _ContextCapture()
    conversation_logger = logging.getLogger("conversation")
    conversation_logger.addHandler(handler)
    yield handler.contexts
    conversation_logger.removeHandler(handler)


@pytest.mark.parametrize(
    ("extend_info", "mentioned_agent_ids"),
    [
        (None, ["a9"]),
        ({"agent_scope": {"source": "mention", "agent_ids": ["a9"]}}, None),
    ],
)
async def test_send_path_rejection_carries_bound_correlation(
    bound_context, extend_info, mentioned_agent_ids
):
    resolver = _resolver(SimpleNamespace())
    resolver.sanitize = AsyncMock(return_value=([], ["a9"]))
    sender = RoomMessageSender(
        store=SimpleNamespace(get_room_by_room_id=AsyncMock(return_value=_room({}))),
        target_scope=resolver,
        facade=MagicMock(),
        user_message_commit=None,
        attachment_metadata_reader=None,
    )
    sender._resolve_and_apply_attachments = AsyncMock(return_value=None)
    request = RoomCenterUserMessageRequest(
        conversation_id="room-1",
        user_id="user-1",
        client_request_id="request-1",
        extend_info=extend_info,
        message=_user_message(message_content=MessageContent(message_text="hi")),
    )

    response, context = await sender.persist_message_to_room(
        request, mentioned_agent_ids=mentioned_agent_ids
    )

    assert response.status_code == 400 and context is None
    assert bound_context["target_scope_rejected"] == {
        "conversation_id": "room-1",
        "client_request_id": "request-1",
    }
    assert get_log_context() == {}


async def test_preflight_mentions_dropped_carries_bound_correlation(bound_context):
    store = SimpleNamespace(
        update_room_user_message_by_message_id=AsyncMock(return_value=True)
    )
    context = _preflight_context(_user_message())
    context.message_text = "<@ghost|Ghost>"

    await MessagePreflight(store=store, target_scope=MagicMock()).run(context)

    assert bound_context["mentions_dropped"] == {
        "conversation_id": "room-1",
        "client_request_id": "request-1",
        "message_id": "msg-1",
    }
    assert get_log_context() == {}


# ── membership ──


async def test_membership_registry_failure_emits_fallback_selected(caplog):
    caplog.set_level(logging.DEBUG)
    source = RepositoryRoomMembershipSeedSource(
        membership_store=SimpleNamespace(
            get_all_active_agents=AsyncMock(return_value=[])
        ),
        agent_registry=SimpleNamespace(
            list_visible_agents=AsyncMock(side_effect=_Failed(SECRET))
        ),
    )

    assert await source.list_current_agents("user-1") == []

    record = _one(caplog, "membership_agents_fallback_selected")
    _assert_failure(record, logging.DEBUG)
    assert record.user_id == "user-1"
    _assert_no_secret(caplog)


def test_membership_agent_without_card_emits_fields_not_found(caplog):
    caplog.set_level(logging.DEBUG)
    agent = SimpleNamespace(agent_id="a1", agent_card=None, is_public=True)

    RepositoryRoomMembershipSeedSource.agent_info_from_record(agent)

    record = _one(caplog, "membership_agent_fields_not_found")
    assert record.levelno == logging.WARNING
    _assert_ids(record, {"agent_id": "a1", "missing_agent_fields": ["name", "status"]})


# ── indexes ──


def _mongo(aggregate) -> MagicMock:
    collection = MagicMock()
    collection.aggregate = AsyncMock(side_effect=aggregate)
    collection.find_one = AsyncMock(return_value=None)
    mongo = MagicMock()
    mongo.collection.return_value = collection
    return mongo


async def test_user_message_index_readiness_failure_emits_check_failed(caplog):
    caplog.set_level(logging.DEBUG)

    async def aggregate(_pipeline):
        return [{"client_request_id": SECRET}]

    with pytest.raises(RuntimeError):
        await ensure_user_message_indexes(_mongo(aggregate))

    record = _one(caplog, "index_readiness_check_failed")
    assert record.levelno == logging.ERROR
    _assert_ids(
        record,
        {
            "collection_name": "room_user_messages",
            "readiness_check": "user_message_unique_indexes",
            "issue_count": 5,
        },
    )
    _assert_no_secret(caplog)


@pytest.mark.parametrize(
    ("bad_field", "readiness_check"),
    [("$message_id", "timeline_identity"), ("$timeline_sort_us", "timeline_key")],
)
async def test_timeline_index_readiness_failure_emits_check_failed(
    caplog, bad_field, readiness_check
):
    caplog.set_level(logging.DEBUG)

    async def aggregate(pipeline):
        match = repr(pipeline[0])
        if bad_field == "$timeline_sort_us":
            return [{"message_id": SECRET}] if "timeline_sort_us" in match else []
        return [] if "timeline_sort_us" in match else [{"message_id": SECRET}]

    with pytest.raises(RuntimeError):
        await ensure_room_timeline_indexes(_mongo(aggregate))

    record = _one(caplog, "index_readiness_check_failed")
    assert record.levelno == logging.ERROR
    _assert_ids(record, {"readiness_check": readiness_check, "issue_count": 2})
    _assert_no_secret(caplog)


# ── client-supplied values on the send path ──


async def test_persist_started_gates_target_group_and_client_request_id(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        get_room_by_room_id=AsyncMock(return_value=_room({})),
        get_agent_group_by_id=AsyncMock(return_value=None),
    )
    sender = RoomMessageSender(
        store=store,
        target_scope=_resolver(store),
        facade=MagicMock(),
        user_message_commit=None,
        attachment_metadata_reader=None,
    )
    sender._resolve_and_apply_attachments = AsyncMock(return_value=None)
    request = RoomCenterUserMessageRequest(
        conversation_id="room-1",
        user_id="user-1",
        client_request_id=_FREE_TEXT_ID,
        message=_user_message(message_content=MessageContent(message_text="hi")),
    )

    response, _context = await sender.persist_message_to_room(
        request, target_group=_FREE_TEXT_ID
    )

    assert response.status_code == 404
    record = _one(caplog, "user_message_persist_started")
    _assert_ids(record, {"target_group": "invalid", "client_request_id": "invalid"})
    _assert_no_secret(caplog)


async def test_preflight_gates_target_group_and_client_request_id(caplog):
    caplog.set_level(logging.DEBUG)
    store = SimpleNamespace(
        update_room_user_message_by_message_id=AsyncMock(return_value=False)
    )
    context = _preflight_context(_user_message(client_request_id=_FREE_TEXT_ID))
    context.target_group = _FREE_TEXT_ID
    context.client_request_id = _FREE_TEXT_ID
    context.message_text = "hi"

    await MessagePreflight(store=store, target_scope=MagicMock()).run(context)

    resolved = _one(caplog, "message_preflight_scope_resolved")
    _assert_ids(resolved, {"target_group": "invalid", "client_request_id": "invalid"})
    for event in ("orchestration_envelope_persist_failed", "message_preflight_failed"):
        assert _one(caplog, event).client_request_id == "invalid"
    _assert_no_secret(caplog)


async def test_file_reference_events_gate_client_request_id(caplog):
    caplog.set_level(logging.DEBUG)
    files = _files()
    files.commit_references.side_effect = _Failed(SECRET)
    message = _message()
    message.client_request_id = _FREE_TEXT_ID

    await _commit_service(_writer(), files).commit(_command(message))

    record = _one(caplog, "file_reference_commit_failed")
    assert record.client_request_id == "invalid"
    _assert_no_secret(caplog)


async def test_quote_source_lookup_failure_gates_run_id(caplog):
    caplog.set_level(logging.DEBUG)
    facade = _facade(quote_repository=SimpleNamespace(insert=AsyncMock()))
    facade.bind_agent_call_reader(
        SimpleNamespace(agent_call_exists=AsyncMock(side_effect=_Failed("boom")))
    )
    message = _user_message(
        message_id="",
        quote={
            "text": "quoted",
            "source_message_id": f"orchestrator:{_FREE_TEXT_ID}:inv_weather_0001",
            "source_kind": QuoteSourceKind.AGENT,
        },
    )

    await facade.materialize_quote(
        room=_QUOTE_ROOM,
        request=RoomCenterUserMessageRequest(user_id="user-1"),
        user_message=message,
    )

    record = _one(caplog, "quote_source_lookup_failed")
    assert record.run_id == "invalid"
    _assert_no_secret(caplog)
