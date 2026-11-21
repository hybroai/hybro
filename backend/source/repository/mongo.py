"""Mongo persistence for sources (collection ``sources``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument

from common.protocols import MongoDAL
from source.models import SourceRecord, TokenRequirement

_PROJECTION = {"_id": 0}
# The owner's switch is one document in its own collection.
_REQUIREMENT = {"_id": "token_requirement"}

SOURCE_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("source_id", 1)], "name": "source_id_unique", "unique": True},
    {
        # A revoked source keeps its record without a hash.
        "keys": [("token_hash", 1)],
        "name": "source_token_hash_unique",
        "unique": True,
        "partialFilterExpression": {"token_hash": {"$type": "string"}},
    },
)


class MongoSourceRepository:
    def __init__(self, mongo: MongoDAL) -> None:
        self._sources = mongo.collection("sources")
        self._settings = mongo.collection("source_settings")

    async def ensure_indexes(self) -> None:
        for index in SOURCE_INDEXES:
            options = {key: value for key, value in index.items() if key != "keys"}
            await self._sources.create_index(index["keys"], **options)

    async def insert(self, record: SourceRecord) -> None:
        await self._sources.insert_one(record.model_dump())

    async def get(self, source_id: str) -> SourceRecord | None:
        return self._record(
            await self._sources.find_one(
                {"source_id": source_id}, projection=_PROJECTION
            )
        )

    async def get_by_token_hash(self, token_hash: str) -> SourceRecord | None:
        return self._record(
            await self._sources.find_one(
                {"token_hash": token_hash}, projection=_PROJECTION
            )
        )

    async def list_all(self) -> list[SourceRecord]:
        # The owner creates each source by hand, so the collection stays small;
        # the list is every source, oldest first.
        docs = await self._sources.find(
            {}, projection=_PROJECTION, sort=[("created_at", 1)]
        )
        return [SourceRecord.model_validate(doc) for doc in docs]

    async def list_by_ids(self, source_ids: list[str]) -> list[SourceRecord]:
        if not source_ids:
            return []
        docs = await self._sources.find(
            {"source_id": {"$in": source_ids}}, projection=_PROJECTION
        )
        return [SourceRecord.model_validate(doc) for doc in docs]

    async def update(
        self, source_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> SourceRecord | None:
        query: dict[str, Any] = {"source_id": source_id}
        if live_only:
            query["revoked_at"] = None
        return self._record(
            await self._sources.find_one_and_update(
                query,
                {"$set": fields},
                projection=_PROJECTION,
                return_document=ReturnDocument.AFTER,
            )
        )

    async def touch(self, source_id: str, at: datetime) -> None:
        await self._sources.update_one(
            {"source_id": source_id}, {"$set": {"last_used_at": at}}
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
    def _record(doc: dict[str, Any] | None) -> SourceRecord | None:
        return SourceRecord.model_validate(doc) if doc is not None else None


__all__ = ["SOURCE_INDEXES", "MongoSourceRepository"]
