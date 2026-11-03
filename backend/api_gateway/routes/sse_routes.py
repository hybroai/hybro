# api/sse.py
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from api_gateway.dependencies import (
    get_execution_engine,
    get_sse_store,
    get_sse_transport,
)
from api_gateway.errors import require_chat_room
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import ClerkUser, get_current_user, get_current_user_with_query_token
from common.dto import CancellationAck
from common.observability import events as E
from common.protocols import ExecutionEngine, SSERouteTransport, SSEStateReader
from common.utils.logger import get_logger
from common.utils.time import utcnow

logger = get_logger(__name__)
router = APIRouter()

_PUBLIC_TERMINAL_ORCHESTRATION_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "budget_exhausted": "failed",
}


async def _fetch_connect_snapshot(
    snapshot_service: Any, conversation_id: str, *, force: bool
) -> dict[str, Any] | None:
    """Build the initial snapshot for one connect, tolerating failures."""

    if snapshot_service is None:
        return None
    try:
        return await snapshot_service.snapshot(conversation_id, force=force)
    except Exception as exc:
        logger.emit(
            E.SSE_SNAPSHOT_BUILD_FAILED, exc=exc, conversation_id=conversation_id
        )
        return None


@router.get("/sse/room/{conversation_id}/stream")
async def stream_room_messages(  # noqa: C901
    conversation_id: str = Path(..., description="room ID"),
    user: ClerkUser = Depends(get_current_user_with_query_token),
    transport: SSERouteTransport = Depends(get_sse_transport),
    db: SSEStateReader = Depends(get_sse_store),
    request: Request = None,  # noqa: B008 — FastAPI injected
    snapshot: str | None = None,
):
    """
    create SSE message stream for specified room

    The stream is snapshot-driven (Room Stream Snapshot plan §4): the
    ``connected`` handshake carries the room's latest contiguous ``room_seq``
    and a ``snapshot`` frame follows as the first frame. ``?snapshot=1``
    forces a fresh fold from the authoritative room event log (gap recovery).
    Without a bound snapshot service, or when the snapshot build fails, the
    stream continues delta-only with an unchanged envelope.

    Args:
        conversation_id: room ID
        user: Authenticated user (from header or query param token)

    Returns:
        StreamingResponse: SSE stream response
    """
    room = await db.get_room_by_room_id(conversation_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.room_owner_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to stream this room",
        )

    snapshot_service = None
    if request is not None:
        snapshot_service = getattr(request.app.state, "room_snapshot_service", None)

    async def event_generator():
        connection = None
        try:
            # create SSE connection
            connection = await transport.add_connection(conversation_id)
            logger.emit(
                E.SSE_STREAM_STARTED,
                conversation_id=conversation_id,
                connection_id=connection.connection_id,
            )

            # Build the snapshot before the handshake so the connected frame
            # can carry the same watermark the snapshot folds to.
            snapshot_data = await _fetch_connect_snapshot(
                snapshot_service, conversation_id, force=snapshot == "1"
            )

            # send connected message (handshake gains room_seq)
            connected_data: dict[str, Any] = {
                "connection_id": connection.connection_id,
            }
            if snapshot_data is not None:
                connected_data["room_seq"] = snapshot_data["room_seq"]
            connected_message = {
                "type": "connected",
                "conversation_id": conversation_id,
                "timestamp": utcnow().isoformat(),
                "data": connected_data,
            }
            yield f"data: {json.dumps(connected_message)}\n\n"

            # Snapshot as the first frame after connected.
            if snapshot_data is not None:
                snapshot_frame = {
                    "type": "snapshot",
                    "conversation_id": conversation_id,
                    "timestamp": utcnow().isoformat(),
                    "data": snapshot_data,
                }
                yield f"data: {json.dumps(snapshot_frame)}\n\n"

            while connection.is_active:
                try:
                    # get next message
                    message = await connection.get_message(timeout=30.0)
                    if message:
                        yield f"data: {message}\n\n"

                except Exception as exc:
                    logger.emit(
                        E.SSE_STREAM_FAILED,
                        exc=exc,
                        conversation_id=conversation_id,
                        connection_id=connection.connection_id,
                    )
                    break

        except Exception as exc:
            logger.emit(
                E.SSE_CONNECTION_FAILED, exc=exc, conversation_id=conversation_id
            )

        finally:
            # clean up connection
            if connection:
                await transport.remove_connection(
                    conversation_id, connection.connection_id
                )
                logger.emit(
                    E.SSE_STREAM_CLOSED,
                    conversation_id=conversation_id,
                    connection_id=connection.connection_id,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Compression proxies must not buffer the handshake or snapshot.
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sse/room/{conversation_id}/status")
async def get_room_sse_status(
    conversation_id: str = Path(..., description="room ID"),
    user: ClerkUser = Depends(get_current_user_with_query_token),
    transport: SSERouteTransport = Depends(get_sse_transport),
    db: SSEStateReader = Depends(get_sse_store),
):
    """
    get SSE connection status for specified room

    Args:
        conversation_id: room ID
        user: Authenticated user (from header or query param token)

    Returns:
        dict: room connection status information
    """
    room = await db.get_room_by_room_id(conversation_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.room_owner_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to inspect this room",
        )
    return transport.get_room_status(conversation_id)


@router.get("/sse/room/{conversation_id}/events")
async def get_room_events(
    conversation_id: str = Path(..., description="room ID"),
    request: Request = None,  # noqa: B008 — FastAPI injected
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    user: ClerkUser = Depends(get_current_user_with_query_token),
    db: SSEStateReader = Depends(get_sse_store),
):
    """
    Replay persisted public room events (Room Stream Snapshot plan §5).

    This is a fallback read path, not a delivery channel: the primary
    live-recovery path remains re-requesting a snapshot over the stream.
    Cold hydration (no ``after`` yet) is ``after=0``. Auth is identical to
    the SSE stream route.
    """
    room = await db.get_room_by_room_id(conversation_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.room_owner_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to replay this room",
        )

    store = getattr(request.app.state, "room_event_store", None)
    if store is None:
        return {"room_seq": 0, "events": []}
    records = await store.read_range(conversation_id, after=after, limit=limit)
    return {"room_seq": await store.latest_seq(conversation_id), "events": records}


@router.post("/sse/message/{message_id}/cancel")
async def cancel_message(
    message_id: str = Path(..., description="Message ID to cancel"),
    user: ClerkUser = Depends(get_current_user),
    db: SSEStateReader = Depends(get_sse_store),
    engine: ExecutionEngine = Depends(get_execution_engine),
):
    """
    Cancel an ongoing message processing workflow.

    This marks the message as cancelled, which will be detected at the next
    checkpoint in the workflow, causing it to stop gracefully.

    Args:
        message_id: The ID of the message to cancel
        user: Authenticated user

    Returns:
        dict: Success status and message
    """
    try:
        # Verify the message exists and the user owns the room it belongs to
        message = await db.get_room_user_message_by_message_id(message_id)
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        room = await db.get_room_by_room_id(message.conversation_id)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        if room.room_owner_id != user.user_id:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to cancel this message",
            )
        require_chat_room(room)

        extend_info = getattr(message, "extend_info", None)
        persisted_status = (
            extend_info.get("orchestration_status")
            if isinstance(extend_info, dict)
            else None
        )
        terminal_status = _PUBLIC_TERMINAL_ORCHESTRATION_STATUS.get(persisted_status)
        if terminal_status is not None and terminal_status != "canceled":
            return {
                "success": True,
                "message_id": message_id,
                "message": "Message processing had already finished",
                "status": terminal_status,
                "outcome": "already_terminal",
            }

        success = await engine.cancel(
            conversation_id=message.conversation_id,
            message_id=message_id,
            requested_by_user_id=user.user_id,
        )
        if not success:
            raise HTTPException(
                status_code=500, detail="Failed to persist cancellation to database"
            )
        if isinstance(success, CancellationAck) and not success.reconciled:
            return {
                "success": True,
                "message_id": message_id,
                "message": "Message cancellation accepted and pending reconciliation",
                "status": success.status,
                "outcome": "pending_reconciliation",
            }
        if isinstance(success, CancellationAck) and not success.cancellation_applied:
            return {
                "success": True,
                "message_id": message_id,
                "message": "Message processing had already finished",
                "status": _PUBLIC_TERMINAL_ORCHESTRATION_STATUS.get(
                    success.status,
                    success.status,
                ),
                "outcome": "already_terminal",
            }

        logger.emit(E.MESSAGE_CANCELLED, message_id=message_id, user_id=user.user_id)

        return {
            "success": True,
            "message_id": message_id,
            "message": "Message cancellation requested",
            "status": "canceled",
            "outcome": "canceled",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.emit(E.MESSAGE_CANCELLATION_FAILED, exc=e, message_id=message_id)
        raise HTTPException(
            status_code=500, detail=f"Failed to cancel message: {str(e)}"
        ) from e


_mark_declared_owner(router, __name__)
