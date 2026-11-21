"""Sources: the stored record, the identity a request carries, and the payloads."""

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

# The built-in source of a request that carries no token while the requirement
# is off. It is a constant, never a stored document, so it cannot be renamed,
# revoked or granted anything.
NO_TOKEN_SOURCE_ID = "no_token"
NO_TOKEN_NAME = "No token"
# Every token starts with this, so a leaked one is recognisable.
TOKEN_PREFIX = "hybro_src_"

# How a source's request reached the Source API: through the MCP server, which
# marks its requests, or directly.
Via = Literal["api", "mcp"]

SourceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


def _utc(value: datetime | None) -> datetime | None:
    # Mongo returns naive datetimes; they are stored as UTC.
    return ensure_utc(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Which source sent a request, as the Source API sees it."""

    source_id: str
    name: str
    has_token: bool


NO_TOKEN = SourceIdentity(
    source_id=NO_TOKEN_SOURCE_ID, name=NO_TOKEN_NAME, has_token=False
)


class SourceRecord(BaseModel):
    """One source (collection ``sources``).

    ``token_hash`` is the SHA-256 of the token, which is never stored, and
    ``token_last4`` its last four characters. Both are null once the source is
    revoked; the record stays, so its sessions still name it.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str
    name: str
    token_hash: str | None = Field(default=None, repr=False)
    token_last4: str | None = None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    # How the last request connected; null before its first use, and for a
    # source last used before this was recorded.
    last_used_via: Via | None = None
    revoked_at: datetime | None = None

    _utc_times = field_validator(
        "created_at", "updated_at", "last_used_at", "revoked_at"
    )(_utc)

    @property
    def has_token(self) -> bool:
        return self.revoked_at is None and self.token_hash is not None

    def identity(self) -> SourceIdentity:
        return SourceIdentity(
            source_id=self.source_id, name=self.name, has_token=self.has_token
        )


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SourceName


class SourceRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SourceName


class SourceView(BaseModel):
    """A source as the owner sees it. It never carries the token."""

    source_id: str
    name: str
    has_token: bool
    token_last4: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    last_used_via: Via | None = None
    revoked_at: datetime | None = None

    _utc_times = field_validator("created_at", "last_used_at", "revoked_at")(_utc)


class SourceWithToken(SourceView):
    """A source with its new token, returned once by create and rotate."""

    token: str = Field(repr=False)


class SourceList(BaseModel):
    sources: list[SourceView] = Field(default_factory=list)


class TokenRequirement(BaseModel):
    """Whether a Source API request needs a source token (the owner's switch).

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
    "NO_TOKEN_SOURCE_ID",
    "TOKEN_PREFIX",
    "SourceCreate",
    "SourceIdentity",
    "SourceList",
    "SourceName",
    "SourceRecord",
    "SourceRename",
    "SourceView",
    "SourceWithToken",
    "TokenRequirement",
    "TokenRequirementChange",
    "Via",
]
