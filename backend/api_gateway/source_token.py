"""The source-token check on the Source API.

Each Source API route takes ``get_request_source``. It resolves which source
sent the request and whose data it acts on:

- A request with ``Authorization: Bearer <token>`` is its source when the
  token is current. Any other ``Authorization`` value is refused, also while
  no token is required, so a revoked token never falls back to "No token".
- A request without the header is refused while a token is required. Else it
  is "No token", except when it carries an ``Origin`` that is not Hybro's own
  address: that is a web page making a browser send it, and it is refused.
- The owner it acts for is the owner account's identity, or the identity the
  account will take (``OwnerAccount.acting_owner``).

The owner's routes never take this dependency, and a source token opens
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, NoReturn

from fastapi import Depends, HTTPException, Request

from account.protocols import OwnerAccount, OwnerIdRequired
from api_gateway.cross_site import from_own_address
from api_gateway.owner_session import OwnerAccountDependency
from common.auth import ClerkUser, get_current_user
from session.protocols import SessionOwner
from source.protocols import NO_TOKEN, SourceIdentity, SourceRegistry

UNKNOWN_TOKEN_MESSAGE = (
    "The Authorization header does not carry a current source token of this "
    "Hybro: the token is unknown, revoked, or replaced by a rotation. Send a "
    "current source token, or no Authorization header."
)


@dataclass(frozen=True, slots=True)
class SourceCaller:
    """The source of one Source API request, and the owner it acts for."""

    source: SourceIdentity
    owner: SessionOwner


def _refuse(
    status: int, code: str, message: str, *, challenge: bool = False
) -> NoReturn:
    raise HTTPException(
        status,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"} if challenge else None,
    )


async def _acting_owner(account: OwnerAccount) -> SessionOwner:
    try:
        owner = await account.acting_owner()
    except OwnerIdRequired:
        _refuse(
            503,
            "owner_unknown",
            "Hybro cannot tell which administrator owns this instance yet. "
            "Create the owner account first.",
        )
    return SessionOwner(user_id=owner.user_id, name=owner.name)


async def get_signed_in_owner(
    user: Annotated[ClerkUser, Depends(get_current_user)],
    account: OwnerAccountDependency,
) -> SessionOwner:
    """The owner, for a signed-in user who is the owner; anyone else is 403.

    Sources act for the same identity, so the owner's routes see their data.
    """
    owner = await _acting_owner(account)
    if user.user_id != owner.user_id:
        _refuse(403, "forbidden", "Only the owner can do this.")
    return owner


SignedInOwnerDependency = Annotated[SessionOwner, Depends(get_signed_in_owner)]


def get_source_registry(request: Request) -> SourceRegistry:
    sources: SourceRegistry | None = getattr(request.app.state, "source_registry", None)
    if sources is None:
        _refuse(503, "sources_unavailable", "Sources are unavailable.")
    return sources


SourceRegistryDependency = Annotated[SourceRegistry, Depends(get_source_registry)]


def _bearer_token(header: str) -> str | None:
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def get_request_source(
    request: Request,
    sources: SourceRegistryDependency,
    account: OwnerAccountDependency,
) -> SourceCaller:
    header = request.headers.get("authorization")
    if header is not None:
        token = _bearer_token(header)
        source = await sources.resolve(token) if token else None
        if source is None:
            _refuse(401, "unknown_source_token", UNKNOWN_TOKEN_MESSAGE, challenge=True)
    elif await sources.token_required():
        _refuse(
            401,
            "source_token_required",
            "This Hybro requires a source token: send it as "
            "Authorization: Bearer <token>.",
            challenge=True,
        )
    elif "origin" in request.headers and not from_own_address(request):
        _refuse(
            403,
            "cross_site_request",
            "A request without a source token must not come from another website.",
        )
    else:
        source = NO_TOKEN
    return SourceCaller(source=source, owner=await _acting_owner(account))


RequestSourceDependency = Annotated[SourceCaller, Depends(get_request_source)]


__all__ = [
    "RequestSourceDependency",
    "SignedInOwnerDependency",
    "SourceCaller",
    "SourceRegistryDependency",
    "UNKNOWN_TOKEN_MESSAGE",
    "get_request_source",
    "get_signed_in_owner",
    "get_source_registry",
]
