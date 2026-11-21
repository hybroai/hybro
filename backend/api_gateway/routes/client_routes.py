"""The owner's clients and their tokens; behaviour lives in ``client``.

Every route takes the owner's credential and refuses any other user. A token
is returned once, by create and rotate, and never again.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from api_gateway.client_token import (
    ClientRegistryDependency,
    SignedInOwnerDependency,
    refuse_unset_requirement,
)
from api_gateway.errors import QuietValidationRoute
from api_gateway.registry import mark_declared_owner
from client.protocols import (
    ClientCreate,
    ClientList,
    ClientNotFound,
    ClientRegistry,
    ClientRename,
    ClientRevoked,
    ClientView,
    ClientWithToken,
    TokenRequirement,
    TokenRequirementChange,
    TokenRequirementLocked,
    TokenRequirementUnset,
)

router = APIRouter(prefix="/clients", route_class=QuietValidationRoute)

_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages clients"},
    503: {"description": "The owner is not known yet"},
    404: {"description": "No client has this id"},
    409: {"description": "The client is revoked"},
}
_REQUIREMENT_READ_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages clients"},
    503: {"description": "The owner is not known yet, or the switch is not set up"},
}
_REQUIREMENT_CHANGE_ERRORS = {
    **_REQUIREMENT_READ_ERRORS,
    409: {"description": "Clerk mode always requires a client token"},
}


def _refuse(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status, detail={"code": code, "message": message})


async def get_owner_clients(
    owner: SignedInOwnerDependency, clients: ClientRegistryDependency
) -> ClientRegistry:
    """Owner only; a client token cannot call these routes."""
    return clients


OwnerClientsDependency = Annotated[ClientRegistry, Depends(get_owner_clients)]


def _missing(exc: Exception) -> NoReturn:
    if isinstance(exc, ClientRevoked):
        _refuse(409, "client_revoked", "This client is revoked.")
    _refuse(404, "client_not_found", "No client has this id.")


@router.get("", responses=_ERRORS)
async def list_clients(clients: OwnerClientsDependency) -> ClientList:
    """List every client, oldest first. Tokens are never returned here."""
    return await clients.list_clients()


@router.get("/token-requirement", responses=_REQUIREMENT_READ_ERRORS)
async def get_token_requirement(clients: OwnerClientsDependency) -> TokenRequirement:
    """Whether a Client API request needs a client token."""
    try:
        return await clients.token_requirement()
    except TokenRequirementUnset:
        refuse_unset_requirement()


@router.put("/token-requirement", responses=_REQUIREMENT_CHANGE_ERRORS)
async def set_token_requirement(
    body: TokenRequirementChange, clients: OwnerClientsDependency
) -> TokenRequirement:
    """Turn the requirement on or off; it applies to the next request."""
    try:
        return await clients.set_token_required(body.required)
    except TokenRequirementUnset:
        refuse_unset_requirement()
    except TokenRequirementLocked:
        _refuse(
            409,
            "token_required_by_auth_mode",
            "backend.auth_mode is clerk, which always requires a client token.",
        )


@router.post("", status_code=201, responses=_ERRORS)
async def create_client(
    body: ClientCreate, clients: OwnerClientsDependency
) -> ClientWithToken:
    """Create a client. Its token is in this response only."""
    return await clients.create(body.name)


@router.patch("/{client_id}", responses=_ERRORS)
async def rename_client(
    client_id: str, body: ClientRename, clients: OwnerClientsDependency
) -> ClientView:
    try:
        return await clients.rename(client_id, body.name)
    except ClientNotFound as exc:
        _missing(exc)


@router.post("/{client_id}/rotate", responses=_ERRORS)
async def rotate_client_token(
    client_id: str, clients: OwnerClientsDependency
) -> ClientWithToken:
    """Replace the token. The old one stops at once; the sessions stay."""
    try:
        return await clients.rotate(client_id)
    except (ClientNotFound, ClientRevoked) as exc:
        _missing(exc)


@router.post("/{client_id}/revoke", responses=_ERRORS)
async def revoke_client(client_id: str, clients: OwnerClientsDependency) -> ClientView:
    """Stop the token for good. The client and its sessions stay listed."""
    try:
        return await clients.revoke(client_id)
    except ClientNotFound as exc:
        _missing(exc)


mark_declared_owner(router, "api_gateway.routes.client_routes")
