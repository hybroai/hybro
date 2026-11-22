"""Client ports.

``ClientRegistry`` is the surface ``api_gateway`` and ``session`` consume:
the owner's ``/clients`` routes, the machine-only route, the client-token
check and the names on session payloads. ``ClientStore`` is the durable
record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from client.models import (
    BUILT_IN_CLIENT_IDS,
    NO_TOKEN,
    NO_TOKEN_CLIENT_ID,
    ClientAnswerGrantChange,
    ClientCreate,
    ClientIdentity,
    ClientList,
    ClientRecord,
    ClientRename,
    ClientView,
    ClientWithToken,
    TokenRequirement,
    TokenRequirementChange,
    Via,
)


class ClientNotFound(Exception):
    """No client has this id."""


class ClientRevoked(Exception):
    """The client is revoked; its token cannot be rotated, nor a grant given."""


class ClientBuiltIn(Exception):
    """A built-in client, such as "No token": it can never be granted anything."""


class TokenRequirementUnset(Exception):
    """The switch was not created at startup, so it cannot be read or changed."""


class TokenRequirementLocked(Exception):
    """The deprecated Clerk mode always requires a token; it cannot be turned off."""


@runtime_checkable
class ClientRegistry(Protocol):
    async def create(self, name: str) -> ClientWithToken: ...
    async def list_clients(self) -> ClientList: ...
    async def rename(self, client_id: str, name: str) -> ClientView: ...
    async def rotate(self, client_id: str) -> ClientWithToken: ...
    async def revoke(self, client_id: str) -> ClientView: ...
    async def set_answer_grant(self, client_id: str, granted: bool) -> ClientView: ...
    async def may_answer_permission_questions(self, client: ClientIdentity) -> bool: ...
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
    "BUILT_IN_CLIENT_IDS",
    "NO_TOKEN",
    "NO_TOKEN_CLIENT_ID",
    "ClientAnswerGrantChange",
    "ClientBuiltIn",
    "ClientCreate",
    "ClientIdentity",
    "ClientList",
    "ClientNotFound",
    "ClientRegistry",
    "ClientRename",
    "ClientRevoked",
    "ClientStore",
    "ClientView",
    "ClientWithToken",
    "TokenRequirement",
    "TokenRequirementChange",
    "TokenRequirementLocked",
    "TokenRequirementUnset",
    "Via",
]
