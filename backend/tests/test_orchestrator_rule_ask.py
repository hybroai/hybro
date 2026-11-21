"""A check rule that answers "ask": the call is held before it is accepted.

The fake tool runtime's ``accept`` raises ``AgentCallNeedsAnswer`` for the
tools a rule asks about, until the kernel passes ``rule_approved=True``.
Every whole sequence is appended through the real publisher and read back
through the snapshot, so an event the fold refuses fails the test.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from hashlib import sha256

import pytest

from execution.orchestrator.kernel import (
    REQUEST_USER_INPUT_TOOL_NAME,
    KernelConflict,
    held_call_answer_observation,
    supervisor_answer_observation,
)
from execution.orchestrator.models import (
    HeldCallAnswer,
    TextPart,
    ToolObservation,
    ToolResult,
    ToolResultMessage,
    ToolSuspension,
)
from execution.orchestrator.ports import AgentCallDenied, AgentCallNeedsAnswer
from execution.orchestrator.run_state import (
    SURFACE_AGENT_QUESTIONS_TOOL_NAME,
    _opaque_public_call_id,
)
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests._rule_ask_helpers import (
    AskingAgentRuntime,
    AskingToolRuntime,
    ProcessDied,
    RecordingRuleQuestion,
    RoomLog,
)
from tests.test_orchestrator_model_first_kernel import (
    InteractionRuntime,
)
from tests.test_orchestrator_model_first_kernel import (
    _interaction_suspension as interaction_suspension,
)
from tests.test_orchestrator_passthrough import (
    AGENT,
    answer,
    make_passthrough_kernel,
    question,
    waiting,
)

ECHO = "fake_agent_echo"
PAUSE = "fake_agent_pause"
RULE = "rule_ask_echo"
SIGNAL = NeverCancelled()


# -- Harness --------------------------------------------------------------------


async def room_chat(
    scripts, *, ask=None, run=None, port=None, supervisor_hitl=None, **runtime_kwargs
):
    room = RoomLog()
    tools = AskingToolRuntime(
        ask if ask is not None else {ECHO: RULE}, **runtime_kwargs
    )
    kernel, store, model, _ = await make_kernel(
        scripts, run=run, tool_runtime=tools, supervisor_hitl=supervisor_hitl
    )
    port = port or RecordingRuleQuestion()
    kernel.rule_question = port
    kernel.canonical_event_reader = room.read
    run_id = next(iter(store.runs))
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    return kernel, store, model, tools, port, room, run_id


async def passthrough(*, port=None):
    room = RoomLog()
    runtime = AskingAgentRuntime({AGENT: RULE})
    kernel, store, _model, _, _history, run_id = await make_passthrough_kernel(
        runtime=runtime
    )
    port = port or RecordingRuleQuestion()
    kernel.rule_question = port
    kernel.canonical_event_reader = room.read
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "passthrough"})
    return kernel, store, runtime, port, room, run_id


def entry_of(run, call_id=None):
    entries = [e for batch in run.tool_batches for e in batch.entries]
    if call_id is None:
        return entries[0]
    return next(e for e in entries if e.call_id == call_id)


def answer_of(run_id, call_id, text):
    return held_call_answer_observation(run_id, call_id, text, NOW)


async def observe(kernel, room, run_id, observation):
    return await kernel.observe_tool(
        run_id, observation, signal=SIGNAL, lifecycle=room.lifecycle
    )


async def start(kernel, room, run_id):
    return await kernel.run(run_id, signal=SIGNAL, lifecycle=room.lifecycle)


def tool_results(run) -> list[ToolResultMessage]:
    return [item for item in run.transcript if isinstance(item, ToolResultMessage)]


async def settled_turn(room, run):
    await room.settle(run)
    return await room.snapshot_turn(run)


# -- The harness itself -------------------------------------------------------


async def test_the_room_log_reads_an_ordinary_call_back_through_the_snapshot():
    kernel, _, _, _, _, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()], ask={}
    )

    result = await start(kernel, room, run_id)

    assert result.outcome == "final_answer"
    turn = await settled_turn(room, result.run)
    assert turn["state"] == "completed"
    assert turn["final_committed"]


# -- Whole sequences ----------------------------------------------------------


async def test_room_chat_held_then_approved_reads_through_the_snapshot():
    kernel, _store, model, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events("Sent.")]
    )

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert tools.execute_log == []
    # The Room reads "awaiting input" from approval's run_waiting_input,
    # which this kernel harness does not publish; the held row must fold.
    parked = await room.snapshot_turn(held.run)
    assert parked["state"] == "active"
    public_id = entry_of(held.run).opaque_public_call_id

    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    assert done.outcome == "final_answer"
    assert tools.accept_calls == [("call-1", False), ("call-1", True)]
    assert tools.execute_log == ["call-1"]
    assert [m.status for m in tool_results(done.run)] == ["completed"]
    assert len(port.calls) == 1
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "completed"
    (tool,) = [item for item in turn["activity"] if item["kind"] == "tool"]
    assert tool["status"] == "completed"
    assert await room.tool_starts(public_id) == 1
    assert len(model.requests) == 2


async def test_room_chat_held_then_refused_returns_the_refusal_to_the_model():
    kernel, _store, model, tools, _port, room, run_id = await room_chat(
        [
            tool_events(("call-1", ECHO, '{"value":"hi"}')),
            final_events("The owner said no."),
        ]
    )
    held = await start(kernel, room, run_id)
    public_id = entry_of(held.run).opaque_public_call_id

    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Reject"))

    assert done.outcome == "final_answer"
    assert tools.execute_log == []
    assert tools.accept_calls == [("call-1", False)]
    (refused,) = tool_results(done.run)
    assert refused.is_error
    assert refused.error_code == "agent_call_refused"
    assert refused.error_message == "The owner refused this agent call."
    assert model.requests[1].messages[-1].role == "tool"
    assert entry_of(done.run).rule_hold.state == "refused"
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "completed"
    (tool,) = [item for item in turn["activity"] if item["kind"] == "tool"]
    assert tool["is_error"]
    assert await room.tool_starts(public_id) == 1


async def test_passthrough_held_then_approved_reads_through_the_snapshot():
    kernel, _store, runtime, _port, room, run_id = await passthrough()
    runtime.answers[AGENT] = [answer("Works were voted in 2023.")]

    held = await start(kernel, room, run_id)
    assert held.outcome == "awaiting_user"
    public_id = entry_of(held.run).opaque_public_call_id

    done = await observe(
        kernel, room, run_id, answer_of(run_id, entry_of(held.run).call_id, "Approve")
    )

    assert done.outcome == "final_answer"
    assert runtime.execute_log == [entry_of(done.run).call_id]
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "completed"
    assert turn["final_answer"] is not None
    assert await room.tool_starts(public_id) == 1


async def test_passthrough_held_then_refused_ends_canceled_owner_refused():
    kernel, _store, runtime, _port, room, run_id = await passthrough()

    held = await start(kernel, room, run_id)
    call_id = entry_of(held.run).call_id
    public_id = entry_of(held.run).opaque_public_call_id

    done = await observe(kernel, room, run_id, answer_of(run_id, call_id, "Reject"))

    assert done.outcome == "aborted"
    assert done.run.status == "canceled"
    assert done.run.cancellation_cause == "owner_refused"
    assert runtime.execute_log == []
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "canceled"
    assert turn["terminal_code"] == "owner_refused"
    assert await room.tool_starts(public_id) == 1


# -- Row 1: the hold ----------------------------------------------------------


async def test_the_hold_creates_one_question_then_writes_the_entry():
    run = make_run()
    kernel, _store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))], run=run
    )

    held = await start(kernel, room, run_id)

    assert held.run.status == "awaiting_user"
    entry = entry_of(held.run)
    assert entry.state == "input_required"
    assert entry.rule_hold is not None
    assert entry.rule_hold.state == "asked"
    assert entry.rule_hold.rule_id == RULE
    assert entry.acceptance is None
    assert entry.invocation is not None
    assert entry.suspended_call_record_id is None
    assert entry.interaction_id is None
    assert entry.budget_reserved
    assert held.run.budget.agent_calls_used == 1
    (call,) = port.calls
    assert call["call_id"] == "call-1"
    assert (
        call["interaction_id"]
        == sha256(f"{run_id}:rule-ask:call-1".encode()).hexdigest()
    )
    assert call["question"] == "Allow this call to fake_agent_echo?"
    starts = [p for kind, p in room.emitted if kind == "tool_execution_started"]
    assert [p["public_event_id"] for p in starts] == [
        f"public:{run_id}:{entry.opaque_public_call_id}:start"
    ]


async def test_the_question_shows_the_task_text_sanitized_and_cut():
    kernel, _store, runtime, port, room, run_id = await passthrough()
    kernel.public_secret_values = ("sk-secret-value",)
    stored = _store.runs[run_id]
    long_text = "Fix the bug with key sk-secret-value. " + "x" * 3000
    message = stored.transcript[0].model_copy(
        update={"content": [TextPart(text=long_text)]}
    )
    _store.runs[run_id] = stored.model_copy(
        update={"transcript": [message, *stored.transcript[1:]]}
    )

    await start(kernel, room, run_id)

    (call,) = port.calls
    text = call["question"]
    # The fixture Run has no frozen catalog, so the label is the tool name.
    head = f"Allow this call to {AGENT}?\n\n"
    assert text.startswith(head + "Fix the bug with key [REDACTED]. xxx")
    assert "sk-secret-value" not in text
    assert len(text) == len(head) + 2_000


async def test_a_held_call_shows_its_question_in_the_room_and_sends_nothing():
    kernel, _store, _m, tools, _port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )

    await start(kernel, room, run_id)

    assert tools.execute_log == []
    types = await room.public_types()
    assert types.count("tool_execution_start") == 1
    assert "tool_execution_update" in types
    assert "turn_end" not in types


# -- Rows 4 to 6: Stop, a canceled question, expiry ---------------------------


async def _stop(store, run):
    requested = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id="stop-1",
        cause="user_requested",
        requested_at=NOW + timedelta(seconds=1),
    )
    assert requested.outcome == "accepted"


@pytest.mark.parametrize("profile", ["room_chat", "passthrough"])
async def test_stop_while_asked_settles_canceled(profile):
    if profile == "room_chat":
        kernel, store, _m, tools, _p, room, run_id = await room_chat(
            [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
        )
    else:
        kernel, store, tools, _p, room, run_id = await passthrough()
    held = await start(kernel, room, run_id)
    await _stop(store, held.run)

    result = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="user_requested",
        cancellation_cause="user_requested",
        lifecycle=room.lifecycle,
    )

    assert result.run.status == "canceled"
    assert result.run.cancellation_cause == "user_requested"
    assert tools.execute_log == []
    turn = await settled_turn(room, result.run)
    assert turn["state"] == "canceled"
    assert turn["terminal_code"] == "user_requested"


async def test_a_canceled_question_settles_the_run_canceled():
    kernel, store, _m, _t, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    held = await start(kernel, room, run_id)
    await _stop(store, held.run)

    result = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="Human input request was canceled",
        lifecycle=room.lifecycle,
    )

    turn = await settled_turn(room, result.run)
    assert turn["state"] == "canceled"


@pytest.mark.parametrize("profile", ["room_chat", "passthrough"])
async def test_an_expired_question_fails_the_run(profile):
    if profile == "room_chat":
        kernel, _s, _m, _t, _p, room, run_id = await room_chat(
            [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
        )
    else:
        kernel, _s, _t, _p, room, run_id = await passthrough()
    await start(kernel, room, run_id)

    result = await kernel.terminalize(
        run_id,
        status="failed",
        reason="Human input request expired",
        lifecycle=room.lifecycle,
    )

    assert result.run.status == "failed"
    turn = await settled_turn(room, result.run)
    assert turn["state"] == "failed"


# -- Rows 7 to 9: late, unknown and repeated answers --------------------------


async def test_an_answer_after_the_run_ended_is_refused():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    held = await start(kernel, room, run_id)
    await _stop(store, held.run)
    ended = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="user_requested",
        cancellation_cause="user_requested",
        lifecycle=room.lifecycle,
    )

    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    assert tools.execute_log == []
    assert (await store.load(run_id)).state_version == ended.run.state_version


@pytest.mark.parametrize("text", ["Maybe", "approve", " Approve", "", "Reject"])
def test_the_answer_fails_closed_unless_it_is_exactly_approve(text):
    observation = held_call_answer_observation("run-1", "call-1", text, NOW)

    assert isinstance(observation.outcome, HeldCallAnswer)
    assert observation.outcome.approved is False
    assert held_call_answer_observation(
        "run-1", "call-1", "Approve", NOW
    ).outcome.approved


def test_the_observation_id_is_a_pure_function_of_the_run_call_and_answer():
    first = held_call_answer_observation("run-1", "call-1", "Approve", NOW)
    again = held_call_answer_observation(
        "run-1", "call-1", "Approve", NOW + timedelta(hours=1)
    )
    refused = held_call_answer_observation("run-1", "call-1", "Reject", NOW)

    assert first.observation_id == again.observation_id
    assert first.observation_id != refused.observation_id
    assert (
        held_call_answer_observation("run-1", "call-2", "Approve", NOW).observation_id
        != first.observation_id
    )


@pytest.mark.parametrize("text", ["Approve", "Reject"])
async def test_the_same_answer_applied_twice_acts_once(text):
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    await start(kernel, room, run_id)
    observation = answer_of(run_id, "call-1", text)
    first = await observe(kernel, room, run_id, observation)
    version = first.run.state_version

    again = await observe(kernel, room, run_id, observation)

    assert again.outcome == first.outcome
    assert again.run.state_version == version
    assert len(tool_results(again.run)) == 1
    assert tools.execute_log == (["call-1"] if text == "Approve" else [])


async def test_a_second_different_answer_is_refused():
    kernel, _s, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    await start(kernel, room, run_id)
    await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Reject"))

    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    assert tools.execute_log == []


async def test_a_held_call_accepts_no_other_observation():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    held = await start(kernel, room, run_id)
    entry = entry_of(held.run)
    result = ToolObservation(
        observation_id="agent-result",
        invocation_id=entry.invocation.invocation_id,
        outcome=ToolResult(
            call_id="call-1",
            tool_name=ECHO,
            status="completed",
            content=[TextPart(text="sent anyway")],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )

    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, result)
    with pytest.raises(KernelConflict):
        await observe(
            kernel,
            room,
            run_id,
            supervisor_answer_observation(run_id, "call-1", "Approve", NOW),
        )

    assert entry_of(await store.load(run_id)) == entry


async def test_a_held_call_answer_for_an_ordinary_call_is_refused():
    kernel, store, _m, _t, _p, room, run_id = await room_chat(
        [tool_events(("call-1", PAUSE, '{"status":"waiting_external"}'))]
    )
    parked = await start(kernel, room, run_id)
    assert parked.outcome == "waiting_external"

    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))


# -- Row 10: approve racing Stop ----------------------------------------------


async def test_stop_after_the_approve_settles_one_terminal_state():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    await start(kernel, room, run_id)
    original_accept = tools.accept

    async def stop_inside_accept(invocation, *, rule_approved=False):
        acceptance = await original_accept(invocation, rule_approved=rule_approved)
        current = await store.load(run_id)
        await _stop(store, current)
        return acceptance

    tools.accept = stop_inside_accept

    resumed = await observe(
        kernel, room, run_id, answer_of(run_id, "call-1", "Approve")
    )

    assert resumed.outcome == "cancellation_pending"
    settled = await kernel.terminalize(
        run_id,
        status="canceled",
        reason="user_requested",
        cancellation_cause="user_requested",
        lifecycle=room.lifecycle,
    )
    assert settled.run.status == "canceled"
    assert tools.execute_log == []
    turn = await settled_turn(room, settled.run)
    assert turn["state"] == "canceled"


async def test_an_approve_on_a_canceling_run_is_refused():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    held = await start(kernel, room, run_id)
    await _stop(store, held.run)

    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    assert tools.accept_calls == [("call-1", False)]


# -- Row 12: the approved call suspends ---------------------------------------


async def test_room_chat_an_approved_call_that_parks_is_stored_as_waiting():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [
            tool_events(("call-1", PAUSE, '{"status":"waiting_external"}')),
            final_events(),
        ],
        ask={PAUSE: RULE},
    )
    await start(kernel, room, run_id)

    resumed = await observe(
        kernel, room, run_id, answer_of(run_id, "call-1", "Approve")
    )

    assert resumed.outcome == "waiting_external"
    stored = await store.load(run_id)
    assert stored.status == "waiting_external"
    assert entry_of(stored).state == "waiting_external"
    assert entry_of(stored).rule_hold.state == "approved"
    entry = entry_of(stored)
    done = await observe(
        kernel,
        room,
        run_id,
        ToolObservation(
            observation_id="later",
            invocation_id=entry.invocation.invocation_id,
            outcome=ToolResult(
                call_id="call-1",
                tool_name=PAUSE,
                status="completed",
                content=[TextPart(text="done later")],
                artifact_refs=[],
            ),
            observed_at=NOW,
        ),
    )
    assert done.outcome == "final_answer"
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "completed"


@pytest.mark.parametrize("suspension", ["waiting", "question"])
async def test_passthrough_an_approved_call_that_parks_is_stored_as_waiting(
    suspension,
):
    kernel, store, runtime, _p, room, run_id = await passthrough()
    runtime.answers[AGENT] = [waiting() if suspension == "waiting" else question()]
    held = await start(kernel, room, run_id)

    resumed = await observe(
        kernel, room, run_id, answer_of(run_id, entry_of(held.run).call_id, "Approve")
    )

    stored = await store.load(run_id)
    assert stored.status in {"waiting_external", "awaiting_user"}
    assert resumed.outcome in {"waiting_external", "awaiting_user"}
    if suspension == "waiting":
        assert entry_of(stored).state == "waiting_external"
    else:
        assert entry_of(stored).state == "input_required"
        assert runtime.published  # the Agent's own question reaches the owner
    await room.snapshot_turn(stored)


# -- Row 13: a sibling's result during the resumed pass -----------------------


async def test_a_siblings_result_is_refused_while_the_approved_call_runs():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"hi"}'),
                ("sibling", PAUSE, '{"status":"waiting_external"}'),
            ),
            final_events(),
        ]
    )
    held = await start(kernel, room, run_id)
    assert held.run.status == "awaiting_user"
    sibling = entry_of(held.run, "sibling")
    assert sibling.state == "waiting_external"
    sibling_result = ToolObservation(
        observation_id="sibling-done",
        invocation_id=sibling.invocation.invocation_id,
        outcome=ToolResult(
            call_id="sibling",
            tool_name=PAUSE,
            status="completed",
            content=[TextPart(text="sibling done")],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )
    refused_during_pass: list[str] = []
    original_execute = tools.execute

    async def execute_while_sibling_arrives(invocation, acceptance, *, signal):
        try:
            await observe(kernel, room, run_id, sibling_result)
        except KernelConflict:
            refused_during_pass.append((await store.load(run_id)).status)
        return await original_execute(invocation, acceptance, signal=signal)

    tools.execute = execute_while_sibling_arrives

    resumed = await observe(
        kernel, room, run_id, answer_of(run_id, "call-1", "Approve")
    )

    # The Run was running, not suspended, while the approved call executed.
    assert refused_during_pass == ["running"]
    assert resumed.outcome == "waiting_external"
    stored = await store.load(run_id)
    assert stored.status == "waiting_external"
    assert entry_of(stored, "sibling").state == "waiting_external"
    assert entry_of(stored, "call-1").state == "terminal"
    # Applied once the pass ends, as the inbox retries it.
    done = await observe(kernel, room, run_id, sibling_result)
    assert done.outcome == "final_answer"
    assert sorted(m.call_id for m in tool_results(done.run)) == ["call-1", "sibling"]
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "completed"


# -- Row 14: the Question port fails ------------------------------------------


@pytest.mark.parametrize("profile", ["room_chat", "passthrough"])
async def test_a_failed_question_port_leaves_no_open_tool_row(profile, caplog):
    port = RecordingRuleQuestion(fail=True)
    if profile == "room_chat":
        kernel, store, _m, tools, _p, room, run_id = await room_chat(
            [tool_events(("call-1", ECHO, '{"value":"hi"}'))], port=port
        )
    else:
        kernel, store, tools, _p, room, run_id = await passthrough(port=port)

    with caplog.at_level(logging.ERROR, logger="execution.orchestrator.kernel"):
        with pytest.raises(RuntimeError, match="secret-detail"):
            await start(kernel, room, run_id)

    stored = await store.load(run_id)
    entry = entry_of(stored)
    assert entry.state == "pending"
    assert entry.rule_hold is None
    assert "tool_execution_start" not in await room.public_types()
    assert tools.execute_log == []
    [record] = [
        r for r in caplog.records if r.getMessage() == "rule_ask_creation_failed"
    ]
    assert record.run_id == run_id
    assert record.client_request_id == stored.client_request_id
    assert record.call_id == entry.call_id
    assert "secret-detail" not in caplog.text

    failed = await kernel.terminalize(
        run_id, status="failed", reason="tool_failure", lifecycle=room.lifecycle
    )
    turn = await settled_turn(room, failed.run)
    assert turn["state"] == "failed"
    assert [item for item in turn["activity"] if item["kind"] == "tool"] == []


async def test_an_unbound_question_port_fails_the_hold():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    kernel.rule_question = None

    with pytest.raises(KernelConflict, match="rule question port"):
        await start(kernel, room, run_id)

    assert entry_of(await store.load(run_id)).state == "pending"


# -- Crashes ------------------------------------------------------------------


async def test_crash_a_after_the_question_before_the_entry(monkeypatch):
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    original = kernel._update_entry
    died = []

    async def die_on_hold(*args, **kwargs):
        if kwargs.get("command") == "rule-hold:call-1" and not died:
            died.append(True)
            raise ProcessDied
        return await original(*args, **kwargs)

    monkeypatch.setattr(kernel, "_update_entry", die_on_hold)
    with pytest.raises(ProcessDied):
        await start(kernel, room, run_id)
    after_crash = await store.load(run_id)
    assert entry_of(after_crash).state == "pending"
    assert entry_of(after_crash).budget_reserved

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert [c["interaction_id"] for c in port.calls] == [
        port.calls[0]["interaction_id"]
    ] * 2
    assert held.run.budget.agent_calls_used == 1
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert done.outcome == "final_answer"
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_crash_b_while_asked_resumes_nothing():
    kernel, _s, _m, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}'))]
    )
    held = await start(kernel, room, run_id)

    again = await start(kernel, room, run_id)

    assert again.outcome == "awaiting_user"
    assert again.run.state_version == held.run.state_version
    assert len(port.calls) == 1
    assert tools.execute_log == []


@pytest.mark.parametrize("profile", ["room_chat", "passthrough"])
async def test_crash_c_after_the_approve_before_accept(profile):
    if profile == "room_chat":
        kernel, store, _m, tools, _p, room, run_id = await room_chat(
            [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
        )
    else:
        kernel, store, tools, _p, room, run_id = await passthrough()
        tools.answers[AGENT] = [answer("Done.")]
    held = await start(kernel, room, run_id)
    call_id = entry_of(held.run).call_id
    tools.die_on_approved_accept = True
    approve = answer_of(run_id, call_id, "Approve")

    with pytest.raises(ProcessDied):
        await observe(kernel, room, run_id, approve)
    stored = await store.load(run_id)
    assert stored.status == "running"
    assert entry_of(stored).state == "pending"
    assert entry_of(stored).rule_hold.state == "approved"

    # A replay never drives a running Run: another pass may still own it.
    # The decision is durable, and recovery resumes the Run.
    with pytest.raises(KernelConflict):
        await observe(kernel, room, run_id, approve)
    assert tools.execute_log == []

    done = await start(kernel, room, run_id)

    assert done.outcome == "final_answer"
    assert tools.accept_calls[-1] == (call_id, True)
    assert tools.execute_log == [call_id]
    assert done.run.budget.agent_calls_used == 1
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_crash_c_recovery_through_run_also_resumes():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    await start(kernel, room, run_id)
    tools.die_on_approved_accept = True
    with pytest.raises(ProcessDied):
        await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    done = await start(kernel, room, run_id)

    assert done.outcome == "final_answer"
    assert tools.accept_calls == [("call-1", False), ("call-1", True), ("call-1", True)]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_crash_e_after_the_refuse_only_the_exit_runs_again():
    kernel, store, runtime, _p, room, run_id = await passthrough()
    held = await start(kernel, room, run_id)
    room.die_before.add("turn_completed")

    with pytest.raises(ProcessDied):
        await observe(
            kernel,
            room,
            run_id,
            answer_of(run_id, entry_of(held.run).call_id, "Reject"),
        )
    stored = await store.load(run_id)
    assert entry_of(stored).state == "terminal"
    assert stored.tool_batches[0].results_flushed

    done = await start(kernel, room, run_id)

    assert done.run.status == "canceled"
    assert done.run.cancellation_cause == "owner_refused"
    assert runtime.execute_log == []
    turn = await settled_turn(room, done.run)
    assert turn["state"] == "canceled"
    assert turn["terminal_code"] == "owner_refused"


async def test_a_crash_before_the_held_start_is_healed_by_the_next_pass():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    room.die_before.add("tool_execution_started")

    with pytest.raises(ProcessDied):
        await start(kernel, room, run_id)
    stored = await store.load(run_id)
    assert entry_of(stored).rule_hold.state == "asked"
    assert stored.status == "running"
    assert "tool_execution_start" not in await room.public_types()

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    public_id = entry_of(held.run).opaque_public_call_id
    assert await room.tool_starts(public_id) == 1
    assert (await room.snapshot_turn(held.run))["state"] == "active"
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert (await settled_turn(room, done.run))["state"] == "completed"
    assert await room.tool_starts(public_id) == 1


# -- Several calls in one batch -----------------------------------------------


async def test_an_allowed_sibling_runs_in_the_same_pass():
    kernel, _s, _m, tools, _p, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"hi"}'),
                ("allowed", "fake_agent_delay_parallel", '{"seconds":0}'),
            ),
            final_events(),
        ]
    )

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert tools.execute_log == ["allowed"]
    assert entry_of(held.run, "allowed").state == "terminal"
    assert not held.run.tool_batches[0].results_flushed
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert done.outcome == "final_answer"
    assert sorted(tools.execute_log) == ["allowed", "call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_a_denied_sibling_is_terminal_at_once():
    class AskAndDeny(AskingToolRuntime):
        async def accept(self, invocation, *, rule_approved=False):
            if invocation.tool.definition.name == "fake_agent_delay_parallel":
                raise AgentCallDenied("rule_deny")
            return await super().accept(invocation, rule_approved=rule_approved)

    room = RoomLog()
    tools = AskAndDeny({ECHO: RULE})
    kernel, store, _m, _ = await make_kernel(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"hi"}'),
                ("denied", "fake_agent_delay_parallel", '{"seconds":0}'),
            ),
            final_events(),
        ],
        tool_runtime=tools,
    )
    kernel.rule_question = RecordingRuleQuestion()
    kernel.canonical_event_reader = room.read
    run_id = next(iter(store.runs))
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert entry_of(held.run, "denied").denied_by_rule
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert done.outcome == "final_answer"
    assert [m.error_code for m in tool_results(done.run)].count(
        "agent_call_denied"
    ) == 1
    # The Room fold is not read here: a denial in Room chat closes its turn
    # as an error with no retry row, which the fold already refuses at a
    # completed settlement without any rule answering "ask".


async def test_a_later_acceptance_failure_fails_the_run_and_closes_the_hold():
    kernel, _s, _m, tools, _p, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"hi"}'),
                ("broken", "fake_agent_delay_parallel", '{"seconds":0}'),
            ),
            final_events(),
        ],
        fail_accept_for={"fake_agent_delay_parallel"},
    )

    result = await start(kernel, room, run_id)

    assert result.outcome == "failed"
    assert result.run.status == "failed"
    assert entry_of(result.run, "call-1").state == "terminal"
    assert (await settled_turn(room, result.run))["state"] == "failed"


# -- One owner Question at a time ---------------------------------------------


def public_id(run_id, call_id):
    # The id a pending call's row would start under.
    return _opaque_public_call_id(run_id, call_id)


def asked_calls(port):
    return [call["call_id"] for call in port.calls]


async def test_two_held_calls_are_asked_one_at_a_time():
    kernel, store, model, tools, port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events(),
        ]
    )
    held = await start(kernel, room, run_id)

    # The Room shows one open Question per turn; the second call waits unasked.
    assert asked_calls(port) == ["call-1"]
    assert held.run.status == "awaiting_user"
    waiting_call = entry_of(held.run, "call-2")
    assert waiting_call.state == "pending"
    assert waiting_call.rule_hold is None
    assert await room.tool_starts(public_id(run_id, "call-2")) == 0

    first = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))

    assert first.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1", "call-2"]
    stored = await store.load(run_id)
    assert stored.status == "awaiting_user"
    assert entry_of(stored, "call-1").state == "terminal"
    assert entry_of(stored, "call-2").rule_hold.state == "asked"

    done = await observe(kernel, room, run_id, answer_of(run_id, "call-2", "Reject"))

    assert done.outcome == "final_answer"
    assert asked_calls(port) == ["call-1", "call-2"]
    by_call = {m.call_id: m for m in tool_results(done.run)}
    assert by_call["call-1"].status == "completed"
    assert by_call["call-2"].error_code == "agent_call_refused"
    last_request = model.requests[-1].messages
    assert sum(1 for message in last_request if message.role == "tool") == 2
    assert tools.execute_log == ["call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_refusing_the_first_held_call_asks_about_the_next():
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events(),
        ]
    )
    await start(kernel, room, run_id)

    refused = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Reject"))

    assert refused.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1", "call-2"]
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-2", "Approve"))
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-2"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


class RecordingAskUser:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


ASK_USER = ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')


async def test_a_held_call_waits_while_an_ask_user_question_is_open():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(ASK_USER, ("call-1", ECHO, '{"value":"one"}')),
            final_events(),
        ],
        supervisor_hitl=ask_user,
    )

    held = await start(kernel, room, run_id)

    assert len(ask_user.calls) == 1
    assert asked_calls(port) == []
    assert entry_of(held.run, "call-1").state == "pending"

    answered = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )

    assert answered.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1"]
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_an_ask_user_question_waits_while_a_held_call_is_asked():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(("call-1", ECHO, '{"value":"one"}'), ASK_USER),
            final_events(),
        ],
        supervisor_hitl=ask_user,
    )

    held = await start(kernel, room, run_id)

    assert asked_calls(port) == ["call-1"]
    assert ask_user.calls == []
    assert entry_of(held.run, "ask-1").state == "pending"
    assert await room.tool_starts(public_id(run_id, "ask-1")) == 0

    approved = await observe(
        kernel, room, run_id, answer_of(run_id, "call-1", "Approve")
    )

    assert approved.outcome == "awaiting_user"
    assert len(ask_user.calls) == 1
    assert entry_of(approved.run, "ask-1").state == "input_required"
    done = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


# -- Only the flag decides ----------------------------------------------------


async def test_an_agents_own_agent_call_refused_code_does_not_end_as_refused():
    kernel, _s, runtime, _p, room, run_id = await passthrough()
    runtime.ask = {}
    runtime.answers[AGENT] = [
        answer("", status="failed", error_code="agent_call_refused")
    ]

    result = await start(kernel, room, run_id)

    assert result.run.status == "failed"
    assert result.run.cancellation_cause is None
    assert (await settled_turn(room, result.run))["state"] == "failed"


async def test_a_replayed_approve_while_the_approved_call_runs_starts_no_second_pass():
    kernel, store, _m, tools, _p, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"hi"}')), final_events()]
    )
    await start(kernel, room, run_id)
    approve = answer_of(run_id, "call-1", "Approve")
    replays: list[str] = []
    original_execute = tools.execute

    async def execute_while_the_answer_is_replayed(invocation, acceptance, *, signal):
        try:
            await observe(kernel, room, run_id, approve)
        except KernelConflict:
            replays.append((await store.load(run_id)).status)
        return await original_execute(invocation, acceptance, signal=signal)

    tools.execute = execute_while_the_answer_is_replayed

    done = await observe(kernel, room, run_id, approve)

    assert replays == ["running"]
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_a_refusal_asks_the_next_call_while_a_sibling_agent_still_works():
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("sibling", PAUSE, '{"status":"waiting_external"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events(),
        ]
    )
    await start(kernel, room, run_id)
    assert asked_calls(port) == ["call-1"]

    refused = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Reject"))

    # The next Question opens now, not when the sibling Agent answers.
    assert refused.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1", "call-2"]
    assert entry_of(refused.run, "sibling").state == "waiting_external"


async def test_an_ask_user_answer_asks_the_held_call_while_a_sibling_agent_works():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(
                ASK_USER,
                ("sibling", PAUSE, '{"status":"waiting_external"}'),
                ("call-1", ECHO, '{"value":"one"}'),
            ),
            final_events(),
        ],
        supervisor_hitl=ask_user,
    )
    await start(kernel, room, run_id)
    assert asked_calls(port) == []

    answered = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )

    assert answered.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1"]
    assert entry_of(answered.run, "sibling").state == "waiting_external"


# -- A waiting call with forwarded Agent questions and joins ------------------


class AskByCallRuntime(InteractionRuntime):
    """The rule asks about the named calls; any other call is allowed."""

    def __init__(self, ask_calls: set[str]) -> None:
        super().__init__()
        self.ask_calls = ask_calls

    async def accept(self, invocation, *, rule_approved: bool = False):
        if invocation.invocation_id in self.ask_calls and not rule_approved:
            raise AgentCallNeedsAnswer(RULE)
        return await super().accept(invocation)


async def agent_question_kernel(scripts, ask_calls):
    """Room chat where call-1 parks with an Agent question the model sees."""
    room = RoomLog()
    tools = AskByCallRuntime(ask_calls)
    tools.suspensions["call-1"] = interaction_suspension("call-1")
    kernel, store, model, _ = await make_kernel(
        scripts, tool_runtime=tools, supervisor_hitl=RecordingAskUser()
    )
    port = RecordingRuleQuestion()
    kernel.rule_question = port
    kernel.canonical_event_reader = room.read
    run_id = next(iter(store.runs))
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    return kernel, store, tools, port, room, run_id


def parent_answer(text="thanks"):
    return ToolObservation(
        observation_id=f"parent-{text}",
        invocation_id="call-1",
        outcome=ToolResult(
            call_id="call-1",
            tool_name=PAUSE,
            status="completed",
            content=[TextPart(text=text)],
            artifact_refs=[],
        ),
        observed_at=NOW,
    )


SURFACE = ("surface", SURFACE_AGENT_QUESTIONS_TOOL_NAME, "{}")
PARK = ("call-1", PAUSE, '{"status":"input_required"}')
HELD = ("held", ECHO, '{"value":"one"}')


async def test_a_held_call_waits_while_a_forwarded_agent_question_is_open():
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [tool_events(PARK), tool_events(SURFACE, HELD), final_events()],
        {"held"},
    )

    forwarded = await start(kernel, room, run_id)

    assert forwarded.outcome == "awaiting_user"
    assert entry_of(forwarded.run, "surface").state == "input_required"
    assert entry_of(forwarded.run, "held").state == "pending"
    assert asked_calls(port) == []

    # The owner's answer goes to the parked Agent and closes the forward.
    answered = await observe(kernel, room, run_id, parent_answer())

    assert answered.outcome == "awaiting_user"
    assert asked_calls(port) == ["held"]
    done = await observe(kernel, room, run_id, answer_of(run_id, "held", "Approve"))
    assert done.outcome == "final_answer"
    assert "held" in tools.execute_log


@pytest.mark.parametrize("decision", ["Approve", "Reject"])
async def test_a_forward_waits_while_a_held_call_is_asked(decision):
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [tool_events(PARK), tool_events(HELD, SURFACE), final_events()],
        {"held"},
    )

    held = await start(kernel, room, run_id)

    assert asked_calls(port) == ["held"]
    assert entry_of(held.run, "surface").state == "pending"
    assert tools.published == []

    answered = await observe(kernel, room, run_id, answer_of(run_id, "held", decision))

    # The forward goes out once the owner decided about the held call.
    assert answered.outcome == "awaiting_user"
    assert tools.published == [("parent-1", "interaction-1")]
    assert entry_of(answered.run, "surface").state == "input_required"
    done = await observe(kernel, room, run_id, parent_answer())
    assert done.outcome == "final_answer"


async def test_a_waiting_call_is_never_sent_as_a_reply_to_a_parked_agent():
    # call-1 parks with a question while "held" is asked; "late" calls the
    # same Agent and waits. The refusal then presents call-1's question, and
    # the waiting call must still go to the rule, not become a reply to it.
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [
            tool_events(HELD, PARK, ("late", PAUSE, '{"status":"input_required"}')),
            final_events(),
        ],
        {"held", "late"},
    )

    first = await start(kernel, room, run_id)

    assert asked_calls(port) == ["held"]
    assert entry_of(first.run, "late").state == "pending"

    await observe(kernel, room, run_id, answer_of(run_id, "held", "Reject"))

    assert tools.model_reply_calls == []
    assert asked_calls(port) == ["held", "late"]


async def test_a_waiting_call_behind_another_batchs_working_agent_waits_for_it():
    # The forward's answer leaves call-1 working in the first batch, which the
    # loop runs first. The held call in the second batch is asked once call-1
    # ends; until then the Run waits on the Agent and accepts its result.
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [tool_events(PARK), tool_events(SURFACE, HELD), final_events()],
        {"held"},
    )
    await start(kernel, room, run_id)
    working = ToolObservation(
        observation_id="parent-working",
        invocation_id="call-1",
        outcome=ToolSuspension(invocation_id="call-1", status="waiting_external"),
        observed_at=NOW,
    )

    waiting = await asyncio.wait_for(observe(kernel, room, run_id, working), 5)

    assert waiting.outcome == "waiting_external"
    assert (await store.load(run_id)).status == "waiting_external"
    assert asked_calls(port) == []

    answered = await asyncio.wait_for(observe(kernel, room, run_id, parent_answer()), 5)

    assert answered.outcome == "awaiting_user"
    assert asked_calls(port) == ["held"]
    done = await observe(kernel, room, run_id, answer_of(run_id, "held", "Approve"))
    assert done.outcome == "final_answer"
    assert "held" in tools.execute_log


@pytest.mark.parametrize("decision", ["Approve", "Reject"])
async def test_an_ask_user_call_that_waited_is_asked_beside_a_parked_agent(decision):
    # ask-1 was valid when it waited; call-1's question, presented meanwhile,
    # does not turn it into a refused declaration that strands the Run.
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [tool_events(HELD, PARK, ASK_USER), final_events()], {"held"}
    )
    held = await start(kernel, room, run_id)
    assert asked_calls(port) == ["held"]
    assert entry_of(held.run, "ask-1").state == "pending"

    answered = await observe(kernel, room, run_id, answer_of(run_id, "held", decision))

    assert answered.outcome == "awaiting_user"
    assert entry_of(answered.run, "ask-1").state == "input_required"
    assert len(kernel.supervisor_hitl.calls) == 1
    assert await room.tool_starts(public_id(run_id, "ask-1")) == 1
    done = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )
    # The model then decides about call-1's question and answers.
    assert done.outcome == "final_answer"


async def test_a_restart_after_a_call_was_deferred_asks_each_call_once():
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(
                ("call-1", ECHO, '{"value":"one"}'),
                ("call-2", ECHO, '{"value":"two"}'),
            ),
            final_events(),
        ]
    )
    original = kernel._checkpoint

    async def die_after_the_deferral(run, *, updates, command_id, **kwargs):
        written = await original(run, updates=updates, command_id=command_id, **kwargs)
        if command_id.startswith("rule-deferred:"):
            raise ProcessDied
        return written

    kernel._checkpoint = die_after_the_deferral
    with pytest.raises(ProcessDied):
        await start(kernel, room, run_id)
    kernel._checkpoint = original

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1"]
    assert entry_of(held.run, "call-2").rule_deferred
    assert entry_of(held.run, "call-2").rule_hold is None
    first = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert asked_calls(port) == ["call-1", "call-2"]
    done = await observe(kernel, room, run_id, answer_of(run_id, "call-2", "Approve"))
    assert first.outcome == "awaiting_user"
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-1", "call-2"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_a_restart_after_an_ask_user_call_was_deferred_asks_it_once():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"one"}'), ASK_USER), final_events()],
        supervisor_hitl=ask_user,
    )
    original = kernel._checkpoint

    async def die_after_the_deferral(run, *, updates, command_id, **kwargs):
        written = await original(run, updates=updates, command_id=command_id, **kwargs)
        if command_id == "rule-deferred:ask-1":
            raise ProcessDied
        return written

    kernel._checkpoint = die_after_the_deferral
    with pytest.raises(ProcessDied):
        await start(kernel, room, run_id)
    kernel._checkpoint = original

    held = await start(kernel, room, run_id)

    assert held.outcome == "awaiting_user"
    assert asked_calls(port) == ["call-1"]
    assert ask_user.calls == []
    assert entry_of(held.run, "ask-1").state == "pending"
    assert entry_of(held.run, "ask-1").rule_deferred
    approved = await observe(
        kernel, room, run_id, answer_of(run_id, "call-1", "Approve")
    )
    assert approved.outcome == "awaiting_user"
    assert len(ask_user.calls) == 1
    assert await room.tool_starts(public_id(run_id, "ask-1")) == 1
    done = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )
    assert done.outcome == "final_answer"
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_an_ask_user_call_after_an_executable_sibling_ends_the_turn_in_order():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(("call-1", ECHO, '{"value":"one"}'), ASK_USER), final_events()],
        ask={},
        supervisor_hitl=ask_user,
    )

    asked = await start(kernel, room, run_id)

    assert asked.outcome == "awaiting_user"
    assert tools.execute_log == ["call-1"]
    done = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )
    assert done.outcome == "final_answer"
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_a_held_call_asked_in_a_later_pass_ends_the_turn_in_order():
    # h2 waits while h1 is asked and starts after c3, a later sibling.
    kernel, store, tools, port, room, run_id = await agent_question_kernel(
        [
            tool_events(
                ("h1", ECHO, '{"value":"one"}'),
                ("h2", ECHO, '{"value":"two"}'),
                ("c3", ECHO, '{"value":"three"}'),
            ),
            final_events(),
        ],
        {"h1", "h2"},
    )
    await start(kernel, room, run_id)
    assert asked_calls(port) == ["h1"]
    await observe(kernel, room, run_id, answer_of(run_id, "h1", "Approve"))
    assert asked_calls(port) == ["h1", "h2"]

    done = await observe(kernel, room, run_id, answer_of(run_id, "h2", "Approve"))

    assert done.outcome == "final_answer"
    assert sorted(tools.execute_log) == ["c3", "h1", "h2"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


ASK_USER_2 = ("ask-2", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which day?"}')


async def test_two_ask_user_calls_are_asked_one_at_a_time():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(ASK_USER, ASK_USER_2), final_events()],
        supervisor_hitl=ask_user,
    )

    first = await start(kernel, room, run_id)

    assert first.outcome == "awaiting_user"
    assert [call["call_id"] for call in ask_user.calls] == ["ask-1"]
    assert entry_of(first.run, "ask-2").state == "pending"
    assert await room.tool_starts(public_id(run_id, "ask-2")) == 0

    second = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
    )

    assert second.outcome == "awaiting_user"
    assert [call["call_id"] for call in ask_user.calls] == ["ask-1", "ask-2"]
    done = await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, "ask-2", "Monday", NOW),
    )
    assert done.outcome == "final_answer"
    assert (await settled_turn(room, done.run))["state"] == "completed"


def asked_users(ask_user) -> list[str]:
    return [call["call_id"] for call in ask_user.calls]


async def answer_user(kernel, room, run_id, call_id):
    return await observe(
        kernel,
        room,
        run_id,
        supervisor_answer_observation(run_id, call_id, "Shanghai", NOW),
    )


async def test_two_ask_user_calls_then_a_held_call_are_asked_one_at_a_time():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [
            tool_events(ASK_USER, ASK_USER_2, ("call-1", ECHO, '{"value":"one"}')),
            final_events(),
        ],
        supervisor_hitl=ask_user,
    )

    await start(kernel, room, run_id)
    assert (asked_users(ask_user), asked_calls(port)) == (["ask-1"], [])

    await answer_user(kernel, room, run_id, "ask-1")
    assert (asked_users(ask_user), asked_calls(port)) == (["ask-1", "ask-2"], [])

    held = await answer_user(kernel, room, run_id, "ask-2")
    assert held.outcome == "awaiting_user"
    assert (asked_users(ask_user), asked_calls(port)) == (
        ["ask-1", "ask-2"],
        ["call-1"],
    )

    done = await observe(kernel, room, run_id, answer_of(run_id, "call-1", "Approve"))
    assert done.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert (await settled_turn(room, done.run))["state"] == "completed"


async def test_a_restart_between_two_ask_user_calls_asks_each_once():
    ask_user = RecordingAskUser()
    kernel, store, _m, tools, port, room, run_id = await room_chat(
        [tool_events(ASK_USER, ASK_USER_2), final_events()],
        supervisor_hitl=ask_user,
    )
    original = kernel._checkpoint

    async def die_after_the_deferral(run, *, updates, command_id, **kwargs):
        written = await original(run, updates=updates, command_id=command_id, **kwargs)
        if command_id == "rule-deferred:ask-2":
            raise ProcessDied
        return written

    kernel._checkpoint = die_after_the_deferral
    with pytest.raises(ProcessDied):
        await start(kernel, room, run_id)
    kernel._checkpoint = original

    restarted = await start(kernel, room, run_id)

    assert restarted.outcome == "awaiting_user"
    assert asked_users(ask_user) == ["ask-1"]
    assert entry_of(restarted.run, "ask-2").rule_deferred
    await answer_user(kernel, room, run_id, "ask-1")
    assert asked_users(ask_user) == ["ask-1", "ask-2"]
    done = await answer_user(kernel, room, run_id, "ask-2")
    assert done.outcome == "final_answer"
    assert (await settled_turn(room, done.run))["state"] == "completed"
