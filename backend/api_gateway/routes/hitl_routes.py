"""HITL (Human-in-the-Loop) REST API endpoints.

Provides endpoints for users to respond to, query, and cancel HITL requests
that are generated when agents or the supervisor need human input.

See docs/HITL_DESIGN.md §7.4 for design details.
"""

from fastapi import APIRouter, Depends, HTTPException

from api_gateway.dependencies import get_hitl_manager, get_room_ownership_reader
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import ClerkUser, get_current_user
from common.dto import HITLCancelCommand, HITLRequest
from common.observability import events as E
from common.protocols import HITLManager, RoomOwnershipReader
from common.utils.logger import get_logger
from models.hitl import HITLBatchResponseRequest

router = APIRouter(prefix="/rooms/{conversation_id}/hitl", tags=["hitl"])
logger = get_logger(__name__)

HITL_ROUTE_FAILED_DETAIL = "HITL request failed."
# Routing and delivery errors can wrap storage or transport text, so the client
# gets fixed text for them; the original message goes to the server log.
_FIXED_HITL_ERROR_DETAIL = {
    "HITLRoutingFailedError": "HITL answers could not be routed to the run.",
    "HITLDeliveryUncertainError": "HITL answer delivery could not be confirmed.",
}


async def verify_room_ownership(
    conversation_id: str,
    user: ClerkUser,
    room_ownership: RoomOwnershipReader,
) -> None:
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")

    owner_id = await room_ownership.get_room_owner(conversation_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if owner_id != user.user_id:
        raise HTTPException(
            status_code=403, detail="You do not have permission to access this room"
        )


_HITL_ERROR_STATUS = {
    "HITLNotFoundError": 404,
    "HITLConflictError": 409,
    "HITLExpiredError": 410,
    "HITLRoomMismatchError": 403,
    "HITLContinuationLostError": 410,
    "HITLRoutingFailedError": 502,
    "HITLDeliveryUncertainError": 503,
}

_PUBLIC_PENDING_HITL_FIELDS = {
    "request_id",
    "message_id",
    "prompt",
    "prompt_type",
    "choices",
    "header",
    "required",
    "allow_other",
    "choice_options",
    "source",
    "agent_id",
    "agent_name",
    "source_step_id",
    "created_at",
    "expires_at",
    "interaction_id",
    "interaction_status",
    "interaction_version",
    "application_status",
    "application_error",
    "question_count",
    "question_index",
    "client_request_id",
}


def _raise_http_for_hitl_error(
    exc: Exception, *, conversation_id: str, operation: str
) -> None:
    name = exc.__class__.__name__
    status_code = _HITL_ERROR_STATUS.get(name)
    if status_code is None or name in _FIXED_HITL_ERROR_DETAIL:
        # These errors can carry internal detail; log it, return fixed text.
        logger.emit(
            E.HITL_ROUTE_FAILED,
            exc=exc,
            conversation_id=conversation_id,
            operation=operation,
            status=status_code or 500,
        )
    if status_code is None:
        raise HTTPException(status_code=500, detail=HITL_ROUTE_FAILED_DETAIL) from exc
    message = _FIXED_HITL_ERROR_DETAIL.get(name) or getattr(exc, "message", str(exc))
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    if name == "HITLDeliveryUncertainError":
        raise HTTPException(
            status_code=status_code,
            detail={"message": message, "code": code, **(details or {})},
        ) from exc
    raise HTTPException(status_code=status_code, detail=message) from exc


def _pending_hitl_public_payload(request: HITLRequest) -> dict:
    payload = request.model_dump(
        mode="json",
        include=_PUBLIC_PENDING_HITL_FIELDS,
        exclude_none=True,
    )
    payload["message_id"] = (
        request.display_message_id
        or request.continuation_message_id
        or request.message_id
        or request.user_message_id
    )
    payload["related_message_id"] = request.user_message_id
    return payload


@router.post("/respond-batch")
async def respond_to_hitl_interaction(
    conversation_id: str,
    body: HITLBatchResponseRequest,
    user: ClerkUser = Depends(get_current_user),
    manager: HITLManager = Depends(get_hitl_manager),
    room_ownership: RoomOwnershipReader = Depends(get_room_ownership_reader),
):
    """Atomically submit every required answer for one interaction."""
    await verify_room_ownership(conversation_id, user, room_ownership)

    try:
        response = await manager.resolve_hitl_batch(
            conversation_id,
            body.interaction_id,
            [answer.model_dump() for answer in body.answers],
            user.user_id,
            body.client_request_id,
        )
    except Exception as exc:
        _raise_http_for_hitl_error(
            exc, conversation_id=conversation_id, operation="respond_batch"
        )
    return {
        "status": response.status,
        "request_id": response.request_id,
        "interaction_id": body.interaction_id,
        "client_request_id": response.client_request_id,
    }


@router.get("/pending")
async def get_pending_hitl_requests(
    conversation_id: str,
    user: ClerkUser = Depends(get_current_user),
    manager: HITLManager = Depends(get_hitl_manager),
    room_ownership: RoomOwnershipReader = Depends(get_room_ownership_reader),
):
    """Get pending HITL requests for a room (SSE reconnect catch-up)."""
    await verify_room_ownership(conversation_id, user, room_ownership)

    requests = await manager.get_pending_hitl(conversation_id)
    return {"requests": [_pending_hitl_public_payload(r) for r in requests]}


@router.post("/interactions/{interaction_id}/cancel")
async def cancel_hitl_interaction(
    conversation_id: str,
    interaction_id: str,
    body: HITLCancelCommand,
    user: ClerkUser = Depends(get_current_user),
    manager: HITLManager = Depends(get_hitl_manager),
    room_ownership: RoomOwnershipReader = Depends(get_room_ownership_reader),
):
    """Cancel one complete interaction with optimistic version fencing."""
    await verify_room_ownership(conversation_id, user, room_ownership)
    if body.interaction_id != interaction_id:
        raise HTTPException(status_code=400, detail="interaction_id mismatch")
    try:
        new_version = await manager.cancel_hitl_interaction(
            conversation_id,
            interaction_id,
            body.expected_interaction_version,
        )
    except Exception as exc:
        _raise_http_for_hitl_error(
            exc, conversation_id=conversation_id, operation="cancel_interaction"
        )
    return {
        "status": "canceled",
        "interaction_id": interaction_id,
        "interaction_version": new_version,
    }


_mark_declared_owner(router, __name__)
