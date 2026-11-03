"""ASGI request correlation and one-event request completion logging."""

from __future__ import annotations

import asyncio
import json
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
_MAX_ENVELOPE_BYTES = 64 * 1024


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


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        key.lower() == b"content-type" and value.lower().startswith(b"application/json")
        for key, value in headers
    )


def _failure_envelope(body: bytes) -> tuple[str, int | None] | None:
    """Return the outcome and status of a ``{"success": false}`` body."""

    # Cheap pre-filter: most successful responses are never decoded.
    if not body.lstrip().startswith(b"{") or b'"success"' not in body:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("success") is not False:
        return None
    status = document.get("status_code")
    if not isinstance(status, int) or isinstance(status, bool):
        return "client_error", None
    return ("error" if status >= 500 else "client_error"), status


def _outcome(
    failure_outcome: str | None,
    envelope: tuple[str, int | None] | None,
    status_code: int,
) -> tuple[str, int | None]:
    """Return the request outcome and the envelope status, if any."""

    if failure_outcome is not None:
        return failure_outcome, None
    if envelope is not None:
        return envelope
    if status_code >= 500:
        return "error", None
    if status_code >= 400:
        return "client_error", None
    return "success", None


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
        inspect_body = False
        envelope: tuple[str, int | None] | None = None

        async def send_with_request_id(message: dict) -> None:
            nonlocal status_code, inspect_body, envelope
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                inspect_body = status_code == 200 and _is_json(
                    list(message.get("headers", []))
                )
                message = {
                    **message,
                    "headers": _set_response_request_id(
                        list(message.get("headers", [])),
                        request_id,
                    ),
                }
            await send(message)
            if message["type"] == "http.response.body" and inspect_body:
                # Only a single-chunk JSON 200 body is inspected, after it was
                # forwarded; streams and multi-chunk bodies are never buffered.
                inspect_body = False
                body = message.get("body", b"")
                if not message.get("more_body", False) and (
                    len(body) <= _MAX_ENVELOPE_BYTES
                ):
                    envelope = _failure_envelope(body)

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
                outcome, envelope_status = _outcome(
                    failure_outcome, envelope, status_code
                )
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
                if envelope_status is not None:
                    fields["envelope_status"] = envelope_status
                fields.update(error_fields)
                logger.emit(E.HTTP_REQUEST_COMPLETED, **fields)


__all__ = ["RequestLoggingMiddleware"]
