import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from execution.model_loop.context import ContextCompiler
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.kernel import KernelConflict, OrchestratorKernel
from execution.orchestrator.models import RecoveryClaim
from execution.orchestrator.session import EventCancellationSignal
from orchestrator_composition import (
    _generic_recovery_failure_decision,
    _run_with_recovery_lease,
)
from tests._orchestrator_helpers import (
    NOW,
    FixedClock,
    FixedIDs,
    ScriptedModelRuntime,
    final_events,
    make_run,
)
from tests.orchestrator_harness.fake_tools import (
    RecordingFakeToolRuntime,
    StaticFakeToolCatalog,
)
from tests.orchestrator_harness.in_memory import (
    InMemoryOrchestratorRunStore,
    InMemoryProjectionDriver,
)


@pytest.mark.asyncio
async def test_irreparable_recovery_conflict_is_durably_quarantined_after_bound():
    store = InMemoryOrchestratorRunStore()
    run = make_run().model_copy(
        update={
            "status": "running",
            "recovery_claim": make_run().recovery_claim.model_copy(
                update={"next_attempt_at": NOW - timedelta(seconds=1)}
            ),
        }
    )
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    attempt_at = NOW

    for attempt in range(1, 4):
        due = await store.list_due_runs(due_at=attempt_at, limit=10)
        assert [item.run_id for item in due] == [run.run_id]
        current = await store.load(run.run_id)
        assert current is not None
        owner = f"worker:{attempt}"
        claimed = await store.claim_recovery(
            run.run_id,
            expected_state_version=current.state_version,
            owner_id=owner,
            lease_expires_at=attempt_at + timedelta(minutes=1),
            claimed_at=attempt_at,
        )
        assert claimed.run is not None
        decision = _generic_recovery_failure_decision(
            claimed.run.recovery_claim,
            KernelConflict("legacy canonical history is inconsistent"),
            now=attempt_at,
        )
        released = await store.release_recovery(
            run.run_id,
            expected_state_version=claimed.run.state_version,
            owner_id=owner,
            next_attempt_at=decision.next_attempt_at,
            failure_count=decision.failure_count,
            quarantined_at=decision.quarantined_at,
            quarantine_reason=decision.quarantine_reason,
        )
        assert released.outcome == "accepted"
        assert released.run is not None
        assert released.run.recovery_claim.failure_count == attempt
        if decision.next_attempt_at is not None:
            attempt_at = decision.next_attempt_at

    quarantined = await store.load(run.run_id)
    assert quarantined is not None
    assert quarantined.status == "running"
    assert quarantined.recovery_claim.quarantine_reason == (
        "terminal_invariant_conflict"
    )
    assert quarantined.recovery_claim.quarantined_at == attempt_at
    assert (
        await store.list_due_runs(due_at=attempt_at + timedelta(days=3650), limit=10)
        == []
    )
    assert (
        await store.claim_recovery(
            run.run_id,
            expected_state_version=quarantined.state_version,
            owner_id="worker:late",
            lease_expires_at=attempt_at + timedelta(days=3650, minutes=1),
            claimed_at=attempt_at + timedelta(days=3650),
        )
    ).outcome == "conflict"


def test_non_invariant_failure_resets_consecutive_quarantine_counter():
    claim = make_run().recovery_claim.model_copy(update={"failure_count": 2})

    decision = _generic_recovery_failure_decision(
        claim, RuntimeError("temporary adapter outage"), now=NOW
    )

    assert decision.failure_count == 0
    assert decision.quarantined_at is None
    assert decision.next_attempt_at == NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_successful_recovery_release_resets_prior_failure_count():
    store = InMemoryOrchestratorRunStore()
    run = make_run().model_copy(
        update={
            "status": "running",
            "recovery_claim": make_run().recovery_claim.model_copy(
                update={
                    "next_attempt_at": NOW,
                    "failure_count": 2,
                }
            ),
        }
    )
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    claimed = await store.claim_recovery(
        run.run_id,
        expected_state_version=run.state_version,
        owner_id="worker:success",
        lease_expires_at=NOW + timedelta(minutes=1),
        claimed_at=NOW,
    )
    assert claimed.run is not None
    released = await store.release_recovery(
        run.run_id,
        expected_state_version=claimed.run.state_version,
        owner_id="worker:success",
        next_attempt_at=None,
    )
    assert released.run is not None
    assert released.run.recovery_claim.failure_count == 0
    assert released.run.recovery_claim.quarantined_at is None


@pytest.mark.asyncio
async def test_long_healthy_provider_window_is_not_due_on_recovery_tick():
    store = InMemoryOrchestratorRunStore()
    run = make_run()
    assert run.recovery_claim.next_attempt_at == run.budget.deadline_at
    assert (await store.create(run, command_id="create")).outcome == "accepted"

    provider_finished = asyncio.Event()

    async def healthy_provider_stream():
        await asyncio.sleep(0.05)
        provider_finished.set()

    provider = asyncio.create_task(healthy_provider_stream())
    for offset in (0, 1, 30):
        due = await store.list_due_runs(
            due_at=run.created_at + timedelta(seconds=offset), limit=10
        )
        assert due == []
        await asyncio.sleep(0.01)
    await provider
    assert provider_finished.is_set()


@pytest.mark.asyncio
async def test_two_workers_slow_runtime_keeps_single_recovery_owner_past_lease():
    store = InMemoryOrchestratorRunStore()
    claim_now = datetime.now(UTC)
    run = make_run().model_copy(
        update={
            "recovery_claim": RecoveryClaim(next_attempt_at=claim_now),
        }
    )
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    owner_one = "instance-one:token-one"
    owner_two = "instance-two:token-two"
    claimed = await store.claim_recovery(
        run.run_id,
        expected_state_version=run.state_version,
        owner_id=owner_one,
        lease_expires_at=claim_now + timedelta(milliseconds=40),
        claimed_at=claim_now,
    )
    assert claimed.outcome == "accepted"
    side_effects: list[str] = []

    async def slow_runtime():
        side_effects.append("provider-called")
        await asyncio.sleep(0.14)
        return "done"

    worker_one = asyncio.create_task(
        _run_with_recovery_lease(
            run_store=store,
            run_id=run.run_id,
            owner_id=owner_one,
            work=slow_runtime(),
            lease_duration=timedelta(milliseconds=40),
            renew_interval_seconds=0.01,
        )
    )
    await asyncio.sleep(0.08)
    due = await store.list_due_runs(due_at=datetime.now(UTC), limit=10)
    assert due == []
    latest = await store.load(run.run_id)
    assert latest is not None
    stolen = (
        await store.claim_recovery(
            run.run_id,
            expected_state_version=latest.state_version,
            owner_id=owner_two,
            lease_expires_at=datetime.now(UTC) + timedelta(milliseconds=40),
            claimed_at=datetime.now(UTC),
        )
        if due
        else None
    )
    assert stolen is None
    assert await worker_one == "done"
    assert side_effects == ["provider-called"]
    latest = await store.load(run.run_id)
    assert latest is not None and latest.recovery_claim.owner_id == owner_one


@pytest.mark.asyncio
async def test_slow_kernel_checkpoint_commits_while_recovery_lease_renews():
    store = InMemoryOrchestratorRunStore()
    claim_now = datetime.now(UTC)
    run = make_run().model_copy(
        update={
            "recovery_claim": RecoveryClaim(next_attempt_at=claim_now),
        }
    )
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    owner = "instance:slow-kernel"
    claimed = await store.claim_recovery(
        run.run_id,
        expected_state_version=run.state_version,
        owner_id=owner,
        lease_expires_at=claim_now + timedelta(milliseconds=30),
        claimed_at=claim_now,
    )
    assert claimed.run is not None
    claimed_version = claimed.run.state_version

    class SlowRuntime(ScriptedModelRuntime):
        async def stream_turn(self, request, *, signal):
            async for event in super().stream_turn(request, signal=signal):
                await asyncio.sleep(0.035)
                yield event

    kernel = OrchestratorKernel(
        run_store=store,
        model_runtime=SlowRuntime([final_events("lease-safe")]),
        tool_runtime=RecordingFakeToolRuntime(),
        tool_catalog=StaticFakeToolCatalog(),
        context_compiler=ContextCompiler(),
        budget_policy=BudgetPolicy(),
        projection_driver=InMemoryProjectionDriver(store),
        clock=FixedClock(),
        id_factory=FixedIDs(),
    )
    result = await _run_with_recovery_lease(
        run_store=store,
        run_id=run.run_id,
        owner_id=owner,
        work=kernel.run(run.run_id, signal=EventCancellationSignal()),
        lease_duration=timedelta(milliseconds=30),
        renew_interval_seconds=0.01,
    )

    assert result.outcome == "final_answer"
    latest = await store.load(run.run_id)
    assert latest is not None
    assert latest.status == "completed"
    assert latest.state_version > claimed_version
