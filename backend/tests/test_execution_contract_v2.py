from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api_gateway.routes.room_routes import send_message
from common.dto import ExecutionAck
from common.dto.execution import ExecutionRequest
from execution.idempotency import (
    IDEMPOTENCY_FINGERPRINT_VERSION,
    execution_request_fingerprint_payload,
)


def test_request_uses_canonical_mode_and_mention_scope() -> None:
    request = ExecutionRequest(
        room_id="room-1",
        sender_id="user-1",
        mode="supervisor",
        agent_scope={"source": "mention", "agent_ids": ["b", "a"]},
    )

    payload = execution_request_fingerprint_payload(request)

    assert IDEMPOTENCY_FINGERPRINT_VERSION == 2
    assert payload["mode"] == "supervisor"
    assert payload["agent_scope"] == {
        "source": "mention",
        "agent_ids": ["a", "b"],
    }
    assert "selected_agent_ids" not in payload
    assert "message_target_mode" not in payload


def test_saved_group_scope_contains_only_server_resolved_group_id() -> None:
    request = ExecutionRequest(
        room_id="room-1",
        sender_id="user-1",
        mode="direct",
        agent_scope={"source": "saved_group", "group_id": "group-1"},
    )
    assert request.agent_scope.model_dump() == {
        "source": "saved_group",
        "group_id": "group-1",
    }


@pytest.mark.parametrize(
    "agent_scope",
    [
        {"source": "mention", "agent_ids": ["agent-1"], "extra": True},
        {"source": "room_default", "agent_ids": ["agent-1"]},
        {"source": "all_agents", "group_id": "group-1"},
        {"source": "saved_group", "group_id": "group-1", "agent_ids": []},
    ],
)
def test_every_agent_scope_variant_forbids_extra_fields(agent_scope) -> None:
    with pytest.raises(ValidationError):
        ExecutionRequest(
            room_id="room-1",
            sender_id="user-1",
            mode="direct",
            agent_scope=agent_scope,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["room_team", "all_agents"])
async def test_saved_group_rejects_reserved_group_ids(group_id: str) -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": "room-1",
            "message": "hello",
            "client_request_id": "request-1",
            "mode": "direct",
            "agent_scope": {"source": "saved_group", "group_id": group_id},
        }
    )

    response = await send_message(
        request,
        MagicMock(user_id="user-1"),
        store=MagicMock(),
        engine=MagicMock(),
    )

    assert response.status_code == 400
    assert response.error == "saved_group agent_scope.group_id cannot be reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_scope",
    [
        {"source": "mention", "agent_ids": ["agent-1"]},
        {"source": "room_default"},
        {"source": "all_agents"},
        {"source": "saved_group", "group_id": "group-1"},
    ],
)
async def test_send_message_schedules_each_canonical_scope(agent_scope) -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": "room-1",
            "message": {"message_content": {"message_text": "hello"}},
            "client_request_id": "request-1",
            "mode": "supervisor",
            "agent_scope": agent_scope,
        }
    )
    user = SimpleNamespace(user_id="user-1", username="User", email=None)
    store = SimpleNamespace(
        get_room_by_room_id=AsyncMock(
            return_value=SimpleNamespace(room_owner_id="user-1")
        )
    )
    engine = SimpleNamespace(
        execute=AsyncMock(
            return_value=ExecutionAck(
                success=True,
                message_id="message-1",
                should_start_orchestration=True,
            )
        ),
        schedule_orchestration=MagicMock(),
    )

    response = await send_message(request, user, store=store, engine=engine)

    assert response.success is True
    execution_request = engine.execute.await_args.args[0]
    assert execution_request.agent_scope.model_dump() == agent_scope
    engine.schedule_orchestration.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_replay_does_not_schedule_again() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "room_id": "room-1",
            "message": {"message_content": {"message_text": "hello"}},
            "client_request_id": "request-replay",
            "mode": "direct",
            "agent_scope": {"source": "room_default"},
        }
    )
    user = SimpleNamespace(user_id="user-1", username="User", email=None)
    store = SimpleNamespace(
        get_room_by_room_id=AsyncMock(
            return_value=SimpleNamespace(room_owner_id="user-1")
        )
    )
    engine = SimpleNamespace(
        execute=AsyncMock(
            return_value=ExecutionAck(
                success=True,
                message_id="message-1",
                should_start_orchestration=False,
            )
        ),
        schedule_orchestration=MagicMock(),
    )

    center = SimpleNamespace(update_room_default_mode=AsyncMock(return_value=True))
    response = await send_message(
        request,
        user,
        store=store,
        engine=engine,
        center=center,
    )

    assert response.message_id == "message-1"
    engine.schedule_orchestration.assert_not_called()


def test_mention_scope_must_be_non_empty_and_debate_is_not_a_mode() -> None:
    with pytest.raises(ValidationError):
        ExecutionRequest(
            room_id="room-1",
            sender_id="user-1",
            mode="supervisor",
            agent_scope={"source": "mention", "agent_ids": []},
        )
    with pytest.raises(ValidationError):
        ExecutionRequest(
            room_id="room-1",
            sender_id="user-1",
            mode="debate",
            agent_scope={"source": "room_default"},
        )
