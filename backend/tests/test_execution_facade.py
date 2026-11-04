import asyncio
import inspect
import logging
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from common.dto import (
    CancellationAck,
    ExecutionAck,
    ExecutionRequest,
    ProcessingStatusEvent,
    RunInfo,
)
from conversation import ConversationRuntime
from execution.facade import (
    ExecutionFacade,
    RoomCenterPort,
)
from execution.shutdown import GRACEFUL_SHUTDOWN_CANCEL_REASON
from execution.translators import room_response_to_execution_ack
from models.response import RoomCenterUserMessageResponse


class RecordingTaskFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, coro, *, name=None):
        self.calls.append(name)
        return asyncio.create_task(coro, name=name)


def _make_facade(**overrides):
    room_center = SimpleNamespace(
        get_idempotent_user_message=AsyncMock(return_value=None),
        persist_user_message=AsyncMock(),
    )

    async def persist_message_to_room(*args, **kwargs):
        response = await room_center.persist_user_message(*args, **kwargs)
        return response, response if response.message_id else None

    async def run_message_preflight_to_room(context):
        return context

    room_center.persist_message_to_room = AsyncMock(side_effect=persist_message_to_room)
    room_center.run_message_preflight_to_room = AsyncMock(
        side_effect=run_message_preflight_to_room
    )
    room_center.discard_message_preflight = MagicMock()
    hitl_manager = SimpleNamespace(
        request_interaction=AsyncMock(),
        handle_batch_response=AsyncMock(),
        get_pending_requests=AsyncMock(return_value=[]),
        cancel_request=AsyncMock(return_value=None),
        cancel_interaction_by_user=AsyncMock(return_value=6),
    )
    run_reader = SimpleNamespace(
        get_runs_for_room=AsyncMock(return_value=[]),
    )
    hitl_message_cancellation = SimpleNamespace(
        cancel_requests_for_message=AsyncMock(),
    )
    event_publisher = SimpleNamespace(emit=AsyncMock())
    client_request_id_resolver = SimpleNamespace(
        resolve_client_request_id=AsyncMock(side_effect=lambda _, provided: provided),
    )
    deps = {
        "room_center": room_center,
        "hitl_manager": hitl_manager,
        "hitl_message_cancellation": hitl_message_cancellation,
        "event_publisher": event_publisher,
        "client_request_id_resolver": client_request_id_resolver,
        "task_factory": RecordingTaskFactory(),
    }
    deps.update(overrides)
    facade = ExecutionFacade(**deps)
    # The canonical active-run reader is bound after construction in production.
    facade.bind_active_run_reader(run_reader)
    deps["run_reader"] = run_reader
    return facade, deps


def _assert_processing_status_event(
    event,
    *,
    conversation_id: str,
    message_id: str,
    status: str,
    related_message_id: str,
    client_request_id: str,
    details: dict | None,
):
    assert isinstance(event, ProcessingStatusEvent)
    assert event.event_type == "processing_status"
    assert event.conversation_id == conversation_id
    assert event.message_id == message_id
    assert event.related_message_id == related_message_id
    assert event.status == status
    assert event.client_request_id == client_request_id
    assert event.details == details
    assert event.timestamp is None
    assert event.trace_id is None
    assert event.agent_id is None
    assert event.agents is None


def _user_message_payload(text: str) -> dict:
    return {
        "room_id": "room-1",
        "message_id": "",
        "message_type": "user",
        "message_content": {"message_text": text},
    }


def _room_response_with_preflight(**kwargs) -> RoomCenterUserMessageResponse:
    assert "preflight_outcome" in RoomCenterUserMessageResponse.model_fields
    assert "preflight_details" in RoomCenterUserMessageResponse.model_fields
    response = RoomCenterUserMessageResponse(**kwargs)
    assert response.preflight_outcome == kwargs.get("preflight_outcome")
    assert response.preflight_details == kwargs.get("preflight_details")
    dumped = response.model_dump(mode="json")
    assert dumped["preflight_outcome"] == kwargs.get("preflight_outcome")
    assert dumped["preflight_details"] == kwargs.get("preflight_details")
    return response


def test_room_center_port_exposes_sync_preflight_cleanup():
    signature = inspect.signature(RoomCenterPort.discard_message_preflight)

    assert not inspect.iscoroutinefunction(RoomCenterPort.discard_message_preflight)
    assert tuple(signature.parameters) == ("self", "context")


def test_constructor_core_dependencies_are_typed_ports():
    hints = get_type_hints(ExecutionFacade.__init__)

    assert hints["room_center"].__name__ == "RoomCenterPort"
    assert hints["hitl_manager"].__name__ == "HITLServicePort"


@pytest.mark.asyncio
async def test_execute_persists_ack_without_starting_orchestration():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = RoomCenterUserMessageResponse(
        conversation_id="room-1",
        message_id="msg-1",
        user_id="user-1",
        user_name="User",
        success=True,
    )
    request = ExecutionRequest(
        conversation_id="room-1",
        sender_id="user-1",
        sender_name="User",
        message={
            "room_id": "room-1",
            "message_id": "draft-msg-1",
            "message_type": "user",
            "message_content": {"message_text": "hello"},
        },
        agent_scope={"source": "mention", "agent_ids": ["agent-1"]},
    )

    ack = await facade.execute(request)

    assert ack.message_id == "msg-1"
    deps["room_center"].persist_user_message.assert_awaited_once()
    sent_request = deps["room_center"].persist_user_message.await_args.args[0]
    assert sent_request.user_id == "user-1"
    assert sent_request.message.message_content.message_text == "hello"
    assert deps["room_center"].persist_user_message.await_args.args[1:] == (
        "room_team",
        ["agent-1"],
    )


@pytest.mark.asyncio
async def test_execute_rejects_pending_hitl_before_room_persist():
    facade, deps = _make_facade()
    deps["hitl_manager"].get_pending_requests.return_value = [SimpleNamespace()]
    deps["room_center"].persist_user_message.side_effect = AssertionError(
        "room persist should not be called"
    )

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is False
    assert ack.conversation_id == "room-1"
    assert ack.status_code == 409
    assert ack.should_start_orchestration is False
    assert "waiting for your input" in ack.error
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["room_center"].persist_user_message.assert_not_awaited()
    deps["run_reader"].get_runs_for_room.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_rejects_active_run_before_room_persist():
    facade, deps = _make_facade()
    deps["room_center"].persist_user_message.side_effect = AssertionError(
        "room persist should not be called"
    )
    deps["run_reader"].get_runs_for_room.return_value = [
        RunInfo(
            run_id="run-1",
            conversation_id="room-1",
            state="processing",
            trigger_message_id="msg-active",
        )
    ]

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is False
    assert ack.conversation_id == "room-1"
    assert ack.status_code == 409
    assert ack.should_start_orchestration is False
    assert "already processing" in ack.error
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")
    deps["room_center"].persist_user_message.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_replay_lookup_precedes_busy_guards_and_bypasses_them():
    facade, deps = _make_facade()
    order: list[str] = []

    async def lookup(**_kwargs):
        order.append("idempotency")
        return RoomCenterUserMessageResponse(
            conversation_id="room-1",
            message_id="existing-message",
            success=True,
            status_code=200,
        )

    async def pending(_conversation_id):
        order.append("hitl")
        return [SimpleNamespace()]

    async def active(_conversation_id):
        order.append("active")
        return [
            RunInfo(
                run_id="run-1",
                conversation_id="room-1",
                state="processing",
            )
        ]

    deps["room_center"].get_idempotent_user_message.side_effect = lookup
    deps["hitl_manager"].get_pending_requests.side_effect = pending
    deps["run_reader"].get_runs_for_room.side_effect = active

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1",
            sender_id="user-1",
            client_request_id="request-1",
            message=_user_message_payload("hello"),
        )
    )

    assert order == ["idempotency"]
    assert ack.success is True
    assert ack.message_id == "existing-message"
    assert ack.should_start_orchestration is False
    deps["hitl_manager"].get_pending_requests.assert_not_awaited()
    deps["run_reader"].get_runs_for_room.assert_not_awaited()
    deps["room_center"].persist_message_to_room.assert_not_awaited()
    deps["room_center"].run_message_preflight_to_room.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_guard", ["hitl", "active_run"])
async def test_execute_rechecks_idempotency_when_winner_appears_during_busy_guard(
    busy_guard: str,
):
    facade, deps = _make_facade()
    winner = RoomCenterUserMessageResponse(
        conversation_id="room-1",
        message_id="winner-message",
        success=True,
        status_code=200,
    )
    deps["room_center"].get_idempotent_user_message.side_effect = [None, winner]
    if busy_guard == "hitl":
        deps["hitl_manager"].get_pending_requests.return_value = [SimpleNamespace()]
    else:
        deps["run_reader"].get_runs_for_room.return_value = [
            RunInfo(
                run_id="run-1",
                conversation_id="room-1",
                state="processing",
            )
        ]

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1",
            sender_id="user-1",
            client_request_id="request-1",
            message=_user_message_payload("hello"),
        )
    )

    assert ack.success is True
    assert ack.message_id == "winner-message"
    assert ack.should_start_orchestration is False
    assert deps["room_center"].get_idempotent_user_message.await_count == 2
    deps["room_center"].persist_message_to_room.assert_not_awaited()
    deps["room_center"].run_message_preflight_to_room.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_idempotency_conflict_returns_body_level_409_before_busy_guards():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].get_idempotent_user_message.return_value = RoomCenterUserMessageResponse(
        conversation_id="room-1",
        success=False,
        error="The client_request_id was already used for a different request",
        status_code=409,
    )

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1",
            sender_id="user-1",
            client_request_id="request-1",
            message=_user_message_payload("changed"),
        )
    )

    assert ack.success is False
    assert ack.status_code == 409
    assert ack.should_start_orchestration is False
    deps["hitl_manager"].get_pending_requests.assert_not_awaited()
    deps["run_reader"].get_runs_for_room.assert_not_awaited()
    deps["room_center"].persist_message_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_does_not_fail_open_when_idempotency_lookup_fails():
    facade, deps = _make_facade()
    deps["room_center"].get_idempotent_user_message.side_effect = RuntimeError(
        "mongo unavailable"
    )

    with pytest.raises(RuntimeError, match="mongo unavailable"):
        await facade.execute(
            ExecutionRequest(
                conversation_id="room-1",
                sender_id="user-1",
                client_request_id="request-1",
                message=_user_message_payload("hello"),
            )
        )

    deps["hitl_manager"].get_pending_requests.assert_not_awaited()
    deps["run_reader"].get_runs_for_room.assert_not_awaited()
    deps["room_center"].persist_message_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_concurrent_insert_loser_skips_preflight_and_all_sse():
    room_center = SimpleNamespace(
        get_idempotent_user_message=AsyncMock(return_value=None),
        persist_message_to_room=AsyncMock(
            return_value=(
                RoomCenterUserMessageResponse(
                    conversation_id="room-1",
                    message_id="winner-message",
                    dispatch_root_message_id=None,
                    success=True,
                    status_code=200,
                ),
                None,
            )
        ),
        run_message_preflight_to_room=AsyncMock(),
    )
    facade, deps = _make_facade(room_center=room_center)

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1",
            sender_id="user-1",
            client_request_id="request-1",
            message=_user_message_payload("hello"),
        )
    )

    assert ack.success is True
    assert ack.message_id == "winner-message"
    assert ack.should_start_orchestration is False
    room_center.run_message_preflight_to_room.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_continues_when_hitl_pending_lookup_fails():
    facade, deps = _make_facade()
    order: list[str] = []

    async def get_pending_requests(_conversation_id):
        order.append("hitl")
        raise RuntimeError("hitl down")

    async def get_runs_for_room(_conversation_id):
        order.append("active_runs")
        return []

    async def persist_user_message(*_args, **_kwargs):
        order.append("persist")
        return _room_response_with_preflight(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["hitl_manager"].get_pending_requests.side_effect = get_pending_requests
    deps["run_reader"].get_runs_for_room.side_effect = get_runs_for_room
    deps["room_center"].persist_user_message.side_effect = persist_user_message

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.message_id == "msg-1"
    assert ack.should_start_orchestration is True
    assert order == ["hitl", "active_runs", "persist"]
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["room_center"].persist_user_message.assert_awaited_once()
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")


@pytest.mark.asyncio
async def test_execute_continues_when_active_run_lookup_fails():
    facade, deps = _make_facade()
    order: list[str] = []

    async def get_pending_requests(_conversation_id):
        order.append("hitl")
        return []

    async def get_runs_for_room(_conversation_id):
        order.append("active_runs")
        raise RuntimeError("runs down")

    async def persist_user_message(*_args, **_kwargs):
        order.append("persist")
        return _room_response_with_preflight(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["hitl_manager"].get_pending_requests.side_effect = get_pending_requests
    deps["run_reader"].get_runs_for_room.side_effect = get_runs_for_room
    deps["room_center"].persist_user_message.side_effect = persist_user_message

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.message_id == "msg-1"
    assert ack.should_start_orchestration is True
    assert order == ["hitl", "active_runs", "persist"]
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")
    deps["room_center"].persist_user_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_orchestration_carries_live_mode_and_scope():
    facade, _deps = _make_facade()
    facade._route_orchestration = AsyncMock()
    request = ExecutionRequest(
        conversation_id="room-1",
        sender_id="user-1",
        client_request_id="cr-1",
        mode="supervisor",
        agent_scope={"source": "all_agents"},
    )
    ack = ExecutionAck(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        should_start_orchestration=True,
        preflight_outcome="ready",
    )

    facade.schedule_orchestration(request, ack)
    await asyncio.sleep(0)

    # The route-validated request itself reaches routing, so the mode and scope
    # never depend on the persisted extend_info rewrite.
    facade._route_orchestration.assert_awaited_once_with(request, "msg-1")


@pytest.mark.asyncio
async def test_scheduled_orchestration_is_pending_until_its_task_ends(caplog):
    facade, _deps = _make_facade()
    release = asyncio.Event()

    async def route(request, message_id):
        await release.wait()
        raise RuntimeError("routing failed")

    facade._route_orchestration = route
    request = ExecutionRequest(
        conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
    )
    ack = ExecutionAck(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        should_start_orchestration=True,
        preflight_outcome="ready",
    )

    assert facade.is_orchestration_pending("msg-1") is False
    facade.schedule_orchestration(request, ack)
    await asyncio.sleep(0)

    assert facade.is_orchestration_pending("msg-1") is True
    assert facade.is_orchestration_pending("msg-2") is False

    # A failed task is no longer pending, so the session can report it.
    with caplog.at_level(logging.ERROR, logger="execution.facade"):
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert facade.is_orchestration_pending("msg-1") is False
    [record] = [
        r for r in caplog.records if r.getMessage() == "orchestration_task_failed"
    ]
    assert record.message_id == "msg-1"
    assert record.error_type == "RuntimeError"
    assert "routing failed" not in caplog.text


@pytest.mark.asyncio
async def test_bound_canonical_active_run_reader_rejects_before_message_persistence():
    facade, deps = _make_facade()
    canonical_reader = SimpleNamespace(
        get_runs_for_room=AsyncMock(return_value=[{"run_id": "run-1"}])
    )
    facade.bind_active_run_reader(canonical_reader)

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is False
    assert ack.status_code == 409
    deps["room_center"].persist_message_to_room.assert_not_awaited()
    canonical_reader.get_runs_for_room.assert_awaited_once_with("room-1")


@pytest.mark.asyncio
async def test_bound_canonical_active_run_reader_owns_public_room_queries():
    facade, deps = _make_facade()
    canonical_run = SimpleNamespace(trigger_message_id="canonical-user")
    canonical_reader = SimpleNamespace(
        get_runs_for_room=AsyncMock(return_value=[canonical_run]),
        get_latest_runs_for_rooms=AsyncMock(return_value={"room-1": canonical_run}),
    )
    facade.bind_active_run_reader(canonical_reader)

    assert await facade.get_runs_for_room("room-1") == [canonical_run]
    assert await facade.get_latest_runs_for_rooms(["room-1"]) == {
        "room-1": canonical_run
    }
    canonical_reader.get_runs_for_room.assert_awaited_once_with("room-1")
    canonical_reader.get_latest_runs_for_rooms.assert_awaited_once_with(["room-1"])
    deps["run_reader"].get_runs_for_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_emits_processing_for_ready_room_preflight():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="frontend-msg-1",
        dispatch_root_message_id="root-msg-1",
        success=True,
        preflight_outcome="ready",
    )
    order: list[tuple[str, str]] = []

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.should_start_orchestration is True
    assert order == [("emit", "processing")]
    deps["event_publisher"].emit.assert_awaited_once()
    event = deps["event_publisher"].emit.await_args.args[0]
    _assert_processing_status_event(
        event,
        conversation_id="room-1",
        message_id="frontend-msg-1",
        status="processing",
        related_message_id="root-msg-1",
        client_request_id="cr-1",
        details=None,
    )


@pytest.mark.asyncio
async def test_execute_emits_processing_before_room_preflight_continuation():
    preflight_context = object()
    room_center = SimpleNamespace(
        get_idempotent_user_message=AsyncMock(return_value=None),
        persist_user_message=AsyncMock(
            side_effect=AssertionError(
                "legacy single-step room path should not be used"
            )
        ),
        persist_message_to_room=AsyncMock(
            return_value=(
                RoomCenterUserMessageResponse(
                    conversation_id="room-1",
                    message_id="msg-1",
                    dispatch_root_message_id="msg-1",
                    success=True,
                ),
                preflight_context,
            )
        ),
        run_message_preflight_to_room=AsyncMock(),
    )
    facade, deps = _make_facade(room_center=room_center)
    order: list[str] = []

    async def emit_event(event):
        order.append(f"emit:{event.status}")

    async def continue_preflight(context):
        assert context is preflight_context
        order.append("room_preflight")
        return _room_response_with_preflight(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["event_publisher"].emit.side_effect = emit_event
    room_center.run_message_preflight_to_room.side_effect = continue_preflight

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.should_start_orchestration is True
    assert order == ["emit:processing", "room_preflight"]
    room_center.persist_message_to_room.assert_awaited_once()
    room_center.run_message_preflight_to_room.assert_awaited_once_with(
        preflight_context
    )
    room_center.persist_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_cancellation_during_preflight_processing_status_discards_token():
    facade, deps = _make_facade()
    preflight_context = object()
    deps["room_center"].persist_message_to_room.side_effect = None
    deps["room_center"].persist_message_to_room.return_value = (
        RoomCenterUserMessageResponse(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
        ),
        preflight_context,
    )
    facade._emit_room_preflight_processing_status = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await facade.execute(
            ExecutionRequest(conversation_id="room-1", sender_id="user-1")
        )

    deps["room_center"].discard_message_preflight.assert_called_once_with(
        preflight_context
    )
    deps["room_center"].run_message_preflight_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_room_adapter_preserves_processing_cancellation_and_discards_token():
    preflight_context = object()
    runtime = SimpleNamespace(
        _bound=True,
        get_idempotent_user_message=AsyncMock(return_value=None),
        persist_message_to_room=AsyncMock(
            return_value=(
                RoomCenterUserMessageResponse(
                    conversation_id="room-1",
                    message_id="msg-1",
                    dispatch_root_message_id="msg-1",
                    success=True,
                ),
                preflight_context,
            )
        ),
        run_message_preflight_to_room=AsyncMock(),
        discard_message_preflight=MagicMock(),
    )
    facade, _ = _make_facade(room_center=runtime)
    facade._emit_room_preflight_processing_status = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await facade.execute(
            ExecutionRequest(conversation_id="room-1", sender_id="user-1")
        )

    assert not inspect.iscoroutinefunction(
        ConversationRuntime.discard_message_preflight
    )
    runtime.discard_message_preflight.assert_called_once_with(preflight_context)
    runtime.run_message_preflight_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_original_processing_cancellation(caplog):
    preflight_context = object()
    runtime = SimpleNamespace(
        _bound=True,
        get_idempotent_user_message=AsyncMock(return_value=None),
        persist_message_to_room=AsyncMock(
            return_value=(
                RoomCenterUserMessageResponse(
                    conversation_id="room-1",
                    message_id="msg-1",
                    dispatch_root_message_id="msg-1",
                    success=True,
                ),
                preflight_context,
            )
        ),
        run_message_preflight_to_room=AsyncMock(),
        discard_message_preflight=MagicMock(side_effect=RuntimeError("cleanup failed")),
    )
    facade, _ = _make_facade(room_center=runtime)
    facade._emit_room_preflight_processing_status = AsyncMock(
        side_effect=asyncio.CancelledError("original cancellation")
    )

    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await facade.execute(
            ExecutionRequest(conversation_id="room-1", sender_id="user-1")
        )

    runtime.discard_message_preflight.assert_called_once_with(preflight_context)
    [record] = [
        r for r in caplog.records if r.getMessage() == "preflight_cleanup_failed"
    ]
    assert record.conversation_id == "room-1"
    assert record.error_type == "RuntimeError"
    assert "cleanup failed" not in caplog.text


@pytest.mark.asyncio
async def test_execute_cancellation_after_ready_preflight_discards_token():
    facade, deps = _make_facade()
    preflight_context = object()
    deps["room_center"].persist_message_to_room.side_effect = None
    deps["room_center"].persist_message_to_room.return_value = (
        RoomCenterUserMessageResponse(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
        ),
        preflight_context,
    )
    deps["room_center"].run_message_preflight_to_room.side_effect = None
    deps[
        "room_center"
    ].run_message_preflight_to_room.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )
    facade._emit_room_preflight_terminal_status = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await facade.execute(
            ExecutionRequest(conversation_id="room-1", sender_id="user-1")
        )

    deps["room_center"].discard_message_preflight.assert_called_once_with(
        preflight_context
    )


@pytest.mark.asyncio
async def test_execute_normal_ready_preflight_does_not_discard_twice():
    facade, deps = _make_facade()
    preflight_context = object()
    deps["room_center"].persist_message_to_room.side_effect = None
    deps["room_center"].persist_message_to_room.return_value = (
        RoomCenterUserMessageResponse(
            conversation_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
        ),
        preflight_context,
    )
    deps["room_center"].run_message_preflight_to_room.side_effect = None
    deps[
        "room_center"
    ].run_message_preflight_to_room.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )

    ack = await facade.execute(
        ExecutionRequest(conversation_id="room-1", sender_id="user-1")
    )

    assert ack.should_start_orchestration is True
    deps["room_center"].discard_message_preflight.assert_not_called()


@pytest.mark.asyncio
async def test_execute_does_not_emit_completed_for_success_without_preflight_outcome():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = RoomCenterUserMessageResponse(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
    )

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    event = deps["event_publisher"].emit.await_args.args[0]
    assert event.status == "processing"


@pytest.mark.asyncio
async def test_execute_emits_completed_for_completed_room_preflight():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
        preflight_outcome="completed",
    )

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    assert [
        await_call.args[0].status
        for await_call in deps["event_publisher"].emit.await_args_list
    ] == [
        "processing",
        "completed",
    ]


@pytest.mark.asyncio
async def test_execute_returns_ack_when_post_persist_status_emit_fails():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )
    deps["event_publisher"].emit.side_effect = RuntimeError("sse down")

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.message_id == "msg-1"
    assert ack.success is True
    assert ack.should_start_orchestration is True
    deps["room_center"].persist_user_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_emits_processing_then_failed_for_persisted_preflight_failure():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        success=False,
        error="Failed to parse user message",
        status_code=500,
        preflight_outcome="failed",
        preflight_details="Failed to parse user message",
    )
    order: list[tuple[str, str]] = []

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is False
    assert ack.should_start_orchestration is False
    assert ack.error == "Failed to parse user message"
    assert ack.status_code == 500
    assert order == [
        ("emit", "processing"),
        ("emit", "failed"),
    ]
    events = [
        await_call.args[0]
        for await_call in deps["event_publisher"].emit.await_args_list
    ]
    assert len(events) == 2
    _assert_processing_status_event(
        events[0],
        conversation_id="room-1",
        message_id="msg-1",
        status="processing",
        related_message_id="msg-1",
        client_request_id="cr-1",
        details=None,
    )


@pytest.mark.asyncio
async def test_execute_emits_canceled_for_canceled_room_preflight():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = _room_response_with_preflight(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
        preflight_outcome="canceled",
    )
    order: list[tuple[str, str]] = []

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(
            conversation_id="room-1", sender_id="user-1", client_request_id="cr-1"
        )
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    assert order == [
        ("emit", "processing"),
        ("emit", "canceled"),
    ]
    events = [
        await_call.args[0]
        for await_call in deps["event_publisher"].emit.await_args_list
    ]
    assert len(events) == 2
    _assert_processing_status_event(
        events[0],
        conversation_id="room-1",
        message_id="msg-1",
        status="processing",
        related_message_id="msg-1",
        client_request_id="cr-1",
        details=None,
    )


@pytest.mark.asyncio
async def test_start_orchestration_tracks_and_awaits_background_task():
    task_factory = RecordingTaskFactory()
    orchestrator_router = SimpleNamespace(process_room_user_message=AsyncMock())
    facade, deps = _make_facade(
        task_factory=task_factory, orchestrator_router=orchestrator_router
    )
    request = ExecutionRequest(
        conversation_id="room-1",
        sender_id="user-1",
        client_request_id="cr-1",
        parent_message_id="parent-1",
    )
    ack = ExecutionAck(success=True, message_id="msg-1")

    await facade.start_orchestration(request, ack)

    orchestrator_router.process_room_user_message.assert_awaited_once_with(
        request, message_id="msg-1", before_run_start=ANY
    )
    assert task_factory.calls == ["execution-orchestrate-msg-1"]
    assert facade._inflight == set()


@pytest.mark.asyncio
async def test_start_orchestration_skips_when_ack_disables_dispatch():
    facade, _ = _make_facade()
    request = ExecutionRequest(conversation_id="room-1", sender_id="user-1")
    ack = ExecutionAck(
        success=True,
        message_id="msg-1",
        should_start_orchestration=False,
    )

    await facade.start_orchestration(request, ack)

    assert facade._inflight == set()


@pytest.mark.asyncio
async def test_orchestrator_cancel_runs_hitl_cleanup_only_after_router_claim():
    router = SimpleNamespace(route_cancellation_by_user_message=AsyncMock())
    facade, deps = _make_facade(orchestrator_router=router)
    order: list[str] = []
    deps["hitl_message_cancellation"].cancel_requests_for_message.side_effect = (
        lambda _message_id: order.append("hitl")
    )

    async def route(*_args, **kwargs):
        order.append("run_claimed")
        await kwargs["post_claim_cleanup"]()
        order.append("run_reconciled")
        return CancellationAck(
            status="canceled", cancellation_applied=True, reconciled=True
        )

    router.route_cancellation_by_user_message.side_effect = route

    ack = await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    assert isinstance(ack, CancellationAck)
    assert ack.status == "canceled"
    deps[
        "hitl_message_cancellation"
    ].cancel_requests_for_message.assert_awaited_once_with("msg-1")
    router.route_cancellation_by_user_message.assert_awaited_once()
    args = router.route_cancellation_by_user_message.await_args
    assert args.args == ("msg-1",)
    assert args.kwargs["reason"] == "user:user-1"
    assert callable(args.kwargs["post_claim_cleanup"])
    assert order == ["run_claimed", "hitl", "run_reconciled"]


@pytest.mark.asyncio
async def test_run_methods_delegate_to_ports():
    facade, deps = _make_facade()
    deps["run_reader"].get_runs_for_room.return_value = [
        SimpleNamespace(trigger_message_id="user-msg-1")
    ]

    runs = await facade.get_runs_for_room("room-1")
    assert runs[0].trigger_message_id == "user-msg-1"


@pytest.mark.asyncio
async def test_cancel_inflight_tasks_interrupts_without_public_cancellation():
    async def wait_forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            cancellation_reasons.append(exc.args)
            raise
        finally:
            marker.append("cleanup")

    marker = []
    cancellation_reasons = []
    facade, deps = _make_facade()

    async def route(_request, _message_id):
        await wait_forever()

    facade._route_orchestration = route
    facade.schedule_orchestration(
        ExecutionRequest(conversation_id="room-1", sender_id="user-1"),
        ExecutionAck(success=True, message_id="msg-test"),
    )
    (task,) = facade._inflight
    await asyncio.sleep(0)

    assert await facade.cancel_inflight_tasks() == 1
    assert task.cancelled()
    assert marker == ["cleanup"]
    assert cancellation_reasons == [(GRACEFUL_SHUTDOWN_CANCEL_REASON,)]
    assert facade._inflight == set()


@pytest.mark.asyncio
async def test_cancel_inflight_tasks_does_not_mark_task_that_completes_during_shutdown():
    async def completes_normally():
        return "done"

    facade, deps = _make_facade()
    task = asyncio.create_task(completes_normally(), name="execution-test")
    await task
    facade._inflight.add(task)

    assert await facade.cancel_inflight_tasks() == 0
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_hitl_pending_and_cancel_delegate_and_translate():
    facade, deps = _make_facade()
    model_request = SimpleNamespace(
        request_id="req-1",
        conversation_id="room-1",
        user_message_id="user-msg-1",
        public_source="agent",
        interaction_id="interaction-1",
        question_count=1,
        question_index=0,
        prompt="Need input",
        prompt_type="text",
        status="pending",
        display_message_id="display-msg-1",
    )
    deps["hitl_manager"].get_pending_requests.return_value = [model_request]

    pending = await facade.get_pending_hitl("room-1")
    canceled = await facade.cancel_hitl_interaction(
        "room-1",
        "interaction-1",
        3,
    )

    assert pending[0].message_id == "display-msg-1"
    assert canceled == 6
    deps["hitl_manager"].cancel_interaction_by_user.assert_awaited_once_with(
        "interaction-1",
        "room-1",
        expected_version=3,
    )


@pytest.mark.asyncio
async def test_orchestrator_hitl_answer_falls_back_only_for_missing_interaction():
    from approval.exceptions import HITLDeliveryUncertainError

    facade, deps = _make_facade()
    router = SimpleNamespace(
        route_hitl_answer=AsyncMock(side_effect=KeyError("interaction-missing")),
        get_pending_hitl=AsyncMock(return_value=[]),
        cancel_hitl_interaction=AsyncMock(side_effect=KeyError("interaction-missing")),
    )
    facade.bind_orchestrator_router(router)
    deps["hitl_manager"].handle_batch_response.return_value = {
        "request_id": "interaction-missing",
        "status": "accepted",
        "responder_id": "user-1",
    }

    response = await facade.resolve_hitl_batch(
        "room-1",
        "interaction-missing",
        [{"request_id": "q1", "user_input": "NYC"}],
        "user-1",
    )
    assert response.status == "accepted"
    deps["hitl_manager"].handle_batch_response.assert_awaited_once()

    canceled = await facade.cancel_hitl_interaction("room-1", "interaction-missing", 1)
    assert canceled == 6
    deps["hitl_manager"].cancel_interaction_by_user.assert_awaited_once()

    router.route_hitl_answer = AsyncMock(side_effect=KeyError("call-record-1"))
    with pytest.raises(KeyError, match="call-record-1"):
        await facade.resolve_hitl_batch(
            "room-1",
            "interaction-owned",
            [{"request_id": "q1", "user_input": "NYC"}],
            "user-1",
        )

    router.route_hitl_answer = AsyncMock(
        side_effect=HITLDeliveryUncertainError("uncertain")
    )
    with pytest.raises(HITLDeliveryUncertainError):
        await facade.resolve_hitl_batch(
            "room-1",
            "interaction-owned",
            [{"request_id": "q1", "user_input": "NYC"}],
            "user-1",
        )


@pytest.mark.asyncio
async def test_orchestrator_hitl_pending_and_cancel_use_router_when_bound():
    facade, deps = _make_facade()
    from common.dto.execution import HITLRequest

    orch_pending = HITLRequest(
        request_id="q1",
        conversation_id="room-1",
        user_message_id="user-msg-1",
        source="agent",
        prompt="Where?",
        message_id="assistant-1",
        interaction_id="orch-1",
    )
    router = SimpleNamespace(
        get_pending_hitl=AsyncMock(return_value=[orch_pending]),
        cancel_hitl_interaction=AsyncMock(return_value=1),
        route_hitl_answer=AsyncMock(return_value="completed"),
    )
    facade.bind_orchestrator_router(router)

    pending = await facade.get_pending_hitl("room-1")
    assert len(pending) == 1
    assert pending[0].interaction_id == "orch-1"

    canceled = await facade.cancel_hitl_interaction("room-1", "orch-1", 1)
    assert canceled == 1
    router.cancel_hitl_interaction.assert_awaited_once()
    deps["hitl_manager"].cancel_interaction_by_user.assert_not_awaited()

    await facade.resolve_hitl_batch(
        "room-1",
        "orch-1",
        [{"request_id": "q1", "user_input": "NYC"}],
        "user-1",
        client_request_id="cr-1",
    )
    router.route_hitl_answer.assert_awaited_once()
    deps["hitl_manager"].handle_batch_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_pending_hitl_dedupes_legacy_and_orchestrator_by_interaction_request():
    facade, deps = _make_facade()
    from common.dto.execution import HITLRequest

    legacy = SimpleNamespace(
        request_id="q1",
        conversation_id="room-1",
        user_message_id="user-msg-1",
        public_source="agent",
        interaction_id="shared-1",
        question_count=1,
        question_index=0,
        prompt="Legacy prompt",
        prompt_type="text",
        status="pending",
        display_message_id="legacy-msg",
    )
    deps["hitl_manager"].get_pending_requests.return_value = [legacy]
    orch_pending = HITLRequest(
        request_id="q1",
        conversation_id="room-1",
        user_message_id="user-msg-1",
        source="agent",
        prompt="Orchestrator prompt",
        message_id="orch-msg",
        interaction_id="shared-1",
    )
    router = SimpleNamespace(
        get_pending_hitl=AsyncMock(return_value=[orch_pending]),
    )
    facade.bind_orchestrator_router(router)

    pending = await facade.get_pending_hitl("room-1")

    assert len(pending) == 1
    assert pending[0].interaction_id == "shared-1"
    assert pending[0].prompt == "Orchestrator prompt"
    assert pending[0].message_id == "orch-msg"


def test_room_response_to_execution_ack_preserves_missing_message_error_shape():
    response = RoomCenterUserMessageResponse(
        success=False,
        error="Message is required",
        status_code=400,
        message_id=None,
        message=None,
    )

    ack = room_response_to_execution_ack(response)

    assert ack.message_id is None
    assert ack.message is None
    assert ack.error == "Message is required"


def test_room_response_to_execution_ack_skips_orchestration_without_dispatch_root():
    response = RoomCenterUserMessageResponse(
        success=True,
        message_id="msg-1",
        dispatch_root_message_id=None,
    )

    ack = room_response_to_execution_ack(response)

    assert ack.success is True
    assert ack.message_id == "msg-1"
    assert ack.should_start_orchestration is False


@pytest.mark.asyncio
async def test_supervisor_hitl_answer_falls_back_to_unified_manager():
    from execution.orchestrator_routing import OrchestratorHITLNotOwnedError

    router = SimpleNamespace(
        route_hitl_answer=AsyncMock(
            side_effect=OrchestratorHITLNotOwnedError("interaction-1")
        )
    )
    facade, deps = _make_facade(orchestrator_router=router)
    deps["hitl_manager"].handle_batch_response.return_value = {
        "request_id": "request-1",
        "status": "applied",
    }

    response = await facade.resolve_hitl_batch(
        "room-1",
        "interaction-1",
        [{"request_id": "request-1", "user_input": "Shanghai"}],
        "user-1",
    )

    router.route_hitl_answer.assert_awaited_once()
    deps["hitl_manager"].handle_batch_response.assert_awaited_once_with(
        conversation_id="room-1",
        interaction_id="interaction-1",
        answers=[{"request_id": "request-1", "user_input": "Shanghai"}],
        user_id="user-1",
        client_request_id=None,
    )
    assert response.status == "applied"
