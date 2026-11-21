"""Mongo persistence for clients (collection ``clients``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument

from client.models import ClientRecord, TokenRequirement, Via
from common.protocols import MongoDAL

_PROJECTION = {"_id": 0}
# The owner's switch is one document in its own collection.
_REQUIREMENT = {"_id": "token_requirement"}

CLIENT_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("client_id", 1)], "name": "client_id_unique", "unique": True},
    {
        # A revoked client keeps its record without a hash.
        "keys": [("token_hash", 1)],
        "name": "client_token_hash_unique",
        "unique": True,
        "partialFilterExpression": {"token_hash": {"$type": "string"}},
    },
)


class MongoClientRepository:
    def __init__(self, mongo: MongoDAL) -> None:
        self._clients = mongo.collection("clients")
        self._settings = mongo.collection("client_settings")

    async def ensure_indexes(self) -> None:
        for index in CLIENT_INDEXES:
            options = {key: value for key, value in index.items() if key != "keys"}
            await self._clients.create_index(index["keys"], **options)

    async def insert(self, record: ClientRecord) -> None:
        await self._clients.insert_one(record.model_dump())

    async def get(self, client_id: str) -> ClientRecord | None:
        return self._record(
            await self._clients.find_one(
                {"client_id": client_id}, projection=_PROJECTION
            )
        )

    async def get_by_token_hash(self, token_hash: str) -> ClientRecord | None:
        return self._record(
            await self._clients.find_one(
                {"token_hash": token_hash}, projection=_PROJECTION
            )
        )

    async def list_all(self) -> list[ClientRecord]:
        # The owner creates each client by hand, so the collection stays small;
        # the list is every client, oldest first.
        docs = await self._clients.find(
            {}, projection=_PROJECTION, sort=[("created_at", 1)]
        )
        return [ClientRecord.model_validate(doc) for doc in docs]

    async def list_by_ids(self, client_ids: list[str]) -> list[ClientRecord]:
        if not client_ids:
            return []
        docs = await self._clients.find(
            {"client_id": {"$in": client_ids}}, projection=_PROJECTION
        )
        return [ClientRecord.model_validate(doc) for doc in docs]

    async def update(
        self, client_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> ClientRecord | None:
        query: dict[str, Any] = {"client_id": client_id}
        if live_only:
            query["revoked_at"] = None
        return self._record(
            await self._clients.find_one_and_update(
                query,
                {"$set": fields},
                projection=_PROJECTION,
                return_document=ReturnDocument.AFTER,
            )
        )

    async def touch(self, client_id: str, at: datetime, via: Via) -> None:
        await self._clients.update_one(
            {"client_id": client_id},
            {"$set": {"last_used_at": at, "last_used_via": via}},
        )

    async def get_requirement(self) -> TokenRequirement | None:
        return self._requirement(
            await self._settings.find_one(_REQUIREMENT, projection=_PROJECTION)
        )

    async def seed_requirement(self, seed: TokenRequirement) -> TokenRequirement:
        # Atomic: a document that exists already is returned unchanged.
        stored = await self._settings.find_one_and_update(
            _REQUIREMENT,
            {"$setOnInsert": seed.model_dump()},
            projection=_PROJECTION,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        requirement = self._requirement(stored)
        if requirement is None:
            raise RuntimeError("the token requirement upsert returned no document")
        return requirement

    async def set_requirement(
        self, required: bool, at: datetime
    ) -> TokenRequirement | None:
        return self._requirement(
            await self._settings.find_one_and_update(
                _REQUIREMENT,
                {"$set": {"required": required, "updated_at": at}},
                projection=_PROJECTION,
                return_document=ReturnDocument.AFTER,
            )
        )

    @staticmethod
    def _requirement(doc: dict[str, Any] | None) -> TokenRequirement | None:
        return TokenRequirement.model_validate(doc) if doc is not None else None

    @staticmethod
    def _record(doc: dict[str, Any] | None) -> ClientRecord | None:
        return ClientRecord.model_validate(doc) if doc is not None else None


__all__ = ["CLIENT_INDEXES", "MongoClientRepository"]
