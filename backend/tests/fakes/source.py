"""An in-memory ``SourceStore`` for tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from source.models import SourceRecord


class MemorySourceStore:
    def __init__(self) -> None:
        self.records: dict[str, SourceRecord] = {}
        self.touched: list[tuple[str, datetime]] = []

    async def ensure_indexes(self) -> None:
        return None

    async def insert(self, record: SourceRecord) -> None:
        self.records[record.source_id] = record

    async def get(self, source_id: str) -> SourceRecord | None:
        return self.records.get(source_id)

    async def get_by_token_hash(self, token_hash: str) -> SourceRecord | None:
        for record in self.records.values():
            if record.token_hash == token_hash:
                return record
        return None

    async def list_all(self) -> list[SourceRecord]:
        return sorted(self.records.values(), key=lambda r: r.created_at)

    async def list_by_ids(self, source_ids: list[str]) -> list[SourceRecord]:
        return [self.records[i] for i in source_ids if i in self.records]

    async def update(
        self, source_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> SourceRecord | None:
        record = self.records.get(source_id)
        if record is None or (live_only and record.revoked_at is not None):
            return None
        updated = record.model_copy(update=fields)
        self.records[source_id] = updated
        return updated

    async def touch(self, source_id: str, at: datetime) -> None:
        self.touched.append((source_id, at))
        record = self.records.get(source_id)
        if record is not None:
            self.records[source_id] = record.model_copy(update={"last_used_at": at})
