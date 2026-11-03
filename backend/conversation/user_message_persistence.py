from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from common.eventing import InternalEventPublisher
from common.message_commit_events import publish_message_committed
from common.observability import events as E
from common.observability import log_id
from common.utils.logger import get_logger
from conversation.dto import UserMessageInsertResult
from conversation.idempotency import UserMessagePersistenceError
from models.room import RoomUserMessage

logger = get_logger(__name__)


class UserMessageWriter(Protocol):
    def ensure_user_message_id(self, message: RoomUserMessage) -> str: ...

    async def persist_user_message(
        self,
        message: RoomUserMessage,
        *,
        idempotency_fingerprint: str | None,
        idempotency_fingerprint_version: int | None,
    ) -> UserMessageInsertResult: ...


class UserMessageFileLifecycle(Protocol):
    def write_lease(
        self,
        conversation_id: str,
        owner: str,
    ) -> AbstractAsyncContextManager[str]: ...

    async def claim_references(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        message_id: str,
        file_ids: list[str],
    ) -> None: ...

    async def commit_references(
        self,
        *,
        message_id: str,
        file_ids: list[str],
    ) -> None: ...

    async def release_references(
        self,
        *,
        message_id: str,
        file_ids: list[str],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class UserMessageCommitCommand:
    message: RoomUserMessage
    room_agent_set: Mapping[str, str]
    idempotency_fingerprint: str | None = None
    idempotency_fingerprint_version: int | None = None


class UserMessageCommitService:
    """Coordinate durable user-message effects around the canonical writer."""

    def __init__(
        self,
        *,
        writer: UserMessageWriter,
        files: UserMessageFileLifecycle | None,
        internal_event_publisher: InternalEventPublisher | None,
    ) -> None:
        self._writer = writer
        self._files = files
        self._internal_event_publisher = internal_event_publisher

    @asynccontextmanager
    async def _hold_room_write(self, conversation_id: str):
        if self._files is None:
            yield None
            return
        async with self._files.write_lease(conversation_id, "user-message") as lease_id:
            yield lease_id

    async def commit(
        self,
        command: UserMessageCommitCommand,
    ) -> UserMessageInsertResult:
        message = command.message
        async with self._hold_room_write(message.conversation_id):
            return await self._commit_with_lease(command)

    async def _commit_with_lease(
        self,
        command: UserMessageCommitCommand,
    ) -> UserMessageInsertResult:
        message = command.message
        file_ids = self._attachment_file_ids(message)
        if file_ids:
            await self._claim_file_references(message, file_ids)

        try:
            persistence = await self._writer.persist_user_message(
                message,
                idempotency_fingerprint=command.idempotency_fingerprint,
                idempotency_fingerprint_version=(
                    command.idempotency_fingerprint_version
                ),
            )
        except Exception:
            await self._release_file_references(
                message,
                file_ids,
                release_reason="persistence_failed",
            )
            raise

        if not persistence.created:
            await self._release_file_references(
                message,
                file_ids,
                release_reason="replay",
            )
            return persistence

        await self._commit_file_references(message, file_ids)
        await self._publish_message_committed(command)
        return persistence

    @staticmethod
    def _attachment_file_ids(message: RoomUserMessage) -> list[str]:
        attachments = (
            message.message_content.attachments if message.message_content else None
        )
        return [attachment.file_id for attachment in attachments or []]

    async def _claim_file_references(
        self,
        message: RoomUserMessage,
        file_ids: list[str],
    ) -> None:
        self._writer.ensure_user_message_id(message)
        files = self._require_files()
        try:
            await files.claim_references(
                conversation_id=message.conversation_id,
                owner_id=message.user_id or "",
                message_id=message.message_id,
                file_ids=file_ids,
            )
        except Exception as exc:
            logger.emit(
                E.FILE_REFERENCE_CLAIM_FAILED,
                exc=exc,
                **_file_log_fields(message, file_ids),
            )
            await self._release_file_references(
                message,
                file_ids,
                release_reason="claim_failed",
            )
            raise UserMessagePersistenceError(
                "Could not claim room file references"
            ) from exc

    async def _release_file_references(
        self,
        message: RoomUserMessage,
        file_ids: list[str],
        *,
        release_reason: str,
    ) -> None:
        if not file_ids:
            return
        try:
            await self._require_files().release_references(
                message_id=message.message_id,
                file_ids=file_ids,
            )
        except Exception as exc:
            # Recovery releases the references later; the original outcome wins.
            logger.emit(
                E.FILE_REFERENCE_RELEASE_FAILED,
                exc=exc,
                release_reason=release_reason,
                **_file_log_fields(message, file_ids),
            )

    async def _commit_file_references(
        self,
        message: RoomUserMessage,
        file_ids: list[str],
    ) -> None:
        if not file_ids:
            return
        try:
            await self._require_files().commit_references(
                message_id=message.message_id,
                file_ids=file_ids,
            )
        except Exception as exc:
            # The references stay pending until durable recovery commits them.
            logger.emit(
                E.FILE_REFERENCE_COMMIT_FAILED,
                exc=exc,
                **_file_log_fields(message, file_ids),
            )

    async def _publish_message_committed(
        self,
        command: UserMessageCommitCommand,
    ) -> None:
        if self._internal_event_publisher is None:
            raise RuntimeError(
                "UserMessageCommitService internal event publisher is required"
            )
        # Wait for local projection before preflight can process agent messages.
        await publish_message_committed(
            self._internal_event_publisher,
            conversation_id=command.message.conversation_id,
            message_id=command.message.message_id,
            message_type="user",
            room_agent_set=dict(command.room_agent_set),
            wait_for_handlers=True,
        )

    def _require_files(self) -> UserMessageFileLifecycle:
        if self._files is None:
            raise RuntimeError("UserMessageCommitService room files are required")
        return self._files


def _file_log_fields(message: RoomUserMessage, file_ids: list[str]) -> dict:
    return {
        "conversation_id": message.conversation_id,
        "message_id": message.message_id,
        "client_request_id": log_id(message.client_request_id),
        "file_count": len(file_ids),
    }


__all__ = [
    "UserMessageCommitCommand",
    "UserMessageCommitService",
    "UserMessageFileLifecycle",
    "UserMessageWriter",
]
