from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from agent.protocols import AgentSuggestionService, serialize_agent_suggestion_result
from api_gateway.dependencies import (
    get_agent_selection_service,
    get_execution_engine,
    get_room_center,
    get_room_store,
)
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import ClerkUser, get_current_user
from common.dto import ExecutionRequest, RunInfo
from common.idempotency import (
    MAX_CLIENT_REQUEST_ID_LENGTH,
    normalize_client_request_id,
)
from common.protocols import ExecutionEngine, RoomRouteReader
from common.utils.logger import get_logger
from models.file_upload import MAX_ATTACHMENT_REFS_PER_REQUEST
from models.request import (
    RoomCenterRoomMessageRequest,
    RoomCenterRoomSettingRequest,
)
from models.response import (
    ActiveRunRef,
    RoomCenterActiveRunsResponse,
    RoomCenterUserMessageResponse,
    RoomHistoryItem,
    RoomHistoryResponse,
)
from room.protocols import RoomCenterCompatibility

router = APIRouter()
logger = get_logger(__name__)


class RoomHistoryUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    is_pinned: bool | None = None


class PinnedRoomOrderUpdate(BaseModel):
    room_ids: list[str] = Field(max_length=100)


def _run_info_to_active_run_ref(run: RunInfo) -> ActiveRunRef:
    return ActiveRunRef(
        state=str(getattr(run.state, "value", run.state)),
        trigger_message_id=run.trigger_message_id,
        agent_id=run.agent_id,
        updated_at=run.updated_at,
    )


def _message_text_len(message: dict | None) -> int:
    if not isinstance(message, dict):
        return 0
    message_content = message.get("message_content")
    if isinstance(message_content, dict):
        text = message_content.get("message_text")
    else:
        text = message.get("message_text")
    return len(text) if isinstance(text, str) else 0


def _raise_room_center_error(response) -> None:
    if response.success:
        return
    raise HTTPException(
        status_code=response.status_code or 500,
        detail=response.error or "Room operation failed",
    )


async def _active_run_refs_for_room(
    room_id: str,
    engine: ExecutionEngine,
) -> list[ActiveRunRef]:
    try:
        runs = await engine.get_runs_for_room(room_id)
    except Exception:
        logger.warning(
            "active-run lookup failed for room_id=%s", room_id, exc_info=True
        )
        return []
    return [_run_info_to_active_run_ref(run) for run in runs]


def _extract_attachments(request_data: dict, message: dict | None):
    """Extract attachment info from both top-level and inline sources.

    Returns (attachments_list_or_None, inline_file_ids_or_None, error_response_or_None).
    If error_response is not None, the caller should return it immediately.
    """
    attachments = request_data.get("attachments")

    msg_content = (message if isinstance(message, dict) else {}).get("message_content")
    msg_content = msg_content if isinstance(msg_content, dict) else {}
    raw_inline_attachments = msg_content.pop("attachments", None)

    top_level_count = len(attachments) if isinstance(attachments, list) else 0
    inline_count = (
        len(raw_inline_attachments) if isinstance(raw_inline_attachments, list) else 0
    )
    if top_level_count + inline_count > MAX_ATTACHMENT_REFS_PER_REQUEST:
        return (
            None,
            None,
            RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=(
                    f"Too many attachment references ({top_level_count + inline_count}); "
                    f"maximum {MAX_ATTACHMENT_REFS_PER_REQUEST} per request"
                ),
                status_code=400,
            ),
        )

    inline_file_ids: list[str] = []
    if raw_inline_attachments and isinstance(raw_inline_attachments, list):
        for item in raw_inline_attachments:
            fid = item.get("file_id") if isinstance(item, dict) else None
            if fid and isinstance(fid, str):
                inline_file_ids.append(fid)

    return attachments, inline_file_ids or None, None


async def verify_room_ownership(
    room_id: str,
    user: ClerkUser,
    store: RoomRouteReader,
) -> None:
    """
    Verify that the current user owns the specified room.
    Raises HTTPException if the room doesn't exist or user is not the owner.
    """
    await _get_verified_room(room_id, user, store)


async def _get_verified_room(
    room_id: str,
    user: ClerkUser,
    store: RoomRouteReader,
):
    if not room_id:
        raise HTTPException(status_code=400, detail="room_id is required")

    room = await store.get_room_by_room_id(room_id)

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.room_owner_id != user.user_id:
        raise HTTPException(
            status_code=403, detail="You do not have permission to access this room"
        )

    return room


async def _persist_room_mode_if_changed(
    *,
    room_id: str,
    room,
    mode: str,
    center: RoomCenterCompatibility,
) -> None:
    room_extend_info = getattr(room, "extend_info", None)
    extend_info = room_extend_info if isinstance(room_extend_info, dict) else {}
    current_uses_supervisor = extend_info.get("use_supervisor") is not False
    requested_uses_supervisor = mode == "supervisor"
    if current_uses_supervisor == requested_uses_supervisor:
        return

    updated = await center.update_room_default_mode(
        room_id,
        use_supervisor=requested_uses_supervisor,
    )
    if not updated:
        raise HTTPException(
            status_code=500,
            detail="Failed to persist the room execution mode",
        )


@router.post("/roomCenter/createNewRoom")
async def create_new_room(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    request_data = await request.json()
    room_center_request = RoomCenterRoomSettingRequest(
        room_name=request_data.get("room_name"),
        room_owner_id=user.user_id,
        room_owner_name=request_data.get("room_owner_name"),
        extend_info=request_data.get("extend_info"),
        requesting_user_id=user.user_id,
        # Legacy fields (accepted during rollout)
        room_agent_set=request_data.get("room_agent_set"),
        applied_from_group=request_data.get("applied_from_group"),
        # Canonical membership write input
        membership_seed_input=request_data.get("membership_seed_input"),
        room_agent_ids=request_data.get("room_agent_ids"),
        seed_group_id=request_data.get("seed_group_id"),
        seed_all_current_agents=request_data.get("seed_all_current_agents"),
    )
    room_center_response = await center.create_new_room(room_center_request)
    return room_center_response


@router.post("/roomCenter/inquiryRoomSetting")
async def inquiry_room_setting(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    engine: ExecutionEngine = Depends(get_execution_engine),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    """Get room settings - PROTECTED (requires room ownership)"""
    request_data = await request.json()
    room_id = request_data.get("room_id")

    # Verify user owns the room
    await verify_room_ownership(room_id, user, store)

    room_center_request = RoomCenterRoomSettingRequest(
        room_id=room_id, requesting_user_id=user.user_id
    )
    room_center_response = await center.inquiry_room_setting(room_center_request)
    if room_center_response.success and room_id:
        room_center_response.active_runs = await _active_run_refs_for_room(
            room_id,
            engine,
        )
    return room_center_response


@router.post("/roomCenter/inquiryActiveRuns")
async def inquiry_active_runs(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    engine: ExecutionEngine = Depends(get_execution_engine),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    """List non-terminal orchestration runs for a room — same auth as inquiryRoomSetting."""
    request_data = await request.json()
    room_id = request_data.get("room_id")
    trigger_message_id = request_data.get("trigger_message_id")

    await verify_room_ownership(room_id, user, store)

    room_center_request = RoomCenterRoomSettingRequest(
        room_id=room_id,
        requesting_user_id=user.user_id,
        trigger_message_id=trigger_message_id,
    )
    active_runs = await _active_run_refs_for_room(room_id, engine)

    turn_completion_kind = None
    trigger_is_active = any(
        run.trigger_message_id == trigger_message_id for run in active_runs
    )
    if trigger_message_id and not trigger_is_active:
        try:
            room_side_response = await center.inquiry_active_runs(room_center_request)
        except Exception:
            logger.warning(
                "turn-completion lookup failed for room_id=%s trigger_message_id=%s",
                room_id,
                trigger_message_id,
                exc_info=True,
            )
        else:
            if room_side_response.success:
                turn_completion_kind = room_side_response.turn_completion_kind

    return RoomCenterActiveRunsResponse(
        room_id=room_id,
        active_runs=active_runs,
        turn_completion_kind=turn_completion_kind,
        success=True,
        error=None,
        status_code=200,
    )


@router.get("/roomCenter/history", response_model=RoomHistoryResponse)
async def get_room_history(
    user: ClerkUser = Depends(get_current_user),
    engine: ExecutionEngine = Depends(get_execution_engine),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    response = await center.inquiry_room_history_by_owner_id(
        RoomCenterRoomSettingRequest(
            room_owner_id=user.user_id,
            requesting_user_id=user.user_id,
        )
    )
    _raise_room_center_error(response)
    rooms = list(response.room_list or [])
    latest_runs = await engine.get_latest_runs_for_rooms(
        [room.room_id for room in rooms if room.room_id]
    )
    allowed_statuses = {"queued", "processing", "awaiting_input"}
    items = []
    for room in rooms:
        if not room.room_id:
            continue
        run = latest_runs.get(room.room_id)
        state = str(
            getattr(getattr(run, "state", None), "value", getattr(run, "state", ""))
        )
        status = state if state in allowed_statuses else "idle"
        items.append(
            RoomHistoryItem(
                room_id=room.room_id,
                title=room.room_name or "Unnamed Room",
                last_activity_at=room.last_activity_at or room.room_created_at,
                is_pinned=room.is_pinned,
                pin_order=room.pin_order,
                status=status,
            )
        )
    return RoomHistoryResponse(items=items)


@router.patch("/roomCenter/history/{room_id}", response_model=RoomHistoryItem)
async def update_room_history_item(
    room_id: str,
    payload: RoomHistoryUpdate,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    room = await _get_verified_room(room_id, user, store)
    if payload.title is None and payload.is_pinned is None:
        raise HTTPException(status_code=400, detail="No history fields supplied")
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Room title cannot be empty")
        response = await center.update_room_name(
            RoomCenterRoomSettingRequest(
                room_id=room_id,
                room_name=title,
                requesting_user_id=user.user_id,
            )
        )
        _raise_room_center_error(response)
        room = response.room or room
    if payload.is_pinned is not None:
        pin_order = None
        if payload.is_pinned:
            listed = await center.inquiry_rooms_by_room_owner_id(
                RoomCenterRoomSettingRequest(
                    room_owner_id=user.user_id,
                    requesting_user_id=user.user_id,
                )
            )
            _raise_room_center_error(listed)
            pinned_rooms = [r for r in listed.room_list or [] if r.is_pinned]
            if len(pinned_rooms) >= 100:
                raise HTTPException(
                    status_code=409,
                    detail="At most 100 conversations can be pinned",
                )
            pin_order = float(1 + max([r.pin_order or 0 for r in pinned_rooms] or [0]))
        response = await center.update_room_history_fields(
            RoomCenterRoomSettingRequest(
                room_id=room_id,
                is_pinned=payload.is_pinned,
                pin_order=pin_order,
                requesting_user_id=user.user_id,
            )
        )
        _raise_room_center_error(response)
        room = response.room or room
    return RoomHistoryItem(
        room_id=room_id,
        title=room.room_name or "Unnamed Room",
        last_activity_at=room.last_activity_at or room.room_created_at,
        is_pinned=room.is_pinned,
        pin_order=room.pin_order,
        status="idle",
    )


@router.put("/roomCenter/history/pinned-order")
async def reorder_pinned_rooms(
    payload: PinnedRoomOrderUpdate,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    if len(payload.room_ids) != len(set(payload.room_ids)):
        raise HTTPException(
            status_code=400, detail="Duplicate room ids are not allowed"
        )

    listed = await center.inquiry_rooms_by_room_owner_id(
        RoomCenterRoomSettingRequest(
            room_owner_id=user.user_id,
            requesting_user_id=user.user_id,
        )
    )
    _raise_room_center_error(listed)
    pinned_room_ids = {
        room.room_id
        for room in listed.room_list or []
        if room.is_pinned and room.room_id
    }
    if set(payload.room_ids) != pinned_room_ids:
        raise HTTPException(
            status_code=409,
            detail="Pinned order must include every pinned room exactly once",
        )

    for room_id in payload.room_ids:
        room = await _get_verified_room(room_id, user, store)
        if not room.is_pinned:
            raise HTTPException(
                status_code=409, detail="Only pinned rooms can be reordered"
            )
    for index, room_id in enumerate(payload.room_ids):
        response = await center.update_room_history_fields(
            RoomCenterRoomSettingRequest(
                room_id=room_id,
                is_pinned=True,
                pin_order=float(index + 1),
                requesting_user_id=user.user_id,
            )
        )
        _raise_room_center_error(response)
    return {"success": True}


@router.delete("/roomCenter/history/{room_id}")
async def delete_room_history_item(
    room_id: str,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    await _get_verified_room(room_id, user, store)
    response = await center.delete_room_by_room_id(
        RoomCenterRoomSettingRequest(
            room_id=room_id,
            requesting_user_id=user.user_id,
        )
    )
    _raise_room_center_error(response)
    return {"success": True}


@router.post("/roomCenter/updateRoomAgentSet")
async def update_room_agent_set(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    """Update room agent set - PROTECTED (requires room ownership)"""
    request_data = await request.json()
    room_id = request_data.get("room_id")

    await verify_room_ownership(room_id, user, store)

    room_center_request = RoomCenterRoomSettingRequest(
        room_id=room_id,
        requesting_user_id=user.user_id,
        # Legacy fields (accepted during rollout)
        room_agent_set=request_data.get("room_agent_set"),
        applied_from_group=request_data.get("applied_from_group"),
        # Canonical membership write input
        membership_seed_input=request_data.get("membership_seed_input"),
        room_agent_ids=request_data.get("room_agent_ids"),
        seed_group_id=request_data.get("seed_group_id"),
        seed_all_current_agents=request_data.get("seed_all_current_agents"),
    )
    room_center_response = await center.update_room_agent_set(room_center_request)
    return room_center_response


@router.get(
    "/rooms/{room_id}/agent-calls/{run_id}/{public_call_id}/detail",
    response_model=None,
)
async def get_canonical_agent_call_detail(
    room_id: str,
    run_id: str,
    public_call_id: str,
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
):
    """Return authenticated private Tool output by opaque canonical identity."""

    await verify_room_ownership(room_id, user, store)
    if not public_call_id.startswith("inv_"):
        raise HTTPException(status_code=404, detail="Agent call output not found")
    reader = getattr(request.app.state, "canonical_agent_call_detail_reader", None)
    if reader is None:
        raise HTTPException(status_code=503, detail="Agent call detail is unavailable")
    detail = await reader.get(
        room_id=room_id,
        run_id=run_id,
        public_call_id=public_call_id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Agent call output not found")
    return detail


@router.post("/roomCenter/inquiryRoomMessagesByRoomId")
async def inquiry_room_messages(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    center: RoomCenterCompatibility = Depends(get_room_center),
):
    """Read room messages - PROTECTED (requires room ownership)"""
    request_data = await request.json()
    room_id = request_data.get("room_id")

    # Authorization intentionally precedes pagination validation so an invalid
    # cursor cannot reveal whether another user's room has timeline data.
    await verify_room_ownership(room_id, user, store)

    room_center_request = RoomCenterRoomMessageRequest(
        room_id=room_id,
        limit=request_data.get("limit"),
        cursor=request_data.get("cursor"),
    )
    room_center_response = await center.inquiry_room_messages_by_room_id(
        room_center_request
    )
    return room_center_response


@router.post("/roomCenter/sendMessage")
async def send_message(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    store: RoomRouteReader = Depends(get_room_store),
    engine: ExecutionEngine = Depends(get_execution_engine),
    center: RoomCenterCompatibility = Depends(get_room_center),
) -> RoomCenterUserMessageResponse:
    """Send message to room - PROTECTED (requires room ownership)

    This endpoint:
    1. Creates the user message and generates agent messages
    2. Persists a changed request mode as the room's next-message default
    3. Automatically queues background processing of agent messages

    The mode write completes before a successful acknowledgement is returned.
    The frontend no longer needs to call processRoomUserMessage separately.
    Processing happens atomically to prevent orphaned messages on page refresh.
    """
    request_data = await request.json()
    room_id = request_data.get("room_id")
    message = request_data.get("message")
    client_request_id = request_data.get("client_request_id")
    mode = request_data.get("mode")
    if mode not in {"direct", "supervisor"}:
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error="mode is required and must be one of: direct, supervisor",
            status_code=400,
        )

    if not isinstance(client_request_id, str) or not client_request_id.strip():
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error="client_request_id is required",
            status_code=400,
        )
    client_request_id = normalize_client_request_id(client_request_id)
    if len(client_request_id) > MAX_CLIENT_REQUEST_ID_LENGTH:
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=(
                "client_request_id exceeds maximum length of "
                f"{MAX_CLIENT_REQUEST_ID_LENGTH} characters"
            ),
            status_code=400,
        )

    legacy_fields = {
        "selected_agent_ids",
        "candidate_scope_mode",
        "candidate_scope_group_id",
        "message_target_mode",
        "target_group_id",
        "target_group",
        "target_agent_ids",
        "mentioned_agent_ids",
    }
    supplied_legacy_fields = sorted(legacy_fields.intersection(request_data))
    if supplied_legacy_fields:
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=(
                "legacy targeting fields are no longer supported; use agent_scope: "
                + ", ".join(supplied_legacy_fields)
            ),
            status_code=400,
        )

    raw_scope = request_data.get("agent_scope")
    if not isinstance(raw_scope, dict):
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error="agent_scope is required",
            status_code=400,
        )
    source = raw_scope.get("source")
    if source not in {"mention", "room_default", "all_agents", "saved_group"}:
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=(
                "agent_scope.source must be one of: mention, room_default, "
                "all_agents, saved_group"
            ),
            status_code=400,
        )

    normalized_scope: dict[str, object] = {"source": source}
    if source == "mention":
        if set(raw_scope) != {"source", "agent_ids"}:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="mention agent_scope accepts only source and agent_ids",
                status_code=400,
            )
        raw_agent_ids = raw_scope.get("agent_ids")
        if (
            not isinstance(raw_agent_ids, list)
            or not raw_agent_ids
            or not all(
                isinstance(agent_id, str) and agent_id.strip()
                for agent_id in raw_agent_ids
            )
        ):
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="mention agent_scope.agent_ids must be a non-empty string list",
                status_code=400,
            )
        normalized_scope["agent_ids"] = list(
            dict.fromkeys(agent_id.strip() for agent_id in raw_agent_ids)
        )
    elif source == "saved_group":
        if set(raw_scope) != {"source", "group_id"}:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="saved_group agent_scope accepts only source and group_id",
                status_code=400,
            )
        group_id = raw_scope.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="saved_group agent_scope.group_id is required",
                status_code=400,
            )
        normalized_group_id = group_id.strip()
        if normalized_group_id in {"room_team", "all_agents"}:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="saved_group agent_scope.group_id cannot be reserved",
                status_code=400,
            )
        normalized_scope["group_id"] = normalized_group_id
    elif set(raw_scope) != {"source"}:
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=f"{source} agent_scope accepts only source",
            status_code=400,
        )

    room = await _get_verified_room(room_id, user, store)
    mentioned_agent_ids = normalized_scope.get("agent_ids")
    attachments, inline_file_ids, err = _extract_attachments(request_data, message)
    if err is not None:
        return err

    logger.info(
        "gateway_send_message_received",
        extra={
            "room_id": room_id,
            "client_request_id": client_request_id,
            "mode": mode,
            "scope_source": source,
            "scope_size": len(mentioned_agent_ids or []),
            "attachment_count": len(attachments or []),
            "inline_file_count": len(inline_file_ids or []),
            "message_length": _message_text_len(message),
        },
    )

    related_message_id = ""
    if isinstance(message, dict):
        related_message_id = message.get("related_message_id") or ""

    execution_request = ExecutionRequest(
        room_id=room_id,
        sender_id=user.user_id,
        sender_name=getattr(user, "username", None) or getattr(user, "email", None),
        message=jsonable_encoder(message),
        attachments=jsonable_encoder(attachments),
        inline_file_ids=inline_file_ids,
        client_request_id=client_request_id,
        parent_message_id=related_message_id or None,
        mode=mode,
        agent_scope=normalized_scope,
    )
    await _persist_room_mode_if_changed(
        room_id=room_id,
        room=room,
        mode=mode,
        center=center,
    )
    ack = await engine.execute(execution_request)
    logger.info(
        "gateway_send_message_completed",
        extra={
            "room_id": room_id,
            "message_id": ack.message_id,
            "user_message_id": ack.message_id,
            "client_request_id": client_request_id,
            "outcome": "success" if ack.success else "error",
            "status": ack.status_code,
            "should_start_orchestration": ack.should_start_orchestration,
            "preflight_outcome": ack.preflight_outcome,
        },
    )

    # Auto-trigger processing as background task if message was created successfully
    if ack.success and ack.message_id and ack.should_start_orchestration:
        logger.info(
            "gateway_send_message_background_scheduled",
            extra={
                "room_id": room_id,
                "message_id": ack.message_id,
                "user_message_id": ack.message_id,
                "client_request_id": client_request_id,
            },
        )
        engine.schedule_orchestration(execution_request, ack)

    return RoomCenterUserMessageResponse(**ack.model_dump())


@router.post("/roomCenter/suggestAgents")
async def suggest_agents(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    selection_service: AgentSuggestionService = Depends(get_agent_selection_service),
):
    """
    Suggest agents for a message based on content analysis.
    Used for Auto mode to preview which agents will be selected.
    """
    request_data = await request.json()
    message_text = request_data.get("message_text", "")
    top_k = request_data.get("top_k", 3)

    if not message_text:
        return {
            "success": False,
            "error": "message_text is required",
            "status_code": 400,
        }
    try:
        suggestion_result = await selection_service.suggest_agents(
            message_text=message_text,
            top_k=top_k,
            user_id=user.user_id,
        )
        return {
            "success": True,
            **serialize_agent_suggestion_result(suggestion_result),
            "status_code": 200,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "status_code": 500}


_mark_declared_owner(router, __name__)
