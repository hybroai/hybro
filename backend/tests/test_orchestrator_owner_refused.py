"""The cancellation cause ``owner_refused``: the owner refused a held call.

A passthrough Run whose one call the owner refused at a check rule's question
ends canceled with this cause. ``/sessions`` reads it as ``rejected``.
"""

from __future__ import annotations

from common.dto import RunSettledPayload
from execution.orchestrator.public_projection import canonical_settlement_payload
from execution.orchestrator.settlement import (
    TerminalStatusCommitRequest,
    commit_terminal_status,
)
from session.projection import resolve_state
from session.waiter import CorrelatedOutcome
from tests._orchestrator_helpers import NOW, make_run


def _refused_run():
    run = make_run()
    request = TerminalStatusCommitRequest(
        expected_state_version=run.state_version,
        command_id="terminate:canceled",
        event_id="event-1",
        event_sequence=1,
        event_intent_id="intent-event-1",
        public_run_intent_id="intent-run-1",
        public_run_target=run.run_id,
        status="canceled",
        terminal_reason="agent_call_refused",
        cancellation_cause="owner_refused",
        created_at=NOW,
    )
    committed = commit_terminal_status(run, request=request)
    assert committed.outcome == "accepted"
    return committed.run


def test_a_refused_run_settles_canceled_with_owner_refused():
    run = _refused_run()

    assert run.status == "canceled"
    assert run.cancellation_cause == "owner_refused"
    payload = canonical_settlement_payload(run)
    assert payload["cancellation_code"] == "owner_refused"
    assert "rule_id" not in payload
    settled = RunSettledPayload.model_validate(payload)
    assert settled.cancellation_code == "owner_refused"


def test_sessions_read_owner_refused_as_rejected():
    outcome = CorrelatedOutcome(seen=True)
    outcome.settlement = {"status": "canceled", "cancellation_code": "owner_refused"}

    assert resolve_state(outcome, closed=False) == ("rejected", None)


def test_other_cancels_still_read_as_before():
    policy = CorrelatedOutcome(seen=True)
    policy.settlement = {"status": "canceled", "cancellation_code": "policy"}
    stop = CorrelatedOutcome(seen=True)
    stop.settlement = {"status": "canceled", "cancellation_code": "user_requested"}

    assert resolve_state(policy, closed=False)[0] == "denied"
    assert resolve_state(stop, closed=False) == ("failed", "canceled:user_requested")
