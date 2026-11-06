"""Read-only diagnostics; behaviour lives in ``diagnostics.service``.

``hybro doctor`` authenticates with the per-start token in the
``X-Hybro-Diagnostics-Token`` header. The header is read from the request, not
declared, so the schema does not advertise it; it is not ``Authorization``, so
Clerk verification never sees it. Everyone else needs a signed-in user who may
read diagnostics (an administrator, or anyone in mock mode).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime

from api_gateway.registry import mark_declared_owner
from common.auth import ClerkUser, get_optional_user
from diagnostics.protocols import (
    ID_PATTERN,
    MAX_EVENTS,
    MAX_LIMIT,
    DiagnosticPage,
    DiagnosticsAccess,
    DiagnosticsSnapshot,
    InvalidCursor,
    LevelName,
    LogSource,
    RecordQuery,
    StoreUnavailable,
)

TOKEN_HEADER = "X-Hybro-Diagnostics-Token"

router = APIRouter(prefix="/diagnostics")


def _unavailable(code: str, message: str) -> HTTPException:
    return HTTPException(503, detail={"code": code, "message": message})


def get_diagnostics(
    request: Request,
    user: Annotated[ClerkUser | None, Depends(get_optional_user)],
) -> DiagnosticsAccess:
    access: DiagnosticsAccess | None = getattr(
        request.app.state, "diagnostics_access", None
    )
    if access is None:
        raise _unavailable("diagnostics_unavailable", "Diagnostics are unavailable.")
    token = request.headers.get(TOKEN_HEADER)
    if token is not None and access.token_matches(token):
        return access
    if user is None:
        raise HTTPException(401, "Sign in to read diagnostics.")
    if not access.can_read(user.user_id):
        raise HTTPException(403, "Only administrators can read diagnostics.")
    return access


DiagnosticsDependency = Annotated[DiagnosticsAccess, Depends(get_diagnostics)]
ServiceQuery = Annotated[str | None, Query()]
LevelQuery = Annotated[LevelName | None, Query()]
EventQuery = Annotated[list[str] | None, Query(max_length=MAX_EVENTS)]
IdQuery = Annotated[str | None, Query(pattern=ID_PATTERN)]
TimeQuery = Annotated[AwareDatetime | None, Query()]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
CursorQuery = Annotated[str | None, Query()]
SourceQuery = Annotated[LogSource, Query()]


async def _page(read: Awaitable[DiagnosticPage]) -> DiagnosticPage:
    try:
        return await read
    except StoreUnavailable as exc:
        raise _unavailable(
            "store_unavailable", "The diagnostics store is unavailable."
        ) from exc
    except InvalidCursor as exc:
        raise HTTPException(422, "Invalid cursor.") from exc


@router.get("/snapshot")
async def diagnostics_snapshot(access: DiagnosticsDependency) -> DiagnosticsSnapshot:
    return await access.snapshot()


@router.get("/events")
async def diagnostics_events(
    access: DiagnosticsDependency,
    service: ServiceQuery = None,
    level: LevelQuery = None,
    event: EventQuery = None,
    trace_id: IdQuery = None,
    instance_id: IdQuery = None,
    since: TimeQuery = None,
    until: TimeQuery = None,
    limit: LimitQuery = 100,
    before: CursorQuery = None,
) -> DiagnosticPage:
    query = RecordQuery(
        service=service,
        level=level,
        event=tuple(event or ()),
        trace_id=trace_id,
        instance_id=instance_id,
        since=since,
        until=until,
        limit=limit,
        before=before,
    )
    return await _page(access.events(query))


@router.get("/logs")
async def diagnostics_logs(
    access: DiagnosticsDependency,
    service: ServiceQuery = None,
    level: LevelQuery = None,
    event: EventQuery = None,
    trace_id: IdQuery = None,
    instance_id: IdQuery = None,
    since: TimeQuery = None,
    until: TimeQuery = None,
    limit: LimitQuery = 100,
    before: CursorQuery = None,
    source: SourceQuery = "store",
) -> DiagnosticPage:
    query = RecordQuery(
        service=service,
        level=level,
        event=tuple(event or ()),
        trace_id=trace_id,
        instance_id=instance_id,
        since=since,
        until=until,
        limit=limit,
        before=before,
    )
    return await _page(access.logs(query, source=source))


mark_declared_owner(router, "api_gateway.routes.diagnostics_routes")
