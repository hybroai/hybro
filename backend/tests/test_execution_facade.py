import asyncio
import inspect
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.dto import (
    CancellationAck,
    ExecutionAck,
    ExecutionRequest,
    ProcessingStatusEvent,
    RunInfo,
)
from execution.cancellation import CancellationPropagationResult
from execution.facade import (
    ExecutionFacade,
    RoomCenterPort,
)
from execution.orchestration.run_store import (
    InMemoryOrchestrationRunStore,
    OrchestrationStoreConflict,
)
from execution.shutdown import GRACEFUL_SHUTDOWN_CANCEL_REASON
from execution.translators import room_response_to_execution_ack
from models.orchestration import (
    OrchestrationEventType,
    OrchestrationRunState,
    OrchestrationStatus,
)
from models.response import RoomCenterUserMessageResponse
from models.run import RunState
from room.route_adapter import RoomRouteAdapter


class RecordingTaskFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, coro, *, name=None):
        self.calls.append(name)
        return asyncio.create_task(coro, name=name)


def _make_facade(**overrides):
    room_center = SimpleNamespace(
        get_idempotent_user_message=AsyncMock(return_value=None),
        send_message_to_room=AsyncMock(),
    )

    async def persist_message_to_room(*args, **kwargs):
        response = await room_center.send_message_to_room(*args, **kwargs)
        return response, response if response.message_id else None

    async def run_message_preflight_to_room(context):
        return context

    room_center.persist_message_to_room = AsyncMock(side_effect=persist_message_to_room)
    room_center.run_message_preflight_to_room = AsyncMock(
        side_effect=run_message_preflight_to_room
    )
    room_center.discard_message_preflight = MagicMock()
    room_center.update_user_message_orchestration_status = AsyncMock(return_value=True)
    hitl_manager = SimpleNamespace(
        request_interaction=AsyncMock(),
        handle_response=AsyncMock(),
        handle_batch_response=AsyncMock(),
        get_pending_requests=AsyncMock(return_value=[]),
        cancel_request=AsyncMock(return_value=None),
        cancel_interaction_by_user=AsyncMock(return_value=6),
    )
    run_lifecycle = SimpleNamespace(
        heal_diverged_runs=AsyncMock(return_value=2),
        record_processing_status=AsyncMock(return_value=None),
        project_run_state=AsyncMock(return_value={"event_id": "cancel-event"}),
    )
    run_reader = SimpleNamespace(
        get_run=AsyncMock(return_value=None),
        get_runs_for_room=AsyncMock(return_value=[]),
    )
    cancellation_state = SimpleNamespace(
        cancel_message_and_broadcast=AsyncMock(),
        get_active_token=MagicMock(return_value=None),
        release_active_token=MagicMock(return_value=True),
        clear_cancellation=MagicMock(),
    )
    cancellation_repository = SimpleNamespace(
        request=AsyncMock(return_value=True),
        mark_reconciled=AsyncMock(return_value=True),
    )
    hitl_message_cancellation = SimpleNamespace(
        cancel_requests_for_message=AsyncMock(),
    )
    agent_task_cleanup = SimpleNamespace(
        cleanup_cancelled_message_tasks=AsyncMock(),
    )
    event_publisher = SimpleNamespace(emit=AsyncMock())
    client_request_id_resolver = SimpleNamespace(
        resolve_client_request_id=AsyncMock(side_effect=lambda _, provided: provided),
    )
    deps = {
        "room_center": room_center,
        "hitl_manager": hitl_manager,
        "run_lifecycle": run_lifecycle,
        "run_reader": run_reader,
        "cancellation_state": cancellation_state,
        "cancellation_repository": cancellation_repository,
        "cancellation_message_reader": AsyncMock(return_value=None),
        "hitl_message_cancellation": hitl_message_cancellation,
        "agent_task_cleanup": agent_task_cleanup,
        "event_publisher": event_publisher,
        "run_event_enabled": lambda: False,
        "client_request_id_resolver": client_request_id_resolver,
        "task_factory": RecordingTaskFactory(),
    }
    deps.update(overrides)
    facade = ExecutionFacade(**deps)
    return facade, deps


def _assert_processing_status_event(
    event,
    *,
    room_id: str,
    message_id: str,
    status: str,
    related_message_id: str,
    client_request_id: str,
    details: dict | None,
):
    assert isinstance(event, ProcessingStatusEvent)
    assert event.event_type == "processing_status"
    assert event.room_id == room_id
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
    ].send_message_to_room.return_value = RoomCenterUserMessageResponse(
        room_id="room-1",
        message_id="msg-1",
        user_id="user-1",
        user_name="User",
        success=True,
    )
    request = ExecutionRequest(
        room_id="room-1",
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
    deps["room_center"].send_message_to_room.assert_awaited_once()
    sent_request = deps["room_center"].send_message_to_room.await_args.args[0]
    assert sent_request.user_id == "user-1"
    assert sent_request.message.message_content.message_text == "hello"
    assert deps["room_center"].send_message_to_room.await_args.args[1:] == (
        "room_team",
        ["agent-1"],
    )


@pytest.mark.asyncio
async def test_execute_rejects_pending_hitl_before_room_persist():
    facade, deps = _make_facade()
    deps["hitl_manager"].get_pending_requests.return_value = [SimpleNamespace()]
    deps["room_center"].send_message_to_room.side_effect = AssertionError(
        "room persist should not be called"
    )

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is False
    assert ack.room_id == "room-1"
    assert ack.status_code == 409
    assert ack.should_start_orchestration is False
    assert "waiting for your input" in ack.error
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["room_center"].send_message_to_room.assert_not_awaited()
    deps["run_reader"].get_run.assert_not_awaited()
    deps["run_reader"].get_runs_for_room.assert_not_awaited()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_rejects_active_run_before_room_persist():
    facade, deps = _make_facade()
    deps["room_center"].send_message_to_room.side_effect = AssertionError(
        "room persist should not be called"
    )
    deps["run_reader"].get_runs_for_room.return_value = [
        RunInfo(
            run_id="run-1",
            room_id="room-1",
            state="processing",
            trigger_message_id="msg-active",
        )
    ]

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is False
    assert ack.room_id == "room-1"
    assert ack.status_code == 409
    assert ack.should_start_orchestration is False
    assert "already processing" in ack.error
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")
    deps["room_center"].send_message_to_room.assert_not_awaited()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_replay_lookup_precedes_busy_guards_and_bypasses_them():
    facade, deps = _make_facade()
    order: list[str] = []

    async def lookup(**_kwargs):
        order.append("idempotency")
        return RoomCenterUserMessageResponse(
            room_id="room-1",
            message_id="existing-message",
            success=True,
            status_code=200,
        )

    async def pending(_room_id):
        order.append("hitl")
        return [SimpleNamespace()]

    async def active(_room_id):
        order.append("active")
        return [
            RunInfo(
                run_id="run-1",
                room_id="room-1",
                state="processing",
            )
        ]

    deps["room_center"].get_idempotent_user_message.side_effect = lookup
    deps["hitl_manager"].get_pending_requests.side_effect = pending
    deps["run_reader"].get_runs_for_room.side_effect = active

    ack = await facade.execute(
        ExecutionRequest(
            room_id="room-1",
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
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_guard", ["hitl", "active_run"])
async def test_execute_rechecks_idempotency_when_winner_appears_during_busy_guard(
    busy_guard: str,
):
    facade, deps = _make_facade()
    winner = RoomCenterUserMessageResponse(
        room_id="room-1",
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
                room_id="room-1",
                state="processing",
            )
        ]

    ack = await facade.execute(
        ExecutionRequest(
            room_id="room-1",
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
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_idempotency_conflict_returns_body_level_409_before_busy_guards():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].get_idempotent_user_message.return_value = RoomCenterUserMessageResponse(
        room_id="room-1",
        success=False,
        error="The client_request_id was already used for a different request",
        status_code=409,
    )

    ack = await facade.execute(
        ExecutionRequest(
            room_id="room-1",
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
                room_id="room-1",
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
                    room_id="room-1",
                    message_id="winner-message",
                    dispatch_root_message_id=None,
                    success=True,
                    status_code=200,
                ),
                None,
            )
        ),
        run_message_preflight_to_room=AsyncMock(),
        update_user_message_orchestration_status=AsyncMock(return_value=True),
    )
    facade, deps = _make_facade(room_center=room_center)

    ack = await facade.execute(
        ExecutionRequest(
            room_id="room-1",
            sender_id="user-1",
            client_request_id="request-1",
            message=_user_message_payload("hello"),
        )
    )

    assert ack.success is True
    assert ack.message_id == "winner-message"
    assert ack.should_start_orchestration is False
    room_center.run_message_preflight_to_room.assert_not_awaited()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_continues_when_hitl_pending_lookup_fails():
    facade, deps = _make_facade()
    order: list[str] = []

    async def get_pending_requests(_room_id):
        order.append("hitl")
        raise RuntimeError("hitl down")

    async def get_runs_for_room(_room_id):
        order.append("active_runs")
        return []

    async def send_message_to_room(*_args, **_kwargs):
        order.append("persist")
        return _room_response_with_preflight(
            room_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["hitl_manager"].get_pending_requests.side_effect = get_pending_requests
    deps["run_reader"].get_runs_for_room.side_effect = get_runs_for_room
    deps["room_center"].send_message_to_room.side_effect = send_message_to_room

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.message_id == "msg-1"
    assert ack.should_start_orchestration is True
    assert order == ["hitl", "active_runs", "persist"]
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["room_center"].send_message_to_room.assert_awaited_once()
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")


@pytest.mark.asyncio
async def test_execute_continues_when_active_run_lookup_fails():
    facade, deps = _make_facade()
    order: list[str] = []

    async def get_pending_requests(_room_id):
        order.append("hitl")
        return []

    async def get_runs_for_room(_room_id):
        order.append("active_runs")
        raise RuntimeError("runs down")

    async def send_message_to_room(*_args, **_kwargs):
        order.append("persist")
        return _room_response_with_preflight(
            room_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["hitl_manager"].get_pending_requests.side_effect = get_pending_requests
    deps["run_reader"].get_runs_for_room.side_effect = get_runs_for_room
    deps["room_center"].send_message_to_room.side_effect = send_message_to_room

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.message_id == "msg-1"
    assert ack.should_start_orchestration is True
    assert order == ["hitl", "active_runs", "persist"]
    deps["hitl_manager"].get_pending_requests.assert_awaited_once_with("room-1")
    deps["run_reader"].get_runs_for_room.assert_awaited_once_with("room-1")
    deps["room_center"].send_message_to_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_orchestration_carries_live_mode_and_scope():
    facade, _deps = _make_facade()
    facade._route_orchestration = AsyncMock()
    request = ExecutionRequest(
        room_id="room-1",
        sender_id="user-1",
        client_request_id="cr-1",
        mode="supervisor",
        agent_scope={"source": "all_agents"},
    )
    ack = ExecutionAck(
        room_id="room-1",
        message_id="msg-1",
        success=True,
        should_start_orchestration=True,
        preflight_outcome="ready",
    )

    facade.schedule_orchestration(request, ack)
    await asyncio.sleep(0)

    routed_request, orchestration_request = facade._route_orchestration.call_args.args
    assert routed_request is request
    # The route-validated mode and scope travel with the orchestration
    # request so routing never depends on the persisted extend_info rewrite.
    assert orchestration_request.mode == "supervisor"
    assert orchestration_request.agent_scope == {"source": "all_agents"}


@pytest.mark.asyncio
async def test_bound_canonical_active_run_reader_rejects_before_message_persistence():
    facade, deps = _make_facade()
    canonical_reader = SimpleNamespace(
        get_runs_for_room=AsyncMock(return_value=[{"run_id": "run-1"}])
    )
    facade.bind_active_run_reader(canonical_reader)

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
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
    ].send_message_to_room.return_value = _room_response_with_preflight(
        room_id="room-1",
        message_id="frontend-msg-1",
        dispatch_root_message_id="root-msg-1",
        success=True,
        preflight_outcome="ready",
    )
    order: list[tuple[str, str]] = []

    async def record_status(_room_id, status, _message_id, **_kwargs):
        order.append(("record", status))

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["run_lifecycle"].record_processing_status.side_effect = record_status
    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.should_start_orchestration is True
    assert order == [("emit", "processing")]
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["event_publisher"].emit.assert_awaited_once()
    event = deps["event_publisher"].emit.await_args.args[0]
    _assert_processing_status_event(
        event,
        room_id="room-1",
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
        send_message_to_room=AsyncMock(
            side_effect=AssertionError(
                "legacy single-step room path should not be used"
            )
        ),
        persist_message_to_room=AsyncMock(
            return_value=(
                RoomCenterUserMessageResponse(
                    room_id="room-1",
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

    async def record_status(_room_id, status, _message_id, **_kwargs):
        order.append(f"record:{status}")

    async def emit_event(event):
        order.append(f"emit:{event.status}")

    async def continue_preflight(context):
        assert context is preflight_context
        order.append("room_preflight")
        return _room_response_with_preflight(
            room_id="room-1",
            message_id="msg-1",
            dispatch_root_message_id="msg-1",
            success=True,
            preflight_outcome="ready",
        )

    deps["run_lifecycle"].record_processing_status.side_effect = record_status
    deps["event_publisher"].emit.side_effect = emit_event
    room_center.run_message_preflight_to_room.side_effect = continue_preflight

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.should_start_orchestration is True
    assert order == ["emit:processing", "room_preflight"]
    room_center.persist_message_to_room.assert_awaited_once()
    room_center.run_message_preflight_to_room.assert_awaited_once_with(
        preflight_context
    )
    room_center.send_message_to_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_cancellation_during_preflight_processing_status_discards_token():
    facade, deps = _make_facade()
    preflight_context = object()
    deps["room_center"].persist_message_to_room.side_effect = None
    deps["room_center"].persist_message_to_room.return_value = (
        RoomCenterUserMessageResponse(
            room_id="room-1",
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
        await facade.execute(ExecutionRequest(room_id="room-1", sender_id="user-1"))

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
                    room_id="room-1",
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
    adapter = RoomRouteAdapter(bound_room_runtime=runtime)
    facade, _ = _make_facade(room_center=adapter)
    facade._emit_room_preflight_processing_status = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await facade.execute(ExecutionRequest(room_id="room-1", sender_id="user-1"))

    assert not inspect.iscoroutinefunction(RoomRouteAdapter.discard_message_preflight)
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
                    room_id="room-1",
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
    adapter = RoomRouteAdapter(bound_room_runtime=runtime)
    facade, _ = _make_facade(room_center=adapter)
    facade._emit_room_preflight_processing_status = AsyncMock(
        side_effect=asyncio.CancelledError("original cancellation")
    )

    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await facade.execute(ExecutionRequest(room_id="room-1", sender_id="user-1"))

    runtime.discard_message_preflight.assert_called_once_with(preflight_context)
    assert "room preflight cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_execute_cancellation_after_ready_preflight_discards_token():
    facade, deps = _make_facade()
    preflight_context = object()
    deps["room_center"].persist_message_to_room.side_effect = None
    deps["room_center"].persist_message_to_room.return_value = (
        RoomCenterUserMessageResponse(
            room_id="room-1",
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
        room_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )
    facade._emit_room_preflight_terminal_status = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await facade.execute(ExecutionRequest(room_id="room-1", sender_id="user-1"))

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
            room_id="room-1",
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
        room_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )

    ack = await facade.execute(ExecutionRequest(room_id="room-1", sender_id="user-1"))

    assert ack.should_start_orchestration is True
    deps["room_center"].discard_message_preflight.assert_not_called()


@pytest.mark.asyncio
async def test_execute_does_not_emit_completed_for_success_without_preflight_outcome():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].send_message_to_room.return_value = RoomCenterUserMessageResponse(
        room_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
    )

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    event = deps["event_publisher"].emit.await_args.args[0]
    assert event.status == "processing"


@pytest.mark.asyncio
async def test_execute_emits_completed_for_completed_room_preflight():
    facade, deps = _make_facade()
    deps["run_lifecycle"].record_processing_status.return_value = {"accepted": True}
    deps[
        "room_center"
    ].send_message_to_room.return_value = _room_response_with_preflight(
        room_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
        preflight_outcome="completed",
    )

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
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
    ].send_message_to_room.return_value = _room_response_with_preflight(
        room_id="room-1",
        message_id="msg-1",
        dispatch_root_message_id="msg-1",
        success=True,
        preflight_outcome="ready",
    )
    deps["run_lifecycle"].record_processing_status.side_effect = RuntimeError(
        "sse down"
    )

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.message_id == "msg-1"
    assert ack.success is True
    assert ack.should_start_orchestration is True
    deps["room_center"].send_message_to_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_emits_processing_then_failed_for_persisted_preflight_failure():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].send_message_to_room.return_value = _room_response_with_preflight(
        room_id="room-1",
        message_id="msg-1",
        success=False,
        error="Failed to parse user message",
        status_code=500,
        preflight_outcome="failed",
        preflight_details="Failed to parse user message",
    )
    order: list[tuple[str, str]] = []

    async def record_status(_room_id, status, _message_id, **_kwargs):
        order.append(("record", status))
        return {"accepted": True}

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["run_lifecycle"].record_processing_status.side_effect = record_status
    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is False
    assert ack.should_start_orchestration is False
    assert ack.error == "Failed to parse user message"
    assert ack.status_code == 500
    assert order == [
        ("emit", "processing"),
        ("emit", "failed"),
    ]
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    events = [
        await_call.args[0]
        for await_call in deps["event_publisher"].emit.await_args_list
    ]
    assert len(events) == 2
    _assert_processing_status_event(
        events[0],
        room_id="room-1",
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
    ].send_message_to_room.return_value = _room_response_with_preflight(
        room_id="room-1",
        message_id="msg-1",
        success=True,
        status_code=200,
        preflight_outcome="canceled",
    )
    order: list[tuple[str, str]] = []

    async def record_status(_room_id, status, _message_id, **_kwargs):
        order.append(("record", status))
        return {"accepted": True}

    async def emit_event(event):
        order.append(("emit", event.status))

    deps["run_lifecycle"].record_processing_status.side_effect = record_status
    deps["event_publisher"].emit.side_effect = emit_event

    ack = await facade.execute(
        ExecutionRequest(room_id="room-1", sender_id="user-1", client_request_id="cr-1")
    )

    assert ack.success is True
    assert ack.should_start_orchestration is False
    assert order == [
        ("emit", "processing"),
        ("emit", "canceled"),
    ]
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    events = [
        await_call.args[0]
        for await_call in deps["event_publisher"].emit.await_args_list
    ]
    assert len(events) == 2
    _assert_processing_status_event(
        events[0],
        room_id="room-1",
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
        room_id="room-1",
        sender_id="user-1",
        client_request_id="cr-1",
        parent_message_id="parent-1",
    )
    ack = ExecutionAck(success=True, message_id="msg-1")

    await facade.start_orchestration(request, ack)

    orchestration_request = (
        orchestrator_router.process_room_user_message.call_args.args[0]
    )
    assert orchestration_request.room_id == "room-1"
    assert orchestration_request.room_user_message_id == "msg-1"
    assert orchestration_request.user_id == "user-1"
    assert orchestration_request.client_request_id == "cr-1"
    assert orchestration_request.room_related_message_id == "parent-1"
    assert orchestrator_router.process_room_user_message.call_args.kwargs == {}
    assert task_factory.calls == ["execution-orchestrate-msg-1"]
    assert facade._inflight == set()


@pytest.mark.asyncio
async def test_start_orchestration_skips_when_ack_disables_dispatch():
    facade, _ = _make_facade()
    request = ExecutionRequest(room_id="room-1", sender_id="user-1")
    ack = ExecutionAck(
        success=True,
        message_id="msg-1",
        should_start_orchestration=False,
    )

    await facade.start_orchestration(request, ack)

    assert facade._inflight == set()


@pytest.mark.core
@pytest.mark.core
@pytest.mark.asyncio
async def test_cancel_preserves_order_and_requested_by_user_id():
    order = []
    facade, deps = _make_facade()

    async def broadcast(message_id):
        order.append("broadcast")

    async def cancel_hitl(message_id):
        order.append("hitl")

    async def persist(message_id, user_id):
        order.append(("persist", user_id))
        return True

    async def record(*args, **kwargs):
        order.append("record")
        return {"event_id": "cancel-event"}

    async def cleanup(**kwargs):
        order.append("cleanup")

    deps["cancellation_state"].cancel_message_and_broadcast.side_effect = broadcast
    deps[
        "hitl_message_cancellation"
    ].cancel_requests_for_message.side_effect = cancel_hitl
    deps["cancellation_repository"].request.side_effect = persist
    deps["run_lifecycle"].project_run_state.side_effect = record
    deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.side_effect = cleanup

    assert await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    assert order == [
        ("persist", "user-1"),
        "record",
        "broadcast",
        "hitl",
        "cleanup",
        "cleanup",
    ]
    deps[
        "room_center"
    ].update_user_message_orchestration_status.assert_awaited_once_with(
        "msg-1",
        "canceled",
    )
    deps["cancellation_state"].clear_cancellation.assert_not_called()


@pytest.mark.asyncio
async def test_failed_cancellation_broadcast_stays_pending_then_retry_reconciles():
    facade, deps = _make_facade()
    deps["cancellation_state"].cancel_message_and_broadcast.side_effect = [
        CancellationPropagationResult(
            kv_configured=True,
            kv_succeeded=False,
            pubsub_configured=True,
            pubsub_succeeded=False,
        ),
        CancellationPropagationResult(
            kv_configured=True,
            kv_succeeded=True,
            pubsub_configured=True,
            pubsub_succeeded=True,
        ),
    ]

    first = await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    assert first is True
    deps["cancellation_repository"].mark_reconciled.assert_not_awaited()
    deps["cancellation_state"].clear_cancellation.assert_not_called()
    deps["hitl_message_cancellation"].cancel_requests_for_message.assert_awaited_once()
    assert deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.await_count == 2

    retried = await facade.finalize_pending_cancellation(
        room_id="room-1",
        message_id="msg-1",
        settle_no_run=True,
    )

    assert retried.status == OrchestrationStatus.CANCELED
    assert retried.cancellation_applied is True
    assert retried.reconciled is True
    deps["cancellation_repository"].mark_reconciled.assert_awaited_once_with("msg-1")
    deps["cancellation_state"].clear_cancellation.assert_called_once_with("msg-1")
    assert deps["cancellation_state"].cancel_message_and_broadcast.await_count == 2


@pytest.mark.asyncio
async def test_cancel_terminalizes_awaiting_orchestration_and_clears_hitl_state():
    run_store = InMemoryOrchestrationRunStore()
    await run_store.create_run(
        OrchestrationRunState(
            run_id="run-1",
            room_id="room-1",
            user_message_id="msg-1",
            goal="Get quote",
            candidate_agent_ids=["agent-1"],
            status=OrchestrationStatus.AWAITING_USER,
            pending_hitl_request_ids=["hitl-1"],
            open_questions=[{"request_id": "hitl-1", "status": "open"}],
        )
    )
    facade, deps = _make_facade(orchestration_run_store=run_store)

    assert await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    saved = await run_store.get_run("run-1")
    assert saved is not None
    assert saved.status == OrchestrationStatus.CANCELED
    assert saved.pending_hitl_request_ids == []
    assert saved.open_questions == [{"request_id": "hitl-1", "status": "canceled"}]
    deps[
        "room_center"
    ].update_user_message_orchestration_status.assert_awaited_once_with(
        "msg-1",
        "canceled",
    )
    terminal_events = [
        event
        for event in run_store._events_by_run["run-1"]
        if event.type == OrchestrationEventType.RUN_TERMINAL
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].event_id == "run-1:run-terminal:canceled:1"
    assert terminal_events[0].payload == {
        "status": "canceled",
        "reason": "request canceled",
    }

    assert await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )
    terminal_events = [
        event
        for event in run_store._events_by_run["run-1"]
        if event.type == OrchestrationEventType.RUN_TERMINAL
    ]
    assert len(terminal_events) == 1
    assert deps["cancellation_repository"].request.await_count == 2
    assert deps["cancellation_state"].cancel_message_and_broadcast.await_count == 2
    assert (
        deps["hitl_message_cancellation"].cancel_requests_for_message.await_count == 2
    )
    assert deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.await_count == 4


@pytest.mark.asyncio
async def test_cancel_does_not_rewrite_budget_exhausted_orchestration():
    run_store = InMemoryOrchestrationRunStore()
    await run_store.create_run(
        OrchestrationRunState(
            run_id="run-1",
            room_id="room-1",
            user_message_id="msg-1",
            goal="Get quote",
            candidate_agent_ids=["agent-1"],
            status=OrchestrationStatus.BUDGET_EXHAUSTED,
            terminal_reason="step budget exhausted",
        )
    )
    facade, deps = _make_facade(orchestration_run_store=run_store)

    assert await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    saved = await run_store.get_run("run-1")
    assert saved is not None
    assert saved.status == OrchestrationStatus.BUDGET_EXHAUSTED
    assert saved.terminal_reason == "step budget exhausted"
    deps[
        "room_center"
    ].update_user_message_orchestration_status.assert_awaited_once_with(
        "msg-1",
        "budget_exhausted",
    )
    deps["cancellation_repository"].request.assert_awaited_once_with(
        "msg-1",
        "user-1",
    )
    deps["cancellation_repository"].mark_reconciled.assert_awaited_once_with("msg-1")
    deps["cancellation_state"].cancel_message_and_broadcast.assert_not_awaited()
    deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.assert_not_awaited()
    deps["run_lifecycle"].project_run_state.assert_awaited_once_with(
        room_id="room-1",
        run_id="msg-1",
        trigger_message_id="msg-1",
        target_state=RunState.FAILED,
        terminal_reason=None,
        causation_id=("orchestration-terminal-repair:msg-1:budget_exhausted"),
    )


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
async def test_cancel_rechecks_canonical_state_after_marker_persistence():
    dispatching = OrchestrationRunState(
        run_id="run-1",
        room_id="room-1",
        user_message_id="msg-1",
        goal="Get quote",
        candidate_agent_ids=["agent-1"],
        status=OrchestrationStatus.DISPATCHING,
    )
    completed = dispatching.model_copy(update={"status": OrchestrationStatus.COMPLETED})
    current = dispatching

    async def get_latest(_message_id):
        return current

    run_store = SimpleNamespace(
        get_latest_by_user_message_id=AsyncMock(side_effect=get_latest),
        save_state=AsyncMock(side_effect=OrchestrationStoreConflict("race")),
    )
    facade, deps = _make_facade(orchestration_run_store=run_store)

    async def persist_marker(_message_id, _user_id):
        nonlocal current
        current = completed
        return True

    deps["cancellation_repository"].request.side_effect = persist_marker

    assert await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    deps[
        "room_center"
    ].update_user_message_orchestration_status.assert_awaited_once_with(
        "msg-1",
        "completed",
    )
    deps["cancellation_state"].cancel_message_and_broadcast.assert_not_awaited()
    deps["hitl_message_cancellation"].cancel_requests_for_message.assert_not_awaited()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()
    deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_conflict_does_not_report_success_for_nonterminal_run():
    dispatching = OrchestrationRunState(
        run_id="run-1",
        room_id="room-1",
        user_message_id="msg-1",
        goal="Get quote",
        candidate_agent_ids=["agent-1"],
        status=OrchestrationStatus.DISPATCHING,
    )
    run_store = SimpleNamespace(
        get_latest_by_user_message_id=AsyncMock(return_value=dispatching),
        save_state=AsyncMock(side_effect=OrchestrationStoreConflict("race")),
    )
    facade, deps = _make_facade(orchestration_run_store=run_store)

    ack = await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    assert isinstance(ack, CancellationAck)
    assert ack.status == "cancellation_pending"
    assert ack.cancellation_applied is False
    assert ack.reconciled is False
    deps["cancellation_repository"].request.assert_awaited_once()
    deps["cancellation_state"].cancel_message_and_broadcast.assert_awaited_once_with(
        "msg-1"
    )
    deps["hitl_message_cancellation"].cancel_requests_for_message.assert_not_awaited()
    deps["agent_task_cleanup"].cleanup_cancelled_message_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_clears_cancellation_when_persistence_fails():
    facade, deps = _make_facade()
    deps["cancellation_repository"].request.return_value = False

    assert not await facade.cancel(
        "room-1",
        "msg-1",
        requested_by_user_id="user-1",
    )

    deps["cancellation_state"].cancel_message_and_broadcast.assert_not_awaited()
    deps["hitl_message_cancellation"].cancel_requests_for_message.assert_not_awaited()
    deps["cancellation_state"].clear_cancellation.assert_not_called()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_methods_delegate_to_ports():
    facade, deps = _make_facade()
    deps["run_reader"].get_runs_for_room.return_value = [
        SimpleNamespace(trigger_message_id="user-msg-1")
    ]

    assert await facade.get_run("run-1") is None
    runs = await facade.get_runs_for_room("room-1")
    assert runs[0].trigger_message_id == "user-msg-1"
    assert await facade.heal_diverged_runs(limit=123) == 2
    deps["run_lifecycle"].heal_diverged_runs.assert_awaited_once_with(limit=123)


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
    task = facade._spawn_orchestration(
        wait_forever(),
        name="execution-test",
    )
    await asyncio.sleep(0)

    assert await facade.cancel_inflight_tasks() == 1
    assert task.cancelled()
    assert marker == ["cleanup"]
    assert cancellation_reasons == [(GRACEFUL_SHUTDOWN_CANCEL_REASON,)]
    assert facade._inflight == set()
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()


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
    deps["run_lifecycle"].record_processing_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_hitl_pending_and_cancel_delegate_and_translate():
    facade, deps = _make_facade()
    model_request = SimpleNamespace(
        request_id="req-1",
        room_id="room-1",
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
    from execution.hitl.exceptions import HITLDeliveryUncertainError

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
        room_id="room-1",
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
        room_id="room-1",
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
        room_id="room-1",
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
        room_id="room-1",
        interaction_id="interaction-1",
        answers=[{"request_id": "request-1", "user_input": "Shanghai"}],
        user_id="user-1",
        client_request_id=None,
    )
    assert response.status == "applied"
