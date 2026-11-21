"""An in-memory ``RuleStore`` for tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from policy.models import CheckRule


class MemoryRuleStore:
    def __init__(self) -> None:
        self.records: dict[str, CheckRule] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def insert(self, record: CheckRule) -> None:
        self.records[record.rule_id] = record

    async def list_all(self) -> list[CheckRule]:
        return sorted(
            self.records.values(),
            key=lambda r: (r.position, r.created_at, r.rule_id),
        )

    async def update(self, rule_id: str, fields: dict[str, Any]) -> CheckRule | None:
        record = self.records.get(rule_id)
        if record is None:
            return None
        updated = record.model_copy(update=fields)
        self.records[rule_id] = updated
        return updated

    async def delete(self, rule_id: str) -> bool:
        return self.records.pop(rule_id, None) is not None

    async def set_positions(self, rule_ids: list[str], at: datetime) -> None:
        for position, rule_id in enumerate(rule_ids):
            if rule_id in self.records:
                self.records[rule_id] = self.records[rule_id].model_copy(
                    update={"position": position, "updated_at": at}
                )
