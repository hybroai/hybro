"""Shared API Gateway error mapping helpers."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from instance_settings import SettingsRequestError
from llm_gateway.errors import ProviderNotConfigured
from llm_gateway.setup_api import SetupRequestError

SESSION_CONVERSATION_READ_ONLY = (
    "This conversation belongs to a session and is read-only in chat"
)


def require_chat_room(room: object) -> None:
    """Refuse a chat write on a conversation container owned by Session.

    Chat read routes stay open so the owner's session page can render the
    record; chat writes (send, membership, history edits, cancel, upload) are
    refused.
    """
    owner_product = getattr(room, "owner_product", "chat")
    if isinstance(owner_product, str) and owner_product != "chat":
        raise HTTPException(status_code=409, detail=SESSION_CONVERSATION_READ_ONLY)


async def setup_request_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A coded browser-setup failure; the body never carries credentials."""
    assert isinstance(exc, SetupRequestError)
    return JSONResponse(
        exc.body().model_dump(exclude_none=True), status_code=exc.status
    )


async def settings_request_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """A coded settings failure; field errors never echo submitted values."""
    assert isinstance(exc, SettingsRequestError)
    return JSONResponse(exc.body().model_dump(), status_code=exc.status)


async def provider_not_configured_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """A model call without a working Provider pair: 409 with the state's code."""
    assert isinstance(exc, ProviderNotConfigured)
    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)


class QuietValidationRoute(APIRoute):
    """Validation failures without FastAPI's echoed ``input``.

    The default 422 body repeats the rejected input, and these bodies carry
    passwords and setup codes.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    {
                        "detail": {
                            "code": "invalid_request",
                            "message": "Invalid request; check the fields.",
                        }
                    },
                    status_code=422,
                )

        return handle
