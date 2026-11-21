"""The client-token check on the Client API.

Each Client API route takes ``get_request_client``. It resolves which client
sent the request and whose data it acts on:

- A request with ``Authorization: Bearer <token>`` is its client when the
  token is current. Any other ``Authorization`` value is refused, also while
  no token is required, so a revoked token never falls back to "No token".
- A request without the header is refused while a token is required (the
  owner's switch, stored by ``client``, read on each request). Else it
  is "No token", except when it carries an ``Origin`` that is not Hybro's own
  address: that is a web page making a browser send it, and it is refused.
- The owner it acts for is the owner account's identity
  (``OwnerAccount.acting_owner``). Until the account exists every request is
  refused, so nothing is recorded for an owner who does not exist yet.

The owner's routes never take this dependency, and a client token opens
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, NoReturn

from fastapi import Depends, HTTPException, Request

from account.protocols import NoAccount, OwnerAccount
from api_gateway.cross_site import from_own_address
from api_gateway.owner_session import OwnerAccountDependency, get_current_user
from client.protocols import NO_TOKEN, ClientIdentity, ClientRegistry, Via
from common.auth import ClerkUser
from session.protocols import SessionOwner

# The MCP server sends this header with every backend call. Caddy removes it
# from requests that come in through the public port, so only a call from
# inside the image carries it. It decides nothing; the owner sees it as how a
# client connects.
VIA_HEADER = "x-hybro-via"

UNKNOWN_TOKEN_MESSAGE = (
    "The Authorization header does not carry a current source token of this "
    "Hybro: the token is unknown, revoked, or replaced by a rotation. Send a "
    "current source token, or no Authorization header."
)


@dataclass(frozen=True, slots=True)
class ClientCaller:
    """The client of one Client API request, and the owner it acts for."""

    client: ClientIdentity
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
    except NoAccount:
        _refuse(
            503,
            "no_account",
            "This Hybro has no owner account yet. Run `hybro` on its machine "
            "for the setup link, or `hybro owner create`.",
        )
    return SessionOwner(user_id=owner.user_id, name=owner.name)


async def get_signed_in_owner(
    user: Annotated[ClerkUser, Depends(get_current_user)],
) -> SessionOwner:
    """The signed-in owner. Clients act for the same identity, so the owner's
    routes see their data."""
    return SessionOwner(
        user_id=user.user_id, name=user.claims.get("name") or user.user_id
    )


SignedInOwnerDependency = Annotated[SessionOwner, Depends(get_signed_in_owner)]


def get_client_registry(request: Request) -> ClientRegistry:
    clients: ClientRegistry | None = getattr(request.app.state, "client_registry", None)
    if clients is None:
        _refuse(503, "sources_unavailable", "Sources are unavailable.")
    return clients


ClientRegistryDependency = Annotated[ClientRegistry, Depends(get_client_registry)]


def refuse_unset_requirement() -> NoReturn:
    """The switch is created at startup; without it nothing can read or change
    it, and the Client API requires a token."""
    _refuse(
        503,
        "token_requirement_unavailable",
        "The token requirement is not set up yet; restart Hybro.",
    )


def _bearer_token(header: str) -> str | None:
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def get_request_client(
    request: Request,
    clients: ClientRegistryDependency,
    account: OwnerAccountDependency,
) -> ClientCaller:
    header = request.headers.get("authorization")
    if header is not None:
        token = _bearer_token(header)
        via: Via = "mcp" if request.headers.get(VIA_HEADER) == "mcp" else "api"
        client = await clients.resolve(token, via=via) if token else None
        if client is None:
            _refuse(401, "unknown_source_token", UNKNOWN_TOKEN_MESSAGE, challenge=True)
    elif await clients.token_required():
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
        client = NO_TOKEN
    return ClientCaller(client=client, owner=await _acting_owner(account))


RequestClientDependency = Annotated[ClientCaller, Depends(get_request_client)]


__all__ = [
    "RequestClientDependency",
    "SignedInOwnerDependency",
    "ClientCaller",
    "ClientRegistryDependency",
    "UNKNOWN_TOKEN_MESSAGE",
    "VIA_HEADER",
    "get_request_client",
    "get_signed_in_owner",
    "get_client_registry",
    "refuse_unset_requirement",
]
