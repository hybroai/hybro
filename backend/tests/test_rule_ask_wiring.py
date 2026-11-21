"""The container's two ports for a check rule that answers "ask".

The question port creates the owner's Question; the resume port turns the
recorded answer into the observation the kernel accepts for the held call.
"""

from __future__ import annotations

import inspect

import pytest

import container
from common.dto.hitl import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
)
from execution.orchestrator.kernel import (
    REQUEST_USER_INPUT_TOOL_NAME,
    run_level_answer_disposition,
    run_level_answer_observation,
)
from execution.orchestrator.models import HeldCallAnswer, RuleHold, ToolResult
from tests._orchestrator_helpers import NOW, make_run


class RecordingHITL:
    def __init__(self, materialized: bool = True) -> None:
        self.calls: list[dict[str, object]] = []
        self.materialized = materialized

    async def request_interaction(self, **kwargs):
        self.calls.append(kwargs)
        return [object()] if self.materialized else None


async def test_the_question_port_asks_the_owner_as_the_system():
    hitl = RecordingHITL()
    run = make_run()
    port = container.rule_question_port(hitl)

    await port(
        run=run,
        interaction_id="interaction-1",
        call_id="call-1",
        question="Allow this call to Echo?",
    )

    (call,) = hitl.calls
    assert call["conversation_id"] == run.conversation_id
    assert call["user_message_id"] == run.request.user_message_id
    assert call["interaction_id"] == "interaction-1"
    assert call["application_route"] == HITLApplicationRoute.SUPERVISOR_RUN
    assert call["public_source"] == HITLPublicSource.SYSTEM
    assert call["evidence_origin"] == HITLEvidenceOrigin.SYSTEM
    assert call["route_snapshot"].route == HITLApplicationRoute.SUPERVISOR_RUN
    assert call["route_snapshot"].orchestration_run_id == run.run_id
    assert call["orchestration_run_id"] == run.run_id
    assert call["questions"] == [
        {
            "prompt": "Allow this call to Echo?",
            "prompt_type": "approval",
            "choices": ["Approve", "Reject"],
            "source_step_id": "call-1",
        }
    ]


async def test_the_question_port_fails_when_no_question_was_made():
    port = container.rule_question_port(RecordingHITL(materialized=False))

    with pytest.raises(RuntimeError):
        await port(
            run=make_run(),
            interaction_id="interaction-1",
            call_id="call-1",
            question="Allow this call to Echo?",
        )


def _with_entry(*, call_id: str, rule_hold: RuleHold | None):
    from execution.orchestrator.models import ToolBatchEntry, ToolCallBatch

    run = make_run()
    entry = ToolBatchEntry(
        call_id=call_id,
        assistant_message_id="assistant-1",
        tool_name="fake_agent_echo" if rule_hold else REQUEST_USER_INPUT_TOOL_NAME,
        source_index=0,
        state="input_required",
        rule_hold=rule_hold,
    )
    batch = ToolCallBatch(assistant_message_id="assistant-1", entries=[entry])
    return run.model_copy(update={"tool_batches": [batch]})


@pytest.mark.parametrize(
    ("answer", "approved"),
    [
        ("Approve", True),
        ("Reject", False),
        ("approve", False),
        ("Approve ", False),
        ("Yes", False),
    ],
)
def test_an_answer_to_a_held_call_fails_closed(answer, approved):
    run = _with_entry(call_id="call-1", rule_hold=RuleHold(rule_id="r1", state="asked"))

    observation = run_level_answer_observation(run, "call-1", answer, NOW)

    assert isinstance(observation.outcome, HeldCallAnswer)
    assert observation.outcome.approved is approved
    assert observation.invocation_id == "call-1"


def test_an_answer_to_an_ask_user_question_is_the_answer_text():
    run = _with_entry(call_id="ask-1", rule_hold=None)

    observation = run_level_answer_observation(run, "ask-1", "Approve", NOW)

    assert isinstance(observation.outcome, ToolResult)
    assert observation.outcome.tool_name == REQUEST_USER_INPUT_TOOL_NAME


def test_the_runtime_binds_the_question_port_and_the_resume_port():
    import orchestrator_composition

    lifespan = inspect.getsource(container._runtime_lifespan)
    compose = inspect.getsource(orchestrator_composition.create_orchestrator_runtime)

    assert lifespan.count("rule_question=rule_question_port(hitl_manager),") == 1
    assert compose.count("rule_question=rule_question,") == 1
    assert lifespan.count("supervisor_resume_port(") == 1
    assert "supervisor_answer_observation(" not in lifespan


@pytest.mark.parametrize(
    ("status", "hold", "expected"),
    [
        ("awaiting_user", "asked", "deliver"),
        ("running", "asked", "busy"),
        ("running", "approved", "applied"),
        ("running", "refused", "applied"),
        ("waiting_external", "approved", "deliver"),
        ("completed", "approved", "deliver"),
    ],
)
def test_the_resume_port_reads_the_hold_and_the_run_status(status, hold, expected):
    run = _with_entry(
        call_id="call-1", rule_hold=RuleHold(rule_id="r1", state=hold)
    ).model_copy(update={"status": status})

    assert run_level_answer_disposition(run, "call-1") == expected


@pytest.mark.parametrize("status", ["awaiting_user", "running"])
def test_an_ask_user_answer_is_always_delivered(status):
    run = _with_entry(call_id="ask-1", rule_hold=None).model_copy(
        update={"status": status}
    )

    assert run_level_answer_disposition(run, "ask-1") == "deliver"


@pytest.mark.parametrize(
    ("status", "hold", "expected"),
    [
        ("running", "asked", "busy"),
        ("running", "approved", True),
        ("running", "refused", True),
        ("awaiting_user", "asked", False),
    ],
)
async def test_a_refused_answer_waits_only_while_a_pass_runs(status, hold, expected):
    from approval.exceptions import HITLRunBusyError

    run = _with_entry(
        call_id="call-1", rule_hold=RuleHold(rule_id="r1", state=hold)
    ).model_copy(update={"status": status})

    class Store:
        async def load(self, _run_id):
            return run

    cause = RuntimeError("Run is currently executing")
    if expected == "busy":
        with pytest.raises(HITLRunBusyError) as raised:
            await container.rule_answer_applied_after_conflict(
                Store(), run.run_id, "call-1", cause, conversation_id="room-1"
            )
        assert raised.value.__cause__ is cause
    else:
        # True: the same answer was applied concurrently, so it is done.
        assert (
            await container.rule_answer_applied_after_conflict(
                Store(), run.run_id, "call-1", cause, conversation_id="room-1"
            )
            is expected
        )


@pytest.mark.parametrize("found", ["missing", "ask_user", "unreadable"])
async def test_an_answer_that_cannot_wait_keeps_its_own_failure(found):
    run = _with_entry(call_id="call-1", rule_hold=None).model_copy(
        update={"status": "running"}
    )

    class Store:
        async def load(self, _run_id):
            if found == "unreadable":
                raise TimeoutError("store timed out")
            return None if found == "missing" else run

    # The caller then logs and raises the original conflict.
    assert (
        await container.rule_answer_applied_after_conflict(
            Store(),
            "run-1",
            "call-1",
            RuntimeError("conflict"),
            conversation_id="room-1",
        )
        is False
    )


# -- The resume port, end to end into a kernel --------------------------------


async def _two_held_calls():
    """A Run whose two calls a rule holds, answered through the real port.

    The rule asks about call-1 first; call-2 is asked in the pass the
    owner's answer to call-1 starts, before call-1 runs.
    """
    from types import SimpleNamespace

    from execution.orchestrator.a2a_runtime.observations import (
        RunAddressedToolObservationSink,
    )
    from tests._orchestrator_helpers import (
        NeverCancelled,
        final_events,
        make_kernel,
        tool_events,
    )
    from tests._rule_ask_helpers import (
        AskingToolRuntime,
        RecordingRuleQuestion,
        RoomLog,
    )

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
    kernel.rule_question = RecordingRuleQuestion()
    kernel.canonical_event_reader = room.read
    run_id = next(iter(store.runs))
    await room.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    held = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=room.lifecycle)
    assert held.outcome == "awaiting_user"
    runtime = SimpleNamespace(
        run_store=store,
        # No live session owns the Run: the answer goes through the sink.
        session_host=SimpleNamespace(get_session=lambda _conversation_id: None),
        observation_sink=RunAddressedToolObservationSink(
            run_store=store,
            kernel_factory=lambda _run: kernel,
            signal_factory=NeverCancelled,
            lifecycle_factory=lambda _run: room.lifecycle,
        ),
    )
    port = container.supervisor_resume_port(lambda: runtime)
    return port, store, tools, room, run_id


async def test_the_port_holds_a_second_answer_until_the_approved_pass_ends():
    from approval.exceptions import HITLRunBusyError

    port, store, tools, room, run_id = await _two_held_calls()
    during_pass: list[object] = []
    original_execute = tools.execute

    async def execute_while_the_owner_answers(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-1":
            assert (await store.load(run_id)).status == "running"
            try:
                await port(run_id, call_id="call-2", answers="Approve")
            except HITLRunBusyError:
                during_pass.append("busy")
            # A replayed answer to the call already approved starts nothing.
            during_pass.append(await port(run_id, call_id="call-1", answers="Approve"))
        return await original_execute(invocation, acceptance, signal=signal)

    tools.execute = execute_while_the_owner_answers

    assert await port(run_id, call_id="call-1", answers="Approve") is True

    assert during_pass == ["busy", True]
    assert tools.execute_log == ["call-1"]
    waiting = await store.load(run_id)
    assert waiting.status == "awaiting_user"
    assert entry_of_call(waiting, "call-2").rule_hold.state == "asked"

    # The reconciler applies the recorded answer again once the pass ended.
    assert await port(run_id, call_id="call-2", answers="Approve") is True

    done = await store.load(run_id)
    assert done.status == "completed"
    assert tools.execute_log == ["call-1", "call-2"]
    await room.settle(done)
    assert (await room.snapshot_turn(done))["state"] == "completed"


async def test_a_replayed_answer_read_before_its_pass_started_is_applied():
    """The port read the Run suspended; the same answer then started a pass.

    Only one Question is open at a time, so a stale read can only race the
    pass its own answer started: the conflict finds the answer applied.
    """
    port, store, tools, _room, run_id = await _two_held_calls()
    stale = (await store.load(run_id)).model_copy(deep=True)
    real_load = store.load
    during_pass: list[object] = []

    async def stale_first_read(requested_run_id):
        if during_pass == ["reading"]:
            during_pass[:] = ["read stale"]
            # Suspended with call-1 asked, so the port must deliver.
            assert stale.status == "awaiting_user"
            assert entry_of_call(stale, "call-1").rule_hold.state == "asked"
            return stale
        return await real_load(requested_run_id)

    store.load = stale_first_read
    original_execute = tools.execute

    async def execute_while_the_answer_is_replayed(invocation, acceptance, *, signal):
        if invocation.invocation_id == "call-1" and not during_pass:
            during_pass[:] = ["reading"]
            during_pass.append(await port(run_id, call_id="call-1", answers="Approve"))
        return await original_execute(invocation, acceptance, signal=signal)

    tools.execute = execute_while_the_answer_is_replayed

    assert await port(run_id, call_id="call-1", answers="Approve") is True

    assert during_pass == ["read stale", True]
    assert tools.execute_log == ["call-1"]
    waiting = await store.load(run_id)
    assert waiting.status == "awaiting_user"
    assert entry_of_call(waiting, "call-2").rule_hold.state == "asked"


async def test_the_port_refuses_through_the_kernel_and_fails_closed():
    port, store, tools, _room, run_id = await _two_held_calls()

    assert await port(run_id, call_id="call-1", answers="approve") is True
    assert await port(run_id, call_id="call-2", answers="Reject") is True

    done = await store.load(run_id)
    assert done.status == "completed"
    assert tools.execute_log == []
    assert [entry_of_call(done, c).rule_hold.state for c in ("call-1", "call-2")] == [
        "refused",
        "refused",
    ]


async def test_the_port_reports_a_finished_run_as_not_resumable():
    port, store, _tools, _room, run_id = await _two_held_calls()
    await port(run_id, call_id="call-1", answers="Reject")
    await port(run_id, call_id="call-2", answers="Reject")

    assert await port(run_id, call_id="call-2", answers="Reject") is False


def entry_of_call(run, call_id):
    return next(
        entry
        for batch in run.tool_batches
        for entry in batch.entries
        if entry.call_id == call_id
    )
