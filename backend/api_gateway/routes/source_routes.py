"""The owner's sources and their tokens; behaviour lives in ``source``.

Every route takes the owner's credential and refuses any other user. A token
is returned once, by create and rotate, and never again.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from api_gateway.errors import QuietValidationRoute
from api_gateway.registry import mark_declared_owner
from api_gateway.source_token import (
    SignedInOwnerDependency,
    SourceRegistryDependency,
)
from source.protocols import (
    SourceCreate,
    SourceList,
    SourceNotFound,
    SourceRegistry,
    SourceRename,
    SourceRevoked,
    SourceView,
    SourceWithToken,
)

router = APIRouter(prefix="/sources", route_class=QuietValidationRoute)

_ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Only the owner manages sources"},
    503: {"description": "The owner is not known yet"},
    404: {"description": "No source has this id"},
    409: {"description": "The source is revoked"},
}


def _refuse(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status, detail={"code": code, "message": message})


async def get_owner_sources(
    owner: SignedInOwnerDependency, sources: SourceRegistryDependency
) -> SourceRegistry:
    """The sources, for the owner only: a source token acts as the owner."""
    return sources


OwnerSourcesDependency = Annotated[SourceRegistry, Depends(get_owner_sources)]


def _missing(exc: Exception) -> NoReturn:
    if isinstance(exc, SourceRevoked):
        _refuse(409, "source_revoked", "This source is revoked.")
    _refuse(404, "source_not_found", "No source has this id.")


@router.get("", responses=_ERRORS)
async def list_sources(sources: OwnerSourcesDependency) -> SourceList:
    """List every source, oldest first. Tokens are never returned here."""
    return await sources.list_sources()


@router.post("", status_code=201, responses=_ERRORS)
async def create_source(
    body: SourceCreate, sources: OwnerSourcesDependency
) -> SourceWithToken:
    """Create a source. Its token is in this response only."""
    return await sources.create(body.name)


@router.patch("/{source_id}", responses=_ERRORS)
async def rename_source(
    source_id: str, body: SourceRename, sources: OwnerSourcesDependency
) -> SourceView:
    try:
        return await sources.rename(source_id, body.name)
    except SourceNotFound as exc:
        _missing(exc)


@router.post("/{source_id}/rotate", responses=_ERRORS)
async def rotate_source_token(
    source_id: str, sources: OwnerSourcesDependency
) -> SourceWithToken:
    """Replace the token. The old one stops at once; the sessions stay."""
    try:
        return await sources.rotate(source_id)
    except (SourceNotFound, SourceRevoked) as exc:
        _missing(exc)


@router.post("/{source_id}/revoke", responses=_ERRORS)
async def revoke_source(source_id: str, sources: OwnerSourcesDependency) -> SourceView:
    """Stop the token for good. The source and its sessions stay listed."""
    try:
        return await sources.revoke(source_id)
    except SourceNotFound as exc:
        _missing(exc)


mark_declared_owner(router, "api_gateway.routes.source_routes")
