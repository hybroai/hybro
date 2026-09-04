from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from dal.orchestrator.run_store import MongoOrchestratorRunStore
from execution.orchestrator import (
    AssistantMessage,
    RecoveryClaim,
    TerminalCommitRequest,
    TerminalDecisionFacts,
    TerminalStatusCommitRequest,
    TextPart,
    commit_terminal_decision,
    commit_terminal_status,
)
from execution.orchestrator.contract_harness import InMemoryOrchestratorContractHarness
from execution.orchestrator.in_memory import InMemoryOrchestratorRunStore
from execution.orchestrator.ports import OrchestratorRunStore

from ._orchestrator_helpers import NOW, make_run


def _run(run_id: str, room_id: str):
    run = make_run()
    return run.model_copy(
        update={
            "run_id": run_id,
            "session_id": room_id,
            "room_id": room_id,
            "client_request_id": f"request-{run_id}",
            "request": run.request.model_copy(
                update={
                    "request_fingerprint": f"fingerprint-{run_id}",
                    "room_epoch": 1,
                    "user_message_id": f"user-{run_id}",
                }
            ),
        }
    )


def test_recovery_store_signatures_have_exact_claim_and_release_inventory():
    implementations = (
        OrchestratorRunStore,
        InMemoryOrchestratorRunStore,
        MongoOrchestratorRunStore,
        InMemoryOrchestratorContractHarness,
    )
    expected_claim = {
        "self",
        "run_id",
        "expected_state_version",
        "owner_id",
        "lease_expires_at",
        "claimed_at",
    }
    expected_release = {
        "self",
        "run_id",
        "expected_state_version",
        "owner_id",
        "next_attempt_at",
        "failure_count",
        "quarantined_at",
        "quarantine_reason",
    }
    assert set(RecoveryClaim.model_fields) == {
        "kind",
        "owner_id",
        "lease_expires_at",
        "next_attempt_at",
        "failure_count",
        "quarantined_at",
        "quarantine_reason",
    }
    for implementation in implementations:
        assert set(inspect.signature(implementation.claim_recovery).parameters) == (
            expected_claim
        )
        assert set(inspect.signature(implementation.release_recovery).parameters) == (
            expected_release
        )


def test_recovery_claim_is_owner_version_and_epoch_fenced():
    store = InMemoryOrchestratorContractHarness()
    run = _run("run-1", "room-1").model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    assert store.create(run) == "accepted"
    assert (
        store.claim_recovery(
            "run-1",
            expected_state_version=0,
            owner_id="worker-1",
            lease_expires_at=NOW + timedelta(minutes=1),
            claimed_at=NOW,
        )
        == "accepted"
    )
    assert (
        store.renew_recovery(
            "run-1",
            expected_state_version=1,
            owner_id="worker-2",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(seconds=1),
        )
        == "conflict"
    )
    assert (
        store.renew_recovery(
            "run-1",
            expected_state_version=1,
            owner_id="worker-1",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(seconds=1),
        )
        == "accepted"
    )


def test_recovery_claim_renew_release_and_due_schedule_are_fully_fenced():
    store = InMemoryOrchestratorContractHarness()
    run = _run("run-recovery", "room-recovery").model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    assert store.create(run) == "accepted"
    assert (
        store.claim_recovery(
            run.run_id,
            expected_state_version=0,
            owner_id="worker",
            lease_expires_at=NOW,
            claimed_at=NOW,
        )
        == "conflict"
    )
    assert (
        store.claim_recovery(
            run.run_id,
            expected_state_version=0,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(minutes=1),
            claimed_at=NOW,
        )
        == "accepted"
    )
    assert (
        store.renew_recovery(
            run.run_id,
            expected_state_version=0,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(seconds=1),
        )
        == "conflict"
    )
    assert (
        store.renew_recovery(
            run.run_id,
            expected_state_version=1,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(seconds=30),
            renewed_at=NOW + timedelta(seconds=1),
        )
        == "conflict"
    )
    assert (
        store.renew_recovery(
            run.run_id,
            expected_state_version=1,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(minutes=1),
        )
        == "conflict"
    )
    assert (
        store.renew_recovery(
            run.run_id,
            expected_state_version=1,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(minutes=2),
            renewed_at=NOW + timedelta(seconds=1),
        )
        == "accepted"
    )
    assert (
        store.release_recovery(
            run.run_id,
            expected_state_version=2,
            owner_id="other",
            next_attempt_at=NOW + timedelta(minutes=5),
        )
        == "conflict"
    )
    assert (
        store.release_recovery(
            run.run_id,
            expected_state_version=2,
            owner_id="worker",
            next_attempt_at=NOW + timedelta(minutes=5),
        )
        == "accepted"
    )
    assert store.list_due_runs(due_at=NOW + timedelta(minutes=4), limit=10) == []
    assert [
        item.run_id
        for item in store.list_due_runs(due_at=NOW + timedelta(minutes=5), limit=10)
    ] == [run.run_id]


def test_contract_harness_release_persists_invariant_quarantine_fields():
    store = InMemoryOrchestratorContractHarness()
    run = _run("run-quarantine", "room-quarantine").model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    assert store.create(run) == "accepted"
    assert (
        store.claim_recovery(
            run.run_id,
            expected_state_version=0,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(minutes=1),
            claimed_at=NOW,
        )
        == "accepted"
    )
    assert (
        store.release_recovery(
            run.run_id,
            expected_state_version=1,
            owner_id="worker",
            next_attempt_at=None,
            failure_count=3,
            quarantined_at=NOW + timedelta(seconds=1),
            quarantine_reason="terminal_invariant_conflict",
        )
        == "accepted"
    )
    claim = store.runs[run.run_id].recovery_claim
    assert claim.failure_count == 3
    assert claim.quarantined_at == NOW + timedelta(seconds=1)
    assert claim.quarantine_reason == "terminal_invariant_conflict"
    assert store.list_due_runs(due_at=NOW + timedelta(days=1), limit=10) == []


def test_deletion_waits_for_live_projection_claim_and_fences_stale_owner():
    store = InMemoryOrchestratorContractHarness()
    run = _run("run-claim", "room-claim")
    assert store.create(run) == "accepted"
    assert (
        store.claim_projection(
            run.run_id,
            "projection-1",
            owner_id="projector",
            room_epoch=1,
        )
        == "accepted"
    )
    assert store.delete_room(run.room_id, owner_id="deleter") == "conflict"
    assert run.run_id in store.runs
    store.release_projection(run.run_id, "projection-1", owner_id="projector")
    assert store.delete_room(run.room_id, owner_id="deleter") == "accepted"
    assert run.run_id not in store.runs
    assert (
        store.confirm_projection(
            run.run_id,
            "projection-1",
            owner_id="projector",
            room_epoch=1,
        )
        == "gone"
    )


def test_only_one_nonterminal_run_per_room():
    store = InMemoryOrchestratorContractHarness()
    first = _run("run-1", "room-1")
    second = _run("run-2", "room-1")
    assert store.create(first) == "accepted"
    assert store.create(second) == "conflict"
    store.runs[first.run_id] = first.model_copy(update={"status": "completed"})
    assert store.create(second) == "accepted"


def test_room_epoch_fences_old_recovery_after_delete():
    store = InMemoryOrchestratorContractHarness()
    assert store.create(_run("run-1", "room-1")) == "accepted"
    assert store.delete_room("room-1", owner_id="deleter") == "accepted"
    assert store.room_epochs["room-1"] == 2
    recreated = _run("run-2", "room-1").model_copy(
        update={
            "request": _run("run-2", "room-1").request.model_copy(
                update={"room_epoch": 2}
            )
        }
    )
    assert store.create(recreated) == "accepted"


def test_due_run_inventory_excludes_live_leases_and_terminal_runs():
    store = InMemoryOrchestratorContractHarness()
    due = _run("due", "room-due").model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    live = _run("live", "room-live").model_copy(
        update={
            "recovery_claim": RecoveryClaim(
                owner_id="worker", lease_expires_at=NOW + timedelta(minutes=1)
            )
        }
    )
    dormant = _run("dormant", "room-dormant").model_copy(
        update={
            "status": "awaiting_user",
            "recovery_claim": RecoveryClaim(next_attempt_at=NOW + timedelta(minutes=1)),
        }
    )
    terminal = _run("terminal", "room-terminal").model_copy(
        update={"status": "completed"}
    )
    for run in (due, live, dormant, terminal):
        assert store.create(run) == "accepted"
    assert [run.run_id for run in store.list_due_runs(due_at=NOW, limit=10)] == ["due"]


@pytest.mark.asyncio
async def test_cancellation_request_is_durable_and_fences_stale_progress():
    store = InMemoryOrchestratorRunStore()
    run = _run("run-cancel", "room-cancel")
    assert (await store.create(run, command_id="create")).outcome == "accepted"

    command_id = f"cancel:{run.run_id}:user_requested"
    requested = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id=command_id,
        cause="user_requested",
        requested_at=NOW,
    )

    assert requested.outcome == "accepted"
    assert requested.run is not None
    assert requested.run.status == "canceling"
    assert requested.run.cancellation_command_id == command_id
    assert requested.run.cancellation_requested_at == NOW
    assert requested.run.cancellation_cause == "user_requested"
    assert requested.run.recovery_claim.kind == "cancellation"
    assert requested.run.state_version == run.state_version + 1

    stale = run.model_copy(
        update={"status": "waiting_external", "state_version": run.state_version + 1}
    )
    conflict = await store.cas_mutate(
        stale,
        expected_state_version=run.state_version,
        command_id="stale-progress",
    )
    assert conflict.outcome == "conflict"
    assert conflict.run is not None and conflict.run.status == "canceling"

    replay = await store.request_cancellation(
        run.run_id,
        expected_state_version=requested.run.state_version,
        command_id=command_id,
        cause="user_requested",
        requested_at=NOW,
    )
    assert replay.outcome == "replayed"


def test_canceling_run_rejects_incomplete_metadata():
    run = _run("run-malformed", "room-malformed")

    with pytest.raises(ValueError, match="complete cancellation metadata"):
        type(run).model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "canceling",
                "recovery_claim": RecoveryClaim(kind="cancellation"),
            }
        )


@pytest.mark.asyncio
async def test_repair_canceling_recovery_restores_cancellation_claim():
    store = InMemoryOrchestratorRunStore()
    run = _run("run-repair", "room-repair").model_copy(
        update={
            "status": "canceling",
            "cancellation_command_id": "cancel:run-repair:user_requested",
            "cancellation_requested_at": NOW,
            "cancellation_cause": "user_requested",
            "recovery_claim": RecoveryClaim(kind="execution"),
        }
    )
    store.runs[run.run_id] = run

    assert await store.repair_canceling_recovery(limit=10) == 1
    repaired = await store.load(run.run_id)
    assert repaired is not None
    assert repaired.recovery_claim.kind == "cancellation"
    assert repaired.recovery_claim.next_attempt_at is not None


@pytest.mark.asyncio
async def test_canceling_store_guard_allows_only_matching_canceled_exit():
    store = InMemoryOrchestratorRunStore()
    run = _run("run-exit", "room-exit")
    await store.create(run, command_id="create")
    command_id = f"cancel:{run.run_id}:user_requested"
    requested = await store.request_cancellation(
        run.run_id,
        expected_state_version=run.state_version,
        command_id=command_id,
        cause="user_requested",
        requested_at=NOW,
    )
    assert requested.run is not None
    canceling = requested.run

    rejected = await store.cas_mutate(
        canceling.model_copy(
            update={
                "status": "running",
                "state_version": canceling.state_version + 1,
            }
        ),
        expected_state_version=canceling.state_version,
        command_id="resume",
    )
    assert rejected.outcome == "conflict"

    committed = commit_terminal_status(
        canceling,
        request=TerminalStatusCommitRequest(
            expected_state_version=canceling.state_version,
            command_id="settle-cancel",
            event_id="cancel-event",
            event_sequence=1,
            event_intent_id="cancel-event-intent",
            public_run_intent_id="cancel-run-intent",
            public_run_target=canceling.run_id,
            status="canceled",
            terminal_reason="cancellation requested",
            cancellation_cause="user_requested",
            created_at=NOW,
        ),
    )
    assert committed.outcome == "accepted"
    stored = await store.cas_mutate(
        committed.run,
        expected_state_version=canceling.state_version,
        command_id="settle-cancel",
    )
    assert stored.outcome == "accepted"
    assert stored.run is not None and stored.run.status == "canceled"
    assert stored.run.cancellation_command_id == command_id


def test_crash_after_terminal_cas_repairs_outbox_idempotently():
    store = InMemoryOrchestratorContractHarness()
    original = _run("run-1", "room-1")
    final = AssistantMessage(
        message_id="final-1",
        content=[TextPart(text="done")],
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        created_at=NOW,
    )
    original = original.model_copy(
        update={
            "status": "finalizing",
            "transcript": [*original.transcript, final],
            "proposed_final_message_id": final.message_id,
        }
    )
    assert store.create(original) == "accepted"
    committed = commit_terminal_decision(
        original,
        facts=TerminalDecisionFacts(final_message_id="final-1"),
        request=TerminalCommitRequest(
            expected_state_version=0,
            command_id="complete",
            event_id="event-terminal",
            event_sequence=1,
            event_intent_id="event-intent",
            final_message_intent_id="message-intent",
            public_run_intent_id="run-intent",
            final_message_target="room-1",
            public_run_target="run-1",
            created_at=NOW,
        ),
    )
    store.save_authoritative(committed.run)
    assert store.repair_outbox("run-1", repaired_at=NOW + timedelta(seconds=1)) == 3
    assert store.runs["run-1"].projection_state == "settled"
    assert store.repair_outbox("run-1", repaired_at=NOW + timedelta(seconds=2)) == 0
