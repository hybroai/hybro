"""The clients of an instance and their tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from client.models import (
    NO_TOKEN,
    NO_TOKEN_CLIENT_ID,
    TOKEN_PREFIX,
    ClientIdentity,
    ClientList,
    ClientRecord,
    ClientView,
    ClientWithToken,
    TokenRequirement,
    Via,
)
from client.protocols import (
    ClientNotFound,
    ClientRevoked,
    ClientStore,
    TokenRequirementLocked,
    TokenRequirementUnset,
)
from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow

logger = get_logger(__name__)

# Last use is shown to the owner; one write a minute per client is enough.
_TOUCH_INTERVAL = timedelta(minutes=1)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _view(record: ClientRecord) -> ClientView:
    return ClientView(
        client_id=record.client_id,
        name=record.name,
        has_token=record.has_token,
        token_last4=record.token_last4 if record.has_token else None,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        last_used_via=record.last_used_via,
        revoked_at=record.revoked_at,
    )


def _with_token(record: ClientRecord, token: str) -> ClientWithToken:
    return ClientWithToken(**_view(record).model_dump(), token=token)


class ClientService:
    def __init__(
        self,
        store: ClientStore,
        *,
        auth_mode: Literal["mock", "clerk"],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._auth_mode = auth_mode
        self._now = now

    async def create(self, name: str) -> ClientWithToken:
        token = _new_token()
        now = self._now()
        record = ClientRecord(
            client_id="client_" + secrets.token_hex(12),
            name=name,
            token_hash=_digest(token),
            token_last4=token[-4:],
            created_at=now,
            updated_at=now,
        )
        await self._store.insert(record)
        return _with_token(record, token)

    async def list_clients(self) -> ClientList:
        return ClientList(clients=[_view(r) for r in await self._store.list_all()])

    async def rename(self, client_id: str, name: str) -> ClientView:
        updated = await self._store.update(
            client_id, {"name": name, "updated_at": self._now()}
        )
        if updated is None:
            raise ClientNotFound
        return _view(updated)

    async def rotate(self, client_id: str) -> ClientWithToken:
        token = _new_token()
        updated = await self._store.update(
            client_id,
            {
                "token_hash": _digest(token),
                "token_last4": token[-4:],
                "updated_at": self._now(),
            },
            live_only=True,
        )
        if updated is None:
            if await self._store.get(client_id) is None:
                raise ClientNotFound
            raise ClientRevoked
        return _with_token(updated, token)

    async def revoke(self, client_id: str) -> ClientView:
        now = self._now()
        updated = await self._store.update(
            client_id,
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
            current = await self._store.get(client_id)
            if current is None:
                raise ClientNotFound
            return _view(current)
        return _view(updated)

    async def resolve(self, token: str, *, via: Via = "api") -> ClientIdentity | None:
        """The client of a current token; ``None`` for any other value.

        Its last use and how it connected (``via``) are recorded for the owner.
        """
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
        if (
            record.last_used_at is None
            or now - record.last_used_at >= _TOUCH_INTERVAL
            or record.last_used_via != via
        ):
            try:
                await self._store.touch(record.client_id, now, via)
            except Exception as exc:
                # The time is only shown to the owner; the request goes on.
                logger.emit(
                    E.CLIENT_LAST_USE_WRITE_FAILED, exc=exc, client_id=record.client_id
                )
        return record.identity()

    async def identities(self, client_ids: list[str]) -> dict[str, ClientIdentity]:
        wanted = list(dict.fromkeys(client_ids))
        found = {
            record.client_id: record.identity()
            for record in await self._store.list_by_ids(
                [i for i in wanted if i != NO_TOKEN_CLIENT_ID]
            )
        }
        if NO_TOKEN_CLIENT_ID in wanted:
            found[NO_TOKEN_CLIENT_ID] = NO_TOKEN
        return found

    async def token_required(self) -> bool:
        """Whether a Client API request needs a token: the owner's switch.

        A switch that was never created requires one, so a failed startup
        never leaves the Client API open. The deprecated Clerk mode always
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


__all__ = ["ClientService"]
