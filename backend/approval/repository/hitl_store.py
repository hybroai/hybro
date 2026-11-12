from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.room_state import delete_room_rows
from common.utils.time import utcnow
from conversation.protocols import ConversationMessageWriter

logger = get_logger(__name__)

_PENDING_HITL_DISPLAY_INDEX = "uq_pending_hitl_display_message"
_PENDING_HITL_CONTINUATION_INDEX = "uq_pending_hitl_continuation_message"
_COLLECTION_NAME = "hitl_requests"


def _default_index_name(keys) -> str:
    """MongoDB's generated name for an unnamed index, e.g. ``room_id_1_status_1``."""
    return "_".join(f"{field}_{direction}" for field, direction in keys)


def _actionable_deadline_clause(now=None) -> dict[str, Any]:
    current = now or utcnow()
    return {
        "$or": [
            {"expires_at": {"$gt": current}},
            {"expires_at": None},
            {"expires_at": {"$exists": False}},
        ]
    }


def _pending_actionable_query(**identity: Any) -> dict[str, Any]:
    return {
        "$and": [
            {**identity, "status": "pending"},
            _actionable_deadline_clause(),
        ]
    }


def _user_visible_interaction_query(**identity: Any) -> dict[str, Any]:
    """Rows needed to restore every nonterminal user-visible interaction."""
    return {
        "$or": [
            _pending_actionable_query(**identity),
            {
                **identity,
                "status": "answer_recorded",
            },
        ]
    }


def _duplicate_key_mentions_index(error: DuplicateKeyError, index_name: str) -> bool:
    details = getattr(error, "details", None)
    return index_name in str(error) or (
        details is not None and index_name in repr(details)
    )


class HITLRequestStore:
    def __init__(
        self, *, hitl_requests, message_writer: ConversationMessageWriter
    ) -> None:
        self._hitl_requests = hitl_requests
        self._messages = message_writer

    async def delete_room_state(self, conversation_id: str) -> bool:
        return await delete_room_rows((self._hitl_requests,), conversation_id)

    async def get_pending_hitl_requests_for_message(
        self, user_message_id: str
    ) -> list[dict]:
        try:
            return await self._hitl_requests.find(
                _pending_actionable_query(user_message_id=user_message_id),
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_pending_hitl_requests_for_message",
                user_message_id=user_message_id,
            )
            return []

    async def get_pending_hitl_requests_for_message_strict(
        self,
        user_message_id: str,
    ) -> list[dict]:
        return await self._hitl_requests.find(
            {
                "$or": [
                    _pending_actionable_query(user_message_id=user_message_id),
                    {
                        "user_message_id": user_message_id,
                        "status": {"$in": ["canceled", "expired"]},
                        "cancellation_reconciled": {"$ne": True},
                    },
                ]
            },
        )

    async def create_hitl_request(self, request_data: dict) -> bool:
        try:
            await self._hitl_requests.insert_one(dict(request_data))
            return True
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="create_hitl_request",
                hitl_request_id=request_data.get("request_id"),
                conversation_id=request_data.get("room_id"),
            )
            return False

    async def get_hitl_request(self, request_id: str) -> dict | None:
        try:
            return await self._hitl_requests.find_one({"request_id": request_id})
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_hitl_request",
                hitl_request_id=request_id,
            )
            return None

    async def update_hitl_request(self, request_id: str, **updates) -> bool:
        try:
            return await self._hitl_requests.update_one(
                {"request_id": request_id},
                {"$set": dict(updates)},
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_hitl_request",
                hitl_request_id=request_id,
            )
            return False

    async def cas_update_hitl_request(
        self,
        request_id: str,
        expected_status: str,
        **updates,
    ) -> bool:
        try:
            return await self._hitl_requests.update_one(
                {"request_id": request_id, "status": expected_status},
                {"$set": dict(updates)},
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="cas_update_hitl_request",
                hitl_request_id=request_id,
            )
            return False

    async def cas_update_hitl_request_strict(
        self,
        request_id: str,
        expected_status: str,
        **updates,
    ) -> bool:
        return await self._hitl_requests.update_one(
            {"request_id": request_id, "status": expected_status},
            {"$set": dict(updates)},
        )

    async def fenced_update_hitl_request(
        self,
        request_id: str,
        claim_id: str,
        updates: dict | None = None,
        **kw_updates,
    ) -> bool:
        merged = {**(updates or {}), **kw_updates}
        try:
            return await self._hitl_requests.update_one(
                {"request_id": request_id, "claim_id": claim_id},
                {"$set": merged},
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="fenced_update_hitl_request",
                hitl_request_id=request_id,
            )
            return False

    async def claim_hitl_request(self, request_id: str, **updates) -> dict | None:
        try:
            return await self._hitl_requests.find_one_and_update(
                _pending_actionable_query(request_id=request_id),
                {"$set": dict(updates)},
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="claim_hitl_request",
                hitl_request_id=request_id,
            )
            return None

    async def get_pending_hitl_requests(self, conversation_id: str) -> list[dict]:
        try:
            return await self._hitl_requests.find(
                _user_visible_interaction_query(room_id=conversation_id),
                sort=[("created_at", 1), ("request_id", 1)],
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_pending_hitl_requests",
                conversation_id=conversation_id,
            )
            return []

    async def get_pending_hitl_requests_strict(
        self, conversation_id: str
    ) -> list[dict]:
        """Authoritative room read; failures must propagate to the API client."""
        return await self._hitl_requests.find(
            _user_visible_interaction_query(room_id=conversation_id),
            sort=[("created_at", 1), ("request_id", 1)],
        )

    async def count_hitl_requests_for_message(
        self,
        continuation_message_id: str,
    ) -> int:
        try:
            return await self._hitl_requests.count(
                {
                    "continuation_message_id": continuation_message_id,
                    "status": {"$ne": "canceled"},
                    "question_index": 0,
                }
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="count_hitl_requests_for_message",
                continuation_message_id=continuation_message_id,
            )
            return 0

    async def update_agent_message_task_state(
        self,
        message_id: str,
        state: str,
    ) -> bool:
        return await self._messages.update_agent_message_task_state(message_id, state)

    async def persist_hitl_request_id_on_message(
        self,
        message_id: str,
        request_id: str | None,
    ) -> bool:
        return await self._messages.persist_hitl_request_id_on_message(
            message_id, request_id
        )

    async def find_pending_hitl_request_for_agent_message(
        self,
        *,
        conversation_id: str,
        display_message_id: str | None,
        continuation_message_id: str | None,
        agent_id: str | None,
        a2a_task_id: str | None,
        a2a_context_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            return await self._find_pending_hitl_request_for_agent_message(
                conversation_id=conversation_id,
                display_message_id=display_message_id,
                continuation_message_id=continuation_message_id,
            )
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="find_pending_hitl_request_for_agent_message",
                conversation_id=conversation_id,
                display_message_id=display_message_id,
                continuation_message_id=continuation_message_id,
                agent_id=agent_id,
                a2a_task_id=a2a_task_id,
                a2a_context_id=a2a_context_id,
            )
            return None

    async def _find_pending_hitl_request_for_agent_message(
        self,
        *,
        conversation_id: str,
        display_message_id: str | None,
        continuation_message_id: str | None,
    ) -> dict[str, Any] | None:
        display_existing = None
        continuation_existing = None
        if display_message_id:
            display_existing = await self._hitl_requests.find_one(
                self._pending_agent_hitl_identity_query(
                    conversation_id=conversation_id,
                    identity_clause={"display_message_id": display_message_id},
                )
            )
        if continuation_message_id:
            continuation_existing = await self._hitl_requests.find_one(
                self._pending_agent_hitl_identity_query(
                    conversation_id=conversation_id,
                    identity_clause={
                        "continuation_message_id": continuation_message_id
                    },
                )
            )
        if display_existing and continuation_existing:
            display_request_id = display_existing.get("request_id")
            continuation_request_id = continuation_existing.get("request_id")
            if display_request_id != continuation_request_id:
                logger.emit(
                    E.PENDING_HITL_REQUEST_CONFLICT_DETECTED,
                    lookup="pending_lookup",
                    conversation_id=conversation_id,
                    display_message_id=display_message_id,
                    continuation_message_id=continuation_message_id,
                    display_request_id=display_request_id,
                    continuation_request_id=continuation_request_id,
                )
                return None
            return display_existing
        return display_existing or continuation_existing

    def _pending_agent_hitl_identity_query(
        self,
        *,
        conversation_id: str,
        identity_clause: dict[str, str],
    ) -> dict[str, Any]:
        return _pending_actionable_query(
            room_id=conversation_id,
            public_source="agent",
            **identity_clause,
        )

    async def create_or_reuse_pending_hitl_request(
        self,
        request_data: dict[str, Any],
    ) -> tuple[dict[str, Any], bool] | None:
        doc = dict(request_data)
        try:
            await self._hitl_requests.insert_one(doc)
            return doc, True
        except DuplicateKeyError as error:
            existing = await self._read_pending_hitl_after_duplicate(doc, error)
            if existing is not None:
                return existing, False
            logger.emit(
                E.PENDING_HITL_DUPLICATE_READBACK_FAILED,
                conversation_id=doc.get("room_id"),
                hitl_request_id=doc.get("request_id"),
                display_message_id=doc.get("display_message_id"),
                continuation_message_id=doc.get("continuation_message_id"),
            )
            return None
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="create_or_reuse_pending_hitl_request",
                hitl_request_id=doc.get("request_id"),
                conversation_id=doc.get("room_id"),
            )
            return None

    async def _find_pending_hitl_duplicate_identity(
        self,
        doc: dict[str, Any],
        *,
        display_message_id: str | None,
        continuation_message_id: str | None,
    ) -> dict[str, Any] | None:
        return await self.find_pending_hitl_request_for_agent_message(
            conversation_id=doc.get("room_id"),
            display_message_id=display_message_id,
            continuation_message_id=continuation_message_id,
            agent_id=doc.get("agent_id"),
            a2a_task_id=doc.get("a2a_task_id"),
            a2a_context_id=doc.get("a2a_context_id"),
        )

    async def _read_pending_hitl_after_duplicate(
        self,
        doc: dict[str, Any],
        error: DuplicateKeyError,
    ) -> dict[str, Any] | None:
        display_message_id = doc.get("display_message_id")
        continuation_message_id = doc.get("continuation_message_id")

        if display_message_id and continuation_message_id:
            first_identity = (
                "continuation"
                if _duplicate_key_mentions_index(
                    error, _PENDING_HITL_CONTINUATION_INDEX
                )
                else "display"
            )
            return await self._read_pending_hitl_duplicate_pair(
                doc,
                display_message_id=display_message_id,
                continuation_message_id=continuation_message_id,
                first_identity=first_identity,
            )

        if _duplicate_key_mentions_index(error, _PENDING_HITL_DISPLAY_INDEX):
            return await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=display_message_id,
                continuation_message_id=None,
            )
        if _duplicate_key_mentions_index(error, _PENDING_HITL_CONTINUATION_INDEX):
            return await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=None,
                continuation_message_id=continuation_message_id,
            )

        return await self._find_pending_hitl_duplicate_identity(
            doc,
            display_message_id=display_message_id,
            continuation_message_id=continuation_message_id,
        )

    async def _read_pending_hitl_duplicate_pair(
        self,
        doc: dict[str, Any],
        *,
        display_message_id: str,
        continuation_message_id: str,
        first_identity: str,
    ) -> dict[str, Any] | None:
        display_existing = None
        continuation_existing = None
        if first_identity == "continuation":
            continuation_existing = await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=None,
                continuation_message_id=continuation_message_id,
            )
            display_existing = await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=display_message_id,
                continuation_message_id=None,
            )
        else:
            display_existing = await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=display_message_id,
                continuation_message_id=None,
            )
            continuation_existing = await self._find_pending_hitl_duplicate_identity(
                doc,
                display_message_id=None,
                continuation_message_id=continuation_message_id,
            )

        if display_existing and continuation_existing:
            display_request_id = display_existing.get("request_id")
            continuation_request_id = continuation_existing.get("request_id")
            if display_request_id != continuation_request_id:
                logger.emit(
                    E.PENDING_HITL_REQUEST_CONFLICT_DETECTED,
                    lookup="duplicate_readback",
                    conversation_id=doc.get("room_id"),
                    display_message_id=display_message_id,
                    continuation_message_id=continuation_message_id,
                    display_request_id=display_request_id,
                    continuation_request_id=continuation_request_id,
                )
                return None
            return display_existing
        return display_existing or continuation_existing

    async def claim_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> dict[str, Any] | None:
        return await self._hitl_requests.find_one_and_update(
            {
                "request_id": request_id,
                "status": "pending",
                "open_projection_completed_at": {"$exists": False},
                "$or": [
                    {"open_projection_claim_id": {"$exists": False}},
                    {"open_projection_claim_id": None},
                    {
                        "open_projection_claimed_at": {
                            "$lte": utcnow() - timedelta(minutes=2)
                        }
                    },
                ],
            },
            {
                "$set": {
                    "open_projection_claim_id": claim_id,
                    "open_projection_claimed_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def complete_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> bool:
        return await self._hitl_requests.update_one(
            {"request_id": request_id, "open_projection_claim_id": claim_id},
            {
                "$set": {"open_projection_completed_at": utcnow()},
                "$unset": {
                    "open_projection_claim_id": "",
                    "open_projection_claimed_at": "",
                },
            },
        )

    async def release_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> bool:
        return await self._hitl_requests.update_one(
            {"request_id": request_id, "open_projection_claim_id": claim_id},
            {
                "$unset": {
                    "open_projection_claim_id": "",
                    "open_projection_claimed_at": "",
                }
            },
        )

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
        return await self._messages.persist_pending_hitl_on_agent_message(
            message_id,
            request_id=request_id,
            prompt=prompt,
            prompt_type=prompt_type,
            choices=choices,
            a2a_task_id=a2a_task_id,
            a2a_context_id=a2a_context_id,
            interaction_id=interaction_id,
            question_count=question_count,
            question_index=question_index,
            expected_request_id=expected_request_id,
            projection_at=projection_at,
        )

    async def persist_hitl_user_answer(
        self,
        message_id: str,
        user_input: str | None,
    ) -> bool:
        return await self._messages.persist_hitl_user_answer(message_id, user_input)

    async def persist_hitl_interaction_metadata(
        self,
        message_id: str,
        *,
        interaction_id: str | None,
        question_count: int | None,
        question_index: int | None,
    ) -> bool:
        return await self._messages.persist_hitl_interaction_metadata(
            message_id,
            interaction_id=interaction_id,
            question_count=question_count,
            question_index=question_index,
        )

    async def ensure_indexes(self) -> None:
        noncritical_indexes = [
            ((("request_id", 1),), {"unique": True}),
            ((("room_id", 1), ("status", 1)), {}),
            ((("expires_at", 1), ("status", 1)), {}),
            ((("user_message_id", 1), ("status", 1)), {}),
            (
                (("interaction_id", 1), ("question_index", 1)),
                {
                    "unique": True,
                    "name": "uq_hitl_interaction_question",
                    "partialFilterExpression": {
                        "interaction_id": {"$type": "string"},
                    },
                },
            ),
            ((("continuation_message_id", 1),), {}),
        ]
        for keys, kwargs in noncritical_indexes:
            try:
                await self._hitl_requests.create_index(list(keys), **kwargs)
            except Exception as exc:
                is_conflict = (
                    isinstance(exc, OperationFailure)
                    and getattr(exc, "code", None) in (85, 86)
                ) or "already exists with different options" in str(exc)
                index_name = kwargs.get("name")
                drop_fn = getattr(self._hitl_requests, "drop_index", None)
                if is_conflict and index_name and drop_fn:
                    try:
                        logger.emit(
                            E.INDEX_CONFLICT_DETECTED,
                            collection_name=_COLLECTION_NAME,
                            index_name=index_name,
                            critical=False,
                        )
                        await drop_fn(index_name)
                        await self._hitl_requests.create_index(list(keys), **kwargs)
                        continue
                    except Exception as recreate_exc:
                        logger.emit(
                            E.INDEX_RECREATION_FAILED,
                            level=logging.WARNING,
                            exc=recreate_exc,
                            collection_name=_COLLECTION_NAME,
                            index_name=index_name,
                            critical=False,
                        )
                logger.emit(
                    E.INDEX_CREATION_FAILED,
                    exc=exc,
                    collection_name=_COLLECTION_NAME,
                    index_name=index_name or _default_index_name(keys),
                    critical=False,
                )

        critical_indexes = [
            (
                [("room_id", 1), ("display_message_id", 1)],
                {
                    "unique": True,
                    "name": _PENDING_HITL_DISPLAY_INDEX,
                    "partialFilterExpression": {
                        "status": "pending",
                        "public_source": "agent",
                        "display_message_id": {"$type": "string"},
                    },
                },
            ),
            (
                [("room_id", 1), ("continuation_message_id", 1)],
                {
                    "unique": True,
                    "name": _PENDING_HITL_CONTINUATION_INDEX,
                    "partialFilterExpression": {
                        "status": "pending",
                        "public_source": "agent",
                        "continuation_message_id": {"$type": "string"},
                    },
                },
            ),
        ]
        for keys, kwargs in critical_indexes:
            index_name = kwargs.get("name")
            try:
                await self._hitl_requests.create_index(keys, **kwargs)
            except Exception as exc:
                is_conflict = (
                    isinstance(exc, OperationFailure)
                    and getattr(exc, "code", None) in (85, 86)
                ) or "already exists with different options" in str(exc)
                drop_fn = getattr(self._hitl_requests, "drop_index", None)
                if is_conflict and index_name and drop_fn:
                    try:
                        logger.emit(
                            E.INDEX_CONFLICT_DETECTED,
                            collection_name=_COLLECTION_NAME,
                            index_name=index_name,
                            critical=True,
                        )
                        await drop_fn(index_name)
                        await self._hitl_requests.create_index(keys, **kwargs)
                        continue
                    except Exception as recreate_exc:
                        logger.emit(
                            E.INDEX_RECREATION_FAILED,
                            exc=recreate_exc,
                            collection_name=_COLLECTION_NAME,
                            index_name=index_name,
                            critical=True,
                        )
                logger.emit(
                    E.INDEX_CREATION_FAILED,
                    level=logging.ERROR,
                    exc=exc,
                    collection_name=_COLLECTION_NAME,
                    index_name=index_name,
                    critical=True,
                )
                raise
