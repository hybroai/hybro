"""An in-memory ``AccountStore`` and a fast ``AccountService`` for tests."""

from __future__ import annotations

from datetime import datetime

from account import AccountService, AttemptLimiter, ScryptParams
from account.models import UserRecord, UserSessionRecord
from account.protocols import AccountExists

# Small enough to hash in microseconds; production parameters are far larger.
FAST_SCRYPT = ScryptParams(n=2**4, r=1, p=1)


class MemoryAccountStore:
    def __init__(self) -> None:
        self.owner: UserRecord | None = None
        self.sessions: dict[str, UserSessionRecord] = {}
        self.setup_code: tuple[str, datetime] | None = None

    async def ensure_indexes(self) -> None:
        return None

    async def get_owner(self) -> UserRecord | None:
        return self.owner

    async def create_owner(self, record: UserRecord) -> UserRecord:
        if self.owner is not None:
            raise AccountExists
        self.owner = record
        return record

    async def update_owner(
        self, user_id: str, fields: dict, *, if_password_id: str | None = None
    ) -> UserRecord | None:
        if self.owner is None or self.owner.user_id != user_id:
            return None
        if if_password_id not in (None, self.owner.password_id):
            return None
        self.owner = UserRecord.model_validate({**self.owner.model_dump(), **fields})
        return self.owner

    async def create_session(self, record: UserSessionRecord) -> None:
        self.sessions[record.session_id] = record

    async def get_session(self, token_hash: str) -> UserSessionRecord | None:
        for record in self.sessions.values():
            if record.token_hash == token_hash:
                return record
        return None

    async def touch_session(self, session_id: str, at: datetime) -> None:
        record = self.sessions.get(session_id)
        if record is not None:
            self.sessions[session_id] = record.model_copy(update={"last_used_at": at})

    async def list_sessions(self, user_id: str) -> list[UserSessionRecord]:
        return sorted(
            (r for r in self.sessions.values() if r.user_id == user_id),
            key=lambda r: r.last_used_at,
            reverse=True,
        )

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        record = self.sessions.get(session_id)
        if record is None or record.user_id != user_id:
            return False
        del self.sessions[session_id]
        return True

    async def delete_sessions(self, user_id: str, *, keep: str | None = None) -> int:
        doomed = [
            sid
            for sid, record in self.sessions.items()
            if record.user_id == user_id and sid != keep
        ]
        for sid in doomed:
            del self.sessions[sid]
        return len(doomed)

    async def replace_setup_code(self, code_hash: str, at: datetime) -> None:
        self.setup_code = (code_hash, at)

    async def has_setup_code(self, code_hash: str, since: datetime) -> bool:
        return (
            self.setup_code is not None
            and self.setup_code[0] == code_hash
            and self.setup_code[1] > since
        )

    async def consume_setup_code(self, code_hash: str, since: datetime) -> bool:
        if not await self.has_setup_code(code_hash, since):
            return False
        self.setup_code = None
        return True


class Clock:
    """A settable wall clock."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Sleeper:
    """Records each requested delay and lets that much time pass at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.elapsed = 0.0

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.elapsed += seconds

    def clock(self) -> float:
        return self.elapsed


def build_service(
    *,
    store: MemoryAccountStore | None = None,
    auth_mode: str = "mock",
    admin_user_ids: frozenset[str] = frozenset(),
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    limiter: AttemptLimiter | None = None,
    params: ScryptParams = FAST_SCRYPT,
) -> AccountService:
    sleeper = sleeper or Sleeper()
    kwargs = {}
    if clock is not None:
        kwargs["now"] = clock
    if limiter is None and isinstance(sleeper, Sleeper):
        # The limiter's time passes as the service "waits".
        limiter = AttemptLimiter(clock=sleeper.clock)
    if limiter is not None:
        kwargs["limiter"] = limiter
    return AccountService(
        store or MemoryAccountStore(),
        auth_mode=auth_mode,
        admin_user_ids=admin_user_ids,
        params=params,
        sleep=sleeper,
        **kwargs,
    )
