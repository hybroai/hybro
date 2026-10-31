"""Authenticated OpenAI-compatible ingress for bundled agents, not an open relay."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, ValidationError

from api_gateway.registry import mark_declared_owner
from llm_gateway._diagnostics import _exception_diagnostic
from llm_gateway.agent_proxy import (
    ChatRequest,
    ProxyInput,
    _AgentLLMProxyPort,
    completion,
    turn_request,
)
from llm_gateway.errors import LLMProviderConfigurationError, ProviderNotConfigured
from llm_gateway.image_types import (
    MAX_IMAGE_BYTES,
    GatewayImage,
    GatewayImageRequest,
    ImageContractError,
)


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "code": code,
            }
        },
        status_code=status,
    )


def _not_configured(exc: ProviderNotConfigured) -> JSONResponse:
    """The same code the API reports, so an agent can name the missing setup."""
    return _error(str(exc), exc.code, 409)


class _PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                # Authenticate before FastAPI buffers JSON or parses multipart.
                get_proxy(request, await _bearer(request))
                return await original(request)
            except (RequestValidationError, ValidationError, ImageContractError):
                # FastAPI's default validation response includes raw input.
                return _error(
                    "Invalid or unsupported proxy request.", "invalid_request", 400
                )
            except ProviderNotConfigured as exc:
                return _not_configured(exc)
            except LLMProviderConfigurationError:
                return _error(
                    "Configure the required gateway model in Settings, Model provider.",
                    "not_configured",
                    503,
                )

        return handle


_bearer = HTTPBearer(auto_error=False)


def get_proxy(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> _AgentLLMProxyPort:
    proxy = getattr(request.app.state, "agent_llm_proxy", None)
    if proxy is None or len(proxy.token.get_secret_value().encode()) < 32:
        raise HTTPException(503, "Internal LLM proxy is not configured")
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode(), proxy.token.get_secret_value().encode()
    ):
        raise HTTPException(401, "Invalid internal LLM credential")
    if proxy.slots.locked():
        raise HTTPException(429, "Internal LLM proxy is busy")
    return proxy


ProxyDependency = Annotated[_AgentLLMProxyPort, Depends(get_proxy)]
router = APIRouter(
    prefix="/internal/llm",
    route_class=_PrivateRoute,
    responses={
        "default": {
            "description": (
                "Rejected request or gateway failure (400/401/409/413/429/502/503)."
            )
        }
    },
)


def _correlation(request: Request, explicit: str | None = None) -> str:
    value = (
        explicit
        or request.headers.get("x-client-request-id")
        or request.headers.get("x-request-id")
    )
    if (
        value
        and len(value) <= 128
        and all(c.isascii() and (c.isalnum() or c in "._:-") for c in value)
    ):
        return value
    return uuid4().hex


def _failure(exc: Exception) -> dict:
    if isinstance(exc, ProviderNotConfigured):
        return {
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "code": exc.code,
            }
        }
    return {
        "error": {
            "message": "Gateway call failed; " + _exception_diagnostic(exc).describe(),
            "type": "server_error",
            "code": "provider_failure",
        }
    }


async def _sse(chunks: AsyncIterator[dict], include_usage: bool) -> AsyncIterator[str]:
    try:
        async with aclosing(chunks):
            async for chunk in chunks:
                if "usage" in chunk and not include_usage:
                    continue
                yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        # OpenAI SDK recognizes error frames; never turn a failed stream into DONE.
        yield "data: " + json.dumps(_failure(exc)) + "\n\n"


@router.post(
    "/chat/completions",
    response_model=None,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def chat_completions(body: ChatRequest, request: Request, proxy: ProxyDependency):
    correlation = _correlation(request, body.client_request_id)
    try:
        selection = await proxy.select_text()
    except ProviderNotConfigured as exc:
        return _not_configured(exc)
    try:
        turn = turn_request(
            body, correlation, max_output_tokens=selection.model.max_output_tokens
        )
    except (ValueError, TypeError):
        return _error("Invalid message or tool history.", "invalid_request", 400)
    chunks = proxy.chat_chunks(turn, selection)
    headers = {"X-Client-Request-ID": correlation, "Cache-Control": "no-store"}
    if body.stream:
        return StreamingResponse(
            _sse(
                chunks, bool(body.stream_options and body.stream_options.include_usage)
            ),
            media_type="text/event-stream",
            headers=headers,
        )
    try:
        return JSONResponse(await completion(chunks), headers=headers)
    except Exception as exc:
        status = 409 if isinstance(exc, ProviderNotConfigured) else 502
        return JSONResponse(_failure(exc), status_code=status, headers=headers)


class ImageGeneration(ProxyInput):
    model: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=32000)
    size: Literal["1024x1024", "1536x1024", "1024x1536", "auto"] = "1024x1024"
    quality: Literal["low", "medium", "high", "auto"] = "auto"
    n: Literal[1] = 1
    response_format: Literal["b64_json"] = "b64_json"
    client_request_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


async def _image(proxy: _AgentLLMProxyPort, body: GatewayImageRequest):
    try:
        result = await proxy.image(body)
        return JSONResponse(
            {
                "created": int(time.time()),
                "data": [{"b64_json": result.image.data_base64}],
            },
            headers={
                "X-Client-Request-ID": body.client_request_id,
                "Cache-Control": "no-store",
            },
        )
    except LLMProviderConfigurationError:
        raise
    except Exception as exc:
        return JSONResponse(_failure(exc), status_code=502)


@router.post("/images/generations", response_model=None)
async def image_generations(
    body: ImageGeneration, request: Request, proxy: ProxyDependency
):
    return await _image(
        proxy,
        GatewayImageRequest(
            prompt=body.prompt,
            size=body.size,
            quality=body.quality,
            client_request_id=_correlation(request, body.client_request_id),
        ),
    )


@router.post("/images/edits", response_model=None)
async def image_edits(
    request: Request,
    proxy: ProxyDependency,
    image: Annotated[UploadFile, File()],
    prompt: Annotated[str, Form(min_length=1, max_length=32000)],
    model: Annotated[str, Form(min_length=1, max_length=128)],
    size: Annotated[
        Literal["1024x1024", "1536x1024", "1024x1536", "auto"], Form()
    ] = "1024x1024",
):
    del model  # The configured gateway owns the image model, not the SDK label.
    try:
        form = await request.form()
        if set(form) != {"image", "prompt", "model", "size"} and set(form) != {
            "image",
            "prompt",
            "model",
        }:
            return _error("Unsupported image edit fields.", "invalid_request", 400)
        if len(form.multi_items()) != len(form):
            return _error("Duplicate image edit fields.", "invalid_request", 400)
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return _error("Image exceeds size limit.", "payload_too_large", 413)
        body = await asyncio.to_thread(
            GatewayImageRequest,
            prompt=prompt,
            size=size,
            client_request_id=_correlation(request),
            reference_images=(
                GatewayImage(
                    mime_type=image.content_type,
                    data_base64=base64.b64encode(data).decode("ascii"),
                ),
            ),
        )
    finally:
        await image.close()
    return await _image(proxy, body)


mark_declared_owner(router, "api_gateway.routes.llm_proxy_routes")
