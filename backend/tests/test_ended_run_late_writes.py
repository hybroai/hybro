"""A Run that has ended takes no further writes and publishes nothing more.

A cancel settles the Run while an Agent call is still out; the call's late
outcome must not reach the Run or its Room. The run stores refuse a change to
an ended Run outside its bookkeeping fields, the kernel ends the pass quietly
when it finds the Run ended, and the session-event listener drops a public
event for an ended Run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

import container
from execution.orchestrator.control import ClientCancellationRequested
from execution.orchestrator.kernel import held_call_answer_observation
from execution.orchestrator.models import RecoveryClaim, TextPart, ToolResult
from execution.repository.orchestrator.run_store import MongoOrchestratorRunStore
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests._rule_ask_helpers import ProcessDied, RoomLog
from tests.orchestrator_harness.in_memory import InMemoryOrchestratorRunStore
from tests.test_orchestrator_a2a_mongo_parity import FakeCollection
from tests.test_orchestrator_model_first_kernel import (
    InteractionRuntime,
    _interaction_suspension,
)
from tests.test_orchestrator_passthrough import (
    AGENT,
    AgentRuntime,
    answer,
    make_passthrough_kernel,
    waiting,
)
from tests.test_orchestrator_rule_ask import ECHO, PAUSE, passthrough, room_chat

SIGNAL = NeverCancelled()
STOPPED = "orchestrator_ended_run_pass_stopped"
DROPPED = "orchestrator_ended_run_event_dropped"


# -- The run stores -------------------------------------------------------------

STORES = [
    pytest.param(InMemoryOrchestratorRunStore, id="in_memory"),
    pytest.param(
        lambda: MongoOrchestratorRunStore(FakeCollection(), FakeCollection()),
        id="mongo",
    ),
]


def _next(run, **updates):
    return run.model_copy(update={**updates, "state_version": run.state_version + 1})


async def _ended(store_factory):
    store = store_factory()
    run = make_run()
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    ended = await store.cas_mutate(
        _next(run, status="failed", terminal_reason="test"),
        expected_state_version=run.state_version,
        command_id="end",
    )
    assert ended.outcome == "accepted"
    return store, await store.load(run.run_id)


@pytest.mark.parametrize("store_factory", STORES)
@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({"status": "waiting_external"}, id="status"),
        pytest.param({"transcript": []}, id="transcript"),
        pytest.param({"terminal_reason": "late"}, id="terminal_reason"),
    ],
)
async def test_an_ended_run_refuses_a_change_to_its_record(store_factory, updates):
    store, ended = await _ended(store_factory)
    assert any(getattr(ended, key) != value for key, value in updates.items())

    result = await store.cas_mutate(
        _next(ended, **updates),
        expected_state_version=ended.state_version,
        command_id="late-write",
    )

    assert result.outcome == "conflict"
    assert await store.load(ended.run_id) == ended


@pytest.mark.parametrize("store_factory", STORES)
async def test_an_ended_run_takes_its_bookkeeping_writes(store_factory):
    store, ended = await _ended(store_factory)

    settled = await store.cas_mutate(
        _next(
            ended,
            projection_state="settled",
            updated_at=NOW + timedelta(seconds=1),
        ),
        expected_state_version=ended.state_version,
        command_id=f"settle:{ended.run_id}:{ended.state_version}",
    )
    assert settled.outcome == "accepted"
    released = await store.cas_mutate(
        _next(settled.run, recovery_claim=RecoveryClaim(next_attempt_at=NOW)),
        expected_state_version=settled.run.state_version,
        command_id="release:owner:1",
    )
    assert released.outcome == "accepted"
    outbox = await store.cas_mutate(
        _next(released.run, projection_outbox=[]),
        expected_state_version=released.run.state_version,
        command_id="complete-intent:none",
    )
    assert outbox.outcome == "accepted"


@pytest.mark.parametrize("store_factory", STORES)
async def test_a_replayed_write_to_an_ended_run_is_still_a_replay(store_factory):
    store, ended = await _ended(store_factory)

    replay = await store.cas_mutate(
        _next(ended, status="waiting_external"),
        expected_state_version=ended.state_version,
        command_id="end",
    )

    assert replay.outcome == "replayed"
    assert (await store.load(ended.run_id)).status == "failed"


@pytest.mark.parametrize("store_factory", STORES)
async def test_a_canceling_run_still_settles_canceled(store_factory):
    store = store_factory()
    run = make_run()
    await store.create(run, command_id="create")
    canceling = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id="cancel:user",
        cause="user_requested",
        requested_at=NOW,
    )
    assert canceling.outcome == "accepted"

    canceled = await store.cas_mutate(
        _next(canceling.run, status="canceled", terminal_reason="stop"),
        expected_state_version=canceling.run.state_version,
        command_id="terminate:canceled",
    )

    assert canceled.outcome == "accepted"
    assert (await store.load(run.run_id)).status == "canceled"


async def test_the_in_memory_store_keeps_processed_command_ids():
    store = InMemoryOrchestratorRunStore()
    run = make_run()
    await store.create(run, command_id="create")

    result = await store.cas_mutate(
        _next(run, active_internal_turn_id="turn-1"),
        expected_state_version=run.state_version,
        command_id="checkpoint-1",
    )

    assert "checkpoint-1" in result.run.processed_command_ids
    assert "checkpoint-1" in store.runs[run.run_id].processed_command_ids


# -- The kernel ------------------------------------------------------------------


class _Blocking:
    """Holds the first ``execute`` until the test lets the agent finish."""

    def __init__(self, tools) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._execute = tools.execute
        tools.execute = self.execute

    async def execute(self, invocation, acceptance, *, signal):
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await self._execute(invocation, acceptance, signal=signal)


async def _cancel(kernel, store, room, run_id):
    run = store.runs[run_id]
    requested = await store.request_cancellation(
        run_id,
        expected_state_version=run.state_version,
        command_id=f"cancel:{run_id}:user_requested",
        cause="user_requested",
        requested_at=NOW + timedelta(seconds=1),
    )
    assert requested.outcome == "accepted"
    settled = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="cancellation requested",
        cancellation_cause="user_requested",
        lifecycle=room.lifecycle,
    )
    assert settled.run.status == "canceled"
    return store.runs[run_id]


async def _fast(late: str):
    """Chat's model loop."""
    call = (
        ("call-1", ECHO, '{"value":"hi"}')
        if late == "result"
        else ("call-1", PAUSE, '{"status":"waiting_external"}')
    )
    kernel, store, model, tools, _port, room, run_id = await room_chat(
        [tool_events(call), final_events("late")], ask={}
    )
    blocking = _Blocking(tools)

    def start():
        return kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    return kernel, store, room, run_id, blocking, start, model


async def _passthrough(late: str):
    """A Session step's first pass."""
    room = RoomLog()
    runtime = AgentRuntime()
    runtime.answers[AGENT] = [answer("Done.") if late == "result" else waiting()]
    kernel, store, model, _, _history, run_id = await make_passthrough_kernel(
        runtime=runtime
    )
    kernel.canonical_event_reader = room.read
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "passthrough"})
    blocking = _Blocking(runtime)

    def start():
        return kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    return kernel, store, room, run_id, blocking, start, model


async def _approved_passthrough(late: str):
    """The owner's approval of a held call starts the pass, as the sink does."""
    kernel, store, runtime, _port, room, run_id = await passthrough()
    runtime.answers[AGENT] = [answer("Done.") if late == "result" else waiting()]
    model = SimpleNamespace(requests=[])
    held = await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)
    assert held.outcome == "awaiting_user"
    call_id = held.run.tool_batches[0].entries[0].call_id
    blocking = _Blocking(runtime)

    def start():
        return kernel.observe_tool(
            run_id,
            held_call_answer_observation(run_id, call_id, "Approve", NOW),
            signal=SIGNAL,
            lifecycle=room.lifecycle,
        )

    return kernel, store, room, run_id, blocking, start, model


@pytest.mark.parametrize("late", ["result", "suspension"])
@pytest.mark.parametrize(
    "harness",
    [_fast, _passthrough, _approved_passthrough],
    ids=["fast", "passthrough", "approved_passthrough"],
)
async def test_a_late_outcome_leaves_the_ended_run_and_its_room_alone(
    harness, late, caplog
):
    kernel, store, room, run_id, blocking, start, model = await harness(late)
    task = asyncio.create_task(start())
    await asyncio.wait_for(blocking.entered.wait(), 5)
    settled = await _cancel(kernel, store, room, run_id)
    emitted = list(room.emitted)
    public = await room.public_types()
    turn = await room.snapshot_turn(settled)
    model_requests = len(model.requests)

    caplog.set_level(logging.INFO)
    blocking.release.set()
    result = await asyncio.wait_for(task, 5)

    # The pass ends quietly with the Run's own outcome.
    assert result.outcome == "aborted"
    assert result.run.status == "canceled"
    # Nothing reached the Run or its Room, and the Room still folds.
    assert store.runs[run_id] == settled
    assert room.emitted == emitted
    assert await room.public_types() == public
    assert await room.snapshot_turn(settled) == turn
    assert len(model.requests) == model_requests
    # The late outcome is logged with ids and its status only.
    (record,) = [r for r in caplog.records if r.getMessage() == STOPPED]
    assert record.run_id == run_id
    assert record.run_status == "canceled"
    assert record.late_statuses == [
        "completed" if late == "result" else "waiting_external"
    ]
    assert len(record.late_call_ids) == 1


class _Tripped:
    """A session signal the test trips, as a Stop does."""

    cancelled = False

    async def wait(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.parametrize(
    "interrupt",
    [asyncio.CancelledError, ClientCancellationRequested],
    ids=["cancelled_error", "client_cancellation"],
)
async def test_an_interrupted_pass_on_an_ended_run_stops_quietly(interrupt):
    kernel, store, room, run_id, blocking, _start, _model = await _passthrough("result")
    signal = _Tripped()

    async def interrupted(invocation, acceptance, *, signal):
        # The Agent call is interrupted after the Stop settled the Run.
        blocking.entered.set()
        await blocking.release.wait()
        raise interrupt()

    kernel.tool_runtime.execute = interrupted
    task = asyncio.create_task(
        kernel.run(run_id, signal=signal, lifecycle=room.lifecycle)
    )
    await asyncio.wait_for(blocking.entered.wait(), 5)
    settled = await _cancel(kernel, store, room, run_id)
    emitted = list(room.emitted)

    signal.cancelled = True
    blocking.release.set()
    result = await asyncio.wait_for(task, 5)

    assert result.outcome == "aborted"
    assert store.runs[run_id] == settled
    assert room.emitted == emitted


async def test_a_bounded_parallel_batch_runs_every_call_while_the_run_is_live():
    run = make_run()
    run = run.model_copy(
        update={"profile": run.profile.model_copy(update={"max_parallel_calls": 1})}
    )
    kernel, store, _model, tools, _port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events("done"),
        ],
        ask={},
        run=run,
    )

    result = await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    assert result.outcome == "final_answer"
    assert tools.execute_log == ["call-1", "call-2"]
    entries = [e for batch in result.run.tool_batches for e in batch.entries]
    assert [e.state for e in entries] == ["terminal", "terminal"]


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param({"tool_execution": "sequential"}, id="sequential"),
        pytest.param({"max_parallel_calls": 1}, id="parallel_bounded"),
    ],
)
async def test_a_cancel_stops_the_next_call_of_a_batch(profile, caplog):
    run = make_run()
    run = run.model_copy(update={"profile": run.profile.model_copy(update=profile)})
    kernel, store, _model, tools, _port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events("late"),
        ],
        ask={},
        run=run,
    )
    caplog.set_level(logging.INFO)
    blocking = _Blocking(tools)
    task = asyncio.create_task(
        kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)
    )
    await asyncio.wait_for(blocking.entered.wait(), 5)
    settled = await _cancel(kernel, store, room, run_id)
    emitted = list(room.emitted)

    blocking.release.set()
    result = await asyncio.wait_for(task, 5)

    assert result.outcome == "aborted"
    assert tools.execute_log == ["call-1"]
    assert room.emitted == emitted
    assert store.runs[run_id] == settled
    (record,) = [r for r in caplog.records if r.getMessage() == STOPPED]
    assert record.late_statuses == ["completed"]


async def test_a_cancel_stops_a_reply_to_an_agent_task_after_a_call():
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name=PAUSE,
        status="completed",
        content=[TextPart(text="done")],
        artifact_refs=[],
    )
    kernel, store, _model, tools = await make_kernel(
        [
            tool_events(("call-1", PAUSE, '{"status":"input_required"}')),
            tool_events(
                ("call-3", ECHO, '{"value":"x"}'),
                ("call-2", PAUSE, '{"status":"input_required"}'),
            ),
            final_events("late"),
        ],
        tool_runtime=runtime,
    )
    run_id = next(iter(store.runs))
    entered = asyncio.Event()
    release = asyncio.Event()
    execute = runtime.execute

    async def hold_call_3(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-3":
            entered.set()
            await release.wait()
        return await execute(invocation, acceptance, signal=signal)

    runtime.execute = hold_call_3
    emitted: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        emitted.append(event_type)

    task = asyncio.create_task(kernel.run(run_id, signal=SIGNAL, lifecycle=lifecycle))
    await asyncio.wait_for(entered.wait(), 5)
    settled = await _cancel(kernel, store, SimpleNamespace(lifecycle=lifecycle), run_id)
    seen = list(emitted)

    release.set()
    result = await asyncio.wait_for(task, 5)

    # The batch's reply to the Agent's open task is never sent.
    assert result.outcome == "aborted"
    assert runtime.model_reply_calls == []
    assert emitted == seen
    assert store.runs[run_id] == settled
    assert tools.execute_log[-1] == "call-3"


@pytest.mark.parametrize("bump", [1, 0], ids=["version_moved", "same_version"])
@pytest.mark.parametrize(
    "write", ["", "complete:"], ids=["first_checkpoint", "terminal_commit"]
)
async def test_a_refused_write_on_an_ended_run_ends_the_pass_quietly(write, bump):
    ended: list = []
    kernel, store, _model, _tools = await make_kernel([final_events()])
    run_id = next(iter(store.runs))

    async def end_first(candidate, *, expected_state_version, command_id):
        # Another driver ended the Run just before this pass writes. Without
        # a version bump the store's ended-Run guard is what refuses the write.
        current = store.runs[run_id]
        if current.status != "failed" and command_id.startswith(write):
            store.runs[run_id] = current.model_copy(
                update={
                    "status": "failed",
                    "terminal_reason": "test",
                    "active_internal_turn_id": None,
                    "active_assistant_message_id": None,
                    "active_attempt": None,
                    "state_version": current.state_version + bump,
                }
            )
            ended.append(store.runs[run_id])
        return await real_cas(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )

    real_cas = store.cas_mutate
    store.cas_mutate = end_first

    result = await kernel.run(run_id, signal=SIGNAL)

    assert result.outcome == "failed"
    (ended_run,) = ended
    assert store.runs[run_id] == ended_run


def _end_run(store, run_id):
    """Another driver ends the Run, as a Stop or recovery does."""
    current = store.runs[run_id]
    store.runs[run_id] = current.model_copy(
        update={
            "status": "failed",
            "terminal_reason": "other driver",
            "active_internal_turn_id": None,
            "active_assistant_message_id": None,
            "active_attempt": None,
            "state_version": current.state_version + 1,
        }
    )
    return store.runs[run_id]


def _end_after(store, run_id, command_prefix, ended):
    """End the Run right after the pass's write with ``command_prefix`` lands."""
    real_cas = store.cas_mutate

    async def cas(candidate, *, expected_state_version, command_id):
        result = await real_cas(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if not ended and command_id.startswith(command_prefix):
            ended.append(_end_run(store, run_id))
        return result

    store.cas_mutate = cas


async def _turn_close_passthrough():
    kernel, store, room, run_id, blocking, start, _model = await _passthrough("result")
    blocking.release.set()
    return kernel, store, room, run_id, start


async def _turn_close_recovery():
    # The first pass dies before its call starts; the restart runs the batch
    # again and closes the turn on the recovery path.
    kernel, store, _model, _tools, _port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"x"}')), final_events("late")],
        ask={},
    )
    room.die_before = {"tool_execution_started"}
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    def start():
        return kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    return kernel, store, room, run_id, start


@pytest.mark.parametrize(
    "harness",
    [_turn_close_passthrough, _turn_close_recovery],
    ids=["passthrough", "recovery"],
)
async def test_a_run_ended_before_its_turn_close_stops_the_pass(harness, caplog):
    kernel, store, room, run_id, start = await harness()
    ended: list = []
    _end_after(store, run_id, "public-tool-end:", ended)
    emitted_at_end: list = []
    lifecycle = room.lifecycle

    async def watch(event_type, run, payload):
        if ended:
            emitted_at_end.append(event_type)
        await lifecycle(event_type, run, payload)

    room.lifecycle = watch
    caplog.set_level(logging.INFO)

    result = await start()

    assert result.outcome == "failed"
    (ended_run,) = ended
    assert store.runs[run_id] == ended_run
    # The turn close found the Run ended: no turn_completed and no write for
    # it. (An event between two of the pass's own writes is the listener's to
    # drop.)
    assert "turn_completed" not in emitted_at_end
    assert [r for r in caplog.records if r.getMessage() == STOPPED]


async def test_terminalize_after_another_driver_ended_the_run():
    kernel, store, _model, _tools = await make_kernel([final_events()])
    run_id = next(iter(store.runs))
    real_cas = store.cas_mutate
    ended: list = []

    async def cas(candidate, *, expected_state_version, command_id):
        # The other driver commits its end just before this terminal commit.
        if not ended and command_id.startswith("terminate:"):
            ended.append(_end_run(store, run_id))
        return await real_cas(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )

    store.cas_mutate = cas

    result = await kernel.terminalize(
        run_id, status="budget_exhausted", reason="deadline"
    )

    assert result.outcome == "failed"
    (ended_run,) = ended
    assert store.runs[run_id] == ended_run


async def test_a_tripped_signal_on_a_run_another_driver_ended_stops_quietly(caplog):
    kernel, store, _model, _tools = await make_kernel([final_events()])
    run_id = next(iter(store.runs))
    signal = _Tripped()
    signal.cancelled = True
    real_load = store.load
    loads = 0
    ended: list = []

    async def load(run_id_):
        # The third load is the signal branch's reload in the pass's loop.
        nonlocal loads
        loads += 1
        if loads == 3:
            ended.append(_end_run(store, run_id))
        return await real_load(run_id_)

    store.load = load
    caplog.set_level(logging.INFO)

    result = await kernel.run(run_id, signal=signal)

    assert result.outcome == "failed"
    (ended_run,) = ended
    assert store.runs[run_id] == ended_run
    # Stopped by the signal branch, not by the loop's own ended check.
    assert [r for r in caplog.records if r.getMessage() == STOPPED]


# -- The session-event listener ----------------------------------------------------


def _event(event_type: str = "turn_completed"):
    return SimpleNamespace(
        event_type=event_type, run_id="run-1", conversation_id="room-1"
    )


@pytest.mark.parametrize("status", ["canceled", "completed", "failed"])
def test_the_listener_drops_a_public_event_for_an_ended_run(status, caplog):
    caplog.set_level(logging.INFO)
    run = SimpleNamespace(run_id="run-1", conversation_id="room-1", status=status)

    assert container._drop_event_for_ended_run(run, _event()) is True

    (record,) = [r for r in caplog.records if r.getMessage() == DROPPED]
    assert record.event_type == "turn_completed"
    assert record.run_status == status


@pytest.mark.parametrize("status", ["running", "canceling", "waiting_external"])
def test_the_listener_publishes_for_a_run_that_has_not_ended(status):
    run = SimpleNamespace(run_id="run-1", conversation_id="room-1", status=status)

    assert container._drop_event_for_ended_run(run, _event()) is False


def test_the_listener_checks_before_it_publishes():
    source = inspect.getsource(container._runtime_lifespan)
    # The listener is a closure inside the lifespan, so pin the guard's exact
    # condition and its place before the publish.
    guard = re.search(
        r"if public_event is not None and not _drop_event_for_ended_run\(\s*"
        r"current_run, event\s*\):",
        source,
    )
    publish = re.search(r"emit_checked_identified\(\s+notification", source)
    assert guard is not None and publish is not None
    assert guard.end() < publish.start()
    assert source.count("_drop_event_for_ended_run(") == 1


# -- An answer that reaches a Run ended after it was read -----------------------


def _end_before(store, run_id, command_prefix, ended):
    """End the Run just before the pass's write with ``command_prefix``."""
    real_cas = store.cas_mutate

    async def cas(candidate, *, expected_state_version, command_id):
        if not ended and command_id.startswith(command_prefix):
            ended.append(_end_run(store, run_id))
        return await real_cas(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )

    store.cas_mutate = cas


async def test_an_ask_user_answer_to_a_run_ended_after_it_was_read_is_refused():
    from execution.orchestrator.kernel import (
        REQUEST_USER_INPUT_TOOL_NAME,
        KernelConflict,
        supervisor_answer_observation,
    )
    from tests.test_orchestrator_kernel import RecordingSupervisorHITL

    kernel, store, _model, _ = await make_kernel(
        [
            tool_events(
                ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')
            ),
            final_events("You chose Shanghai."),
        ],
        supervisor_hitl=RecordingSupervisorHITL(),
    )
    run_id = next(iter(store.runs))
    asked = await kernel.run(run_id, signal=SIGNAL)
    assert asked.outcome == "awaiting_user"
    ended: list = []
    # The answer passes the kernel's terminal check; a Stop ends the Run
    # before the answer's checkpoint.
    _end_before(store, run_id, "tool-observation:", ended)

    with pytest.raises(KernelConflict, match="terminal Run"):
        await kernel.observe_tool(
            run_id,
            supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
            signal=SIGNAL,
        )

    (ended_run,) = ended
    assert store.runs[run_id] == ended_run


async def test_a_held_call_answer_to_a_run_ended_after_it_was_read_is_not_applied():
    from execution.orchestrator.kernel import KernelConflict
    from tests.test_rule_ask_wiring import _two_held_calls

    port, store, tools, _room, run_id = await _two_held_calls()
    ended: list = []
    _end_before(store, run_id, "tool-observation:", ended)

    # The port read the Run suspended. The answer must not count as applied:
    # the HITL coordinator would mark the interaction applied on True.
    with pytest.raises(KernelConflict, match="terminal Run"):
        await port(run_id, call_id="call-1", answers="Approve")

    (ended_run,) = ended
    assert store.runs[run_id] == ended_run
    assert tools.execute_log == []


# -- A Stop during a batch's acceptance loop ------------------------------------


def _cancel_after(kernel, store, room, run_id, after, settled):
    """A Stop settles the Run right after the pass's write ``after`` lands."""
    real_cas = store.cas_mutate

    async def cas(candidate, *, expected_state_version, command_id):
        result = await real_cas(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if command_id == after:
            store.cas_mutate = real_cas
            settled.append(await _cancel(kernel, store, room, run_id))
            settled.append(list(room.emitted))
        return result

    store.cas_mutate = cas


@pytest.mark.parametrize(
    "after",
    # Before call-2 is reached, or after the last acceptance before dispatch.
    ["accepted-tool:call-1", "accepted-tool:call-2"],
    ids=["before_the_next_call", "before_dispatch"],
)
@pytest.mark.parametrize(
    "profile",
    [
        pytest.param({"tool_execution": "sequential"}, id="sequential"),
        pytest.param({"max_parallel_calls": 1}, id="parallel_bounded"),
    ],
)
async def test_a_stop_during_acceptance_dispatches_no_call_of_the_batch(profile, after):
    run = make_run()
    run = run.model_copy(update={"profile": run.profile.model_copy(update=profile)})
    kernel, store, _model, tools, _port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events("late"),
        ],
        ask={},
        run=run,
    )
    settled: list = []
    # The Stop ends the Run and every entry right after the ``after`` write.
    _cancel_after(kernel, store, room, run_id, after, settled)

    result = await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)

    ended, emitted = settled
    assert result.outcome == "aborted"
    assert tools.execute_log == []
    assert room.emitted == emitted
    assert store.runs[run_id] == ended


@pytest.mark.parametrize(
    "calls",
    [
        # The Stop comes before call-3 is reached.
        [("call-2", PAUSE, '{"status":"input_required"}'), ("call-3", ECHO, "{}")],
        # The reply is the batch's only call: the Stop comes before dispatch.
        [("call-2", PAUSE, '{"status":"input_required"}')],
    ],
    ids=["before_the_next_call", "before_dispatch"],
)
async def test_a_stop_during_acceptance_sends_no_reply_to_an_agent_task(calls):
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    runtime.model_replies["call-2"] = ToolResult(
        call_id="call-2",
        tool_name=PAUSE,
        status="completed",
        content=[TextPart(text="done")],
        artifact_refs=[],
    )
    kernel, store, _model, tools = await make_kernel(
        [
            tool_events(("call-1", PAUSE, '{"status":"input_required"}')),
            tool_events(*calls),
            final_events("late"),
        ],
        tool_runtime=runtime,
    )
    run_id = next(iter(store.runs))
    emitted: list[str] = []

    async def lifecycle(event_type, _run, _payload):
        emitted.append(event_type)

    room = SimpleNamespace(lifecycle=lifecycle, emitted=emitted)
    settled: list = []
    # The Stop comes right after the reply to call-1's task is accepted.
    _cancel_after(kernel, store, room, run_id, "accepted-join:call-2", settled)

    result = await kernel.run(run_id, signal=SIGNAL, lifecycle=lifecycle)

    ended, seen = settled
    assert result.outcome == "aborted"
    assert runtime.model_reply_calls == []
    assert "call-3" not in tools.execute_log
    assert emitted == seen
    assert store.runs[run_id] == ended
