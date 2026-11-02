"""C2 step 4: orchestrator inputs come from ExecutionRequest, not a translation DTO."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

import execution.orchestrator_routing as routing
import models.request
from common.dto.execution import ExecutionAck, ExecutionRequest
from execution.orchestrator_routing import (
    MODE_PROFILE_MAP,
    DualRuntimeRouter,
    RoomMessageEnvelopeResolver,
)


def _message(extend_info: dict | None = None):
    return SimpleNamespace(
        message_id="msg-1",
        conversation_id="room-1",
        message_content=SimpleNamespace(message_text="hello", attachments=None),
        extend_info=extend_info or {},
    )


def test_orchestration_request_dto_is_gone():
    assert not hasattr(models.request, "OrchestrationRequest")
    assert "OrchestrationRequest" not in inspect.getsource(routing)


def test_router_has_no_uncalled_preflight():
    assert not hasattr(DualRuntimeRouter, "preflight_room_user_message")


def test_mode_profile_map_accepts_only_execution_request_modes():
    assert MODE_PROFILE_MAP == {
        "direct": "fast",
        "supervisor": "ultimate",
        "passthrough": "passthrough",
    }


def test_answer_mapper_is_not_named_legacy():
    assert not hasattr(routing, "_map_legacy_answers")
    assert callable(routing._map_interaction_answers)


@pytest.mark.asyncio
async def test_envelope_reads_mode_and_scope_from_the_execution_request():
    # Persisted extend_info disagrees with the live request; the request wins and
    # the persisted fallback no longer exists.
    message = _message(
        {
            "execution_mode": "passthrough",
            "candidate_scope_source": "saved_group",
            "candidate_scope_group_id": "group-1",
        }
    )
    resolver = RoomMessageEnvelopeResolver(
        get_user_message=AsyncMock(return_value=message),
        list_room_agent_ids=AsyncMock(return_value=["agent-room"]),
    )
    request = ExecutionRequest(
        conversation_id="room-1",
        sender_id="user-1",
        mode="supervisor",
        agent_scope={"source": "mention", "agent_ids": ["agent-1"]},
    )

    envelope = await resolver.load_envelope(request, message_id="msg-1")

    assert envelope.mode == "supervisor"
    assert envelope.scope_source == "mention"
    assert envelope.candidate_agent_ids == ["agent-1"]
    assert envelope.requesting_subject_id == "user-1"
    assert envelope.message_text == "hello"


@pytest.mark.asyncio
async def test_envelope_loads_the_persisted_message_by_id():
    get_user_message = AsyncMock(return_value=_message())
    resolver = RoomMessageEnvelopeResolver(
        get_user_message=get_user_message,
        list_room_agent_ids=AsyncMock(return_value=["agent-room"]),
    )

    envelope = await resolver.load_envelope(
        ExecutionRequest(conversation_id="room-1", sender_id="user-1"),
        message_id="msg-9",
    )

    get_user_message.assert_awaited_once_with("msg-9")
    assert envelope.candidate_agent_ids == ["agent-room"]


@pytest.mark.asyncio
async def test_facade_passes_the_execution_request_and_message_id_to_the_router():
    from tests.test_execution_facade import RecordingTaskFactory, _make_facade

    router = SimpleNamespace(process_room_user_message=AsyncMock())
    facade, _ = _make_facade(
        task_factory=RecordingTaskFactory(), orchestrator_router=router
    )
    request = ExecutionRequest(conversation_id="room-1", sender_id="user-1")

    await facade.start_orchestration(
        request, ExecutionAck(success=True, message_id="msg-1")
    )

    router.process_room_user_message.assert_awaited_once_with(
        request, message_id="msg-1", before_run_start=ANY
    )
