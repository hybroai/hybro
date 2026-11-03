"""High-level SDK facade for A2A client operations.

This module owns the SDK client surface for outbound calls. Internal callers
pass provider-neutral data (card dicts, internal messages, plain payloads) and
receive normalized ``{"kind", "result", "error"}`` dicts, so protocol objects
never leave the adapter.

Binding selection happens before the first send: an agent card advertises one or
more interfaces, and this facade picks the best supported binding and pins it to
the URL. Querying, cancelling, and continuation reuse the same resolved address,
so a card refresh cannot silently move an accepted task onto another interface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

import httpx
from a2a.client import A2ACardResolver as SDKCardResolver
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.client.errors import A2AClientError, A2AClientTimeoutError
from a2a.types import AgentCard, SendMessageRequest
from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP, A2AError
from google.protobuf.json_format import ParseError

from common.observability import events as E
from common.observability import get_logger

from .bounded_http import bounded_client, event_bounded_client
from .card_data import build_agent_card
from .constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PREV_AGENT_CARD_WELL_KNOWN_PATH,
    SUPPORTED_BINDINGS,
)
from .docker_host_fallback import (
    stream_with_docker_host_fallback,
    with_docker_host_fallback,
    with_docker_host_url_fallback,
)
from .message_factory import to_sdk_message
from .task_requests import (
    build_cancel_task_request,
    build_push_notification_config,
    build_send_message_request,
)

logger = get_logger(__name__)

# JSON-RPC code per SDK error type, taken from the SDK's own mapping so a new
# protocol error does not require editing this table.
_ERROR_CODE_BY_TYPE = dict(JSON_RPC_ERROR_CODE_MAP)


class A2AClientFacadeError(Exception):
    """Raised when an A2A client operation fails."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class A2AMessageValidationError(ValueError):
    """Raised when caller message data cannot be represented by the SDK."""


def _build_card(agent_card_data: Any) -> AgentCard:
    return build_agent_card(agent_card_data)


def _client_config(
    *,
    client: httpx.AsyncClient,
    streaming: bool,
    accepted_output_modes: list[str] | None = None,
    push_notification_config: dict[str, Any] | None = None,
) -> ClientConfig:
    return ClientConfig(
        streaming=streaming,
        httpx_client=client,
        supported_protocol_bindings=list(SUPPORTED_BINDINGS),
        # Client preference keeps JSON-RPC as the binding of choice and only
        # falls back to HTTP+JSON for agents that do not publish JSON-RPC.
        # Server preference would instead let card ordering switch an agent
        # that offers both onto a binding Hybro has not used before.
        use_client_preference=True,
        accepted_output_modes=list(accepted_output_modes or []),
        push_notification_config=build_push_notification_config(
            push_notification_config
        ),
    )


def _create_client(card: AgentCard, config: ClientConfig) -> Client:
    """Create a client bound to the card's best supported interface."""
    return ClientFactory(config).create(card)


async def fetch_agent_card_with_fallback(
    agent_url: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    async with bounded_client(timeout=timeout) as client:
        return await with_docker_host_url_fallback(
            str(agent_url),
            lambda candidate_url: _fetch_agent_card_from_url(client, candidate_url),
        )
    raise A2AClientFacadeError("Agent card not found at any known path")


async def send_message(
    agent_card_data: Any,
    message_data: Any,
    *,
    accepted_output_modes: list[str] | None = None,
    push_notification_config: dict[str, Any] | None = None,
    blocking: bool = True,
    timeout: float = 600.0,
    allow_docker_host_fallback: bool = True,
) -> dict[str, Any]:
    """Send once, optionally retrying through the Docker-host URL fallback."""
    started_at = time.perf_counter()
    card: AgentCard | None = None
    try:
        card = _build_card(agent_card_data)
        try:
            message = to_sdk_message(message_data)
        except (ValueError, TypeError, ParseError) as exc:
            raise A2AMessageValidationError(
                "Message cannot be represented as an A2A message"
            ) from exc
        request = build_send_message_request(
            message,
            accepted_output_modes=accepted_output_modes,
            push_notification_config=push_notification_config,
            blocking=blocking,
        )
        async with bounded_client(timeout=timeout) as client:
            config = _client_config(
                client=client,
                streaming=False,
                accepted_output_modes=accepted_output_modes,
                push_notification_config=push_notification_config,
            )
            if allow_docker_host_fallback:
                response = await with_docker_host_fallback(
                    card,
                    lambda candidate: _send_once(
                        _create_client(candidate, config), request
                    ),
                )
            else:
                response = await _send_once(_create_client(card, config), request)
        normalized = _normalize_response(response)
    except A2AError as exc:
        if not is_protocol_error(exc):
            _log_a2a_completed(
                card=card,
                operation="message_send",
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        normalized = _protocol_error_response(exc)
    except asyncio.CancelledError as exc:
        _log_a2a_completed(
            card=card,
            operation="message_send",
            started_at=started_at,
            outcome="cancelled",
            error=exc,
        )
        raise
    except Exception as exc:
        _log_a2a_completed(
            card=card,
            operation="message_send",
            started_at=started_at,
            outcome="error",
            error=exc,
        )
        raise
    outcome, error_type, error_code = _normalized_response_outcome(normalized)
    _log_a2a_completed(
        card=card,
        operation="message_send",
        started_at=started_at,
        outcome=outcome,
        error_type=error_type,
        error_code=error_code,
    )
    return normalized


async def stream_message(
    agent_card_data: Any,
    message_data: Any,
    *,
    accepted_output_modes: list[str] | None = None,
    push_notification_config: dict[str, Any] | None = None,
    timeout: float = 600.0,
) -> AsyncGenerator[dict[str, Any], None]:
    started_at = time.perf_counter()
    card: AgentCard | None = None
    outcome = "success"
    response_error_type: str | None = None
    response_error_code: Any = None
    try:
        card = _build_card(agent_card_data)
        request = build_send_message_request(
            to_sdk_message(message_data),
            accepted_output_modes=accepted_output_modes,
            push_notification_config=push_notification_config,
            blocking=False,
        )
        async with event_bounded_client(timeout=timeout) as client:
            config = _client_config(
                client=client,
                streaming=True,
                accepted_output_modes=accepted_output_modes,
                push_notification_config=push_notification_config,
            )
            async with aclosing(
                stream_with_docker_host_fallback(
                    card,
                    lambda candidate: _stream_once(
                        _create_client(candidate, config), request
                    ),
                )
            ) as response_stream:
                async for response in response_stream:
                    normalized = _normalize_response(response)
                    (
                        response_outcome,
                        current_error_type,
                        current_error_code,
                    ) = _normalized_response_outcome(normalized)
                    if response_outcome == "error":
                        outcome = "error"
                        response_error_type = current_error_type
                        response_error_code = current_error_code
                    yield normalized
    except A2AError as exc:
        if not is_protocol_error(exc):
            _log_a2a_completed(
                card=card,
                operation="message_stream",
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        outcome = "error"
        response_error_type = type(exc).__name__
        response_error_code = getattr(exc, "code", None)
        yield _protocol_error_response(exc)
    except asyncio.CancelledError as exc:
        _log_a2a_completed(
            card=card,
            operation="message_stream",
            started_at=started_at,
            outcome="cancelled",
            error=exc,
        )
        raise
    except GeneratorExit as exc:
        _log_a2a_completed(
            card=card,
            operation="message_stream",
            started_at=started_at,
            outcome="cancelled",
            error=exc,
        )
        raise
    except Exception as exc:
        _log_a2a_completed(
            card=card,
            operation="message_stream",
            started_at=started_at,
            outcome="error",
            error=exc,
        )
        raise
    else:
        _log_a2a_completed(
            card=card,
            operation="message_stream",
            started_at=started_at,
            outcome=outcome,
            error_type=response_error_type,
            error_code=response_error_code,
        )


async def cancel_remote_task(
    agent_card_data: Any,
    task_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    started_at = time.perf_counter()
    card: AgentCard | None = None
    try:
        card = _build_card(agent_card_data)
        async with bounded_client(timeout=timeout) as client:
            config = _client_config(client=client, streaming=False)
            await with_docker_host_fallback(
                card,
                lambda candidate: _create_client(candidate, config).cancel_task(
                    build_cancel_task_request(task_id)
                ),
            )
        _log_a2a_completed(
            card=card,
            operation="task_cancel",
            started_at=started_at,
            outcome="success",
        )
        return True
    except asyncio.CancelledError as exc:
        _log_a2a_completed(
            card=card,
            operation="task_cancel",
            started_at=started_at,
            outcome="cancelled",
            error=exc,
        )
        raise
    except Exception as exc:
        _log_a2a_completed(
            card=card,
            operation="task_cancel",
            started_at=started_at,
            outcome="error",
            error=exc,
        )
        return False


async def _send_once(client: Client, request: SendMessageRequest) -> Any:
    """Take the single response payload from a non-streaming send.

    With ``streaming=False`` the SDK yields exactly one ``StreamResponse``
    wrapping the Task or Message that settles the call.
    """
    result: Any = None
    async for chunk in client.send_message(request):
        result = chunk
        break
    if result is None:
        raise A2AClientFacadeError("A2A agent returned no response")
    return result


async def _stream_once(client: Client, request: SendMessageRequest):
    async for chunk in client.send_message(request):
        yield chunk


def is_protocol_error(exc: BaseException) -> bool:
    """True for a rejected request, false for a transport failure.

    A2A 1.0 raises typed errors for JSON-RPC failures (``InvalidParamsError``,
    ``TaskNotFoundError``, ...) where 0.3 returned an error envelope. Transport
    failures raise ``A2AClientError``, which is also an ``A2AError``: those must
    stay exceptions so Execution can keep treating the send as uncertain rather
    than as a definite protocol rejection.
    """
    return isinstance(exc, A2AError) and not isinstance(exc, A2AClientError)


def is_timeout_error(exc: BaseException) -> bool:
    """Recognize HTTP timeouts both before and after SDK transport wrapping."""
    return isinstance(
        exc, (TimeoutError, httpx.TimeoutException, A2AClientTimeoutError)
    )


def _protocol_error_response(exc: A2AError) -> dict[str, Any]:
    """Project a rejected request onto the normalized error envelope.

    Callers branch on ``response["kind"] == "error"``, so a raise is converted
    back into the envelope shape rather than changing every call site.
    """
    error: dict[str, Any] = {
        "code": _ERROR_CODE_BY_TYPE.get(type(exc), -32603),
        "message": str(exc),
        "data": getattr(exc, "data", None),
    }
    return {"kind": "error", "error": error, "result": None}


async def _fetch_agent_card_from_url(
    client: httpx.AsyncClient,
    agent_url: str,
) -> dict[str, Any]:
    for path in (AGENT_CARD_WELL_KNOWN_PATH, PREV_AGENT_CARD_WELL_KNOWN_PATH):
        resolver = SDKCardResolver(client, str(agent_url), path)
        try:
            card = await resolver.get_agent_card()
            return _card_to_dict(card)
        except A2AError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 404 and path == AGENT_CARD_WELL_KNOWN_PATH:
                continue
            raise A2AClientFacadeError(
                f"Failed to fetch card: {exc}",
                status_code=status_code if isinstance(status_code, int) else None,
            ) from exc
        except Exception as exc:
            raise A2AClientFacadeError(str(exc)) from exc
    raise A2AClientFacadeError("Agent card not found at any known path")


def _card_to_dict(card: Any) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(card, preserving_proto_field_name=False)


def _normalize_response(response: Any) -> dict[str, Any]:
    """Project one 1.0 ``StreamResponse`` frame onto the internal envelope."""
    if not response:
        return {"kind": "error", "error": "No response", "result": None}

    from google.protobuf.json_format import MessageToDict

    payload_kind = response.WhichOneof("payload")
    if payload_kind is None:
        return {"kind": "error", "error": "Empty A2A response payload", "result": None}

    # ProtoJSON member names are snake_case; the internal envelope uses the
    # hyphenated event names the rest of the adapter and Execution branch on.
    kind = payload_kind.replace("_", "-")
    result = MessageToDict(getattr(response, payload_kind))
    result["kind"] = kind
    return {"kind": kind, "result": result, "error": None}


def _normalized_response_outcome(
    response: dict[str, Any],
) -> tuple[str, str | None, Any]:
    if response.get("kind") != "error":
        return "success", None, None
    error = response.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    error_type = "A2AJSONRPCError" if isinstance(error, dict) else "A2AResponseError"
    return "error", error_type, error_code


def _log_a2a_completed(
    *,
    card: AgentCard | None,
    agent: str | None = None,
    operation: str,
    started_at: float,
    outcome: str,
    error: BaseException | None = None,
    error_type: str | None = None,
    error_code: Any = None,
) -> None:
    fields: dict[str, Any] = {
        "agent": agent or (card.name if card is not None else None),
        "operation": operation,
        "attempt": 1,
        "outcome": outcome,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
    }
    if error is not None:
        fields["error_code"] = (
            error_code
            if error_code is not None
            else getattr(error, "status_code", None)
        )
    elif outcome == "error":
        fields["error_type"] = error_type or "A2AResponseError"
        fields["error_code"] = error_code
    logger.emit(
        E.A2A_CALL_COMPLETED,
        level=logging.ERROR if outcome == "error" else logging.INFO,
        exc=error,
        **fields,
    )


__all__ = [
    "A2AClientFacadeError",
    "A2AMessageValidationError",
    "cancel_remote_task",
    "fetch_agent_card_with_fallback",
    "is_protocol_error",
    "is_timeout_error",
    "send_message",
    "stream_message",
]
