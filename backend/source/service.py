"""The sources of an instance and their tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow
from source.models import (
    NO_TOKEN,
    NO_TOKEN_SOURCE_ID,
    TOKEN_PREFIX,
    SourceIdentity,
    SourceList,
    SourceRecord,
    SourceView,
    SourceWithToken,
    TokenRequirement,
)
from source.protocols import (
    SourceNotFound,
    SourceRevoked,
    SourceStore,
    TokenRequirementLocked,
    TokenRequirementUnset,
)

logger = get_logger(__name__)

# Last use is shown to the owner; one write a minute per source is enough.
_TOUCH_INTERVAL = timedelta(minutes=1)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _view(record: SourceRecord) -> SourceView:
    return SourceView(
        source_id=record.source_id,
        name=record.name,
        has_token=record.has_token,
        token_last4=record.token_last4 if record.has_token else None,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )


def _with_token(record: SourceRecord, token: str) -> SourceWithToken:
    return SourceWithToken(**_view(record).model_dump(), token=token)


class SourceService:
    def __init__(
        self,
        store: SourceStore,
        *,
        auth_mode: Literal["mock", "clerk"],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._auth_mode = auth_mode
        self._now = now

    async def create(self, name: str) -> SourceWithToken:
        token = _new_token()
        now = self._now()
        record = SourceRecord(
            source_id="src_" + secrets.token_hex(12),
            name=name,
            token_hash=_digest(token),
            token_last4=token[-4:],
            created_at=now,
            updated_at=now,
        )
        await self._store.insert(record)
        return _with_token(record, token)

    async def list_sources(self) -> SourceList:
        return SourceList(sources=[_view(r) for r in await self._store.list_all()])

    async def rename(self, source_id: str, name: str) -> SourceView:
        updated = await self._store.update(
            source_id, {"name": name, "updated_at": self._now()}
        )
        if updated is None:
            raise SourceNotFound
        return _view(updated)

    async def rotate(self, source_id: str) -> SourceWithToken:
        token = _new_token()
        updated = await self._store.update(
            source_id,
            {
                "token_hash": _digest(token),
                "token_last4": token[-4:],
                "updated_at": self._now(),
            },
            live_only=True,
        )
        if updated is None:
            if await self._store.get(source_id) is None:
                raise SourceNotFound
            raise SourceRevoked
        return _with_token(updated, token)

    async def revoke(self, source_id: str) -> SourceView:
        now = self._now()
        updated = await self._store.update(
            source_id,
            {
                "token_hash": None,
                "token_last4": None,
                "revoked_at": now,
                "updated_at": now,
            },
            live_only=True,
        )
        if updated is None:
            # Already revoked: report it as it is.
            current = await self._store.get(source_id)
            if current is None:
                raise SourceNotFound
            return _view(current)
        return _view(updated)

    async def resolve(self, token: str) -> SourceIdentity | None:
        """The source of a current token; ``None`` for any other value."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        token_hash = _digest(token)
        record = await self._store.get_by_token_hash(token_hash)
        if (
            record is None
            or not record.has_token
            or record.token_hash is None
            or not hmac.compare_digest(record.token_hash, token_hash)
        ):
            return None
        now = self._now()
        if record.last_used_at is None or now - record.last_used_at >= _TOUCH_INTERVAL:
            try:
                await self._store.touch(record.source_id, now)
            except Exception as exc:
                # The time is only shown to the owner; the request goes on.
                logger.emit(
                    E.SOURCE_LAST_USE_WRITE_FAILED, exc=exc, source_id=record.source_id
                )
        return record.identity()

    async def identities(self, source_ids: list[str]) -> dict[str, SourceIdentity]:
        wanted = list(dict.fromkeys(source_ids))
        found = {
            record.source_id: record.identity()
            for record in await self._store.list_by_ids(
                [i for i in wanted if i != NO_TOKEN_SOURCE_ID]
            )
        }
        if NO_TOKEN_SOURCE_ID in wanted:
            found[NO_TOKEN_SOURCE_ID] = NO_TOKEN
        return found

    async def token_required(self) -> bool:
        """Whether a Source API request needs a token: the owner's switch.

        A switch that was never created requires one, so a failed startup
        never leaves the Source API open. The deprecated Clerk mode always
        requires one, whatever was stored before it was set.
        """
        if self._auth_mode == "clerk":
            return True
        requirement = await self._store.get_requirement()
        return requirement is None or requirement.required

    async def token_requirement(self) -> TokenRequirement:
        requirement = await self._store.get_requirement()
        if requirement is None:
            raise TokenRequirementUnset
        if self._auth_mode == "clerk":
            return requirement.model_copy(update={"required": True})
        return requirement

    async def set_token_required(self, required: bool) -> TokenRequirement:
        if self._auth_mode == "clerk" and not required:
            raise TokenRequirementLocked
        updated = await self._store.set_requirement(required, self._now())
        if updated is None:
            raise TokenRequirementUnset
        return updated

    async def seed_token_requirement(
        self, *, existing_install: bool
    ) -> TokenRequirement:
        """Create the switch once, at backend startup, before any request.

        It starts on for an install with no data yet, and off for one that
        already has some, so its connected programs keep working. It always
        starts on in Clerk mode, which required a credential before. A switch
        that exists is returned unchanged.
        """
        required = self._auth_mode == "clerk" or not existing_install
        return await self._store.seed_requirement(
            TokenRequirement(
                required=required, started_off=not required, updated_at=self._now()
            )
        )


__all__ = ["SourceService"]
