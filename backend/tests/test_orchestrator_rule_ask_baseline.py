"""A Run with no rules, or with allow and deny rules only, is unchanged by R3.

The fixture holds the public event ids and the checkpoint command ids of
each scenario as recorded before the rule answer "ask" was added. Every
scenario here must still produce exactly those.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution.orchestrator.kernel import (
    REQUEST_USER_INPUT_TOOL_NAME,
    supervisor_answer_observation,
)
from execution.orchestrator.models import TextPart, ToolObservation, ToolResult
from execution.orchestrator.ports import AgentCallDenied
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    tool_events,
)
from tests.orchestrator_harness.fake_tools import RecordingFakeToolRuntime
from tests.test_orchestrator_passthrough import (
    AGENT,
    PublicHistory,
    answer,
    make_passthrough_kernel,
    waiting,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rule_ask_baseline.json"


class _DenyEcho(RecordingFakeToolRuntime):
    async def accept(self, invocation):
        if invocation.tool.definition.name == "fake_agent_echo":
            raise AgentCallDenied("rule_deny")
        return await super().accept(invocation)


class _AskUser:
    async def __call__(self, **_kwargs) -> None:
        return None


def _trace(history: PublicHistory, store) -> dict[str, list[str]]:
    return {
        "events": [
            record["payload_public"]["event_id"]
            for record in sorted(history.records.values(), key=lambda r: r["room_seq"])
        ],
        "commands": [command for _run_id, command in store.commands],
    }


async def _room_chat(scripts, *, tool_runtime=None, supervisor_hitl=None):
    history = PublicHistory()
    kernel, store, _, _ = await make_kernel(
        scripts, tool_runtime=tool_runtime, supervisor_hitl=supervisor_hitl
    )
    kernel.canonical_event_reader = history.read
    run_id = next(iter(store.runs))
    await history.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    return kernel, store, history, run_id


async def _allowed():
    kernel, store, history, run_id = await _room_chat(
        [tool_events(("call-1", "fake_agent_echo", '{"value":"hi"}')), final_events()]
    )
    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert result.outcome == "final_answer"
    return _trace(history, store)


async def _denied_and_allowed():
    kernel, store, history, run_id = await _room_chat(
        [
            tool_events(
                ("denied", "fake_agent_echo", '{"value":"hi"}'),
                ("allowed", "fake_agent_delay_parallel", '{"seconds":0}'),
            ),
            final_events(),
        ],
        tool_runtime=_DenyEcho(),
    )
    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert result.outcome == "final_answer"
    return _trace(history, store)


async def _ask_user():
    kernel, store, history, run_id = await _room_chat(
        [
            tool_events(
                ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')
            ),
            final_events("Shanghai."),
        ],
        supervisor_hitl=_AskUser(),
    )
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert parked.outcome == "awaiting_user"
    resumed = await kernel.observe_tool(
        run_id,
        supervisor_answer_observation(run_id, "ask-1", "Shanghai", NOW),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )
    assert resumed.outcome == "final_answer"
    return _trace(history, store)


async def _passthrough_completed():
    kernel, store, _m, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [answer("Works were voted in 2023.")]
    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert result.outcome == "final_answer"
    return _trace(history, store)


async def _passthrough_waiting_then_result():
    kernel, store, _m, runtime, history, run_id = await make_passthrough_kernel()
    runtime.answers[AGENT] = [waiting()]
    parked = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert parked.outcome == "waiting_external"
    entry = parked.run.tool_batches[0].entries[0]
    resumed = await kernel.observe_tool(
        run_id,
        ToolObservation(
            observation_id="obs-1",
            invocation_id=entry.invocation.invocation_id,
            outcome=ToolResult(
                call_id=entry.call_id,
                tool_name=entry.tool_name,
                status="completed",
                content=[TextPart(text="Later.")],
                artifact_refs=[],
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=history.lifecycle,
    )
    assert resumed.outcome == "final_answer"
    return _trace(history, store)


async def _passthrough_denied():
    from tests.test_orchestrator_passthrough import AgentRuntime

    class Denying(AgentRuntime):
        async def accept(self, invocation):
            raise AgentCallDenied("rule_deny")

    kernel, store, _m, _runtime, history, run_id = await make_passthrough_kernel(
        runtime=Denying()
    )
    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert result.run.status == "canceled"
    return _trace(history, store)


SCENARIOS = {
    "allowed": _allowed,
    "denied_and_allowed": _denied_and_allowed,
    "ask_user": _ask_user,
    "passthrough_completed": _passthrough_completed,
    "passthrough_waiting_then_result": _passthrough_waiting_then_result,
    "passthrough_denied": _passthrough_denied,
}


async def record_baseline() -> dict[str, dict[str, list[str]]]:
    return {name: await scenario() for name, scenario in SCENARIOS.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_events_and_checkpoints_match_the_recorded_baseline(name):
    expected = json.loads(FIXTURE.read_text())[name]

    assert await SCENARIOS[name]() == expected
