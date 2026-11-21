"""An in-memory ``ClientStore`` for tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from client.models import ClientRecord, TokenRequirement


class MemoryClientStore:
    def __init__(self) -> None:
        self.records: dict[str, ClientRecord] = {}
        self.touched: list[tuple[str, datetime, str]] = []
        self.requirement: TokenRequirement | None = None

    async def ensure_indexes(self) -> None:
        return None

    async def insert(self, record: ClientRecord) -> None:
        self.records[record.client_id] = record

    async def get(self, client_id: str) -> ClientRecord | None:
        return self.records.get(client_id)

    async def get_by_token_hash(self, token_hash: str) -> ClientRecord | None:
        for record in self.records.values():
            if record.token_hash == token_hash:
                return record
        return None

    async def list_all(self) -> list[ClientRecord]:
        return sorted(self.records.values(), key=lambda r: r.created_at)

    async def list_by_ids(self, client_ids: list[str]) -> list[ClientRecord]:
        return [self.records[i] for i in client_ids if i in self.records]

    async def update(
        self, client_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> ClientRecord | None:
        record = self.records.get(client_id)
        if record is None or (live_only and record.revoked_at is not None):
            return None
        updated = record.model_copy(update=fields)
        self.records[client_id] = updated
        return updated

    async def touch(self, client_id: str, at: datetime, via: str) -> None:
        self.touched.append((client_id, at, via))
        record = self.records.get(client_id)
        if record is not None:
            self.records[client_id] = record.model_copy(
                update={"last_used_at": at, "last_used_via": via}
            )

    async def get_requirement(self) -> TokenRequirement | None:
        return self.requirement

    async def seed_requirement(self, seed: TokenRequirement) -> TokenRequirement:
        if self.requirement is None:
            self.requirement = seed
        return self.requirement

    async def set_requirement(
        self, required: bool, at: datetime
    ) -> TokenRequirement | None:
        if self.requirement is None:
            return None
        self.requirement = self.requirement.model_copy(
            update={"required": required, "updated_at": at}
        )
        return self.requirement


def store_with_requirement(*, required: bool) -> MemoryClientStore:
    """A store whose switch was seeded at startup, as in a running backend."""
    from common.utils.time import utcnow

    store = MemoryClientStore()
    store.requirement = TokenRequirement(
        required=required, started_off=not required, updated_at=utcnow()
    )
    return store
