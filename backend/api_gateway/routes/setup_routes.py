"""Browser model-provider setup; behavior lives in ``llm_gateway.setup_api``."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api_gateway.registry import mark_declared_owner
from common.auth import ClerkUser, get_current_user
from common.config.runtime_config import AuthMethod, ProviderId
from llm_gateway.setup_api import (
    ApiKeySetup,
    ImageSizeSaved,
    ImageSizeUpdate,
    OAuthComplete,
    OAuthLoginView,
    OAuthModels,
    SetupAccess,
    SetupErrorBody,
    SetupModels,
    SetupRequestError,
    SetupSaved,
    SetupStatus,
)

_ERRORS = {
    status: {"model": SetupErrorBody} for status in (400, 403, 404, 409, 422, 503)
}


class _SetupRoute(APIRoute):
    """Validation failures without FastAPI's echoed ``input``.

    Request bodies carry API keys and authorization codes, and the default 422
    body repeats the rejected input, including the whole body when a field is
    missing.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                body = SetupErrorBody(
                    detail="Invalid setup request; check the fields and try again.",
                    code="invalid_request",
                )
                return JSONResponse(body.model_dump(exclude_none=True), status_code=422)

        return handle


router = APIRouter(prefix="/setup", responses=_ERRORS, route_class=_SetupRoute)


def _setup_access(request: Request) -> SetupAccess:
    setup = getattr(request.app.state, "setup_access", None)
    if setup is None:
        raise SetupRequestError("setup_unavailable", 503, "Setup is unavailable.")
    return setup


def get_setup_reader(
    request: Request, _user: Annotated[ClerkUser, Depends(get_current_user)]
) -> SetupAccess:
    """The setup owner, for any signed-in user reading the status.

    The status never carries credentials, and every user's banners and composer
    gating depend on it.
    """
    return _setup_access(request)


def get_setup(
    request: Request, user: Annotated[ClerkUser, Depends(get_current_user)]
) -> SetupAccess:
    """The setup owner, for users allowed to change the model provider."""
    setup = _setup_access(request)
    if not setup.can_configure(user.user_id):
        raise SetupRequestError(
            "forbidden", 403, "Only administrators can change the model provider."
        )
    return setup


SetupDependency = Annotated[SetupAccess, Depends(get_setup)]
SetupReaderDependency = Annotated[SetupAccess, Depends(get_setup_reader)]


@router.get("/status")
async def setup_status(setup: SetupReaderDependency) -> SetupStatus:
    return await setup.status()


@router.get("/models")
async def setup_models(
    setup: SetupDependency,
    provider: Annotated[ProviderId, Query()],
    auth: Annotated[AuthMethod, Query()],
) -> SetupModels:
    return setup.models(provider, auth)


@router.post("/api-key")
async def setup_api_key(body: ApiKeySetup, setup: SetupDependency) -> SetupSaved:
    return await setup.save_api_key(body)


@router.post("/oauth/start")
async def setup_oauth_start(setup: SetupDependency) -> OAuthLoginView:
    return await setup.start_oauth()


@router.post("/oauth/complete")
async def setup_oauth_complete(
    body: OAuthComplete, setup: SetupDependency
) -> OAuthLoginView:
    return await setup.complete_oauth(body)


@router.post("/oauth/models")
async def setup_oauth_models(body: OAuthModels, setup: SetupDependency) -> SetupSaved:
    return await setup.save_oauth_models(body)


@router.get("/oauth/{login_id}")
async def setup_oauth_login(login_id: str, setup: SetupDependency) -> OAuthLoginView:
    return await setup.oauth_login(login_id)


@router.delete("/oauth/{login_id}", status_code=204)
async def setup_oauth_cancel(login_id: str, setup: SetupDependency) -> Response:
    await setup.cancel_oauth(login_id)
    return Response(status_code=204)


@router.put("/image-size")
async def setup_image_size(
    body: ImageSizeUpdate, setup: SetupDependency
) -> ImageSizeSaved:
    return await setup.save_image_size(body)


mark_declared_owner(router, "api_gateway.routes.setup_routes")
