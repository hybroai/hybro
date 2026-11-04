"""Room document reads and writes for the conversation module."""

from __future__ import annotations

from common.observability import events as E
from common.utils.logger import get_logger
from conversation.repository.message_documents import _safe_parse_room
from models.room import Room

logger = get_logger(__name__)


class ConversationRoomStore:
    def __init__(self, *, room_repository) -> None:
        self._room_repository = room_repository

    async def get_room_by_room_id(self, conversation_id: str) -> Room | None:
        try:
            return _safe_parse_room(
                await self._room_repository.get_by_id(conversation_id)
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_by_room_id",
                conversation_id=conversation_id,
            )
            return None

    async def get_rooms_by_room_owner_id(self, room_owner_id: str) -> list[Room]:
        try:
            docs = await self._room_repository.get_by_owner(room_owner_id)
            rooms = [_safe_parse_room(doc) for doc in docs if doc is not None]
            return [room for room in rooms if room is not None]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_rooms_by_room_owner_id",
                user_id=room_owner_id,
            )
            return []
