"""Instance settings; behavior lives in ``instance_settings.service``.

Every route is admin-only, including the read: the view shows deployment
details and which keys config.json sets.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api_gateway.registry import mark_declared_owner
from common.auth import ClerkUser, get_current_user
from instance_settings.models import (
    RestartAccepted,
    SettingsErrorBody,
    SettingsSaved,
    SettingsUpdate,
    SettingsView,
)
from instance_settings.protocols import SettingsAccess, SettingsRequestError

_ERRORS = {status: {"model": SettingsErrorBody} for status in (403, 409, 422, 503)}


class _SettingsRoute(APIRoute):
    """Validation failures without FastAPI's echoed ``input``."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                body = SettingsErrorBody(
                    detail="Invalid settings request; check the fields and try again.",
                    code="invalid_request",
                )
                return JSONResponse(body.model_dump(), status_code=422)

        return handle


router = APIRouter(prefix="/settings", responses=_ERRORS, route_class=_SettingsRoute)


def get_instance_settings(
    request: Request, user: Annotated[ClerkUser, Depends(get_current_user)]
) -> SettingsAccess:
    """The settings owner, for users allowed to configure this instance."""
    settings = getattr(request.app.state, "instance_settings", None)
    if settings is None:
        raise SettingsRequestError(
            "settings_unavailable", 503, "Settings are unavailable."
        )
    if not settings.can_configure(user.user_id):
        raise SettingsRequestError(
            "forbidden", 403, "Only administrators can change settings."
        )
    return settings


SettingsDependency = Annotated[SettingsAccess, Depends(get_instance_settings)]


@router.get("")
async def read_settings(settings: SettingsDependency) -> SettingsView:
    return await settings.view()


@router.put("")
async def update_settings(
    body: SettingsUpdate, settings: SettingsDependency
) -> SettingsSaved:
    return await settings.update(body)


@router.post("/restart", status_code=202)
async def restart_services(
    settings: SettingsDependency, background: BackgroundTasks
) -> RestartAccepted:
    """Restart the backend, frontend, MCP and agents; Caddy and MongoDB stay up."""
    accepted = await settings.restart()
    # After the response is sent: the restart stops this backend too.
    background.add_task(settings.launch_restart)
    return accepted


mark_declared_owner(router, "api_gateway.routes.settings_routes")
