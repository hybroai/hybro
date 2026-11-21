"""The owner's sources and their tokens; behaviour lives in ``client``.

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
    ClientNotFound,
    ClientRegistry,
    ClientRevoked,
    SourceCreate,
    SourceList,
    SourceRename,
    SourceView,
    SourceWithToken,
    TokenRequirement,
    TokenRequirementChange,
    TokenRequirementLocked,
    TokenRequirementUnset,
)

router = APIRouter(prefix="/sources", route_class=QuietValidationRoute)

_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages sources"},
    503: {"description": "The owner is not known yet"},
    404: {"description": "No source has this id"},
    409: {"description": "The source is revoked"},
}
_REQUIREMENT_READ_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages sources"},
    503: {"description": "The owner is not known yet, or the switch is not set up"},
}
_REQUIREMENT_CHANGE_ERRORS = {
    **_REQUIREMENT_READ_ERRORS,
    409: {"description": "Clerk mode always requires a source token"},
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
        _refuse(409, "source_revoked", "This source is revoked.")
    _refuse(404, "source_not_found", "No source has this id.")


@router.get("", responses=_ERRORS)
async def list_sources(sources: OwnerClientsDependency) -> SourceList:
    """List every source, oldest first. Tokens are never returned here."""
    return await sources.list_clients()


@router.get("/token-requirement", responses=_REQUIREMENT_READ_ERRORS)
async def get_token_requirement(sources: OwnerClientsDependency) -> TokenRequirement:
    """Whether a Source API request needs a source token."""
    try:
        return await sources.token_requirement()
    except TokenRequirementUnset:
        refuse_unset_requirement()


@router.put("/token-requirement", responses=_REQUIREMENT_CHANGE_ERRORS)
async def set_token_requirement(
    body: TokenRequirementChange, sources: OwnerClientsDependency
) -> TokenRequirement:
    """Turn the requirement on or off; it applies to the next request."""
    try:
        return await sources.set_token_required(body.required)
    except TokenRequirementUnset:
        refuse_unset_requirement()
    except TokenRequirementLocked:
        _refuse(
            409,
            "token_required_by_auth_mode",
            "backend.auth_mode is clerk, which always requires a source token.",
        )


@router.post("", status_code=201, responses=_ERRORS)
async def create_source(
    body: SourceCreate, sources: OwnerClientsDependency
) -> SourceWithToken:
    """Create a source. Its token is in this response only."""
    return await sources.create(body.name)


@router.patch("/{source_id}", responses=_ERRORS)
async def rename_source(
    source_id: str, body: SourceRename, sources: OwnerClientsDependency
) -> SourceView:
    try:
        return await sources.rename(source_id, body.name)
    except ClientNotFound as exc:
        _missing(exc)


@router.post("/{source_id}/rotate", responses=_ERRORS)
async def rotate_source_token(
    source_id: str, sources: OwnerClientsDependency
) -> SourceWithToken:
    """Replace the token. The old one stops at once; the sessions stay."""
    try:
        return await sources.rotate(source_id)
    except (ClientNotFound, ClientRevoked) as exc:
        _missing(exc)


@router.post("/{source_id}/revoke", responses=_ERRORS)
async def revoke_source(source_id: str, sources: OwnerClientsDependency) -> SourceView:
    """Stop the token for good. The source and its sessions stay listed."""
    try:
        return await sources.revoke(source_id)
    except ClientNotFound as exc:
        _missing(exc)


mark_declared_owner(router, "api_gateway.routes.source_routes")
