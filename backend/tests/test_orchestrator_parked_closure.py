"""Ending a Run while a call is parked with no ledger or interaction identity.

A parked ``request_user_input`` entry is ``input_required`` with an
invocation, but it has neither a ledger record id nor an Agent interaction id:
its Question belongs to the Run, not to an Agent call. Stop, a canceled
question and a failure must still close it and settle the Run.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from execution.orchestrator.kernel import (
    REQUEST_USER_INPUT_TOOL_NAME,
    KernelConflict,
)
from execution.orchestrator.models import ToolBatchEntry, ToolCallBatch
from execution.orchestrator.run_state import _terminal_closure_plan
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    make_kernel,
    make_run,
    tool_events,
)
from tests.test_orchestrator_passthrough import PublicHistory


class RecordingSupervisorHITL:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def _parked_ask():
    history = PublicHistory()
    kernel, store, _, _ = await make_kernel(
        [
            tool_events(
                ("ask-1", REQUEST_USER_INPUT_TOOL_NAME, '{"question": "Which city?"}')
            )
        ],
        supervisor_hitl=RecordingSupervisorHITL(),
    )
    kernel.canonical_event_reader = history.read
    run_id = next(iter(store.runs))
    await history.lifecycle("run_started", store.runs[run_id], {"mode": "fast"})
    result = await kernel.run(
        run_id, signal=NeverCancelled(), lifecycle=history.lifecycle
    )
    assert result.outcome == "awaiting_user"
    (entry,) = result.run.tool_batches[0].entries
    assert entry.state == "input_required"
    assert entry.suspended_call_record_id is None
    assert entry.interaction_id is None
    return kernel, store, history, result.run


async def _request_stop(store, run) -> None:
    requested = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id="stop-1",
        cause="user_requested",
        requested_at=NOW + timedelta(seconds=1),
    )
    assert requested.outcome == "accepted"


def _assert_closed(run, history, *, status: str, turn_status: str) -> None:
    assert run.status == status
    (entry,) = run.tool_batches[0].entries
    assert entry.state == "terminal"
    assert entry.public_terminal_emitted
    assert run.tool_batches[0].results_flushed
    turn = history.fold(run)
    assert turn["state"] == status
    (tool,) = [item for item in turn["activity"] if item["kind"] == "tool"]
    assert tool["status"] in {"failed", "canceled"}
    assert [item["status"] for item in turn["internal_turns"]] == [turn_status]
    assert history.public_types().count("turn_end") == 1
    ended = [
        record["payload_public"]["payload"]
        for record in history.records.values()
        if record["payload_public"]["type"] == "turn_end"
    ]
    assert ended[0]["status"] == turn_status


@pytest.mark.asyncio
async def test_stop_closes_a_parked_ask():
    kernel, store, history, run = await _parked_ask()
    await _request_stop(store, run)

    result = await kernel.terminalize(
        run.run_id,
        status="canceled",
        reason="user_requested",
        cancellation_cause="user_requested",
        lifecycle=history.lifecycle,
    )

    assert result.outcome == "aborted"
    _assert_closed(result.run, history, status="canceled", turn_status="aborted")
    assert result.run.cancellation_cause == "user_requested"


@pytest.mark.asyncio
async def test_a_canceled_question_closes_a_parked_ask():
    # The owner cancels the run-level question: approval ends the owning Run as
    # canceled, which the cancellation path settles through the same call.
    kernel, store, history, run = await _parked_ask()
    await _request_stop(store, run)

    result = await kernel.terminalize(
        run.run_id,
        status="canceled",
        reason="Human input request was canceled",
        lifecycle=history.lifecycle,
    )

    assert result.outcome == "aborted"
    _assert_closed(result.run, history, status="canceled", turn_status="aborted")


@pytest.mark.asyncio
async def test_a_failure_closes_a_parked_ask():
    # An expired question, or an interrupted driver, fails the Run.
    kernel, _store, history, run = await _parked_ask()

    result = await kernel.terminalize(
        run.run_id,
        status="failed",
        reason="Human input request expired",
        lifecycle=history.lifecycle,
    )

    assert result.outcome == "failed"
    _assert_closed(result.run, history, status="failed", turn_status="error")


def _parked_run(**identity):
    run = make_run()
    return run.model_copy(
        update={
            "tool_batches": [
                ToolCallBatch(
                    assistant_message_id="assistant-1",
                    internal_turn_id="turn-1",
                    entries=[
                        ToolBatchEntry(
                            call_id="call-1",
                            assistant_message_id="assistant-1",
                            source_index=0,
                            tool_name="fake_agent_echo",
                            state="input_required",
                            opaque_public_call_id="inv_call-1",
                            **identity,
                        )
                    ],
                )
            ]
        }
    )


def test_a_parked_entry_with_no_identity_is_planned_for_closure():
    plan = _terminal_closure_plan(_parked_run(), [], canonical_reader_available=False)
    assert plan.interactions == ()
    assert [turn.internal_turn_id for turn in plan.turns] == ["turn-1"]


@pytest.mark.parametrize(
    "identity",
    [{"suspended_call_record_id": "parent-1"}, {"interaction_id": "interaction-1"}],
)
def test_a_parked_entry_with_half_an_identity_still_refuses(identity):
    with pytest.raises(KernelConflict, match="incomplete interaction identity"):
        _terminal_closure_plan(
            _parked_run(**identity), [], canonical_reader_available=False
        )
