import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conversation.dto import UserMessageInsertResult
from conversation.idempotency import UserMessagePersistenceError
from conversation.user_message_persistence import (
    UserMessageCommitCommand,
    UserMessageCommitService,
)
from models.room import MessageContent, RoomUserMessage, UserAttachment


class _Lease:
    async def __aenter__(self):
        return "lease-1"

    async def __aexit__(self, *_args):
        return False


def _message(*, with_attachment: bool = True) -> RoomUserMessage:
    attachments = None
    if with_attachment:
        attachments = [
            UserAttachment(
                file_id="file-1",
                mime_type="text/plain",
                file_name="note.txt",
                size_bytes=10,
            )
        ]
    return RoomUserMessage(
        conversation_id="room-1",
        message_id="message-1",
        user_id="user-1",
        message_content=MessageContent(
            message_text="hello",
            attachments=attachments,
        ),
    )


def _writer(*, created: bool = True):
    writer = MagicMock()
    writer.ensure_user_message_id.side_effect = lambda message: message.message_id
    writer.persist_user_message = AsyncMock(
        return_value=UserMessageInsertResult(
            message_id="message-1" if created else "winner-message",
            created=created,
            document={},
        )
    )
    return writer


def _files():
    files = MagicMock()
    files.write_lease.return_value = _Lease()
    files.claim_references = AsyncMock()
    files.commit_references = AsyncMock()
    files.release_references = AsyncMock()
    return files


def _publisher():
    return SimpleNamespace(publish=AsyncMock())


def _command(message: RoomUserMessage) -> UserMessageCommitCommand:
    return UserMessageCommitCommand(
        message=message,
        room_agent_set={"agent-1": "Agent One"},
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )


def test_commit_service_is_independent_from_compatibility_and_broad_owners():
    root = Path(__file__).resolve().parents[1]
    service_source = (root / "conversation" / "user_message_persistence.py").read_text()
    runtime_tree = ast.parse((root / "conversation" / "message_sending.py").read_text())
    room_services = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RoomMessageSender"
    )
    runtime_methods = {
        node.name: node
        for node in room_services.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "conversation.runtime" not in service_source
    assert "conversation.facade" not in service_source
    assert "room_files" not in service_source
    assert "_hold_room_write" not in runtime_methods
    assert "_persist_user_message_with_lease" not in runtime_methods
    delegated = runtime_methods["_persist_user_message"]
    assert not {
        "claim_references",
        "commit_references",
        "release_references",
        "publish",
    } & {node.attr for node in ast.walk(delegated) if isinstance(node, ast.Attribute)}

    container_tree = ast.parse((root / "container.py").read_text())
    service_call = next(
        node
        for node in ast.walk(container_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UserMessageCommitService"
    )
    service_args = {keyword.arg: keyword.value for keyword in service_call.keywords}
    writer_adapter = service_args["writer"]
    files_adapter = service_args["files"]
    assert isinstance(writer_adapter, ast.Call)
    assert isinstance(files_adapter, ast.Call)
    assert {keyword.arg for keyword in writer_adapter.keywords} == {
        "ensure_user_message_id",
        "persist_user_message",
    }
    assert {keyword.arg for keyword in files_adapter.keywords} == {
        "write_lease",
        "claim_references",
        "commit_references",
        "release_references",
    }


@pytest.mark.asyncio
async def test_commit_reference_failure_still_publishes_winner_event(caplog):
    writer = _writer()
    files = _files()
    files.commit_references.side_effect = RuntimeError("commit unavailable")
    publisher = _publisher()
    service = UserMessageCommitService(
        writer=writer,
        files=files,
        internal_event_publisher=publisher,
    )

    result = await service.commit(_command(_message()))

    assert result.created is True
    files.commit_references.assert_awaited_once_with(
        message_id="message-1",
        file_ids=["file-1"],
    )
    publisher.publish.assert_awaited_once()
    assert publisher.publish.await_args.kwargs == {
        "wait_for_handlers": True,
        "fanout": False,
    }
    assert "file_reference_commit_failed" in caplog.text


@pytest.mark.asyncio
async def test_publisher_failure_propagates_after_insert_and_file_commit():
    writer = _writer()
    files = _files()
    publisher = _publisher()
    publisher.publish.side_effect = RuntimeError("publisher unavailable")
    service = UserMessageCommitService(
        writer=writer,
        files=files,
        internal_event_publisher=publisher,
    )

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await service.commit(_command(_message()))

    writer.persist_user_message.assert_awaited_once()
    files.commit_references.assert_awaited_once_with(
        message_id="message-1",
        file_ids=["file-1"],
    )
    files.release_references.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_and_partial_release_failure_preserve_claim_error(caplog):
    writer = _writer()
    files = _files()
    files.claim_references.side_effect = RuntimeError("claim unavailable")
    files.release_references.side_effect = RuntimeError("release unavailable")
    service = UserMessageCommitService(
        writer=writer,
        files=files,
        internal_event_publisher=_publisher(),
    )

    with pytest.raises(
        UserMessagePersistenceError,
        match="Could not claim room file references",
    ):
        await service.commit(_command(_message()))

    files.release_references.assert_awaited_once_with(
        message_id="message-1",
        file_ids=["file-1"],
    )
    writer.persist_user_message.assert_not_awaited()
    assert "file_reference_release_failed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "insert", "publisher"])
async def test_cancellation_propagates_without_exception_compensation(stage):
    writer = _writer()
    files = _files()
    publisher = _publisher()
    if stage == "claim":
        files.claim_references.side_effect = asyncio.CancelledError()
    elif stage == "insert":
        writer.persist_user_message.side_effect = asyncio.CancelledError()
    else:
        publisher.publish.side_effect = asyncio.CancelledError()
    service = UserMessageCommitService(
        writer=writer,
        files=files,
        internal_event_publisher=publisher,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.commit(_command(_message()))

    files.release_references.assert_not_awaited()
    if stage == "claim":
        writer.persist_user_message.assert_not_awaited()
        files.commit_references.assert_not_awaited()
    elif stage == "insert":
        files.claim_references.assert_awaited_once()
        files.commit_references.assert_not_awaited()
    else:
        writer.persist_user_message.assert_awaited_once()
        files.commit_references.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_publisher_fails_after_winner_insert_and_file_commit():
    writer = _writer()
    files = _files()
    service = UserMessageCommitService(
        writer=writer,
        files=files,
        internal_event_publisher=None,
    )

    with pytest.raises(RuntimeError, match="internal event publisher is required"):
        await service.commit(_command(_message()))

    writer.persist_user_message.assert_awaited_once()
    files.commit_references.assert_awaited_once_with(
        message_id="message-1",
        file_ids=["file-1"],
    )
    files.release_references.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_without_attachments_keeps_no_files_compatibility_path():
    writer = _writer()
    publisher = _publisher()
    service = UserMessageCommitService(
        writer=writer,
        files=None,
        internal_event_publisher=publisher,
    )
    message = _message(with_attachment=False)

    result = await service.commit(_command(message))

    assert result.created is True
    writer.ensure_user_message_id.assert_not_called()
    writer.persist_user_message.assert_awaited_once_with(
        message,
        idempotency_fingerprint="fingerprint-1",
        idempotency_fingerprint_version=1,
    )
    publisher.publish.assert_awaited_once()
