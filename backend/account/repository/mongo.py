"""Mongo persistence for the owner account.

Collections: ``users`` (the owner record), ``user_sessions`` (signed-in
browsers) and ``setup_codes`` (the hash of the one valid setup code).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from account.models import UserRecord, UserSessionRecord
from account.protocols import AccountExists
from common.protocols import MongoDAL

_PROJECTION = {"_id": 0}
_OWNER = {"role": "owner"}
_SETUP_CODE = {"key": "owner_setup"}
# A browser that has not been used for this long is signed out. The service
# checks it on each request; the TTL index removes the record afterwards.
SESSION_IDLE_SECONDS = 30 * 24 * 3600

USER_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("user_id", 1)], "name": "user_id_unique", "unique": True},
    {
        # At most one owner: two creations at the same moment cannot both win.
        "keys": [("role", 1)],
        "name": "single_owner",
        "unique": True,
        "partialFilterExpression": _OWNER,
    },
)
USER_SESSION_INDEXES: tuple[dict[str, Any], ...] = (
    {
        "keys": [("token_hash", 1)],
        "name": "user_session_token_hash_unique",
        "unique": True,
    },
    {"keys": [("session_id", 1)], "name": "user_session_id_unique", "unique": True},
    {"keys": [("user_id", 1)], "name": "user_session_user"},
    {
        "keys": [("last_used_at", 1)],
        "name": "user_session_idle_expiry",
        "expireAfterSeconds": SESSION_IDLE_SECONDS,
    },
)
SETUP_CODE_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("key", 1)], "name": "setup_code_key_unique", "unique": True},
)


class MongoAccountRepository:
    def __init__(self, mongo: MongoDAL) -> None:
        self._users = mongo.collection("users")
        self._sessions = mongo.collection("user_sessions")
        self._setup_codes = mongo.collection("setup_codes")

    async def ensure_indexes(self) -> None:
        for collection, indexes in (
            (self._users, USER_INDEXES),
            (self._sessions, USER_SESSION_INDEXES),
            (self._setup_codes, SETUP_CODE_INDEXES),
        ):
            for index in indexes:
                options = {key: value for key, value in index.items() if key != "keys"}
                await collection.create_index(index["keys"], **options)

    async def get_owner(self) -> UserRecord | None:
        doc = await self._users.find_one(_OWNER, projection=_PROJECTION)
        return UserRecord.model_validate(doc) if doc is not None else None

    async def create_owner(self, record: UserRecord) -> UserRecord:
        try:
            await self._users.insert_one(record.model_dump())
        except DuplicateKeyError:
            raise AccountExists from None
        return record

    async def update_owner(
        self,
        user_id: str,
        fields: dict[str, Any],
        *,
        if_password_id: str | None = None,
    ) -> UserRecord | None:
        query: dict[str, Any] = {"user_id": user_id, **_OWNER}
        if if_password_id is not None:
            # Only while the stored password is still the one that was read.
            query["password_id"] = if_password_id
        doc = await self._users.find_one_and_update(
            query,
            {"$set": fields},
            projection=_PROJECTION,
            return_document=ReturnDocument.AFTER,
        )
        return UserRecord.model_validate(doc) if doc is not None else None

    async def create_session(self, record: UserSessionRecord) -> None:
        await self._sessions.insert_one(record.model_dump())

    async def get_session(self, token_hash: str) -> UserSessionRecord | None:
        doc = await self._sessions.find_one(
            {"token_hash": token_hash}, projection=_PROJECTION
        )
        return UserSessionRecord.model_validate(doc) if doc is not None else None

    async def touch_session(self, session_id: str, at: datetime) -> None:
        await self._sessions.update_one(
            {"session_id": session_id}, {"$set": {"last_used_at": at}}
        )

    async def list_sessions(self, user_id: str) -> list[UserSessionRecord]:
        docs = await self._sessions.find(
            {"user_id": user_id},
            projection=_PROJECTION,
            sort=[("last_used_at", -1)],
        )
        return [UserSessionRecord.model_validate(doc) for doc in docs]

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        return await self._sessions.delete_one(
            {"user_id": user_id, "session_id": session_id}
        )

    async def delete_sessions(self, user_id: str, *, keep: str | None = None) -> int:
        query: dict[str, Any] = {"user_id": user_id}
        if keep is not None:
            query["session_id"] = {"$ne": keep}
        return await self._sessions.delete_many(query)

    async def replace_setup_code(self, code_hash: str, at: datetime) -> None:
        await self._setup_codes.update_one(
            _SETUP_CODE,
            {"$set": {"code_hash": code_hash, "created_at": at}},
            upsert=True,
        )

    async def has_setup_code(self, code_hash: str, since: datetime) -> bool:
        doc = await self._setup_codes.find_one(self._code_query(code_hash, since))
        return doc is not None

    async def consume_setup_code(self, code_hash: str, since: datetime) -> bool:
        # One delete decides it, so a code cannot be used by two requests.
        return await self._setup_codes.delete_one(self._code_query(code_hash, since))

    @staticmethod
    def _code_query(code_hash: str, since: datetime) -> dict[str, Any]:
        return {**_SETUP_CODE, "code_hash": code_hash, "created_at": {"$gt": since}}


__all__ = [
    "SESSION_IDLE_SECONDS",
    "SETUP_CODE_INDEXES",
    "USER_INDEXES",
    "USER_SESSION_INDEXES",
    "MongoAccountRepository",
]
