"""Client sessions with the owner's agents, and the owner's session views.

The four Client API routes (send, check, close, and the answer to a
permission question) take a client token; the owner's views (the list, the
record, and the owner's close) take the owner.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

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
    SessionQuestionAnswer,
    SessionQuestionAnswered,
    SessionStatus,
)

router = APIRouter()

_ERROR_STATUS = {
    "request_rejected": 400,
    "forbidden": 403,
    "answer_not_granted": 403,
    "question_owner_only": 403,
    "session_not_found": 404,
    "agent_not_found": 404,
    "subnet_not_found": 404,
    "not_found": 404,
    "session_closed": 409,
    "request_conflict": 409,
    "agent_unavailable": 409,
    "question_not_pending": 409,
    "invalid_request": 422,
    "answer_failed": 502,
    "clients_unavailable": 503,
    "questions_unavailable": 503,
    "answer_unconfirmed": 503,
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
    question_id: str | None = None,
) -> HTTPException:
    detail = SessionErrorDetail(
        code=error.code,
        message=error.message,
        session_id=session_id,
        client_request_id=client_request_id,
        question_id=question_id,
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
    agents' final replies. The owner's decisions are never included.

    For a client the owner allowed to answer permission questions, a waiting
    session also carries `question`: its id, text, the agent it concerns, and
    the answers `approve` and `refuse`."""
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


class _AnswerRoute(APIRoute):
    """A 422 that names the field, ``answer``, and never echoes the input."""

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
                            "message": "Invalid request; check: answer, approve "
                            "or refuse, and the only field.",
                            "fields": ["answer"],
                        }
                    },
                    status_code=422,
                )

        return handle


async def answer_session_question(
    session_id: SessionIdentifier,
    question_id: SessionIdentifier,
    body: SessionQuestionAnswer,
    sessions: SessionAccessDependency,
) -> SessionQuestionAnswered:
    """Answer the session's permission question: `approve` or `refuse`.

    Only a client the owner allowed to answer permission questions may call
    it, in its own session; every other client is refused. The question is
    the `question` that `GET /sessions/{session_id}` shows, a check rule's
    ask or an agent's own approval question. Other questions stay with the
    owner. The answer applies as the owner's Approve or Reject in the Panel,
    and the session records which client gave it. A question that is no
    longer waiting, also because the owner answered it first, is refused
    with `question_not_pending`.
    """
    try:
        return await sessions.answer(session_id, question_id, body)
    except AppError as exc:
        raise _http_error(exc, session_id=session_id, question_id=question_id) from exc


# Added directly: only this route names the invalid field in its 422.
router.add_api_route(
    "/sessions/{session_id}/questions/{question_id}/answer",
    answer_session_question,
    methods=["POST"],
    responses={
        **_ERRORS,
        **_CLIENT_AUTH,
        403: {
            "model": SessionErrorResponse,
            "description": "Not allowed to answer permission questions, a "
            "question only the owner answers, or a token-less request from "
            "another website",
        },
        409: {
            "model": SessionErrorResponse,
            "description": "The session is closed, or the question is no "
            "longer pending",
        },
        422: {"description": 'The body is not {"answer": "approve" or "refuse"}'},
        502: {"model": SessionErrorResponse, "description": "Not applied"},
        503: {
            "model": SessionErrorResponse,
            "description": "Clients, the questions or the owner are "
            "unavailable, or the delivery is not confirmed",
        },
    },
    route_class_override=_AnswerRoute,
)


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
