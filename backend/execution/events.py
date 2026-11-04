from __future__ import annotations

from typing import Any

from common.dto import ProcessingStatusEvent
from common.observability import events as E
from common.observability import get_logger
from common.protocols import EventPublisher
from execution.ports import ClientRequestIdResolver, ProcessingStatusLike

logger = get_logger(__name__)

SUPPORTED_TYPED_PROCESSING_STATUSES = {
    "queued",
    "processing",
    "awaiting_input",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "rate_limited",
    "error",
}


def _processing_status_value(status: ProcessingStatusLike) -> str:
    return str(getattr(status, "value", status))


def _normalize_processing_status(status: ProcessingStatusLike) -> str:
    value = _processing_status_value(status)
    if value not in SUPPORTED_TYPED_PROCESSING_STATUSES:
        raise ValueError(f"Unsupported ProcessingStatusEvent status: {value}")
    return value


def _typed_processing_status_details(
    details: dict[str, Any] | None,
    error_message: str | None,
) -> dict[str, Any] | None:
    if details is not None:
        return details
    if error_message:
        return {"message": error_message}
    return None


def _require_frontend_message_id(message_id: str | None) -> str:
    if not message_id:
        raise ValueError("ProcessingStatusEvent requires frontend message_id")
    return message_id


async def _resolve_processing_status_client_request_id(
    resolver: ClientRequestIdResolver,
    message_id: str | None,
    client_request_id: str | None,
) -> str | None:
    try:
        return await resolver.resolve_client_request_id(message_id, client_request_id)
    except Exception as exc:
        logger.emit(
            E.CLIENT_REQUEST_ID_RESOLUTION_FAILED,
            exc=exc,
            message_id=message_id,
            client_request_id=client_request_id,
        )
        return client_request_id


async def emit_processing_status(
    *,
    conversation_id: str,
    status: ProcessingStatusLike,
    message_id: str | None,
    event_publisher: EventPublisher,
    client_request_id_resolver: ClientRequestIdResolver,
    lifecycle_message_id: str | None = None,
    client_request_id: str | None = None,
    details: dict[str, Any] | None = None,
    error_message: str | None = None,
    agents: list[dict] | None = None,
) -> None:
    status_value = _normalize_processing_status(status)
    frontend_message_id = _require_frontend_message_id(message_id)
    await event_publisher.emit(
        ProcessingStatusEvent(
            conversation_id=conversation_id,
            message_id=frontend_message_id,
            status=status_value,
            related_message_id=lifecycle_message_id,
            details=_typed_processing_status_details(details, error_message),
            client_request_id=await _resolve_processing_status_client_request_id(
                client_request_id_resolver, message_id, client_request_id
            ),
            agents=agents,
        )
    )


def _room_processing_status_details(
    details: dict[str, Any] | str | None,
    error_message: str | None,
) -> dict[str, Any] | None:
    if isinstance(details, dict):
        return details
    if isinstance(details, str):
        return {"message": details}
    if error_message:
        return {"message": error_message}
    return None


def _room_processing_status_error_message(
    status: ProcessingStatusLike,
    details: dict[str, Any] | str | None,
    error_message: str | None,
) -> str | None:
    if error_message:
        return error_message
    status_value = _processing_status_value(status)
    if isinstance(details, str) and status_value in {"failed", "canceled"}:
        return details
    return None


async def emit_room_processing_status(
    *,
    conversation_id: str,
    status: ProcessingStatusLike,
    message_id: str | None,
    event_publisher: EventPublisher,
    client_request_id_resolver: ClientRequestIdResolver,
    lifecycle_message_id: str | None = None,
    client_request_id: str | None = None,
    details: dict[str, Any] | str | None = None,
    error_message: str | None = None,
    agents: list[dict] | None = None,
) -> None:
    normalized_error = _room_processing_status_error_message(
        status, details, error_message
    )
    await emit_processing_status(
        conversation_id=conversation_id,
        status=status,
        message_id=message_id,
        event_publisher=event_publisher,
        client_request_id_resolver=client_request_id_resolver,
        lifecycle_message_id=lifecycle_message_id,
        client_request_id=client_request_id,
        details=_room_processing_status_details(details, normalized_error),
        error_message=normalized_error,
        agents=agents,
    )
