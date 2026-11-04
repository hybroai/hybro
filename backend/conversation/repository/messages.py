from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from common.a2a_constants import FAILURE_STATES, TERMINAL_STATES, CommonTaskState
from common.dto.base import to_stored_conversation_keys
from common.observability import events as E
from common.observability import log_id
from common.utils.a2a_helpers import sanitize_artifact_parts
from common.utils.logger import get_logger
from common.utils.time import utcnow
from conversation.protocols import FinalMessageWriteOutcome
from conversation.repository.message_documents import (
    _extract_text_from_artifact_parts,
    _modified_count,
    _mongo_update_succeeded,
    _safe_parse_agent_message,
    _safe_parse_user_message,
    _strip_file_urls,
    _strip_unset_task_tracking_fields,
)
from conversation.timeline import normalize_timeline_document
from models.room import MessageContent, RoomAgentMessage, RoomUserMessage

logger = get_logger(__name__)
ARTIFACT_MATERIALIZATION_WAIT_SECONDS = 20 * 60 + 60
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

    async def get_room_user_messages_by_room_id(
        self,
        conversation_id: str,
    ) -> list[RoomUserMessage]:
        try:
            docs = await self._message_repository.get_user_messages_for_room(
                conversation_id
            )
            messages = [
                _safe_parse_user_message(doc) for doc in docs if doc is not None
            ]
            return [message for message in messages if message is not None]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_user_messages_by_room_id",
                conversation_id=conversation_id,
            )
            return []

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

    async def get_room_agent_messages_by_room_id(
        self,
        conversation_id: str,
    ) -> list[RoomAgentMessage]:
        try:
            docs = await self._message_repository.get_agent_messages_for_room(
                conversation_id
            )
            messages = [
                _safe_parse_agent_message(doc) for doc in docs if doc is not None
            ]
            return [message for message in messages if message is not None]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_agent_messages_by_room_id",
                conversation_id=conversation_id,
            )
            return []

    async def get_room_agent_messages_by_related_message_id(
        self, related_message_id: str
    ) -> list[RoomAgentMessage]:
        try:
            docs = (
                await self._message_repository.get_agent_messages_by_related_message_id(
                    related_message_id
                )
            )
            messages = [
                _safe_parse_agent_message(doc) for doc in docs if doc is not None
            ]
            return [message for message in messages if message is not None]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_room_agent_messages_by_related_message_id",
                related_message_id=related_message_id,
            )
            return []

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

    async def add_room_user_message(self, room_user_message: RoomUserMessage) -> bool:
        try:
            if room_user_message.message_id == "":
                room_user_message.message_id = str(uuid.uuid4())
            doc = to_stored_conversation_keys(
                room_user_message.model_dump(mode="json", exclude={"quote"})
            )
            _strip_file_urls(doc)
            return bool(await self._message_repository.save_user_message(doc))
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="add_room_user_message",
                conversation_id=room_user_message.conversation_id,
                message_id=room_user_message.message_id,
                client_request_id=log_id(room_user_message.client_request_id),
            )
            return False

    async def set_system_task_terminal_state(
        self, message_id: str, target_state: str, *, event_id: str
    ) -> str:
        """CAS and tag a system task with its durable projection winner."""
        state_path = "message_content.message_task.status.state"
        doc = await self._room_agent_messages.find_one({"message_id": message_id})
        if doc is None:
            return "missing"
        current = (
            ((doc.get("message_content") or {}).get("message_task") or {})
            .get("status", {})
            .get("state")
        )
        current_value = getattr(current, "value", current)
        current_winner = doc.get("terminal_projection_event_id")
        if current_winner == event_id and current_value == target_state:
            return "already"
        if current_winner not in {None, event_id}:
            return "conflict"
        terminal_values = {state.value for state in TERMINAL_STATES}
        if current_value in terminal_values and current_value != target_state:
            return "conflict"
        updated = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "$and": [
                    {
                        "$or": [
                            {"terminal_projection_event_id": {"$exists": False}},
                            {"terminal_projection_event_id": None},
                            {"terminal_projection_event_id": event_id},
                        ]
                    },
                    {
                        "$or": [
                            {state_path: target_state},
                            {state_path: {"$nin": sorted(terminal_values)}},
                        ]
                    },
                ],
            },
            {
                "$set": {
                    state_path: target_state,
                    "terminal_projection_event_id": event_id,
                    "task_updated_at": utcnow(),
                }
            },
        )
        if updated:
            return "updated"
        reread = await self._room_agent_messages.find_one({"message_id": message_id})
        if reread is None:
            return "missing"
        reread_state = (
            ((reread.get("message_content") or {}).get("message_task") or {})
            .get("status", {})
            .get("state")
        )
        return (
            "already"
            if reread_state == target_state
            and reread.get("terminal_projection_event_id") == event_id
            else "conflict"
        )

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

    async def upsert_room_agent_message(  # noqa: C901
        self, room_agent_message: RoomAgentMessage
    ) -> None:
        try:
            raw_candidate = to_stored_conversation_keys(
                room_agent_message.model_dump(mode="json")
            )
            for field in ("room_id", "message_id"):
                value = raw_candidate.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Agent-message upsert requires non-empty {field}")
            candidate = normalize_timeline_document(raw_candidate)
            message_id = candidate["message_id"]
            conversation_id = candidate["room_id"]
            existing = await self._room_agent_messages.find_one(
                {"message_id": message_id}
            )
            if existing is None:
                try:
                    await self._room_agent_messages.insert_one(candidate)
                    return
                except DuplicateKeyError:
                    # Another writer won the unique message_id race. Preserve its
                    # immutable identity and use the normal replacement CAS below.
                    existing = await self._room_agent_messages.find_one(
                        {"message_id": message_id}
                    )
                    if existing is None:
                        raise

            if existing.get("room_id") != conversation_id:
                raise ValueError("Agent-message upsert room_id mismatch")
            existing_state = (
                ((existing.get("message_content") or {}).get("message_task") or {}).get(
                    "status"
                )
                or {}
            ).get("state")
            existing_state = getattr(existing_state, "value", existing_state)
            if existing_state in {state.value for state in TERMINAL_STATES}:
                # Full-document retry/upsert is stale once any terminal winner
                # is durable; in particular it must not erase its projection tag.
                return
            immutable_fields = (
                "room_id",
                "message_id",
                "message_created_at",
                "timeline_sort_us",
            )
            if any(field not in existing for field in immutable_fields):
                raise ValueError(
                    "Existing agent message has incomplete timeline identity"
                )
            # Validate the persisted position before carrying it into the full
            # replacement. This prevents a corrupt key from being blessed.
            normalize_timeline_document(existing)
            immutable = {field: existing[field] for field in immutable_fields}
            candidate.update(immutable)
            terminal_states = sorted(state.value for state in TERMINAL_STATES)
            replace_filter = {
                **immutable,
                "message_content.message_task.status.state": {"$nin": terminal_states},
                "$or": [
                    {"terminal_projection_event_id": {"$exists": False}},
                    {"terminal_projection_event_id": None},
                ],
            }
            replaced = await self._room_agent_messages.replace_one(
                replace_filter,
                candidate,
                upsert=False,
            )
            if not replaced:
                durable = await self._room_agent_messages.find_one(
                    {"message_id": message_id}
                )
                durable_state = (
                    (
                        ((durable or {}).get("message_content") or {}).get(
                            "message_task"
                        )
                        or {}
                    ).get("status")
                    or {}
                ).get("state")
                durable_state = getattr(durable_state, "value", durable_state)
                durable_winner = (durable or {}).get("terminal_projection_event_id")
                if durable_state in set(terminal_states) or durable_winner is not None:
                    return
                raise RuntimeError("Agent-message upsert lost replacement CAS")
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="upsert_room_agent_message",
                conversation_id=room_agent_message.conversation_id,
                message_id=room_agent_message.message_id,
            )
            raise

    async def delete_room_agent_message_by_message_id(self, message_id: str) -> bool:
        try:
            return await self._room_agent_messages.delete_one(
                {"message_id": message_id}
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="delete_room_agent_message_by_message_id",
                message_id=message_id,
            )
            return False

    async def update_room_agent_message_by_message_id(
        self, message_id: str, room_agent_message: RoomAgentMessage
    ) -> bool:
        try:
            update_data = _strip_unset_task_tracking_fields(
                to_stored_conversation_keys(
                    room_agent_message.model_dump(exclude_unset=True, mode="json")
                )
            )
            return await self._message_repository.update_agent_message(
                message_id,
                update_data,
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_room_agent_message_by_message_id",
                message_id=message_id,
            )
            return False

    async def refresh_processing_claim(self, message_id: str) -> bool:
        try:
            return await self._room_user_messages.update_one(
                {"message_id": message_id, "processing_claimed_at": {"$ne": None}},
                {"$set": {"processing_claimed_at": utcnow()}},
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="refresh_processing_claim",
                message_id=message_id,
            )
            return False

    async def update_orchestration_projection_if_status(
        self,
        message_id: str,
        *,
        expected_status: str,
        status: str,
        clear_processing_claim: bool = False,
    ) -> bool:
        updates: dict[str, Any] = {
            "extend_info.orchestration_status": status,
        }
        if clear_processing_claim:
            updates["processing_claimed_at"] = None
        try:
            return await self._room_user_messages.update_one(
                {
                    "message_id": message_id,
                    "extend_info.orchestration_status": expected_status,
                },
                {"$set": updates},
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_orchestration_projection_if_status",
                message_id=message_id,
            )
            return False

    async def turn_exists(self, conversation_id: str, turn_id: str) -> bool:
        try:
            user = await self._room_user_messages.find_one(
                {"room_id": conversation_id, "turn_id": turn_id}
            )
            if user is not None:
                return True
            agent = await self._room_agent_messages.find_one(
                {"room_id": conversation_id, "turn_id": turn_id}
            )
            return agent is not None
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="turn_exists",
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            return False

    async def cancel_descendants(self, message_id: str) -> int:
        terminal_statuses = sorted(state.value for state in TERMINAL_STATES)
        to_visit = [message_id]
        visited = {message_id}
        all_descendant_ids: list[str] = []

        while to_visit:
            children = await self._room_agent_messages.find(
                {"related_message_id": {"$in": to_visit}},
                projection={"message_id": 1},
            )
            child_ids = [
                str(child["message_id"])
                for child in children
                if child.get("message_id") is not None
                and str(child["message_id"]) not in visited
            ]
            visited.update(child_ids)
            all_descendant_ids.extend(child_ids)
            to_visit = child_ids

        if not all_descendant_ids:
            return 0

        result = await self._room_agent_messages.update_many(
            {
                "message_id": {"$in": all_descendant_ids},
                "message_content.message_task": {"$ne": None},
                "message_content.message_task.status.state": {
                    "$nin": terminal_statuses
                },
            },
            {
                "$set": {
                    "message_content.message_task.status.state": (
                        CommonTaskState.CANCELED.value
                    ),
                }
            },
        )
        return _modified_count(result)

    async def project_descendant_terminal_state(
        self,
        message_id: str,
        *,
        event_id: str,
        target_state: str,
        exclude_message_ids: list[str] | None = None,
    ) -> list[str]:
        """Terminalize descendants and durably tag the winner-owned child set."""
        terminal_statuses = sorted(state.value for state in TERMINAL_STATES)
        excluded = set(exclude_message_ids or [])
        to_visit = [message_id]
        visited = {message_id}
        all_descendant_ids: list[str] = []
        while to_visit:
            children = await self._room_agent_messages.find(
                {"related_message_id": {"$in": to_visit}},
                projection={"message_id": 1},
            )
            child_ids = [
                str(child["message_id"])
                for child in children
                if child.get("message_id") is not None
                and str(child["message_id"]) not in visited
            ]
            visited.update(child_ids)
            all_descendant_ids.extend(child_ids)
            to_visit = child_ids
        if all_descendant_ids:
            await self._room_agent_messages.update_many(
                {
                    "message_id": {
                        "$in": all_descendant_ids,
                        "$nin": sorted(excluded),
                    },
                    "message_content.message_task": {"$ne": None},
                    "message_content.message_task.status.state": {
                        "$nin": terminal_statuses
                    },
                },
                {
                    "$set": {
                        "message_content.message_task.status.state": target_state,
                        "terminal_projection_event_id": event_id,
                    }
                },
            )
        projected = await self._room_agent_messages.find(
            {
                "message_id": {
                    "$in": all_descendant_ids,
                    "$nin": sorted(excluded),
                },
                "terminal_projection_event_id": event_id,
            },
            projection={"message_id": 1},
        )
        return sorted(
            str(child["message_id"])
            for child in projected
            if child.get("message_id") is not None
        )

    async def cancel_agent_messages_by_ids(self, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        terminal_statuses = sorted(state.value for state in TERMINAL_STATES)
        result = await self._room_agent_messages.update_many(
            {
                "message_id": {"$in": list(message_ids)},
                "message_content.message_task": {"$ne": None},
                "message_content.message_task.status.state": {
                    "$nin": terminal_statuses
                },
            },
            {
                "$set": {
                    "message_content.message_task.status.state": (
                        CommonTaskState.CANCELED.value
                    ),
                }
            },
        )
        return _modified_count(result)

    async def update_room_agent_message_with_new_message_content_by_message_id(
        self, message_id: str, message_content: MessageContent
    ) -> bool:
        try:
            return await self._message_repository.update_agent_message(
                message_id,
                {"message_content": message_content.model_dump(mode="json")},
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_room_agent_message_with_new_message_content_by_message_id",
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

    async def claim_terminal_finalization(
        self,
        message_id: str,
        state: str,
        *,
        message_text: str | None,
        artifacts: list[dict] | None,
        task_metadata: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        """Claim a recoverable terminal finalizer without making the task terminal."""
        from common.utils.a2a_helpers import prepare_terminal_agent_content

        message_text, artifacts, _ = prepare_terminal_agent_content(
            message_text=message_text,
            artifacts=artifacts,
        )
        token = await self.begin_terminal_finalization(message_id, state)
        if token is None:
            return None, message_text
        if not await self.set_terminal_finalization_content(
            message_id,
            token,
            message_text=message_text,
            artifacts=artifacts,
            task_metadata=task_metadata,
        ):
            return None, message_text
        return token, message_text

    async def begin_terminal_finalization(
        self,
        message_id: str,
        state: str,
        *,
        recovery_source: str = "message",
        recovery_id: str | None = None,
    ) -> str | None:
        """Fence artifact journal writes before terminal projection is assembled."""
        token = uuid.uuid4().hex
        now = utcnow()
        stale_before = now - timedelta(minutes=5)
        terminal_values = sorted(state.value for state in TERMINAL_STATES)
        updates: dict[str, Any] = {
            "terminal_finalization.state": "finalizing",
            "terminal_finalization.token": token,
            "terminal_finalization.target_state": state,
            "terminal_finalization.recovery_source": recovery_source,
            "terminal_finalization.recovery_id": recovery_id,
            "terminal_finalization.started_at": now,
            "terminal_finalization.heartbeat_at": now,
            "task_updated_at": now,
        }
        query = {
            "message_id": message_id,
            "message_content.message_task.status.state": {"$nin": terminal_values},
            "$and": [
                {
                    "$or": [
                        {"terminal_finalization.state": {"$ne": "finalizing"}},
                        {"terminal_finalization.heartbeat_at": {"$lt": stale_before}},
                        {
                            "$and": [
                                {
                                    "terminal_finalization.heartbeat_at": {
                                        "$exists": False
                                    }
                                },
                                {
                                    "terminal_finalization.started_at": {
                                        "$lt": stale_before
                                    }
                                },
                            ]
                        },
                    ]
                },
                {
                    "$or": [
                        {"artifact_materialization.state": {"$ne": "running"}},
                        {"artifact_materialization.expires_at": {"$lte": now}},
                    ]
                },
            ],
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ARTIFACT_MATERIALIZATION_WAIT_SECONDS
        while loop.time() < deadline:
            claimed_at = utcnow()
            updates.update(
                {
                    "terminal_finalization.started_at": claimed_at,
                    "terminal_finalization.heartbeat_at": claimed_at,
                    "task_updated_at": claimed_at,
                }
            )
            query["$and"][1]["$or"][1]["artifact_materialization.expires_at"] = {
                "$lte": claimed_at
            }
            result = await self._room_agent_messages.update_one(
                query,
                {"$set": updates},
            )
            if _mongo_update_succeeded(result):
                return token
            current = await self._room_agent_messages.find_one(
                {"message_id": message_id},
                {
                    "terminal_finalization.state": 1,
                    "artifact_materialization.state": 1,
                    "artifact_materialization.expires_at": 1,
                },
            )
            if current is None:
                return None
            finalization = current.get("terminal_finalization") or {}
            if finalization.get("state") == "finalizing":
                return None
            artifact_lock = current.get("artifact_materialization") or {}
            if artifact_lock.get("state") != "running":
                return None
            await asyncio.sleep(0.05)
        return None

    async def terminal_finalization_matches(
        self,
        message_id: str,
        state: str,
        *,
        recovery_source: str,
        recovery_id: str | None,
    ) -> bool:
        """Return whether this exact durable recovery already reached terminal."""
        if recovery_id is None:
            return False
        current = await self._room_agent_messages.find_one(
            {
                "message_id": message_id,
                "message_content.message_task.status.state": state,
                "terminal_finalization.state": "terminal",
                "terminal_finalization.target_state": state,
                "terminal_finalization.recovery_source": recovery_source,
                "terminal_finalization.recovery_id": recovery_id,
            }
        )
        return current is not None

    async def set_terminal_finalization_content(
        self,
        message_id: str,
        token: str,
        *,
        message_text: str | None,
        artifacts: list[dict] | None,
        task_metadata: dict[str, Any] | None = None,
    ) -> bool:
        from common.utils.a2a_helpers import prepare_terminal_agent_content

        message_text, artifacts, _ = prepare_terminal_agent_content(
            message_text=message_text,
            artifacts=artifacts,
        )
        updates: dict[str, Any] = {
            "terminal_finalization.heartbeat_at": utcnow(),
            "task_updated_at": utcnow(),
        }
        if message_text is not None:
            updates["message_content.message_text"] = message_text
        if artifacts is not None:
            updates["message_content.message_task.artifacts"] = artifacts
        if task_metadata is not None:
            updates["message_content.message_task.metadata"] = task_metadata
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "terminal_finalization.state": "finalizing",
                "terminal_finalization.token": token,
            },
            {"$set": updates},
        )
        return _mongo_update_succeeded(result)

    async def heartbeat_terminal_finalization(
        self, message_id: str, token: str
    ) -> bool:
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "terminal_finalization.state": "finalizing",
                "terminal_finalization.token": token,
            },
            {
                "$set": {
                    "terminal_finalization.heartbeat_at": utcnow(),
                    "task_updated_at": utcnow(),
                }
            },
        )
        return _mongo_update_succeeded(result)

    async def claim_terminal_finalization_step(
        self, message_id: str, token: str, step: str
    ) -> bool:
        now = utcnow()
        stale_before = now - timedelta(minutes=5)
        prefix = f"terminal_finalization.steps.{step}"
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "terminal_finalization.state": "finalizing",
                "terminal_finalization.token": token,
                f"{prefix}.completed": {"$ne": True},
                "$or": [
                    {f"{prefix}.state": {"$ne": "running"}},
                    {f"{prefix}.started_at": {"$lt": stale_before}},
                ],
            },
            {
                "$set": {
                    f"{prefix}.state": "running",
                    f"{prefix}.started_at": now,
                    "terminal_finalization.heartbeat_at": now,
                }
            },
        )
        return _mongo_update_succeeded(result)

    async def complete_terminal_finalization_step(
        self, message_id: str, token: str, step: str
    ) -> bool:
        prefix = f"terminal_finalization.steps.{step}"
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "terminal_finalization.state": "finalizing",
                "terminal_finalization.token": token,
                f"{prefix}.state": "running",
            },
            {
                "$set": {
                    f"{prefix}.state": "completed",
                    f"{prefix}.completed": True,
                    f"{prefix}.completed_at": utcnow(),
                    "terminal_finalization.heartbeat_at": utcnow(),
                }
            },
        )
        return _mongo_update_succeeded(result)

    async def claim_artifact_materialization(
        self, message_id: str, owner: str
    ) -> str | None:
        token = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ARTIFACT_MATERIALIZATION_WAIT_SECONDS
        terminal_values = sorted(state.value for state in TERMINAL_STATES)
        while loop.time() < deadline:
            now = utcnow()
            result = await self._room_agent_messages.update_one(
                {
                    "message_id": message_id,
                    "message_content.message_task.status.state": {
                        "$nin": terminal_values
                    },
                    "terminal_finalization.state": {"$ne": "finalizing"},
                    "$or": [
                        {"artifact_materialization.state": {"$ne": "running"}},
                        {"artifact_materialization.expires_at": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "artifact_materialization": {
                            "state": "running",
                            "token": token,
                            "owner": owner,
                            "started_at": now,
                            "expires_at": now + timedelta(minutes=2),
                        }
                    }
                },
            )
            if _mongo_update_succeeded(result):
                return token
            current = await self._room_agent_messages.find_one(
                {"message_id": message_id},
                {
                    "terminal_finalization.state": 1,
                    "artifact_materialization.state": 1,
                },
            )
            if current is None:
                return None
            if (current.get("terminal_finalization") or {}).get(
                "state"
            ) == "finalizing":
                return None
            await asyncio.sleep(0.05)
        return None

    async def heartbeat_artifact_materialization(
        self, message_id: str, token: str
    ) -> bool:
        now = utcnow()
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "artifact_materialization.state": "running",
                "artifact_materialization.token": token,
            },
            {
                "$set": {
                    "artifact_materialization.expires_at": now + timedelta(minutes=2)
                }
            },
        )
        return _mongo_update_succeeded(result)

    async def release_artifact_materialization(
        self, message_id: str, token: str
    ) -> None:
        await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "artifact_materialization.token": token,
            },
            {"$unset": {"artifact_materialization": ""}},
        )

    async def complete_terminal_finalization(
        self, message_id: str, token: str, state: str
    ) -> bool:
        result = await self._room_agent_messages.update_one(
            {
                "message_id": message_id,
                "terminal_finalization.state": "finalizing",
                "terminal_finalization.token": token,
                "terminal_projection_event_id": {"$exists": False},
                "message_content.message_task.status.state": {
                    "$nin": sorted(value.value for value in TERMINAL_STATES)
                },
            },
            {
                "$set": {
                    "message_content.message_task.status.state": state,
                    "terminal_finalization.state": "terminal",
                    "terminal_finalization.completed_at": utcnow(),
                    "task_updated_at": utcnow(),
                }
            },
        )
        return _mongo_update_succeeded(result)

    async def accumulate_artifact_on_message(
        self,
        message_id: str,
        artifact: dict,
        append: bool = False,
        update_key: str | None = None,
    ) -> bool:
        """Accumulate A2A artifact chunks with atomic DAL collection updates."""
        try:
            raw_parts = artifact.get("parts", [])
            clean_parts = sanitize_artifact_parts(raw_parts)
            artifact = {**artifact, "parts": clean_parts}

            if append and not clean_parts:
                logger.emit(
                    E.ARTIFACT_APPEND_SKIPPED,
                    message_id=message_id,
                    reason="parts_dropped",
                )
                return False

            artifact_id = artifact.get("artifactId") or artifact.get("artifact_id")
            explicit_index = artifact.get("index")
            if not artifact_id and explicit_index is not None:
                artifact_id = f"index:{explicit_index}"
                artifact = {**artifact, "artifactId": artifact_id}
            artifact_text = _extract_text_from_artifact_parts(clean_parts)

            base_filter = {
                "message_id": message_id,
                "terminal_finalization.state": {"$ne": "finalizing"},
                "message_content.message_task.status.state": {
                    "$nin": sorted(state.value for state in TERMINAL_STATES)
                },
            }
            if update_key:
                base_filter["artifact_update_keys"] = {"$ne": update_key}
            if not artifact_id:
                update: dict[str, Any] = {
                    "$push": {"message_content.message_task.artifacts": artifact},
                    "$set": {
                        "message_content.message_task.status.state": "working",
                        "task_updated_at": utcnow(),
                    },
                }
                if artifact_text:
                    update["$set"]["message_content.message_text"] = artifact_text
                if update_key:
                    update["$addToSet"] = {"artifact_update_keys": update_key}
                result = await self._room_agent_messages.update_one(base_filter, update)
                return _mongo_update_succeeded(result)

            if append:
                return await self._append_parts_to_artifact(
                    message_id,
                    artifact_id,
                    artifact,
                    artifact_text,
                    base_filter,
                    update_key,
                )
            return await self._replace_or_insert_artifact(
                artifact_id,
                artifact,
                artifact_text,
                base_filter,
                update_key,
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="accumulate_artifact_on_message",
                message_id=message_id,
            )
            return False

    async def is_artifact_update_recorded(
        self, message_id: str, update_key: str
    ) -> bool:
        return (
            await self._room_agent_messages.find_one(
                {
                    "message_id": message_id,
                    "artifact_update_keys": update_key,
                },
                {"_id": 1},
            )
            is not None
        )

    @staticmethod
    def _artifact_id_match_expr(artifact_id: str) -> dict[str, Any]:
        return {
            "$or": [
                {"$eq": ["$$art.artifactId", artifact_id]},
                {"$eq": ["$$art.artifact_id", artifact_id]},
            ]
        }

    @classmethod
    def _map_replace_artifact_expr(
        cls, artifact_id: str, artifact: dict
    ) -> dict[str, Any]:
        return {
            "$map": {
                "input": {"$ifNull": ["$message_content.message_task.artifacts", []]},
                "as": "art",
                "in": {
                    "$cond": {
                        "if": cls._artifact_id_match_expr(artifact_id),
                        "then": artifact,
                        "else": "$$art",
                    }
                },
            }
        }

    @classmethod
    def _map_append_parts_expr(
        cls,
        artifact_id: str,
        new_parts: list[dict],
        artifact_metadata: dict | None,
    ) -> dict[str, Any]:
        return {
            "$map": {
                "input": {"$ifNull": ["$message_content.message_task.artifacts", []]},
                "as": "art",
                "in": {
                    "$cond": {
                        "if": cls._artifact_id_match_expr(artifact_id),
                        "then": {
                            "$mergeObjects": [
                                "$$art",
                                {
                                    "parts": {
                                        "$concatArrays": [
                                            {"$ifNull": ["$$art.parts", []]},
                                            new_parts,
                                        ]
                                    },
                                    **(
                                        {"metadata": artifact_metadata}
                                        if artifact_metadata is not None
                                        else {}
                                    ),
                                },
                            ]
                        },
                        "else": "$$art",
                    }
                },
            }
        }

    async def _append_parts_to_artifact(
        self,
        message_id: str,
        artifact_id: str,
        artifact: dict,
        artifact_text: str,
        base_filter: dict,
        update_key: str | None,
    ) -> bool:
        new_parts = artifact.get("parts", [])
        if not new_parts:
            return False

        filter_with_artifact = {
            **base_filter,
            "message_content.message_task.artifacts": {
                "$elemMatch": {
                    "$or": [
                        {"artifactId": artifact_id},
                        {"artifact_id": artifact_id},
                    ]
                }
            },
        }
        set_fields: dict[str, Any] = {
            "message_content.message_task.artifacts": self._map_append_parts_expr(
                artifact_id,
                new_parts,
                artifact.get("metadata"),
            ),
            "message_content.message_task.status.state": "working",
            "task_updated_at": utcnow(),
        }
        if artifact_text:
            set_fields["message_content.message_text"] = {
                "$concat": [
                    {"$ifNull": ["$message_content.message_text", ""]},
                    artifact_text,
                ]
            }
        if update_key:
            set_fields["artifact_update_keys"] = {
                "$setUnion": [
                    {"$ifNull": ["$artifact_update_keys", []]},
                    [update_key],
                ]
            }
        result = await self._room_agent_messages.update_one(
            filter_with_artifact,
            [{"$set": set_fields}],
        )
        if _mongo_update_succeeded(result):
            return True

        logger.emit(
            E.TASK_ARTIFACT_APPEND_TARGET_NOT_FOUND,
            artifact_id=artifact_id,
            message_id=message_id,
        )
        insert_update: dict[str, Any] = {
            "$push": {"message_content.message_task.artifacts": artifact},
            "$set": {
                "message_content.message_task.status.state": "working",
                "task_updated_at": utcnow(),
            },
        }
        if artifact_text:
            insert_update["$set"]["message_content.message_text"] = artifact_text
        if update_key:
            insert_update["$addToSet"] = {"artifact_update_keys": update_key}
        result = await self._room_agent_messages.update_one(base_filter, insert_update)
        return _mongo_update_succeeded(result)

    async def _replace_or_insert_artifact(
        self,
        artifact_id: str,
        artifact: dict,
        artifact_text: str,
        base_filter: dict,
        update_key: str | None,
    ) -> bool:
        filter_with_artifact = {
            **base_filter,
            "message_content.message_task.artifacts": {
                "$elemMatch": {
                    "$or": [
                        {"artifactId": artifact_id},
                        {"artifact_id": artifact_id},
                    ]
                }
            },
        }
        set_fields: dict[str, Any] = {
            "message_content.message_task.artifacts": self._map_replace_artifact_expr(
                artifact_id,
                artifact,
            ),
            "message_content.message_task.status.state": "working",
            "task_updated_at": utcnow(),
        }
        if artifact_text:
            set_fields["message_content.message_text"] = artifact_text
        if update_key:
            set_fields["artifact_update_keys"] = {
                "$setUnion": [
                    {"$ifNull": ["$artifact_update_keys", []]},
                    [update_key],
                ]
            }
        result = await self._room_agent_messages.update_one(
            filter_with_artifact,
            [{"$set": set_fields}],
        )
        if _mongo_update_succeeded(result):
            return True

        insert_update: dict[str, Any] = {
            "$push": {"message_content.message_task.artifacts": artifact},
            "$set": {
                "message_content.message_task.status.state": "working",
                "task_updated_at": utcnow(),
            },
        }
        if artifact_text:
            insert_update["$set"]["message_content.message_text"] = artifact_text
        if update_key:
            insert_update["$addToSet"] = {"artifact_update_keys": update_key}
        result = await self._room_agent_messages.update_one(base_filter, insert_update)
        return _mongo_update_succeeded(result)

    async def update_task_state_on_message_if_not_terminal(
        self,
        message_id: str,
        state: str,
    ) -> bool:
        try:
            terminal_values = sorted(state.value for state in TERMINAL_STATES)
            return await self._message_repository.update_agent_message_if_not_terminal(
                message_id,
                {
                    "message_content.message_task.status.state": state,
                    "task_updated_at": utcnow(),
                },
                terminal_values,
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_task_state_on_message_if_not_terminal",
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
