"""Send admission and cancellation before an orchestration Run exists.

The orchestration task creates the Run some time after the send is
acknowledged. These tests pin what happens inside that window: a second send
to the same Room is rejected, a task that fails before its Run still ends the
message, and a cancel stops the Run from starting.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.dto import CancellationAck, ExecutionAck, ExecutionRequest
from common.errors import NotFoundError
from execution import orchestrator_routing
from execution.orchestrator import SessionConflict
from execution.orchestrator_routing import DualRuntimeRouter
from models.response import RoomCenterUserMessageResponse
from tests.test_execution_facade import RecordingTaskFactory, _make_facade


def _request(client_request_id: str, *, room: str = "room-1") -> ExecutionRequest:
    return ExecutionRequest(
        conversation_id=room,
        sender_id="user-1",
        client_request_id=client_request_id,
        message={
            "room_id": room,
            "message_id": "",
            "message_type": "user",
            "message_content": {"message_text": client_request_id},
        },
    )


def _accepted(
    message_id: str, *, room: str = "room-1"
) -> RoomCenterUserMessageResponse:
    return RoomCenterUserMessageResponse(
        conversation_id=room,
        message_id=message_id,
        dispatch_root_message_id=message_id,
        success=True,
        preflight_outcome="ready",
    )


def _ack(message_id: str, *, room: str = "room-1") -> ExecutionAck:
    return ExecutionAck(
        conversation_id=room,
        message_id=message_id,
        dispatch_root_message_id=message_id,
        success=True,
        should_start_orchestration=True,
        preflight_outcome="ready",
    )


def _statuses(deps) -> list[tuple[str, str, str | None]]:
    return [
        (event.status, event.message_id, event.client_request_id)
        for event in (
            call.args[0] for call in deps["event_publisher"].emit.call_args_list
        )
        if getattr(event, "event_type", None) == "processing_status"
    ]


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1.0)


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class _GatedRouter:
    """Router double that pauses before asking to start the Run."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.reached = asyncio.Event()
        self.admitted: list[bool] = []
        self.error: Exception | None = None
        self.run_exists = False
        self.route_cancellation_by_user_message = AsyncMock(side_effect=self._no_run)

    @staticmethod
    async def _no_run(message_id: str, **_kwargs):
        raise KeyError(message_id)

    async def process_room_user_message(self, request, *, message_id, before_run_start):
        self.reached.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        self.admitted.append(before_run_start())

    async def has_run_for_user_message(self, message_id: str) -> bool:
        return self.run_exists


# -- H1: send admission ------------------------------------------------------


async def test_concurrent_sends_to_one_room_admit_only_one():
    facade, deps = _make_facade()
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()
    persisted: list[str] = []

    async def persist(request, *_args, **_kwargs):
        persisted.append(request.client_request_id)
        persist_started.set()
        await release_persist.wait()
        return _accepted("msg-1")

    deps["room_center"].persist_user_message.side_effect = persist

    first = asyncio.create_task(facade.execute(_request("cr-1")))
    await _wait(persist_started)
    # Before the fix the second send also waited inside persistence.
    second = await asyncio.wait_for(facade.execute(_request("cr-2")), timeout=1.0)
    release_persist.set()
    first_ack = await first

    assert first_ack.success is True
    assert first_ack.should_start_orchestration is True
    assert second.success is False
    assert second.status_code == 409
    assert second.error == "Room is already processing another message."
    assert second.should_start_orchestration is False
    assert persisted == ["cr-1"]


async def test_send_is_rejected_while_the_room_orchestration_task_runs():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)
    deps["room_center"].persist_user_message.return_value = _accepted("msg-1")

    first_ack = await facade.execute(_request("cr-1"))
    facade.schedule_orchestration(_request("cr-1"), first_ack)
    await _wait(router.reached)

    deps["room_center"].persist_user_message.return_value = _accepted("msg-2")
    second = await facade.execute(_request("cr-2"))

    assert second.status_code == 409
    assert second.should_start_orchestration is False
    assert deps["room_center"].persist_user_message.await_count == 1

    router.release.set()
    await _drain()

    third = await facade.execute(_request("cr-3"))
    assert third.success is True
    assert third.should_start_orchestration is True


async def test_send_that_does_not_start_orchestration_releases_the_room():
    facade, deps = _make_facade()
    deps[
        "room_center"
    ].persist_user_message.return_value = RoomCenterUserMessageResponse(
        conversation_id="room-1",
        message_id="msg-1",
        success=True,
        preflight_outcome="completed",
    )

    first = await facade.execute(_request("cr-1"))
    assert first.should_start_orchestration is False

    deps["room_center"].persist_user_message.return_value = _accepted("msg-2")
    second = await facade.execute(_request("cr-2"))
    assert second.should_start_orchestration is True


async def test_send_that_raises_releases_the_room():
    facade, deps = _make_facade()
    deps["room_center"].persist_user_message.side_effect = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await facade.execute(_request("cr-1"))

    deps["room_center"].persist_user_message.side_effect = None
    deps["room_center"].persist_user_message.return_value = _accepted("msg-2")
    second = await facade.execute(_request("cr-2"))
    assert second.should_start_orchestration is True


async def test_rooms_are_admitted_independently():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)
    deps["room_center"].persist_user_message.return_value = _accepted("msg-1")
    first_ack = await facade.execute(_request("cr-1"))
    facade.schedule_orchestration(_request("cr-1"), first_ack)
    await _wait(router.reached)

    deps["room_center"].persist_user_message.return_value = _accepted(
        "msg-2", room="room-2"
    )
    other = await facade.execute(_request("cr-2", room="room-2"))

    assert other.success is True
    assert other.should_start_orchestration is True
    router.release.set()
    await _drain()


# -- H1: failure before the Run exists ---------------------------------------


async def test_orchestration_failure_before_run_emits_terminal_failed_status():
    router = _GatedRouter()
    router.error = SessionConflict("a Run is already active for this Room")
    router.release.set()
    facade, deps = _make_facade(orchestrator_router=router)

    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _drain()

    assert ("failed", "msg-1", "cr-1") in _statuses(deps)
    failed = [
        call.args[0]
        for call in deps["event_publisher"].emit.call_args_list
        if getattr(call.args[0], "status", None) == "failed"
    ][0]
    assert failed.related_message_id == "msg-1"
    # The internal exception text never reaches the client.
    assert "already active" not in str(failed.details)
    assert facade.is_orchestration_pending("msg-1") is False


async def test_orchestration_failure_after_run_creation_leaves_status_to_the_run():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)

    async def fail_after_start(request, *, message_id, before_run_start):
        before_run_start()
        router.run_exists = True
        raise RuntimeError("kernel crashed")

    router.process_room_user_message = fail_after_start
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _drain()

    assert _statuses(deps) == []


async def test_orchestration_failure_while_creating_the_run_emits_failed():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)

    async def conflict_on_create(request, *, message_id, before_run_start):
        before_run_start()
        raise SessionConflict("Run create failed: conflict")

    router.process_room_user_message = conflict_on_create
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _drain()

    assert _statuses(deps) == [("failed", "msg-1", "cr-1")]


# -- H2: cancel before the Run exists ----------------------------------------


async def test_cancel_before_run_creation_cancels_and_the_run_never_starts():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)

    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _wait(router.reached)

    ack = await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")

    assert ack == CancellationAck(
        status="canceled", cancellation_applied=True, reconciled=True
    )
    assert _statuses(deps) == [("canceled", "msg-1", "cr-1")]

    router.release.set()
    await _drain()

    assert router.admitted == [False]
    # No second terminal status once the task ends.
    assert _statuses(deps) == [("canceled", "msg-1", "cr-1")]
    assert facade.is_orchestration_pending("msg-1") is False


async def test_repeated_cancel_before_run_creation_is_idempotent():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _wait(router.reached)

    first = await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")
    second = await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")

    assert first == second
    assert _statuses(deps) == [("canceled", "msg-1", "cr-1")]
    router.release.set()
    await _drain()


async def test_cancel_while_the_run_is_being_created_cancels_the_run():
    router = _GatedRouter()
    facade, _deps = _make_facade(orchestrator_router=router)
    created = asyncio.Event()
    finish = asyncio.Event()
    canceled = CancellationAck(
        status="canceled", cancellation_applied=True, reconciled=True
    )
    calls = 0

    async def route_cancellation(message_id, **_kwargs):
        nonlocal calls
        calls += 1
        if not created.is_set():
            raise KeyError(message_id)
        return canceled

    router.route_cancellation_by_user_message = AsyncMock(
        side_effect=route_cancellation
    )
    starting = asyncio.Event()

    async def create_slowly(request, *, message_id, before_run_start):
        assert before_run_start() is True
        starting.set()
        await asyncio.sleep(0.02)
        created.set()
        await finish.wait()

    router.process_room_user_message = create_slowly
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _wait(starting)

    ack = await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")

    assert ack == canceled
    assert calls >= 2
    finish.set()
    await _drain()


async def test_cancel_without_run_or_pending_orchestration_is_not_found():
    router = _GatedRouter()
    facade, _deps = _make_facade(orchestrator_router=router)

    with pytest.raises(NotFoundError):
        await facade.cancel("room-1", "msg-unknown", requested_by_user_id="user-1")


async def test_cancel_with_existing_run_uses_durable_cancellation():
    router = _GatedRouter()
    settled = CancellationAck(
        status="completed", cancellation_applied=False, reconciled=True
    )
    router.route_cancellation_by_user_message = AsyncMock(return_value=settled)
    facade, _deps = _make_facade(orchestrator_router=router)

    assert (
        await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1") == settled
    )


# -- Router: the run-start gate ----------------------------------------------


async def test_router_does_not_start_the_run_when_the_gate_refuses(monkeypatch):
    session_host = SimpleNamespace(
        get_session=lambda _room: None,
        create_session=AsyncMock(),
        drop_session=lambda room: dropped.append(room),
        prompt=AsyncMock(),
    )
    dropped: list[str] = []
    runtime = SimpleNamespace(
        session_host=session_host,
        epoch_store=SimpleNamespace(
            read_active=AsyncMock(return_value=SimpleNamespace(epoch=1))
        ),
        catalog_assembler=SimpleNamespace(
            prepare=AsyncMock(return_value=SimpleNamespace(snapshot=object()))
        ),
    )
    router = DualRuntimeRouter(runtime=runtime)
    envelope = SimpleNamespace(
        requesting_subject_id="user-1",
        candidate_agent_ids=["agent-1"],
        scope_source="room_team",
        group_id=None,
        message_text="hi",
        attachments=[],
        addressed_agents=[],
        model_text=None,
        attachment_texts=[],
    )
    monkeypatch.setattr(
        router,
        "_resolve_envelope_and_profile",
        AsyncMock(return_value=(envelope, object())),
    )
    monkeypatch.setattr(
        orchestrator_routing,
        "_build_candidate_scope",
        lambda **_: SimpleNamespace(
            agent_ids=["agent-1"],
            authorization_basis=SimpleNamespace(model_dump=lambda mode: {}),
        ),
    )

    response = await router.process_room_user_message(
        _request("cr-1"), message_id="msg-1", before_run_start=lambda: False
    )

    session_host.prompt.assert_not_awaited()
    assert dropped == ["room-1"]
    assert response.success is False


async def test_router_reports_whether_a_run_exists_for_a_message():
    runs = {"msg-1": object()}
    router = DualRuntimeRouter(
        runtime=SimpleNamespace(
            run_store=SimpleNamespace(
                load_by_user_message_id=AsyncMock(side_effect=runs.get)
            )
        )
    )

    assert await router.has_run_for_user_message("msg-1") is True
    assert await router.has_run_for_user_message("msg-2") is False


async def test_router_gate_marks_the_run_as_starting():
    seen: list[object] = []

    async def process(request, *, message_id, before_run_start):
        seen.append(before_run_start())
        seen.append(facade._orchestrating[message_id].stage)

    router = SimpleNamespace(process_room_user_message=process)
    facade, _ = _make_facade(
        task_factory=RecordingTaskFactory(), orchestrator_router=router
    )

    await facade.start_orchestration(_request("cr-1"), _ack("msg-1"))

    assert seen == [True, "starting"]


# -- Review follow-ups: races around failure and abandoned claims -------------


async def test_cancel_while_failure_is_being_reported_returns_failed():
    router = _GatedRouter()
    router.error = SessionConflict("a Run is already active for this Room")
    facade, deps = _make_facade(orchestrator_router=router)
    emitting = asyncio.Event()
    release_emit = asyncio.Event()

    async def slow_emit(event):
        emitting.set()
        await release_emit.wait()

    deps["event_publisher"].emit.side_effect = slow_emit
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    router.release.set()
    await _wait(emitting)

    ack = await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")
    release_emit.set()
    await _drain()

    assert ack == CancellationAck(
        status="failed", cancellation_applied=False, reconciled=True
    )
    assert _statuses(deps) == [("failed", "msg-1", "cr-1")]


async def test_cancel_while_starting_reports_a_run_creation_failure():
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)
    starting = asyncio.Event()
    fail = asyncio.Event()

    async def conflict_on_create(request, *, message_id, before_run_start):
        before_run_start()
        starting.set()
        await fail.wait()
        raise SessionConflict("Run create failed: conflict")

    router.process_room_user_message = conflict_on_create
    facade.RUN_START_POLL_SECONDS = 0.001
    # Hold the failure report so the cancel observes the failed stage.
    release_emit = asyncio.Event()

    async def held_emit(event):
        await release_emit.wait()

    deps["event_publisher"].emit.side_effect = held_emit
    facade.schedule_orchestration(_request("cr-1"), _ack("msg-1"))
    await _wait(starting)

    cancel = asyncio.create_task(
        facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")
    )
    await asyncio.sleep(0.01)
    assert not cancel.done()
    fail.set()
    ack = await asyncio.wait_for(cancel, timeout=1.0)
    release_emit.set()
    await _drain()

    assert ack == CancellationAck(
        status="failed", cancellation_applied=False, reconciled=True
    )
    assert _statuses(deps) == [("failed", "msg-1", "cr-1")]


async def test_admitted_send_that_is_never_scheduled_blocks_the_room_briefly():
    facade, deps = _make_facade()
    deps["room_center"].persist_user_message.return_value = _accepted("msg-1")
    first = await facade.execute(_request("cr-1"))
    assert first.should_start_orchestration is True

    deps["room_center"].persist_user_message.return_value = _accepted("msg-2")
    assert (await facade.execute(_request("cr-2"))).status_code == 409

    # The caller never scheduled msg-1; once that is clearly abandoned, the
    # room accepts sends again instead of staying blocked until restart.
    facade.UNSCHEDULED_CLAIM_SECONDS = 0.0
    third = await facade.execute(_request("cr-3"))
    assert third.success is True
    assert third.should_start_orchestration is True


async def test_room_stays_claimed_until_a_canceled_task_reaches_its_gate():
    # Releasing earlier would let the canceled task, still preparing, drop or
    # replace the session of the next send.
    router = _GatedRouter()
    facade, deps = _make_facade(orchestrator_router=router)
    deps["room_center"].persist_user_message.return_value = _accepted("msg-1")
    first = await facade.execute(_request("cr-1"))
    facade.schedule_orchestration(_request("cr-1"), first)
    await _wait(router.reached)
    await facade.cancel("room-1", "msg-1", requested_by_user_id="user-1")

    deps["room_center"].persist_user_message.return_value = _accepted("msg-2")
    assert (await facade.execute(_request("cr-2"))).status_code == 409

    router.release.set()
    await _drain()
    assert (await facade.execute(_request("cr-3"))).should_start_orchestration
