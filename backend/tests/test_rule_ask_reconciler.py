"""An owner's answer that finds the Run busy is applied by the reconciler.

Two Agent calls a check rule holds, each with its own Question. The owner
approves the first; while that call runs, the owner approves the second. The
second answer is recorded and reads as accepted, and the HITL reconciler
applies it once the first call's pass has ended.

Everything from the answer to the kernel is the production code: the HITL
service, its coordinator and reconciler, both approval repositories (over an
in-memory Mongo collection), the container's question and resume ports, the
run-addressed observation sink and the kernel. Only Mongo, the room message
store and event delivery are replaced.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import container
from approval.adapters import HITLDeliveryAdapter
from approval.application import HITLApplicationCoordinator
from approval.factory import create_hitl_service
from approval.reconciler import HITLLifecycleReconciler
from approval.repository import HITLLifecycleStore, HITLRequestStore
from common.dto import RunEventNotification
from execution.adapters.session_host import RoomSessionHost
from execution.orchestrator.a2a_runtime.observations import (
    RunAddressedToolObservationSink,
    RunPassEndHooks,
)
from execution.orchestrator.models import TERMINAL_RUN_STATUSES
from execution.orchestrator.session import RoomAgentSession
from tests._in_memory_mongo import collection
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    drain_pass_end_hooks,
    final_events,
    make_kernel,
    session_config,
    tool_events,
)
from tests._rule_ask_helpers import AskingToolRuntime, RoomLog


class _Recorder:
    """Any awaited call is recorded and succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def record(*_args, **_kwargs):
            self.calls.append(name)
            return True

        return record


# The request store methods the container exposes as HITL persistence.
_REQUEST_STORE_METHODS = frozenset(
    {
        "cas_update_hitl_request",
        "cas_update_hitl_request_strict",
        "claim_hitl_open_projection",
        "claim_hitl_request",
        "complete_hitl_open_projection",
        "count_hitl_requests_for_message",
        "create_hitl_request",
        "create_or_reuse_pending_hitl_request",
        "fenced_update_hitl_request",
        "find_pending_hitl_request_for_agent_message",
        "get_hitl_request",
        "get_pending_hitl_requests",
        "get_pending_hitl_requests_for_message",
        "get_pending_hitl_requests_for_message_strict",
        "get_pending_hitl_requests_strict",
        "persist_hitl_interaction_metadata",
        "persist_hitl_request_id_on_message",
        "persist_hitl_user_answer",
        "persist_pending_hitl_on_agent_message",
        "release_hitl_open_projection",
        "update_agent_message_task_state",
        "update_hitl_request",
    }
)


class _Persistence:
    """The container's HITL persistence: the request store and room reads."""

    def __init__(self, requests: HITLRequestStore, run) -> None:
        self._requests = requests
        self._run = run

    def __getattr__(self, name: str):
        if name not in _REQUEST_STORE_METHODS:
            raise AttributeError(name)
        return getattr(self._requests, name)

    async def get_room_user_message_by_message_id(self, message_id):
        return SimpleNamespace(
            message_id=message_id,
            room_id=self._run.conversation_id,
            client_request_id=self._run.client_request_id,
        )

    async def resolve_client_request_id_for_message_id(self, _message_id):
        return self._run.client_request_id

    async def reset_last_notified_state(self, *_args, **_kwargs):
        return True

    async def get_room_agent_message_by_message_id(self, *_args, **_kwargs):
        return None


class _RoomFiles:
    @asynccontextmanager
    async def write_lease(self, *_args, **_kwargs):
        yield


async def _owner_and_two_held_calls(*, pass_end=False, live_session=False):
    """``pass_end`` applies parked answers when a pass ends, as the container
    wires it; ``live_session`` delivers answers through a live session."""
    room = RoomLog()
    tools = AskingToolRuntime({"fake_agent_echo": "rule_ask_echo"})
    kernel, store, _model, _ = await make_kernel(
        [
            tool_events(
                ("call-1", "fake_agent_echo", '{"value":"one"}'),
                ("call-2", "fake_agent_echo", '{"value":"two"}'),
            ),
            final_events(),
        ],
        tool_runtime=tools,
    )
    run_id = next(iter(store.runs))
    reconcilers: list[HITLLifecycleReconciler] = []

    async def apply_parked(ended_run_id):
        await reconcilers[0].apply_parked_run_answers(ended_run_id)

    hooks = RunPassEndHooks(apply_parked if pass_end else None)
    # No live session owns the Run: answers go through the sink.
    session_host = SimpleNamespace(get_session=lambda _conversation_id: None)
    if live_session:
        session_host = RoomSessionHost(
            kernel_factory=lambda _catalog: kernel,
            run_store=store,
            epoch_store=None,
            pass_end_hooks=hooks,
        )
        session = RoomAgentSession(
            config=session_config(), kernel=kernel, run_store=store
        )
        session._run_id = run_id
        session._emit_kernel_event = room.lifecycle
        session_host._sessions[store.runs[run_id].conversation_id] = session
    runtime = SimpleNamespace(
        run_store=store,
        session_host=session_host,
        observation_sink=RunAddressedToolObservationSink(
            run_store=store,
            kernel_factory=lambda _run: kernel,
            signal_factory=NeverCancelled,
            lifecycle_factory=lambda _run: room.lifecycle,
            pass_end_hooks=hooks,
        ),
        pass_end_hooks=hooks,
    )

    requests_collection = collection("hitl_requests")
    lifecycle = HITLLifecycleStore(
        interactions=collection("hitl_interactions"),
        resume_commands=collection("hitl_resume_commands"),
        hitl_requests=requests_collection,
    )
    requests = HITLRequestStore(
        hitl_requests=requests_collection, message_writer=_Recorder()
    )
    # The production unique indexes, enforced by the in-memory collection.
    await lifecycle.ensure_indexes()
    await requests.ensure_indexes()
    application = HITLApplicationCoordinator(lifecycle=lifecycle)

    async def project_run_answer(hitl_result, response):
        # The container's projector only checks the route; the store moves
        # run_projection_status to applied.
        del hitl_result, response

    application.bind_run_answer_projector(project_run_answer)

    async def run_state(requested_run_id):
        run = await store.load(requested_run_id)
        return run.status if run is not None else "missing"

    controls: list[tuple[str, str, str]] = []

    async def publish_control(kind, interaction, request_ids):
        """The container's canonical HITL control events, into the Room."""
        run = await store.load(interaction["orchestration_run_id"])
        if kind == "run_resumed" and run.status in {
            "canceling",
            *TERMINAL_RUN_STATUSES,
        }:
            return
        interaction_id = interaction["interaction_id"]
        payload = (
            {
                "interaction_id": interaction_id,
                "request_ids": request_ids,
                "requested_at": NOW,
            }
            if kind == "run_waiting_input"
            else {
                "interaction_id": interaction_id,
                "resolved_request_ids": request_ids,
                "resumed_at": NOW,
            }
        )
        status, _ = await room.publisher.emit_checked_identified(
            RunEventNotification(
                conversation_id=run.conversation_id,
                event_id=f"public:{run.run_id}:{kind}:{interaction_id}",
                run_id=run.run_id,
                seq=run.state_version,
                run_event_type=kind,
                payload=payload,
                correlation_id=run.client_request_id,
            )
        )
        controls.append((kind, interaction_id, status.value))

    service = create_hitl_service(
        persistence=_Persistence(requests, store.runs[run_id]),
        delivery=HITLDeliveryAdapter(room.publisher),
        lifecycle=lifecycle,
        application=application,
        room_files=_RoomFiles(),
        canonical_control_publisher=publish_control,
        canonical_run_state_reader=run_state,
        supervisor_resume=container.supervisor_resume_port(lambda: runtime),
        public_text_sanitizer=lambda text, *_args, **_kwargs: text,
    )
    reconciler = HITLLifecycleReconciler(
        lifecycle=lifecycle, service=service, application=application
    )
    reconcilers.append(reconciler)
    if pass_end:
        application.bind_parked_run_check(container.parked_run_check(lambda: runtime))

    kernel.rule_question = container.rule_question_port(service)
    kernel.canonical_event_reader = room.read
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    held = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=room.lifecycle)
    assert held.outcome == "awaiting_user"

    return SimpleNamespace(
        hooks=hooks,
        session_host=session_host,
        service=service,
        application=application,
        reconciler=reconciler,
        lifecycle=lifecycle,
        store=store,
        tools=tools,
        room=room,
        run_id=run_id,
        controls=controls,
    )


async def _question_for(world, call_id):
    rows = await world.service.persistence.get_pending_hitl_requests_strict(
        world.store.runs[world.run_id].conversation_id
    )
    (row,) = [row for row in rows if row["source_step_id"] == call_id]
    return SimpleNamespace(
        **{k: row[k] for k in ("room_id", "interaction_id", "request_id")}
    )


async def _answer(world, question, text):
    return await world.application.handle_batch_response(
        world.service,
        conversation_id=question.room_id,
        interaction_id=question.interaction_id,
        answers=[{"request_id": question.request_id, "user_input": text}],
        user_id="owner-1",
    )


def _entry(run, call_id):
    return next(
        entry
        for batch in run.tool_batches
        for entry in batch.entries
        if entry.call_id == call_id
    )


async def _turn(world):
    run = await world.store.load(world.run_id)
    return await world.room.snapshot_turn(run)


async def test_two_held_calls_open_one_question_at_a_time_in_the_room():
    world = await _owner_and_two_held_calls()
    first = await _question_for(world, "call-1")

    # Only the first Question is open; the Room reads as waiting on it.
    assert [kind for kind, *_ in world.controls] == ["run_waiting_input"]
    turn = await _turn(world)
    assert turn["state"] == "awaiting_input"
    assert turn["active_interaction_id"] == first.interaction_id

    applied = await _answer(world, first, "Approve")

    assert applied["status"] == "applied"
    second = await _question_for(world, "call-2")
    turn = await _turn(world)
    assert turn["state"] == "awaiting_input"
    assert turn["active_interaction_id"] == second.interaction_id

    done = await _answer(world, second, "Reject")

    assert done["status"] == "applied"
    run = await world.store.load(world.run_id)
    assert run.status == "completed"
    assert world.tools.execute_log == ["call-1"]
    assert [(kind, interaction) for kind, interaction, _ in world.controls] == [
        ("run_waiting_input", first.interaction_id),
        ("run_resumed", first.interaction_id),
        ("run_waiting_input", second.interaction_id),
        ("run_resumed", second.interaction_id),
    ]
    await world.room.settle(run)
    assert (await world.room.snapshot_turn(run))["state"] == "completed"
    # Every Question, answer and control event carries the Run's request id.
    records = await world.room.store.read_range(
        "room-1", after=-1, limit=10_000, include_skipped=False
    )
    hitl = [
        record["payload_public"]
        for record in records
        if record["kind"] in {"hitl_request", "hitl_response"}
        or record["payload_public"].get("type") in {"run_waiting_input", "run_resumed"}
    ]
    assert len(hitl) == 8
    assert {
        item.get("client_request_id") or item.get("correlation_id") for item in hitl
    } == {run.client_request_id}


async def test_an_answer_the_run_was_too_busy_for_is_applied_by_the_reconciler():
    world = await _owner_and_two_held_calls()
    first = await _question_for(world, "call-1")
    during_pass: list[object] = []
    original_execute = world.tools.execute

    async def execute_while_the_owner_answers(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-1":
            # The pass asked about call-2 before it ran call-1.
            second = await _question_for(world, "call-2")
            during_pass.append(second)
            result = await _answer(world, second, "Approve")
            during_pass.append(result["status"])
        return await original_execute(invocation, acceptance, signal=signal)

    world.tools.execute = execute_while_the_owner_answers

    applied = await _answer(world, first, "Approve")

    assert applied["status"] == "applied"
    second, status = during_pass
    assert status == "accepted"
    waiting = await world.store.load(world.run_id)
    assert waiting.status == "awaiting_user"
    assert _entry(waiting, "call-2").rule_hold.state == "asked"
    parked = await world.lifecycle.get_interaction_strict(second.interaction_id)
    assert parked["status"] == "applying"
    assert parked["application_error"] == "run_busy"
    # The answer is shown as received while the pass runs; the Room reads
    # active, not waiting, since no Question is open.
    assert (await _turn(world))["state"] == "active"

    counts = await world.reconciler.reconcile_lifecycle()

    # The lease of the parked application already expired, so the first
    # pass applies it. The retryable resume command would be a second route.
    assert counts["applications"] == 1
    assert counts["errors"] == 0
    done = await world.store.load(world.run_id)
    assert done.status == "completed"
    assert world.tools.execute_log == ["call-1", "call-2"]
    finished = await world.lifecycle.get_interaction_strict(second.interaction_id)
    assert finished["status"] == "applied"
    # The reconciler resumes again under the same event id; the Room keeps
    # one resumed event per Question.
    assert [kind for kind, _, _ in world.controls] == [
        "run_waiting_input",
        "run_resumed",
        "run_waiting_input",
        "run_resumed",
        "run_resumed",
    ]
    records = await world.room.store.read_range(
        "room-1", after=-1, limit=10_000, include_skipped=False
    )
    assert [
        record["event_id"]
        for record in records
        if record["event_id"].startswith(f"public:{world.run_id}:run_resumed:")
    ] == [
        f"public:{world.run_id}:run_resumed:{first.interaction_id}",
        f"public:{world.run_id}:run_resumed:{second.interaction_id}",
    ]
    await world.room.settle(done)
    assert (await world.room.snapshot_turn(done))["state"] == "completed"

    again = await world.reconciler.reconcile_lifecycle()

    assert again["errors"] == 0
    assert world.tools.execute_log == ["call-1", "call-2"]


async def _second_answer_during_call_1(world, *, park_after_the_pass=False):
    """Answer call-2's Question while call-1 runs; return the answer status.

    With ``park_after_the_pass`` the second answer reads the Run as busy,
    but is parked only after call-1's pass ended and its hook found nothing.
    """
    first = await _question_for(world, "call-1")
    during_pass: list[object] = []
    original_execute = world.tools.execute
    reading_busy = asyncio.Event()
    parked = asyncio.Event()
    mark = world.lifecycle.mark_interaction_application_state

    async def park_late(interaction_id, **kwargs):
        if kwargs.get("error") == "run_busy" and not parked.is_set():
            reading_busy.set()
            await parked.wait()
        return await mark(interaction_id, **kwargs)

    if park_after_the_pass:
        world.lifecycle.mark_interaction_application_state = park_late

    async def execute_while_the_owner_answers(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-1":
            # The pass asked about call-2 before it ran call-1.
            second = await _question_for(world, "call-2")
            during_pass.append(second)
            if park_after_the_pass:
                during_pass.append(
                    asyncio.create_task(_answer(world, second, "Approve"))
                )
                await reading_busy.wait()
            else:
                during_pass.append(await _answer(world, second, "Approve"))
        return await original_execute(invocation, acceptance, signal=signal)

    world.tools.execute = execute_while_the_owner_answers

    applied = await _answer(world, first, "Approve")
    assert applied["status"] == "applied"
    second, answered = during_pass
    if park_after_the_pass:
        # call-1's pass ended; its hook ran before the answer was parked.
        await drain_pass_end_hooks(world.hooks)
        assert (await world.store.load(world.run_id)).status == "awaiting_user"
        parked.set()
        answered = await answered
    await drain_pass_end_hooks(world.hooks)
    return second, answered["status"]


async def _applied_without_the_reconciler(world, second):
    done = await world.store.load(world.run_id)
    assert done.status == "completed"
    assert world.tools.execute_log == ["call-1", "call-2"]
    finished = await world.lifecycle.get_interaction_strict(second.interaction_id)
    assert finished["status"] == "applied"
    counts = await world.reconciler.reconcile_lifecycle()
    assert counts["applications"] == 0


async def test_an_answer_the_run_was_too_busy_for_is_applied_when_the_pass_ends():
    world = await _owner_and_two_held_calls(pass_end=True)

    second, status = await _second_answer_during_call_1(world)

    assert status == "accepted"
    await _applied_without_the_reconciler(world, second)


async def test_a_live_session_applies_the_parked_answer_when_its_pass_ends():
    # The live session's lease is released before its pass-end hook runs, so
    # the hook's delivery through the same session is not refused.
    world = await _owner_and_two_held_calls(pass_end=True, live_session=True)

    second, status = await _second_answer_during_call_1(world)

    assert status == "accepted"
    await _applied_without_the_reconciler(world, second)


async def test_an_answer_parked_after_the_pass_ended_is_applied_at_once():
    world = await _owner_and_two_held_calls(pass_end=True)

    second, status = await _second_answer_during_call_1(world, park_after_the_pass=True)

    assert status == "accepted"
    await _applied_without_the_reconciler(world, second)


async def test_a_live_session_answer_parked_after_its_pass_ended_is_applied():
    # The park lands after the session's pass ended and let the Run go, so
    # the check after the park applies the answer through the same session.
    world = await _owner_and_two_held_calls(pass_end=True, live_session=True)

    second, status = await _second_answer_during_call_1(world, park_after_the_pass=True)

    assert status == "accepted"
    await _applied_without_the_reconciler(world, second)


async def test_shutdown_settles_a_pass_a_hook_drives_as_interrupted():
    world = await _owner_and_two_held_calls(pass_end=True, live_session=True)
    first = await _question_for(world, "call-1")
    original_execute = world.tools.execute
    call_2_running = asyncio.Event()
    answers: list[object] = []
    interrupted_by: list[str] = []

    async def execute(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-1":
            second = await _question_for(world, "call-2")
            answers.append(await _answer(world, second, "Approve"))
        if invocation.invocation_id == "call-2":
            # The hook's pass: shutdown starts while it runs.
            call_2_running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                interrupted_by.append("session shutdown")
                raise
        return await original_execute(invocation, acceptance, signal=signal)

    world.tools.execute = execute
    assert (await _answer(world, first, "Approve"))["status"] == "applied"
    await call_2_running.wait()

    world.hooks.close()
    await world.session_host.shutdown()

    assert interrupted_by == ["session shutdown"]
    ended = await world.store.load(world.run_id)
    assert ended.status == "failed"
    assert ended.terminal_reason == "interrupted"
