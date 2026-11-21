"""Clients: the stored record, the identity a request carries, and the payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
)

from common.utils.time import ensure_utc

# The built-in client of a request that carries no token while the requirement
# is off. It is a constant, never a stored document, so it cannot be renamed,
# revoked or granted anything.
NO_TOKEN_CLIENT_ID = "no_token"
NO_TOKEN_NAME = "No token"
# Every token starts with this, so a leaked one is recognisable.
TOKEN_PREFIX = "hybro_client_"

# How a client's request reached the Client API: through the MCP server, which
# marks its requests, or directly.
Via = Literal["api", "mcp"]

ClientName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


def _utc(value: datetime | None) -> datetime | None:
    # Mongo returns naive datetimes; they are stored as UTC.
    return ensure_utc(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Which client sent a request, as the Client API sees it."""

    client_id: str
    name: str
    has_token: bool


NO_TOKEN = ClientIdentity(
    client_id=NO_TOKEN_CLIENT_ID, name=NO_TOKEN_NAME, has_token=False
)


class ClientRecord(BaseModel):
    """One client (collection ``clients``).

    ``token_hash`` is the SHA-256 of the token, which is never stored, and
    ``token_last4`` its last four characters. Both are null once the client is
    revoked; the record stays, so its sessions still name it.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    name: str
    token_hash: str | None = Field(default=None, repr=False)
    token_last4: str | None = None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    # How the last request connected; null before its first use, and for a
    # client last used before this was recorded.
    last_used_via: Via | None = None
    revoked_at: datetime | None = None

    _utc_times = field_validator(
        "created_at", "updated_at", "last_used_at", "revoked_at"
    )(_utc)

    @property
    def has_token(self) -> bool:
        return self.revoked_at is None and self.token_hash is not None

    def identity(self) -> ClientIdentity:
        return ClientIdentity(
            client_id=self.client_id, name=self.name, has_token=self.has_token
        )


class ClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ClientName


class ClientRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ClientName


class ClientView(BaseModel):
    """A client as the owner sees it. It never carries the token."""

    client_id: str
    name: str
    has_token: bool
    token_last4: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    last_used_via: Via | None = None
    revoked_at: datetime | None = None

    _utc_times = field_validator("created_at", "last_used_at", "revoked_at")(_utc)


class ClientWithToken(ClientView):
    """A client with its new token, returned once by create and rotate."""

    token: str = Field(repr=False)


class ClientList(BaseModel):
    clients: list[ClientView] = Field(default_factory=list)


class TokenRequirement(BaseModel):
    """Whether a Client API request needs a client token (the owner's switch).

    Product state, not configuration: one document, created at backend
    startup and changed by the owner in the Panel without a restart.
    ``started_off`` is true when the install already had data when the switch
    was created, so it began off; it never changes afterwards.
    """

    model_config = ConfigDict(frozen=True)

    required: bool
    started_off: bool
    updated_at: datetime

    _utc_times = field_validator("updated_at")(_utc)


class TokenRequirementChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: StrictBool


__all__ = [
    "NO_TOKEN",
    "NO_TOKEN_NAME",
    "NO_TOKEN_CLIENT_ID",
    "TOKEN_PREFIX",
    "ClientCreate",
    "ClientIdentity",
    "ClientList",
    "ClientName",
    "ClientRecord",
    "ClientRename",
    "ClientView",
    "ClientWithToken",
    "TokenRequirement",
    "TokenRequirementChange",
    "Via",
]
