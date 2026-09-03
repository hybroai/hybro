from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api_gateway.routes.room_routes import send_message
from common.dto import ExecutionAck, ExecutionRequest


def test_execution_request_accepts_only_canonical_scope() -> None:
    request = ExecutionRequest(
        room_id="room-1",
        sender_id="user-1",
        mode="supervisor",
        agent_scope={"source": "mention", "agent_ids": ["agent-1", "agent-2"]},
    )

    assert request.mode == "supervisor"
    assert request.agent_scope.source == "mention"
    assert request.agent_scope.agent_ids == ["agent-1", "agent-2"]
    assert not hasattr(request, "selected_agent_ids")


@pytest.mark.asyncio
async def test_send_message_persists_changed_mode_before_execution_and_ack(
    mock_user,
    sample_room,
    sample_user_message,
    patch_room_center_deps,
):
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": sample_room.room_id,
            "message": sample_user_message.model_dump(mode="json"),
            "client_request_id": "scope-v2-1",
            "mode": "supervisor",
            "agent_scope": {"source": "mention", "agent_ids": ["agent-1"]},
        }
    )
    sample_room.extend_info = {"use_supervisor": False, "preserved": "value"}
    patch_room_center_deps["db_service"].get_room_by_room_id.return_value = sample_room
    call_order: list[str] = []

    async def update_mode(*_args, **_kwargs):
        call_order.append("persist_mode")
        return True

    async def execute(_request):
        call_order.append("execute")
        return ExecutionAck(
            success=True,
            message_id="message-1",
            should_start_orchestration=False,
        )

    patch_room_center_deps[
        "room_center"
    ].update_room_default_mode.side_effect = update_mode
    patch_room_center_deps["execution_engine"].execute.side_effect = execute

    response = await send_message(
        request,
        mock_user,
        store=patch_room_center_deps["db_service"],
        engine=patch_room_center_deps["execution_engine"],
        center=patch_room_center_deps["room_center"],
    )

    assert response.success is True
    assert call_order == ["persist_mode", "execute"]
    patch_room_center_deps[
        "room_center"
    ].update_room_default_mode.assert_awaited_once_with(
        sample_room.room_id,
        use_supervisor=True,
    )
    execution_request = patch_room_center_deps[
        "execution_engine"
    ].execute.await_args.args[0]
    assert execution_request.mode == "supervisor"
    assert execution_request.agent_scope.model_dump() == {
        "source": "mention",
        "agent_ids": ["agent-1"],
    }


@pytest.mark.asyncio
async def test_send_message_stops_before_execution_when_mode_write_fails(
    mock_user,
    sample_room,
    sample_user_message,
    patch_room_center_deps,
):
    sample_room.extend_info = {"use_supervisor": False}
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": sample_room.room_id,
            "message": sample_user_message.model_dump(mode="json"),
            "client_request_id": "mode-write-failure",
            "mode": "supervisor",
            "agent_scope": {"source": "room_default"},
        }
    )
    patch_room_center_deps["db_service"].get_room_by_room_id.return_value = sample_room
    patch_room_center_deps["room_center"].update_room_default_mode.return_value = False

    with pytest.raises(HTTPException) as exc_info:
        await send_message(
            request,
            mock_user,
            store=patch_room_center_deps["db_service"],
            engine=patch_room_center_deps["execution_engine"],
            center=patch_room_center_deps["room_center"],
        )

    assert exc_info.value.status_code == 500
    patch_room_center_deps["execution_engine"].execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_rejects_debate_and_legacy_target_fields(
    mock_user,
    sample_room,
    sample_user_message,
    patch_room_center_deps,
):
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": sample_room.room_id,
            "message": sample_user_message.model_dump(mode="json"),
            "client_request_id": "scope-v2-2",
            "mode": "debate",
            "agent_scope": {"source": "room_default"},
            "selected_agent_ids": ["agent-1"],
        }
    )

    response = await send_message(
        request,
        mock_user,
        store=patch_room_center_deps["db_service"],
        engine=patch_room_center_deps["execution_engine"],
    )

    assert response.status_code == 400
    assert "direct, supervisor" in (response.error or "")
    patch_room_center_deps["execution_engine"].execute.assert_not_awaited()
