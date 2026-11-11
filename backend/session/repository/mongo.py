"""Mongo persistence for session records (collection ``sessions``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from common.protocols import MongoDAL
from session.models import SessionRecord

_PROJECTION = {"_id": 0}

SESSION_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("session_id", 1)], "name": "session_id_unique", "unique": True},
    {
        # One session per owner and creation request; retries of a first send
        # load the reserved record instead of opening a second session.
        "keys": [("owner_user_id", 1), ("creation_request_id", 1)],
        "name": "session_creation_request",
        "unique": True,
        "partialFilterExpression": {"creation_request_id": {"$type": "string"}},
    },
    {
        "keys": [("owner_user_id", 1), ("status", 1), ("last_activity_at", -1)],
        "name": "session_owner_status_activity",
    },
)


class MongoSessionRepository:
    def __init__(self, mongo: MongoDAL, collection_name: str = "sessions") -> None:
        self._sessions = mongo.collection(collection_name)

    async def ensure_indexes(self) -> None:
        for index in SESSION_INDEXES:
            options = {key: value for key, value in index.items() if key != "keys"}
            await self._sessions.create_index(index["keys"], **options)

    async def reserve(self, record: SessionRecord) -> SessionRecord:
        try:
            await self._sessions.insert_one(record.model_dump())
        except DuplicateKeyError:
            existing = await self._sessions.find_one(
                {
                    "owner_user_id": record.owner_user_id,
                    "creation_request_id": record.creation_request_id,
                },
                projection=_PROJECTION,
            )
            if existing is None:
                raise
            return SessionRecord.model_validate(existing)
        return record

    async def get(self, session_id: str) -> SessionRecord | None:
        doc = await self._sessions.find_one(
            {"session_id": session_id}, projection=_PROJECTION
        )
        return SessionRecord.model_validate(doc) if doc is not None else None

    async def set_conversation_if_unset(
        self, session_id: str, conversation_id: str
    ) -> SessionRecord | None:
        doc = await self._update(
            {"session_id": session_id, "conversation_id": None},
            {"conversation_id": conversation_id},
        )
        if doc is not None:
            return doc
        return await self.get(session_id)

    async def record_send(
        self,
        session_id: str,
        *,
        client_request_id: str,
        user_message_id: str,
        event_seq: int,
        at: datetime,
    ) -> SessionRecord | None:
        fields = {
            "last_client_request_id": client_request_id,
            "last_user_message_id": user_message_id,
            "last_send_event_seq": event_seq,
            "last_activity_at": at,
        }
        # Concurrent sends of one request both record it. Only the first write
        # lands; a later one must not undo a request recorded since.
        appended = await self._update(
            {
                "session_id": session_id,
                "requests.client_request_id": {"$ne": client_request_id},
            },
            fields,
            push={
                "requests": {
                    "client_request_id": client_request_id,
                    "user_message_id": user_message_id,
                    "sent_at": at,
                }
            },
        )
        if appended is not None:
            return appended
        return await self.get(session_id)

    async def record_accepted_agent(
        self, session_id: str, agent_id: str
    ) -> SessionRecord | None:
        return await self._update(
            {"session_id": session_id}, {"last_accepted_agent_id": agent_id}
        )

    async def mark_closed(self, session_id: str, at: datetime) -> SessionRecord | None:
        return await self._update(
            {"session_id": session_id},
            {"status": "closed", "closed_at": at, "last_activity_at": at},
        )

    async def list_for_owner(
        self, owner_user_id: str, limit: int = 100
    ) -> list[SessionRecord]:
        docs = await self._sessions.find(
            {"owner_user_id": owner_user_id},
            projection=_PROJECTION,
            sort=[("last_activity_at", -1)],
            limit=limit,
        )
        return [SessionRecord.model_validate(doc) for doc in docs]

    async def list_open_conversations(self, owner_user_id: str) -> dict[str, str]:
        """Every open session's conversation id, mapped to its session id."""
        docs = await self._sessions.find(
            {
                "owner_user_id": owner_user_id,
                "status": "open",
                "conversation_id": {"$type": "string"},
            },
            projection={"_id": 0, "session_id": 1, "conversation_id": 1},
        )
        return {doc["conversation_id"]: doc["session_id"] for doc in docs}

    async def _update(
        self,
        query: dict[str, Any],
        fields: dict[str, Any],
        *,
        push: dict[str, Any] | None = None,
    ) -> SessionRecord | None:
        update: dict[str, Any] = {"$set": fields}
        if push:
            update["$push"] = push
        doc = await self._sessions.find_one_and_update(
            query,
            update,
            projection=_PROJECTION,
            return_document=ReturnDocument.AFTER,
        )
        return SessionRecord.model_validate(doc) if doc is not None else None


__all__ = ["SESSION_INDEXES", "MongoSessionRepository"]
