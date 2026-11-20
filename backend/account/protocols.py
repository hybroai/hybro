"""Owner account ports.

``OwnerAccount`` is the surface ``api_gateway`` consumes: the ``/auth`` routes
and the machine-only routes. ``AccountStore`` is the durable record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from account.models import (
    OWNER_USER_ID,
    AccountStatus,
    BrowserList,
    BrowserSession,
    NewSession,
    OwnerCreate,
    OwnerProfile,
    PasswordChange,
    PasswordReset,
    ProfileUpdate,
    SetupCode,
    SetupRequest,
    SignedIn,
    SignInRequest,
    UserRecord,
    UserSessionRecord,
)


class AccountExists(Exception):
    """The owner account exists; setup and creation work only before that."""


class NoAccount(Exception):
    """No owner account exists yet."""


class InvalidSetupCode(Exception):
    """The setup code is unknown, used, replaced or older than 24 hours."""


class InvalidCredentials(Exception):
    """The email or the password is wrong."""


class TooManyAttempts(Exception):
    """Too many password checks (sign-in, setup, password change) are waiting."""


class BrowserNotFound(Exception):
    """No signed-in browser of the owner has this id."""


class OwnerIdRequired(Exception):
    """The request must name which stored identity the owner takes over."""

    def __init__(self, choices: tuple[str, ...]) -> None:
        super().__init__("owner_id is required")
        self.choices = choices


@runtime_checkable
class OwnerAccount(Protocol):
    async def exists(self) -> bool: ...
    async def setup(
        self, request: SetupRequest, *, address: str, user_agent: str
    ) -> NewSession: ...
    async def sign_in(
        self, request: SignInRequest, *, address: str, user_agent: str
    ) -> NewSession: ...
    async def resolve(self, token: str) -> SignedIn | None: ...
    async def sign_out(self, session: SignedIn) -> None: ...
    async def update_profile(
        self, session: SignedIn, update: ProfileUpdate
    ) -> OwnerProfile: ...
    async def change_password(
        self, session: SignedIn, change: PasswordChange
    ) -> None: ...
    async def list_browsers(self, session: SignedIn) -> BrowserList: ...
    async def revoke_browser(self, session: SignedIn, browser_id: str) -> None: ...
    async def issue_setup_code(self) -> SetupCode: ...
    async def create_owner(self, request: OwnerCreate) -> OwnerProfile: ...
    async def reset_password(self, request: PasswordReset) -> None: ...


@runtime_checkable
class AccountStore(Protocol):
    async def ensure_indexes(self) -> None: ...
    async def get_owner(self) -> UserRecord | None: ...
    async def create_owner(self, record: UserRecord) -> UserRecord: ...
    async def update_owner(
        self,
        user_id: str,
        fields: dict[str, Any],
        *,
        if_password_id: str | None = None,
    ) -> UserRecord | None: ...
    async def create_session(self, record: UserSessionRecord) -> None: ...
    async def get_session(self, token_hash: str) -> UserSessionRecord | None: ...
    async def touch_session(self, session_id: str, at: datetime) -> None: ...
    async def list_sessions(self, user_id: str) -> list[UserSessionRecord]: ...
    async def delete_session(self, user_id: str, session_id: str) -> bool: ...
    async def delete_sessions(
        self, user_id: str, *, keep: str | None = None
    ) -> int: ...
    async def replace_setup_code(self, code_hash: str, at: datetime) -> None: ...
    async def has_setup_code(self, code_hash: str, since: datetime) -> bool: ...
    async def consume_setup_code(self, code_hash: str, since: datetime) -> bool: ...


__all__ = [
    "OWNER_USER_ID",
    "AccountExists",
    "AccountStatus",
    "AccountStore",
    "BrowserList",
    "BrowserNotFound",
    "BrowserSession",
    "InvalidCredentials",
    "InvalidSetupCode",
    "NewSession",
    "NoAccount",
    "OwnerAccount",
    "OwnerCreate",
    "OwnerIdRequired",
    "OwnerProfile",
    "PasswordChange",
    "PasswordReset",
    "ProfileUpdate",
    "SetupCode",
    "SetupRequest",
    "SignInRequest",
    "SignedIn",
    "TooManyAttempts",
]
