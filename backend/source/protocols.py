"""Source ports.

``SourceRegistry`` is the surface ``api_gateway`` and ``session`` consume:
the owner's ``/sources`` routes, the machine-only route, the source-token
check and the names on session payloads. ``SourceStore`` is the durable
record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from source.models import (
    NO_TOKEN,
    NO_TOKEN_SOURCE_ID,
    SourceCreate,
    SourceIdentity,
    SourceList,
    SourceRecord,
    SourceRename,
    SourceView,
    SourceWithToken,
    TokenRequirement,
    TokenRequirementChange,
    Via,
)


class SourceNotFound(Exception):
    """No source has this id."""


class SourceRevoked(Exception):
    """The source is revoked; its token cannot be rotated."""


class TokenRequirementUnset(Exception):
    """The switch was not created at startup, so it cannot be read or changed."""


class TokenRequirementLocked(Exception):
    """The deprecated Clerk mode always requires a token; it cannot be turned off."""


@runtime_checkable
class SourceRegistry(Protocol):
    async def create(self, name: str) -> SourceWithToken: ...
    async def list_sources(self) -> SourceList: ...
    async def rename(self, source_id: str, name: str) -> SourceView: ...
    async def rotate(self, source_id: str) -> SourceWithToken: ...
    async def revoke(self, source_id: str) -> SourceView: ...
    async def resolve(
        self, token: str, *, via: Via = "api"
    ) -> SourceIdentity | None: ...
    async def identities(self, source_ids: list[str]) -> dict[str, SourceIdentity]: ...
    async def token_required(self) -> bool: ...
    async def token_requirement(self) -> TokenRequirement: ...
    async def set_token_required(self, required: bool) -> TokenRequirement: ...
    async def seed_token_requirement(
        self, *, existing_install: bool
    ) -> TokenRequirement: ...


@runtime_checkable
class SourceStore(Protocol):
    async def ensure_indexes(self) -> None: ...
    async def insert(self, record: SourceRecord) -> None: ...
    async def get(self, source_id: str) -> SourceRecord | None: ...
    async def get_by_token_hash(self, token_hash: str) -> SourceRecord | None: ...
    async def list_all(self) -> list[SourceRecord]: ...
    async def list_by_ids(self, source_ids: list[str]) -> list[SourceRecord]: ...
    async def update(
        self, source_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> SourceRecord | None: ...
    async def touch(self, source_id: str, at: datetime, via: Via) -> None: ...
    async def get_requirement(self) -> TokenRequirement | None: ...
    async def seed_requirement(self, seed: TokenRequirement) -> TokenRequirement: ...
    async def set_requirement(
        self, required: bool, at: datetime
    ) -> TokenRequirement | None: ...


__all__ = [
    "NO_TOKEN",
    "NO_TOKEN_SOURCE_ID",
    "SourceCreate",
    "SourceIdentity",
    "SourceList",
    "SourceNotFound",
    "SourceRegistry",
    "SourceRename",
    "SourceRevoked",
    "SourceStore",
    "SourceView",
    "SourceWithToken",
    "TokenRequirement",
    "TokenRequirementChange",
    "TokenRequirementLocked",
    "TokenRequirementUnset",
    "Via",
]
