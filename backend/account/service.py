"""The instance's one account: the owner's.

Setup, sign-in, the signed-in browsers, and the three actions the ``hybro``
command performs on the machine. Passwords are hashed off the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from account.attempts import AttemptLimiter
from account.models import (
    OWNER_USER_ID,
    BrowserList,
    BrowserSession,
    NewSession,
    OwnerCreate,
    OwnerProfile,
    PasswordChange,
    PasswordHash,
    PasswordReset,
    ProfileUpdate,
    SetupCode,
    SetupRequest,
    SignedIn,
    SignInRequest,
    UserRecord,
    UserSessionRecord,
)
from account.passwords import (
    DEFAULT_PARAMS,
    ScryptParams,
    hash_password,
    needs_upgrade,
    verify_password,
)
from account.protocols import (
    AccountExists,
    AccountStore,
    BrowserNotFound,
    InvalidCredentials,
    InvalidSetupCode,
    NoAccount,
    OwnerIdRequired,
    TooManyAttempts,
)
from account.repository.mongo import SESSION_IDLE_SECONDS
from common.utils.time import utcnow

SETUP_CODE_LIFETIME = timedelta(hours=24)
SESSION_IDLE = timedelta(seconds=SESSION_IDLE_SECONDS)
# Last use is written at most this often, not on every request.
_TOUCH_INTERVAL = timedelta(minutes=5)
_USER_AGENT_LIMIT = 512
# Each hash holds about 32 MiB while it runs. Sign-in's checks, which anyone
# can ask for, run at most two at a time; the hashes of a caller who has
# already proved something (a setup code, the cookie, the machine token) run
# one at a time beside them, so a flood of sign-ins does not hold them up.
_CONCURRENT_SIGN_IN_HASHES = 2
# Password checks taken at one time (sign-in, setup, password change),
# waiting ones included. Beyond it a request is refused at once: the bound on
# what a flood can make the backend hold, and on guesses sent at once.
_PENDING_LIMIT = 256


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile(record: UserRecord) -> OwnerProfile:
    return OwnerProfile(user_id=record.user_id, name=record.name, email=record.email)


class AccountService:
    def __init__(
        self,
        store: AccountStore,
        *,
        auth_mode: Literal["mock", "clerk"],
        admin_user_ids: frozenset[str],
        params: ScryptParams = DEFAULT_PARAMS,
        limiter: AttemptLimiter | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._auth_mode = auth_mode
        self._admins = admin_user_ids
        self._params = params
        self._limiter = limiter or AttemptLimiter()
        self._now = now
        self._sleep = sleep
        self._sign_in_hashing = asyncio.Semaphore(_CONCURRENT_SIGN_IN_HASHES)
        self._hashing = asyncio.Semaphore(1)
        self._pending = 0

    # ── Creating the owner ──

    async def exists(self) -> bool:
        return await self._store.get_owner() is not None

    async def issue_setup_code(self) -> SetupCode:
        if await self.exists():
            raise AccountExists
        code = secrets.token_urlsafe(32)
        now = self._now()
        await self._store.replace_setup_code(_digest(code), now)
        return SetupCode(code=code, expires_at=now + SETUP_CODE_LIFETIME)

    async def setup(
        self, request: SetupRequest, *, address: str, user_agent: str
    ) -> NewSession:
        async with self._admitted():
            return await self._setup(request, address, user_agent)

    @contextlib.asynccontextmanager
    async def _admitted(self) -> AsyncIterator[None]:
        if self._pending >= _PENDING_LIMIT:
            raise TooManyAttempts
        self._pending += 1
        try:
            yield
        finally:
            self._pending -= 1

    async def _setup(
        self, request: SetupRequest, address: str, user_agent: str
    ) -> NewSession:
        key = f"setup:{address}"
        await self._sleep(self._limiter.begin(key))
        if await self.exists():
            raise AccountExists
        code_hash = _digest(request.code.get_secret_value())
        since = self._now() - SETUP_CODE_LIFETIME
        if not await self._store.has_setup_code(code_hash, since):
            raise InvalidSetupCode
        # Resolved before the code is used up, so a request that must still
        # name the identity can be sent again with the same code.
        user_id = self._owner_id(request.owner_id)
        password = await self._hash(request.password.get_secret_value())
        if not await self._store.consume_setup_code(code_hash, since):
            raise InvalidSetupCode
        owner = await self._create(user_id, request.name, request.email, password)
        self._limiter.succeeded(key)
        return await self._open_session(owner, user_agent)

    async def create_owner(self, request: OwnerCreate) -> OwnerProfile:
        if await self.exists():
            raise AccountExists
        user_id = self._owner_id(request.owner_id)
        password = await self._hash(request.password.get_secret_value())
        owner = await self._create(user_id, request.name, request.email, password)
        return _profile(owner)

    def _owner_id(self, requested: str | None) -> str:
        """The stored identity the owner record takes.

        ``user_local_developer``, except when ``auth_mode`` is ``clerk`` and
        administrators are listed: then one of those, named by the request
        when there are several.
        """
        choices: tuple[str, ...] = (OWNER_USER_ID,)
        if self._auth_mode == "clerk" and self._admins:
            choices = tuple(sorted(self._admins))
        if requested is None and len(choices) == 1:
            return choices[0]
        if requested in choices:
            return requested
        raise OwnerIdRequired(choices)

    async def _create(
        self, user_id: str, name: str, email: str, password: PasswordHash
    ) -> UserRecord:
        now = self._now()
        return await self._store.create_owner(
            UserRecord(
                user_id=user_id,
                name=name,
                email=email,
                password=password,
                password_id=uuid.uuid4().hex,
                created_at=now,
                updated_at=now,
            )
        )

    # ── Sign-in ──

    async def sign_in(
        self, request: SignInRequest, *, address: str, user_agent: str
    ) -> NewSession:
        async with self._admitted():
            return await self._sign_in(request, address, user_agent)

    async def _sign_in(
        self, request: SignInRequest, address: str, user_agent: str
    ) -> NewSession:
        keys = (f"account:{request.email.casefold()}", f"address:{address}")
        await self._sleep(self._limiter.begin(*keys))
        owner = await self._store.get_owner()
        if owner is None or owner.email.casefold() != request.email.casefold():
            # The same work as a real check, so the answer's timing does not
            # tell whether the email is the owner's.
            async with self._sign_in_hashing:
                await asyncio.to_thread(
                    hash_password, request.password.get_secret_value(), self._params
                )
            raise InvalidCredentials
        async with self._sign_in_hashing:
            matches = await asyncio.to_thread(
                verify_password, request.password.get_secret_value(), owner.password
            )
        if not matches:
            raise InvalidCredentials
        if needs_upgrade(owner.password, self._params):
            await self._upgrade_hash(owner, request.password.get_secret_value())
        new = await self._open_session(owner, user_agent)
        self._limiter.succeeded(*keys)
        return new

    async def _open_session(self, owner: UserRecord, user_agent: str) -> NewSession:
        """A new browser session, unless the password was replaced meanwhile.

        ``owner`` is the record whose password the caller checked. Whether
        that password is still the stored one is looked at after the session
        exists: a replacement before that point is seen here, and one after
        it deletes the session itself.
        """
        token = secrets.token_urlsafe(32)
        session_id = uuid.uuid4().hex
        now = self._now()
        try:
            await self._store.create_session(
                UserSessionRecord(
                    session_id=session_id,
                    user_id=owner.user_id,
                    token_hash=_digest(token),
                    created_at=now,
                    last_used_at=now,
                    user_agent=user_agent[:_USER_AGENT_LIMIT],
                )
            )
            current = await self._store.get_owner()
        except BaseException:
            # Nobody holds the cookie value yet; do not leave its session.
            await self._discard_session(owner.user_id, session_id)
            raise
        if current is None or current.password_id != owner.password_id:
            await self._discard_session(owner.user_id, session_id)
            raise InvalidCredentials
        return NewSession(owner=_profile(owner), token=token)

    async def _discard_session(self, user_id: str, session_id: str) -> None:
        """Delete a session this request stored; never replaces its outcome."""
        with contextlib.suppress(Exception):
            await self._store.delete_session(user_id, session_id)

    async def resolve(self, token: str) -> SignedIn | None:
        if not token:
            return None
        record = await self._store.get_session(_digest(token))
        if record is None:
            return None
        now = self._now()
        if now - record.last_used_at > SESSION_IDLE:
            await self._store.delete_session(record.user_id, record.session_id)
            return None
        owner = await self._store.get_owner()
        if owner is None or owner.user_id != record.user_id:
            return None
        if now - record.last_used_at >= _TOUCH_INTERVAL:
            await self._store.touch_session(record.session_id, now)
        return SignedIn(owner=_profile(owner), session_id=record.session_id)

    async def sign_out(self, session: SignedIn) -> None:
        await self._store.delete_session(session.owner.user_id, session.session_id)

    # ── The owner's account pages ──

    async def update_profile(
        self, session: SignedIn, update: ProfileUpdate
    ) -> OwnerProfile:
        owner = await self._store.update_owner(
            session.owner.user_id,
            {"name": update.name, "email": update.email, "updated_at": self._now()},
        )
        if owner is None:
            raise NoAccount
        return _profile(owner)

    async def change_password(self, session: SignedIn, change: PasswordChange) -> None:
        async with self._admitted():
            await self._change_password(session, change)

    async def _change_password(self, session: SignedIn, change: PasswordChange) -> None:
        key = f"account:{session.owner.email.casefold()}"
        await self._sleep(self._limiter.begin(key))
        # Read after the wait, so the check is against the stored password.
        owner = await self._store.get_owner()
        if owner is None or owner.user_id != session.owner.user_id:
            raise NoAccount
        if not await self._verify(
            change.current_password.get_secret_value(), owner.password
        ):
            raise InvalidCredentials
        # Not if the password was reset while this one was checked.
        if not await self._set_password(
            owner.user_id,
            change.new_password.get_secret_value(),
            if_password_id=owner.password_id,
        ):
            raise InvalidCredentials
        self._limiter.succeeded(key)
        await self._store.delete_sessions(owner.user_id, keep=session.session_id)

    async def list_browsers(self, session: SignedIn) -> BrowserList:
        records = await self._store.list_sessions(session.owner.user_id)
        return BrowserList(
            items=[
                BrowserSession(
                    id=record.session_id,
                    created_at=record.created_at,
                    last_used_at=record.last_used_at,
                    user_agent=record.user_agent,
                    current=record.session_id == session.session_id,
                )
                for record in records
            ]
        )

    async def revoke_browser(self, session: SignedIn, browser_id: str) -> None:
        if not await self._store.delete_session(session.owner.user_id, browser_id):
            raise BrowserNotFound

    # ── On the machine ──

    async def reset_password(self, request: PasswordReset) -> None:
        owner = await self._store.get_owner()
        if owner is None:
            raise NoAccount
        await self._set_password(owner.user_id, request.password.get_secret_value())
        await self._store.delete_sessions(owner.user_id)

    # ── Hashing ──

    async def _set_password(
        self, user_id: str, password: str, *, if_password_id: str | None = None
    ) -> bool:
        """Replace the password; false when the condition no longer held."""
        hashed = await self._hash(password)
        updated = await self._store.update_owner(
            user_id,
            {
                "password": hashed.model_dump(),
                "password_id": uuid.uuid4().hex,
                "updated_at": self._now(),
            },
            if_password_id=if_password_id,
        )
        return updated is not None

    async def _upgrade_hash(self, owner: UserRecord, password: str) -> None:
        """Store the same password under the current parameters.

        The password's id stays, so this is not a replacement. It is skipped
        if the password was replaced since ``owner`` was read.
        """
        hashed = await self._hash(password)
        await self._store.update_owner(
            owner.user_id,
            {"password": hashed.model_dump(), "updated_at": self._now()},
            if_password_id=owner.password_id,
        )

    async def _hash(self, password: str) -> PasswordHash:
        async with self._hashing:
            return await asyncio.to_thread(hash_password, password, self._params)

    async def _verify(self, password: str, stored: PasswordHash) -> bool:
        async with self._hashing:
            return await asyncio.to_thread(verify_password, password, stored)


__all__ = ["SESSION_IDLE", "SETUP_CODE_LIFETIME", "AccountService"]
