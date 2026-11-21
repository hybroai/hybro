"""Mongo persistence for the owner's rules (collection ``rules``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument, UpdateOne

from common.protocols import MongoDAL
from policy.models import CheckRule

_PROJECTION = {"_id": 0}
_CHECK = {"kind": "check"}
# Positions can tie after a concurrent create; creation time breaks the tie.
_ORDER = [("position", 1), ("created_at", 1), ("rule_id", 1)]

RULE_INDEXES: tuple[dict[str, Any], ...] = (
    {"keys": [("rule_id", 1)], "name": "rule_id_unique", "unique": True},
    {"keys": [("kind", 1), ("position", 1)], "name": "rule_kind_position"},
)


class MongoRuleRepository:
    def __init__(self, mongo: MongoDAL) -> None:
        self._rules = mongo.collection("rules")

    async def ensure_indexes(self) -> None:
        for index in RULE_INDEXES:
            options = {key: value for key, value in index.items() if key != "keys"}
            await self._rules.create_index(index["keys"], **options)

    async def insert(self, record: CheckRule) -> None:
        await self._rules.insert_one(record.model_dump())

    async def list_all(self) -> list[CheckRule]:
        # Small by design: create refuses past MAX_RULES. Never cut short,
        # because evaluate must see every rule.
        docs = await self._rules.find(_CHECK, projection=_PROJECTION, sort=_ORDER)
        return [CheckRule.model_validate(doc) for doc in docs]

    async def update(self, rule_id: str, fields: dict[str, Any]) -> CheckRule | None:
        doc = await self._rules.find_one_and_update(
            {"rule_id": rule_id},
            {"$set": fields},
            projection=_PROJECTION,
            return_document=ReturnDocument.AFTER,
        )
        return CheckRule.model_validate(doc) if doc is not None else None

    async def delete(self, rule_id: str) -> bool:
        return await self._rules.delete_one({"rule_id": rule_id})

    async def set_positions(self, rule_ids: list[str], at: datetime) -> None:
        if not rule_ids:
            return
        await self._rules.bulk_write(
            [
                UpdateOne(
                    {"rule_id": rule_id},
                    {"$set": {"position": position, "updated_at": at}},
                )
                for position, rule_id in enumerate(rule_ids)
            ],
            ordered=True,
        )


__all__ = ["RULE_INDEXES", "MongoRuleRepository"]
