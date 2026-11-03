"""ASGI request correlation and one-event request completion logging."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from common.observability import (
    bind_log_context,
    get_logger,
    safe_exception_metadata,
)
from common.observability import events as E

logger = get_logger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id(headers: list[tuple[bytes, bytes]]) -> str:
    for key, value in headers:
        if key.lower() != b"x-request-id":
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if _REQUEST_ID_PATTERN.fullmatch(candidate):
            return candidate
        break
    return str(uuid.uuid4())


def _set_response_request_id(
    headers: list[tuple[bytes, bytes]],
    request_id: str,
) -> list[tuple[bytes, bytes]]:
    filtered = [
        (key, value) for key, value in headers if key.lower() != b"x-request-id"
    ]
    filtered.append((b"x-request-id", request_id.encode("ascii")))
    return filtered


class RequestLoggingMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope.get("headers", []))
        started_at = time.perf_counter()
        status_code = 500
        error_fields: dict[str, str] = {}
        failure_outcome: str | None = None

        async def send_with_request_id(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                message = {
                    **message,
                    "headers": _set_response_request_id(
                        list(message.get("headers", [])),
                        request_id,
                    ),
                }
            await send(message)

        with bind_log_context(request_id=request_id, trace_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except asyncio.CancelledError as exc:
                error_fields = safe_exception_metadata(exc)
                failure_outcome = "cancelled"
                raise
            except Exception as exc:
                error_fields = safe_exception_metadata(exc)
                failure_outcome = "error"
                raise
            finally:
                route = scope.get("route")
                route_template = getattr(route, "path", None) or scope.get(
                    "path",
                    "<unknown>",
                )
                if failure_outcome is not None:
                    outcome = failure_outcome
                elif status_code >= 500:
                    outcome = "error"
                elif status_code >= 400:
                    outcome = "client_error"
                else:
                    outcome = "success"
                fields = {
                    "request_id": request_id,
                    "trace_id": request_id,
                    "method": scope.get("method", "<unknown>"),
                    "route": route_template,
                    "status": status_code,
                    "outcome": outcome,
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        3,
                    ),
                }
                fields.update(error_fields)
                logger.emit(E.HTTP_REQUEST_COMPLETED, **fields)


__all__ = ["RequestLoggingMiddleware"]
