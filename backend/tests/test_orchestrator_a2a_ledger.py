from __future__ import annotations

from datetime import timedelta

import pytest

from execution.orchestrator.a2a_runtime.ledger import (
    AGENT_CALL_STATES,
    AGENT_CALL_TRANSITIONS,
    ConflictingTerminalObservation,
    IllegalAgentCallTransition,
    apply_observation,
    ownership_alias_keys,
    transition_call,
    validate_agent_call_transition,
)
from execution.orchestrator.a2a_runtime.models import (
    A2AOwnershipAlias,
    NormalizedA2AObservation,
)
from tests.orchestrator_harness.a2a_in_memory import InMemoryAgentCallLedgerStore

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import NOW


def test_transition_table_is_closed_and_terminal_states_are_monotonic():
    assert len(AGENT_CALL_STATES) == 15
    assert set(AGENT_CALL_TRANSITIONS) == AGENT_CALL_STATES
    for state in {"completed", "failed", "canceled", "rejected", "expired"}:
        assert AGENT_CALL_TRANSITIONS[state] == frozenset()
        with pytest.raises(IllegalAgentCallTransition):
            validate_agent_call_transition(state, "working")


async def test_same_provider_call_id_in_different_runs_is_not_a_collision():
    store = InMemoryAgentCallLedgerStore()
    first = ledger_record(run_id="run-1")
    second = ledger_record(run_id="run-2")
    assert await store.insert(first) == "accepted"
    assert await store.insert(second) == "accepted"
    assert first.call_record_id != second.call_record_id


async def test_exact_replay_and_mismatched_digest_conflict():
    store = InMemoryAgentCallLedgerStore()
    record = ledger_record()
    assert await store.insert(record) == "accepted"
    assert await store.insert(record) == "replayed"
    changed = record.model_copy(update={"binding_digest": "changed"})
    assert await store.insert(changed) == "conflict"


async def test_claim_renew_release_is_owner_and_version_fenced():
    store = InMemoryAgentCallLedgerStore()
    record = ledger_record()
    assert await store.insert(record) == "accepted"
    claimed = await store.claim(
        record.call_record_id,
        expected_state_version=0,
        owner_id="worker-1",
        lease_expires_at=NOW + timedelta(minutes=1),
        claimed_at=NOW,
    )
    assert claimed is not None and claimed.state_version == 1
    assert (
        await store.renew(
            record.call_record_id,
            expected_state_version=1,
            owner_id="worker-2",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(seconds=1),
        )
        is None
    )
    renewed = await store.renew(
        record.call_record_id,
        expected_state_version=1,
        owner_id="worker-1",
        lease_expires_at=NOW + timedelta(minutes=2),
        renewed_at=NOW + timedelta(seconds=1),
    )
    assert renewed is not None and renewed.state_version == 2


async def test_alias_lookup_is_scoped_and_rejects_ambiguous_matches():
    store = InMemoryAgentCallLedgerStore()
    first_aliases = [
        A2AOwnershipAlias(kind="task", value="task-1", binding_scope="scope-1")
    ]
    second_aliases = [
        A2AOwnershipAlias(kind="task", value="task-1", binding_scope="scope-2")
    ]
    first = ledger_record(run_id="run-1").model_copy(
        update={
            "endpoint_scope_digest": "scope-1",
            "ownership_aliases": first_aliases,
            "ownership_alias_keys": ownership_alias_keys(first_aliases),
        }
    )
    second = ledger_record(run_id="run-2").model_copy(
        update={
            "endpoint_scope_digest": "scope-2",
            "ownership_aliases": second_aliases,
            "ownership_alias_keys": ownership_alias_keys(second_aliases),
        }
    )
    await store.insert(first)
    await store.insert(second)
    assert (
        await store.find_by_alias("scope-1", task_id="task-1", context_id=None)
    ) == first
    assert await store.find_by_alias("wrong", task_id="task-1", context_id=None) is None


@pytest.mark.parametrize("state", ["accepted", "ready_to_dispatch"])
@pytest.mark.parametrize("event_kind", ["working", "artifact"])
def test_pre_dispatch_progress_cannot_bind_authoritative_aliases(state, event_kind):
    record = ledger_record()
    if state == "ready_to_dispatch":
        record = transition_call(record, to_state=state, updated_at=NOW)
    observation = NormalizedA2AObservation(
        observation_id=f"pre-dispatch-{state}-{event_kind}",
        call_record_id=record.call_record_id,
        source_kind="webhook",
        source_identity=f"webhook:{state}:{event_kind}",
        binding_scope=record.endpoint_scope_digest,
        event_kind=event_kind,
        observed_at=NOW,
        task_id="task-stolen",
        context_id="context-stolen",
        artifact_refs=["artifact-stolen"] if event_kind == "artifact" else [],
    )

    assert apply_observation(record, observation, recent_limit=10) == record


def test_terminal_observation_wins_once_and_recent_inventory_is_bounded():
    record = transition_call(
        ledger_record(), to_state="ready_to_dispatch", updated_at=NOW
    )
    record = transition_call(record, to_state="dispatching", updated_at=NOW)
    observation = NormalizedA2AObservation(
        observation_id="observation-1",
        source_kind="direct",
        source_identity="source-1",
        binding_scope="endpoint",
        event_kind="terminal",
        status="completed",
        observed_at=NOW,
        task_id="task-1",
        context_id="context-1",
    )
    completed = apply_observation(record, observation, recent_limit=2)
    assert completed.state == "completed"
    assert completed.terminal_result is not None
    late = observation.model_copy(
        update={"observation_id": "observation-2", "status": "failed"}
    )
    with pytest.raises(ConflictingTerminalObservation):
        apply_observation(completed, late, recent_limit=2)
