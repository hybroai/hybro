"""The owner's check rules at Agent call acceptance (Room chat profiles).

A deny rule refuses a new Agent call in ``accept``. In Room chat the refused
call returns to the model as a tool result and the Run goes on; each refused
call uses one unit of the Agent-call budget, which bounds repeats.
"""

from __future__ import annotations

import pytest

from execution.orchestrator.a2a_runtime.runtime import A2AAcceptanceConflict
from execution.orchestrator.models import ToolResultMessage
from execution.orchestrator.ports import AgentCallDenied
from execution.orchestrator.public_projection import canonical_settlement_payload
from policy import PolicyService
from policy.models import RuleCreate
from tests._orchestrator_a2a_helpers import invocation, prepared
from tests._orchestrator_helpers import (
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests.fakes.policy import MemoryRuleStore
from tests.orchestrator_harness.fake_tools import RecordingFakeToolRuntime
from tests.test_orchestrator_a2a_runtime import setup

DENIED = "fake_agent_echo"


class DenyingToolRuntime(RecordingFakeToolRuntime):
    """Refuses every new call to the tools a rule denies."""

    def __init__(self, deny: dict[str, str]) -> None:
        super().__init__()
        self.deny = deny
        self.denied_log: list[str] = []

    async def accept(self, invocation):
        rule_id = self.deny.get(invocation.tool.definition.name)
        if rule_id is not None:
            self.denied_log.append(invocation.invocation_id)
            raise AgentCallDenied(rule_id)
        return await super().accept(invocation)


def _tool_results(run) -> list[ToolResultMessage]:
    return [item for item in run.transcript if isinstance(item, ToolResultMessage)]


async def test_a_denied_call_returns_to_the_model_and_the_run_survives():
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(("denied", DENIED, '{"value":"hi"}')),
            final_events("I could not ask that agent."),
        ],
        tool_runtime=tools,
    )
    turns: list[str] = []

    async def lifecycle(event_type, _run, payload):
        if event_type == "turn_completed":
            turns.append(payload["status"])

    result = await kernel.run(
        next(iter(store.runs)), signal=NeverCancelled(), lifecycle=lifecycle
    )

    assert result.outcome == "final_answer"
    assert result.run.status == "completed"
    assert tools.denied_log == ["denied"]
    assert tools.execute_log == []
    (refused,) = _tool_results(result.run)
    assert refused.is_error
    assert refused.error_code == "agent_call_denied"
    # Fixed text: no rule text and no exception text reaches the model.
    assert refused.error_message == (
        "A check rule set by the owner denies this agent call."
    )
    assert len(runtime.requests) == 2
    assert runtime.requests[1].messages[-1].role == "tool"
    assert turns == ["error", "completed"]
    assert result.run.denied_rule_id == "rule_deny_echo"
    (entry,) = [e for batch in result.run.tool_batches for e in batch.entries]
    assert entry.denied_by_rule


async def test_a_denied_call_does_not_stop_its_allowed_sibling():
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(
                ("denied", DENIED, '{"value":"hi"}'),
                ("allowed", "fake_agent_delay_parallel", '{"seconds":0}'),
            ),
            final_events(),
        ],
        tool_runtime=tools,
    )

    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert tools.accept_log == ["allowed"]
    assert tools.execute_log == ["allowed"]
    assert [m.error_code for m in _tool_results(result.run)][0] == ("agent_call_denied")


async def test_each_denied_call_uses_the_agent_call_budget():
    run = make_run(max_agent_calls=2)
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(("try-1", DENIED, '{"value":"1"}')),
            tool_events(("try-2", DENIED, '{"value":"2"}')),
            tool_events(("try-3", DENIED, '{"value":"3"}')),
            final_events("giving up"),
        ],
        run=run,
        tool_runtime=tools,
    )

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    # The third call never reaches the check: the budget refuses it first.
    assert tools.denied_log == ["try-1", "try-2"]
    assert result.run.budget.agent_calls_used == 2
    assert [m.error_code for m in _tool_results(result.run)] == [
        "agent_call_denied",
        "agent_call_denied",
        "tool_call_budget_exhausted",
    ]


async def test_an_ordinary_acceptance_failure_still_fails_the_run():
    tools = RecordingFakeToolRuntime(fail_accept_for={DENIED})
    kernel, store, _, _ = await make_kernel(
        [tool_events(("boom", DENIED, '{"value":"hi"}')), final_events()],
        tool_runtime=tools,
    )

    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())

    assert result.outcome == "failed"
    assert result.run.denied_rule_id is None


# -- The check inside A2AAgentToolRuntime.accept ------------------------------


async def _rules(*creates: RuleCreate) -> PolicyService:
    service = PolicyService(MemoryRuleStore())
    for create in creates:
        await service.create(create)
    return service


async def test_accept_refuses_a_denied_agent_before_any_ledger_record():
    agent_id = prepared().binding.agent_id
    rules = await _rules(RuleCreate(agent_id=agent_id, answer="deny"))
    (rule,) = (await rules.list_rules()).rules
    runtime, ledger, authorization, dispatch, _ = await setup()
    runtime.call_check = rules

    with pytest.raises(AgentCallDenied) as denied:
        await runtime.accept(invocation())

    assert denied.value.rule_id == rule.rule_id
    assert await ledger.load("run-1", "call-1") is None
    assert authorization.calls == 0
    assert dispatch.commands == []


async def test_accept_lets_an_allowed_or_unmatched_agent_go():
    rules = await _rules(
        RuleCreate(agent_id="someone-else", answer="deny"),
        RuleCreate(answer="allow"),
    )
    runtime, ledger, _, _, _ = await setup()
    runtime.call_check = rules

    accepted = await runtime.accept(invocation())

    assert accepted.invocation_id == "call-1"
    assert await ledger.load("run-1", "call-1") is not None


async def test_a_replayed_acceptance_is_not_checked_again():
    runtime, _, _, _, _ = await setup()
    accepted = await runtime.accept(invocation())
    # A rule added after the call was accepted does not touch the call.
    runtime.call_check = await _rules(RuleCreate(answer="deny"))

    assert await runtime.accept(invocation()) == accepted


async def test_an_instance_with_no_rules_accepts_exactly_as_before():
    plain, plain_ledger, _, _, _ = await setup()
    checked, checked_ledger, _, _, _ = await setup()
    checked.call_check = await _rules()
    times = {"accepted_at", "updated_at"}

    with_check = await checked.accept(invocation())
    without = await plain.accept(invocation())

    assert with_check.model_dump(exclude=times) == without.model_dump(exclude=times)
    stored = await checked_ledger.load("run-1", "call-1")
    expected = await plain_ledger.load("run-1", "call-1")
    assert stored.model_dump(exclude=times) == expected.model_dump(exclude=times)


async def test_the_check_runs_after_the_binding_correlates():
    # A conflicting replay is refused for its own reason, not by a rule.
    runtime, _, _, _, _ = await setup()
    await runtime.accept(invocation())
    runtime.call_check = await _rules(RuleCreate(answer="deny"))
    changed = invocation().model_copy(update={"arguments": {"task": "other"}})

    with pytest.raises(A2AAcceptanceConflict):
        await runtime.accept(changed)


# -- Settlement payload --------------------------------------------------------


def test_only_a_policy_cancel_carries_the_rule_id():
    base = make_run()
    denied = base.model_copy(
        update={
            "status": "canceled",
            "cancellation_cause": "policy",
            "denied_rule_id": "rule_1",
        }
    )
    other = base.model_copy(
        update={"status": "canceled", "cancellation_cause": "user_requested"}
    )

    assert canonical_settlement_payload(denied)["rule_id"] == "rule_1"
    assert "rule_id" not in canonical_settlement_payload(other)


def _settled(**extra):
    from common.dto import RunSettledPayload

    return RunSettledPayload.model_validate(
        {
            "status": "canceled",
            "started_at": "2026-11-21T00:00:00Z",
            "settled_at": "2026-11-21T00:00:01Z",
            "duration_ms": 1000,
            **extra,
        }
    )


def test_the_settlement_contract_takes_a_rule_id_on_a_policy_cancel_only():
    from pydantic import ValidationError

    assert _settled(cancellation_code="policy", rule_id="rule_1").rule_id == "rule_1"
    assert _settled(cancellation_code="policy").rule_id is None
    with pytest.raises(ValidationError):
        _settled(cancellation_code="user_requested", rule_id="rule_1")


class ProcessDied(BaseException):
    """The process dies between two checkpoints."""


async def test_a_restart_after_the_deny_neither_rechecks_nor_recounts(monkeypatch):
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(("denied", DENIED, '{"value":"hi"}')),
            final_events("done without it"),
        ],
        tool_runtime=tools,
    )
    run_id = next(iter(store.runs))
    real_checkpoint = kernel._checkpoint
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        if not crashed and kwargs["command_id"].startswith("complete-tool-batch:"):
            crashed = True
            raise ProcessDied
        return await real_checkpoint(run, *args, **kwargs)

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled())

    result = await kernel.run(run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert tools.denied_log == ["denied"]
    assert result.run.budget.agent_calls_used == 1
    assert [m.error_code for m in _tool_results(result.run)] == ["agent_call_denied"]


async def test_a_restart_with_public_history_resumes_room_chat_after_a_deny(
    monkeypatch,
):
    from tests.test_orchestrator_passthrough import PublicHistory

    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(("denied", DENIED, '{"value":"hi"}')),
            final_events("done without it"),
        ],
        tool_runtime=tools,
    )
    run_id = next(iter(store.runs))
    history = PublicHistory()
    kernel.canonical_event_reader = history.read
    await history.lifecycle("run_started", await store.load(run_id), {"mode": "fast"})
    real_checkpoint = kernel._checkpoint
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        if not crashed and kwargs["command_id"].startswith("complete-tool-batch:"):
            crashed = True
            raise ProcessDied
        return await real_checkpoint(run, *args, **kwargs)

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    with pytest.raises(ProcessDied):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=history.lifecycle)

    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )

    assert result.outcome == "final_answer"
    assert tools.denied_log == ["denied"]
    assert len(runtime.requests) == 2
    assert runtime.requests[1].messages[-1].role == "tool"
    # Recovery closes the refused turn as it closes any locally rejected
    # declaration after a restart; only passthrough needs it closed as an error.
    assert result.run.budget.agent_calls_used == 1


async def test_a_crash_while_recording_the_deny_still_returns_the_refusal(
    monkeypatch,
):
    run = make_run(max_agent_calls=1)
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})
    kernel, store, runtime, _ = await make_kernel(
        [
            tool_events(("denied", DENIED, '{"value":"hi"}')),
            final_events("done without it"),
        ],
        run=run,
        tool_runtime=tools,
    )
    real_checkpoint = kernel._checkpoint
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        stored = await real_checkpoint(run, *args, **kwargs)
        if not crashed and "denied_rule_id" in kwargs["updates"]:
            # The process dies just after the rule is first stored.
            crashed = True
            raise ProcessDied
        return stored

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    with pytest.raises(ProcessDied):
        await kernel.run(run.run_id, signal=NeverCancelled())
    monkeypatch.setattr(kernel, "_checkpoint", real_checkpoint)

    result = await kernel.run(run.run_id, signal=NeverCancelled())

    assert result.outcome == "final_answer"
    assert result.run.budget.agent_calls_used == 1
    assert [m.error_code for m in _tool_results(result.run)] == ["agent_call_denied"]
    assert result.run.denied_rule_id == "rule_deny_echo"


async def _room_chat_crash_after_the_reserve(tools, monkeypatch, first_call):
    from tests.test_orchestrator_passthrough import PublicHistory

    run = make_run(max_agent_calls=1)
    kernel, store, _, _ = await make_kernel(
        [tool_events(first_call), final_events("done")],
        run=run,
        tool_runtime=tools,
    )
    history = PublicHistory()
    kernel.canonical_event_reader = history.read
    await history.lifecycle("run_started", run, {"mode": "fast"})
    real_checkpoint = kernel._checkpoint
    crashed = False

    async def checkpoint(run, *args, **kwargs):
        nonlocal crashed
        stored = await real_checkpoint(run, *args, **kwargs)
        if not crashed and kwargs["command_id"].startswith("tool-budget-reserve:"):
            crashed = True
            raise ProcessDied
        return stored

    monkeypatch.setattr(kernel, "_checkpoint", checkpoint)
    with pytest.raises(ProcessDied):
        await kernel.run(
            run.run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
        )
    monkeypatch.setattr(kernel, "_checkpoint", real_checkpoint)
    return await kernel.run(
        run.run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )


async def test_a_crash_after_the_reserve_still_returns_the_refusal(monkeypatch):
    tools = DenyingToolRuntime({DENIED: "rule_deny_echo"})

    result = await _room_chat_crash_after_the_reserve(
        tools, monkeypatch, ("denied", DENIED, '{"value":"hi"}')
    )

    assert result.outcome == "final_answer"
    assert result.run.budget.agent_calls_used == 1
    assert [m.error_code for m in _tool_results(result.run)] == ["agent_call_denied"]
    assert result.run.denied_rule_id == "rule_deny_echo"


async def test_a_crash_after_the_reserve_still_runs_an_allowed_call(monkeypatch):
    tools = RecordingFakeToolRuntime()

    result = await _room_chat_crash_after_the_reserve(
        tools,
        monkeypatch,
        ("allowed", "fake_agent_delay_parallel", '{"seconds":0}'),
    )

    assert result.outcome == "final_answer"
    assert result.run.budget.agent_calls_used == 1
    assert tools.execute_log == ["allowed"]
    assert [m.error_code for m in _tool_results(result.run)] == [None]
