"""Client sessions with the owner's agents, and the owner's session views.

The three Client API routes (send, check, close) take a client token; the
owner's views (the list, the record, and the owner's close) take the owner.
"""

from fastapi import APIRouter, HTTPException

from api_gateway.dependencies import (
    OwnerSessionAccessDependency,
    SessionAccessDependency,
)
from api_gateway.registry import mark_declared_owner
from common.errors import AppError
from models.session import (
    SessionClosed,
    SessionErrorDetail,
    SessionErrorResponse,
    SessionIdentifier,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionStatus,
)

router = APIRouter()

_ERROR_STATUS = {
    "request_rejected": 400,
    "forbidden": 403,
    "session_not_found": 404,
    "agent_not_found": 404,
    "subnet_not_found": 404,
    "not_found": 404,
    "session_closed": 409,
    "request_conflict": 409,
    "agent_unavailable": 409,
    "invalid_request": 422,
}
_ERRORS = {
    status: {"model": SessionErrorResponse} for status in (400, 403, 404, 409, 422)
}
_CLIENT_AUTH = {
    401: {"description": "Unknown, revoked or missing client token"},
    403: {
        "model": SessionErrorResponse,
        "description": "Refused, or sent without a token from another website",
    },
    503: {"description": "Clients or the owner are unavailable"},
}
_OWNER_AUTH = {
    401: {"description": "Authentication required"},
    503: {"description": "The owner is not known yet"},
}


def _http_error(
    error: AppError,
    *,
    session_id: str | None = None,
    client_request_id: str | None = None,
) -> HTTPException:
    detail = SessionErrorDetail(
        code=error.code,
        message=error.message,
        session_id=session_id,
        client_request_id=client_request_id,
    )
    return HTTPException(
        status_code=_ERROR_STATUS.get(error.code, 500),
        detail=detail.model_dump(exclude_none=True),
    )


@router.post(
    "/sessions/messages",
    responses={
        413: {"description": "Request body exceeds 64 KiB"},
        429: {"description": "Session send concurrency limit reached"},
        **_ERRORS,
        **_CLIENT_AUTH,
    },
)
async def send_session_message(
    request: SessionMessageRequest,
    sessions: SessionAccessDependency,
) -> SessionMessageResponse:
    """Send one text message to one agent, opening a session without `session_id`.

    Returns a receipt as soon as Hybro has the message: `status` is `created`
    when this message opened the session, `accepted` for a later message, or
    `busy` when it was not recorded because an earlier message is still
    active. The receipt carries the session's `created_at` and `updated_at`.
    The agents' responses come from `GET /sessions/{session_id}`. Retrying
    with the same `client_request_id` returns the same receipt.

    Send the client token as `Authorization: Bearer <token>`. The session
    belongs to the client that opened it. `client_label` is required to open
    a session without a token, and deprecated with one.
    """
    try:
        return await sessions.send(request)
    except AppError as exc:
        raise _http_error(
            exc,
            session_id=request.session_id,
            client_request_id=request.client_request_id,
        ) from exc


@router.get(
    "/sessions",
    responses={**_OWNER_AUTH, 403: {"description": "Only the owner"}},
)
async def list_sessions(
    sessions: OwnerSessionAccessDependency,
) -> SessionListResponse:
    """List the owner's sessions, most recently active first."""
    return await sessions.list_sessions()


@router.get("/sessions/{session_id}", responses={**_ERRORS, **_CLIENT_AUTH})
async def check_session(
    session_id: SessionIdentifier, sessions: SessionAccessDependency
) -> SessionStatus:
    """Return the session state and its history: each client message and the
    agents' final replies. The owner's decisions are never included."""
    try:
        return await sessions.check(session_id)
    except AppError as exc:
        raise _http_error(exc, session_id=session_id) from exc


@router.post("/sessions/{session_id}/close", responses={**_ERRORS, **_CLIENT_AUTH})
async def close_session(
    session_id: SessionIdentifier, sessions: SessionAccessDependency
) -> SessionClosed:
    """Close the session, canceling a pending approval or an active run first."""
    try:
        return await sessions.close(session_id)
    except AppError as exc:
        raise _http_error(exc, session_id=session_id) from exc


@router.get(
    "/sessions/{session_id}/record",
    responses={**_ERRORS, **_OWNER_AUTH},
)
async def get_session_record(
    session_id: SessionIdentifier, sessions: OwnerSessionAccessDependency
) -> SessionOwnerRecord:
    """Return the owner's view of a session, including its conversation id."""
    try:
        return await sessions.get_record(session_id)
    except AppError as exc:
        raise _http_error(exc, session_id=session_id) from exc


@router.post(
    "/sessions/{session_id}/record/close",
    responses={**_ERRORS, **_OWNER_AUTH},
)
async def close_session_record(
    session_id: SessionIdentifier, sessions: OwnerSessionAccessDependency
) -> SessionClosed:
    """Close any client's session as the owner, canceling a pending approval
    or an active run first."""
    try:
        return await sessions.close(session_id)
    except AppError as exc:
        raise _http_error(exc, session_id=session_id) from exc


mark_declared_owner(router, __name__)
