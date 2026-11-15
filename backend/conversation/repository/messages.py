from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from common.a2a_constants import FAILURE_STATES, TERMINAL_STATES
from common.dto.base import to_stored_conversation_keys
from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow
from conversation.protocols import FinalMessageWriteOutcome
from conversation.repository.message_documents import (
    _safe_parse_agent_message,
    _safe_parse_user_message,
    _strip_file_urls,
)
from conversation.timeline import normalize_timeline_document
from models.room import RoomAgentMessage, RoomUserMessage

logger = get_logger(__name__)
_TERMINAL_TASK_STATES = sorted(state.value for state in TERMINAL_STATES)
_UNRESUMABLE_TASK_STATES = sorted(state.value for state in FAILURE_STATES)


class ConversationMessageStore:
    def __init__(
        self, *, room_agent_messages, room_user_messages, message_repository
    ) -> None:
        self._room_agent_messages = room_agent_messages
        self._room_user_messages = room_user_messages
        self._message_repository = message_repository

    async def get_room_user_message_by_message_id(
        self, message_id: str
    ) -> RoomUserMessage | None:
        try:
            return _safe_parse_user_message(
                await self._message_repository.get_user_message_by_id(message_id)
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_user_message_by_message_id",
                message_id=message_id,
            )
            return None

    async def get_room_agent_message_by_message_id(
        self, message_id: str
    ) -> RoomAgentMessage | None:
        try:
            return _safe_parse_agent_message(
                await self._message_repository.get_agent_message_by_id(message_id)
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_agent_message_by_message_id",
                message_id=message_id,
            )
            return None

    async def add_room_agent_message(
        self, room_agent_message: RoomAgentMessage
    ) -> bool:
        try:
            if room_agent_message.message_id == "":
                room_agent_message.message_id = str(uuid.uuid4())
            await self._message_repository.save_agent_message(
                to_stored_conversation_keys(room_agent_message.model_dump(mode="json"))
            )
            return True
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="add_room_agent_message",
                conversation_id=room_agent_message.conversation_id,
                message_id=room_agent_message.message_id,
                agent_id=room_agent_message.agent_id,
            )
            return False

    async def update_room_user_message_by_message_id(
        self, message_id: str, room_user_message: RoomUserMessage
    ) -> bool:
        try:
            update_data = to_stored_conversation_keys(
                room_user_message.model_dump(exclude_unset=True, mode="json")
            )
            _strip_file_urls(update_data)
            return await self._message_repository.update_user_message(
                message_id,
                update_data,
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_room_user_message_by_message_id",
                conversation_id=room_user_message.conversation_id,
                message_id=message_id,
            )
            return False

    async def reset_last_notified_state(self, message_id: str) -> bool:
        try:
            return await self._room_agent_messages.update_one(
                {"message_id": message_id},
                {"$unset": {"last_notified_state": ""}},
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="reset_last_notified_state",
                message_id=message_id,
            )
            return False

    async def update_agent_message_task_state(
        self,
        message_id: str,
        state: str,
    ) -> bool:
        try:
            allowed_terminal_guard = (
                _UNRESUMABLE_TASK_STATES
                if state in {"input-required", "completed"}
                else _TERMINAL_TASK_STATES
            )
            query: dict[str, Any] = {
                "message_id": message_id,
                "message_content.message_task.status.state": {
                    "$nin": allowed_terminal_guard
                },
            }
            if state == "input-required":
                query["$or"] = [
                    {"terminal_projection_event_id": {"$exists": False}},
                    {"terminal_projection_event_id": None},
                ]

            projected = await self._room_agent_messages.find_one_and_update(
                query,
                {"$set": {"message_content.message_task.status.state": state}},
                return_document=ReturnDocument.AFTER,
            )
            if not isinstance(projected, dict):
                return False
            message_task = projected.get("message_content", {}).get("message_task", {})
            return (message_task.get("status") or {}).get("state") == state
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_agent_message_task_state",
                message_id=message_id,
            )
            return False

    async def persist_hitl_request_id_on_message(
        self,
        message_id: str,
        request_id: str | None,
    ) -> bool:
        try:
            await self._ensure_message_task_metadata(message_id)
            metadata_path = "message_content.message_task.metadata.hitl_request_id"
            update = (
                {"$set": {metadata_path: request_id}}
                if request_id is not None
                else {"$unset": {metadata_path: ""}}
            )
            projected = await self._room_agent_messages.find_one_and_update(
                {"message_id": message_id},
                update,
                return_document=ReturnDocument.AFTER,
            )
            if not isinstance(projected, dict):
                return False
            metadata = (
                projected.get("message_content", {})
                .get("message_task", {})
                .get("metadata")
                or {}
            )
            return metadata.get("hitl_request_id") == request_id
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="persist_hitl_request_id_on_message",
                message_id=message_id,
                hitl_request_id=request_id,
            )
            return False

    async def persist_pending_hitl_on_agent_message(
        self,
        message_id: str,
        *,
        request_id: str,
        prompt: str,
        prompt_type: Any,
        choices: list[str] | None,
        a2a_task_id: str | None,
        a2a_context_id: str | None,
        interaction_id: str,
        question_count: int,
        question_index: int,
        expected_request_id: str | None = None,
        projection_at: datetime | None = None,
    ) -> bool:
        try:
            projection_timestamp = projection_at or utcnow()
            projection_at_iso = projection_timestamp.isoformat()
            metadata: dict[str, Any] = {
                "hitl_request_id": request_id,
                "hitl_prompt": prompt,
                "hitl_prompt_type": getattr(prompt_type, "value", prompt_type),
                "user_answer": None,
            }
            if choices is not None:
                metadata["hitl_choices"] = choices
            optional_metadata = {
                "hitl_a2a_task_id": a2a_task_id,
                "hitl_a2a_context_id": a2a_context_id,
                "hitl_interaction_id": interaction_id,
                "hitl_question_count": question_count,
                "hitl_question_index": question_index,
                "hitl_projection_at": projection_at_iso,
            }
            metadata.update(
                {
                    key: value
                    for key, value in optional_metadata.items()
                    if value is not None
                }
            )
            updates: dict[str, Any] = {
                "message_content.message_task.status.state": "input-required",
                "message_content.message_task.metadata": metadata,
                "task_updated_at": utcnow(),
            }

            query: dict[str, Any] = {
                "message_id": message_id,
                "$or": [
                    {"terminal_projection_event_id": {"$exists": False}},
                    {"terminal_projection_event_id": None},
                ],
                "message_content.message_task.status.state": {
                    "$nin": _UNRESUMABLE_TASK_STATES
                },
            }

            if expected_request_id is not None:
                query["message_content.message_task.metadata.hitl_request_id"] = (
                    expected_request_id
                )
            else:
                query["$and"] = [
                    {
                        "$or": [
                            {
                                "message_content.message_task.status.state": {
                                    "$ne": "completed"
                                }
                            },
                            {
                                "$and": [
                                    {
                                        "message_content.message_task.metadata.hitl_request_id": {
                                            "$ne": request_id
                                        }
                                    },
                                    {
                                        "$or": [
                                            {
                                                "message_content.message_task.metadata.hitl_projection_at": {
                                                    "$exists": False
                                                }
                                            },
                                            {
                                                "message_content.message_task.metadata.hitl_projection_at": None
                                            },
                                            {
                                                "message_content.message_task.metadata.hitl_projection_at": {
                                                    "$lt": projection_at_iso
                                                }
                                            },
                                        ]
                                    },
                                ]
                            },
                        ]
                    },
                    {
                        "$or": [
                            {
                                "message_content.message_task.metadata.hitl_question_index": {
                                    "$exists": False
                                }
                            },
                            {
                                "message_content.message_task.metadata.hitl_question_index": None
                            },
                            {
                                "message_content.message_task.metadata.hitl_question_index": {
                                    "$lte": question_index
                                }
                            },
                        ]
                    },
                ]

            projected = await self._room_agent_messages.find_one_and_update(
                query,
                {"$set": updates},
                return_document=ReturnDocument.AFTER,
            )
            if not projected:
                return False

            message_task = (
                projected.get("message_content", {}).get("message_task", {})
                if isinstance(projected, dict)
                else {}
            )
            metadata = message_task.get("metadata") or {}
            state = (message_task.get("status") or {}).get("state")
            return (
                state == "input-required"
                and metadata.get("hitl_request_id") == request_id
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="persist_pending_hitl_on_agent_message",
                message_id=message_id,
                hitl_request_id=request_id,
                interaction_id=interaction_id,
            )
            return False

    async def _ensure_message_task_metadata(self, message_id: str) -> None:
        await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "message_content.message_task.metadata": None,
            },
            {"$set": {"message_content.message_task.metadata": {}}},
        )

    async def persist_hitl_user_answer(
        self,
        message_id: str,
        user_input: str | None,
    ) -> bool:
        try:
            await self._ensure_message_task_metadata(message_id)
            return await self._room_agent_messages.update_one(
                {"message_id": message_id},
                {
                    "$set": {
                        "message_content.message_task.metadata.user_answer": user_input
                    }
                },
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="persist_hitl_user_answer",
                message_id=message_id,
            )
            return False

    async def persist_hitl_interaction_metadata(
        self,
        message_id: str,
        *,
        interaction_id: str | None,
        question_count: int | None,
        question_index: int | None,
    ) -> bool:
        try:
            await self._ensure_message_task_metadata(message_id)
            if interaction_id is None:
                return False
            updates: dict[str, Any] = {
                "message_content.message_task.metadata.hitl_interaction_id": interaction_id,
                "message_content.message_task.metadata.hitl_question_count": question_count
                or 1,
                "message_content.message_task.metadata.hitl_question_index": question_index
                or 0,
            }
            return await self._room_agent_messages.update_one(
                {"message_id": message_id},
                {"$set": updates},
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="persist_hitl_interaction_metadata",
                message_id=message_id,
                interaction_id=interaction_id,
            )
            return False

    async def persist_final_agent_message(
        self, document: dict[str, Any]
    ) -> FinalMessageWriteOutcome:
        """Insert a terminal agent message once, keyed by ``message_id``.

        A document already stored for the same room is a replay; one stored for
        another room is a conflict.
        """
        message_id = document["message_id"]
        conversation_id = document["room_id"]
        existing = await self._room_agent_messages.find_one({"message_id": message_id})
        if existing is not None:
            return (
                "replayed" if existing.get("room_id") == conversation_id else "conflict"
            )
        try:
            await self._room_agent_messages.insert_one(
                normalize_timeline_document(document)
            )
        except DuplicateKeyError:
            existing = await self._room_agent_messages.find_one(
                {"message_id": message_id}
            )
            if existing is None or existing.get("room_id") != conversation_id:
                return "conflict"
            return "replayed"
        return "accepted"
