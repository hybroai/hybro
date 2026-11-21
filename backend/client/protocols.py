"""Client ports.

``ClientRegistry`` is the surface ``api_gateway`` and ``session`` consume:
the owner's ``/sources`` routes, the machine-only route, the client-token
check and the names on session payloads. ``ClientStore`` is the durable
record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from client.models import (
    NO_TOKEN,
    NO_TOKEN_CLIENT_ID,
    ClientIdentity,
    ClientRecord,
    SourceCreate,
    SourceList,
    SourceRename,
    SourceView,
    SourceWithToken,
    TokenRequirement,
    TokenRequirementChange,
    Via,
)


class ClientNotFound(Exception):
    """No client has this id."""


class ClientRevoked(Exception):
    """The client is revoked; its token cannot be rotated."""


class TokenRequirementUnset(Exception):
    """The switch was not created at startup, so it cannot be read or changed."""


class TokenRequirementLocked(Exception):
    """The deprecated Clerk mode always requires a token; it cannot be turned off."""


@runtime_checkable
class ClientRegistry(Protocol):
    async def create(self, name: str) -> SourceWithToken: ...
    async def list_clients(self) -> SourceList: ...
    async def rename(self, client_id: str, name: str) -> SourceView: ...
    async def rotate(self, client_id: str) -> SourceWithToken: ...
    async def revoke(self, client_id: str) -> SourceView: ...
    async def resolve(
        self, token: str, *, via: Via = "api"
    ) -> ClientIdentity | None: ...
    async def identities(self, client_ids: list[str]) -> dict[str, ClientIdentity]: ...
    async def token_required(self) -> bool: ...
    async def token_requirement(self) -> TokenRequirement: ...
    async def set_token_required(self, required: bool) -> TokenRequirement: ...
    async def seed_token_requirement(
        self, *, existing_install: bool
    ) -> TokenRequirement: ...


@runtime_checkable
class ClientStore(Protocol):
    async def ensure_indexes(self) -> None: ...
    async def insert(self, record: ClientRecord) -> None: ...
    async def get(self, client_id: str) -> ClientRecord | None: ...
    async def get_by_token_hash(self, token_hash: str) -> ClientRecord | None: ...
    async def list_all(self) -> list[ClientRecord]: ...
    async def list_by_ids(self, client_ids: list[str]) -> list[ClientRecord]: ...
    async def update(
        self, client_id: str, fields: dict[str, Any], *, live_only: bool = False
    ) -> ClientRecord | None: ...
    async def touch(self, client_id: str, at: datetime, via: Via) -> None: ...
    async def get_requirement(self) -> TokenRequirement | None: ...
    async def seed_requirement(self, seed: TokenRequirement) -> TokenRequirement: ...
    async def set_requirement(
        self, required: bool, at: datetime
    ) -> TokenRequirement | None: ...


__all__ = [
    "NO_TOKEN",
    "NO_TOKEN_CLIENT_ID",
    "SourceCreate",
    "ClientIdentity",
    "SourceList",
    "ClientNotFound",
    "ClientRegistry",
    "SourceRename",
    "ClientRevoked",
    "ClientStore",
    "SourceView",
    "SourceWithToken",
    "TokenRequirement",
    "TokenRequirementChange",
    "TokenRequirementLocked",
    "TokenRequirementUnset",
    "Via",
]
