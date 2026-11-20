"""The owner account's stored records and its request and response shapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from common.utils.time import ensure_utc

# The identity every stored room, session and agent of an install already
# carries; the owner record takes it, so no stored document is rewritten.
OWNER_USER_ID = "user_local_developer"

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    ),
]
# The upper bound keeps one request from hashing an arbitrarily large value.
NewPassword = Annotated[SecretStr, Field(min_length=8, max_length=1024)]
GivenPassword = Annotated[SecretStr, Field(min_length=1, max_length=1024)]
SetupCodeValue = Annotated[SecretStr, Field(min_length=1, max_length=200)]
OwnerId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


@dataclass(frozen=True, slots=True)
class ActingOwner:
    """The owner whose data a Source API request acts on."""

    user_id: str
    name: str


class _Stored(BaseModel):
    model_config = ConfigDict(frozen=True)


class PasswordHash(_Stored):
    algorithm: Literal["scrypt"] = "scrypt"
    n: int
    r: int
    p: int
    salt: str
    hash: str = Field(repr=False)


class UserRecord(_Stored):
    """A user of the instance (collection ``users``). There is one: the owner."""

    user_id: str
    role: Literal["owner"] = "owner"
    name: str
    email: str
    password: PasswordHash
    # New whenever the password itself is set (creation, change, reset), and
    # kept when only its hash is upgraded. A request that checked the password
    # earlier uses it to tell whether the password was replaced since.
    password_id: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class UserSessionRecord(_Stored):
    """One signed-in browser (collection ``user_sessions``).

    ``token_hash`` is the SHA-256 of the cookie value, which is never stored.
    """

    session_id: str
    user_id: str
    token_hash: str = Field(repr=False)
    created_at: datetime
    last_used_at: datetime
    user_agent: str

    @field_validator("created_at", "last_used_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class OwnerProfile(BaseModel):
    user_id: str
    name: str
    email: str


class AccountStatus(BaseModel):
    account_exists: bool
    # Set only for a request that carries the owner's sign-in.
    owner: OwnerProfile | None = None


class SetupRequest(BaseModel):
    code: SetupCodeValue
    name: Name
    email: Email
    password: NewPassword
    # Needed only when ``backend.auth_mode`` is ``clerk`` and
    # ``backend.admin_user_ids`` lists several ids.
    owner_id: OwnerId | None = None


class SignInRequest(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]
    password: GivenPassword


class ProfileUpdate(BaseModel):
    name: Name
    email: Email


class PasswordChange(BaseModel):
    current_password: GivenPassword
    new_password: NewPassword


class BrowserSession(BaseModel):
    id: str
    created_at: datetime
    last_used_at: datetime
    user_agent: str
    current: bool


class BrowserList(BaseModel):
    items: list[BrowserSession]


class OwnerCreate(BaseModel):
    name: Name
    email: Email
    password: NewPassword
    owner_id: OwnerId | None = None


class PasswordReset(BaseModel):
    password: NewPassword


class SetupCode(BaseModel):
    code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SignedIn:
    """The owner behind a request's cookie, and that browser's session."""

    owner: OwnerProfile
    session_id: str


@dataclass(frozen=True, slots=True)
class NewSession:
    """A new browser session; ``token`` is the cookie value, shown this once."""

    owner: OwnerProfile
    token: str

    def __repr__(self) -> str:
        return f"NewSession(owner={self.owner!r})"
