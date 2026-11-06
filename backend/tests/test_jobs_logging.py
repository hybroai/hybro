"""Background job loops emit the shared job lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from agent.health import AgentHealthService
from common.config.loader import Settings
from common.observability.job_runs import job_runs
from jobs.cleanup_orphaned_uploads import (
    OrphanedUploadCleaner,
    OrphanedUploadCleanerDeps,
)
from jobs.compaction_sweep import CompactionSweep, CompactionSweepDeps
from jobs.constants import (
    COMPACTION_SWEEP,
    ORCHESTRATOR_CANARY,
    ORCHESTRATOR_PROJECTION,
    ORCHESTRATOR_RECOVERY,
    ORPHANED_UPLOAD_CLEANER,
    STALE_TASK_CHECKER,
)
from jobs.orchestrator_workers import (
    OrchestratorCanaryDeps,
    OrchestratorCanaryJob,
    OrchestratorProjectionDeps,
    OrchestratorProjectionJob,
    OrchestratorRecoveryDeps,
    OrchestratorRecoveryJob,
)
from jobs.stale_task_checker import StaleHITLDeps, StaleTaskChecker


class _DepsFailed(RuntimeError):
    pass


async def _raise(*_args: Any, **_kwargs: Any) -> Any:
    raise _DepsFailed


class _FailingRoomFiles:
    async def recover(self, *, max_age_hours: int) -> int:
        del max_age_hours
        raise _DepsFailed


class _UnusedCompaction:
    async def compact_if_needed(self, conversation_id: str) -> None:
        del conversation_id


def _recovery(*, fast: bool) -> OrchestratorRecoveryJob:
    job = OrchestratorRecoveryJob(interval_seconds=0 if fast else 30)
    job.set_recovery_deps(OrchestratorRecoveryDeps(recover_once=_raise))
    return job


def _projection(*, fast: bool) -> OrchestratorProjectionJob:
    job = OrchestratorProjectionJob(interval_seconds=0 if fast else 30)
    job.set_projection_deps(OrchestratorProjectionDeps(project_once=_raise))
    return job


def _canary(*, fast: bool) -> OrchestratorCanaryJob:
    job = OrchestratorCanaryJob(interval_seconds=0 if fast else 30)
    job.set_canary_deps(OrchestratorCanaryDeps(collect=_raise))
    return job


def _stale(*, fast: bool) -> StaleTaskChecker:
    del fast  # The checker runs its first iteration before sleeping.
    job = StaleTaskChecker(check_interval_minutes=5)
    job.set_hitl_deps(StaleHITLDeps(reconcile_lifecycle=_raise))
    return job


def _compaction(*, fast: bool) -> CompactionSweep:
    job = CompactionSweep(interval_minutes=0 if fast else 30)
    job.set_sweep_deps(
        CompactionSweepDeps(
            list_room_ids_with_memory=_raise,
            get_room_ids_with_non_terminal_runs=_raise,
            context_compaction=_UnusedCompaction(),
        )
    )
    return job


def _cleaner(*, fast: bool) -> OrphanedUploadCleaner:
    del fast  # The cleaner runs its first iteration before sleeping.
    job = OrphanedUploadCleaner(interval_hours=24)
    job.set_cleanup_deps(OrphanedUploadCleanerDeps(room_files=_FailingRoomFiles()))
    return job


# (factory, job name, interval in seconds for the non-fast configuration)
JOBS: list[tuple[Callable[..., Any], str, int]] = [
    (_recovery, ORCHESTRATOR_RECOVERY, 30),
    (_projection, ORCHESTRATOR_PROJECTION, 30),
    (_canary, ORCHESTRATOR_CANARY, 30),
    (_stale, STALE_TASK_CHECKER, 300),
    (_compaction, COMPACTION_SWEEP, 1800),
    (_cleaner, ORPHANED_UPLOAD_CLEANER, 86400),
]
JOB_IDS = [name for _, name, _ in JOBS]


@pytest.fixture(autouse=True)
def _reset_job_runs():
    job_runs.reset()
    yield
    job_runs.reset()


def _job_state(job_name: str):
    [state] = [state for state in job_runs.snapshot() if state.job == job_name]
    return state


@pytest.fixture(autouse=True)
def _enable_flagged_jobs(monkeypatch):
    monkeypatch.setattr(
        "jobs.orchestrator_workers.settings",
        SimpleNamespace(orchestrator_canary_enabled=True),
    )
    monkeypatch.setattr(
        "jobs.compaction_sweep.settings", SimpleNamespace(compaction_enabled=True)
    )


def _events(caplog, event: str, prefix: str = "jobs.") -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith(prefix) and record.getMessage() == event
    ]


async def _wait_for(
    caplog, event: str, prefix: str = "jobs."
) -> list[logging.LogRecord]:
    for _ in range(200):
        found = _events(caplog, event, prefix)
        if found:
            return found
        await asyncio.sleep(0.005)
    raise AssertionError(f"no {event} record")


@pytest.mark.asyncio
@pytest.mark.parametrize(("factory", "job_name", "interval"), JOBS, ids=JOB_IDS)
async def test_job_start_and_stop_emit_lifecycle_events(
    caplog, factory, job_name, interval
):
    caplog.set_level(logging.DEBUG)
    job = factory(fast=False)

    await job.start()
    assert _job_state(job_name).running is True
    assert _job_state(job_name).interval_seconds == interval
    await job.stop()
    assert _job_state(job_name).running is False

    [started] = _events(caplog, "job_started")
    assert started.levelno == logging.INFO
    assert started.job == job_name
    assert started.interval_seconds == interval
    [stopped] = _events(caplog, "job_stopped")
    assert stopped.levelno == logging.INFO
    assert stopped.job == job_name


@pytest.mark.asyncio
@pytest.mark.parametrize(("factory", "job_name", "interval"), JOBS, ids=JOB_IDS)
async def test_job_iteration_failure_emits_job_iteration_failed(
    caplog, factory, job_name, interval
):
    del interval
    caplog.set_level(logging.DEBUG)
    job = factory(fast=True)

    await job.start()
    try:
        failures = await _wait_for(caplog, "job_iteration_failed")
    finally:
        await job.stop()

    record = failures[0]
    assert record.levelno == logging.ERROR
    assert record.job == job_name
    assert record.error_type == "_DepsFailed"
    state = _job_state(job_name)
    assert state.last_outcome == "failed"
    assert state.last_error_type == "_DepsFailed"
    assert state.failures >= 1


class _Follower:
    """A leader gate held by another instance."""

    def __init__(self) -> None:
        self.released: list[str] = []

    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        del name, ttl_seconds
        return False

    async def release(self, name: str) -> None:
        self.released.append(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("factory", "job_name", "interval"), JOBS, ids=JOB_IDS)
async def test_job_skipped_when_not_leader(caplog, factory, job_name, interval):
    del interval
    caplog.set_level(logging.DEBUG)
    job = factory(fast=False)
    leader = _Follower()
    job.set_leader_election(leader)

    await job._run_one_iteration()
    await job._run_one_iteration()

    first, second = _events(caplog, "job_skipped_not_leader")
    assert first.levelno == logging.INFO
    assert second.levelno == logging.DEBUG
    assert first.job == job_name
    assert _events(caplog, "job_iteration_failed") == []
    assert leader.released == []
    assert _job_state(job_name).last_outcome == "skipped_not_leader"


class _FailingHealthLeader:
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        del name, ttl_seconds
        raise _DepsFailed

    async def release(self, name: str) -> None:
        del name


@pytest.mark.asyncio
async def test_health_loop_skipped_when_not_leader(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(check_interval_seconds=30)
    service.set_leader_election(_Follower(), lock_name="agent_health_checker")

    await service._run_one_iteration()
    await service._run_one_iteration()

    first, second = _events(caplog, "job_skipped_not_leader", prefix="agent.")
    assert first.levelno == logging.INFO
    assert second.levelno == logging.DEBUG
    assert first.job == "agent_health_checker"


@pytest.mark.asyncio
async def test_health_loop_failure_emits_job_iteration_failed(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "agent.health.settings", SimpleNamespace(agent_health_check_enabled=True)
    )
    service = AgentHealthService(check_interval_seconds=30)
    service.set_leader_election(
        _FailingHealthLeader(), lock_name="agent_health_checker"
    )

    await service.start()
    try:
        failures = await _wait_for(caplog, "job_iteration_failed", prefix="agent.")
    finally:
        await service.stop()

    record = failures[0]
    assert record.levelno == logging.ERROR
    assert record.job == "agent_health_checker"
    assert record.error_type == "_DepsFailed"


@pytest.mark.asyncio
async def test_health_loop_start_and_stop_emit_lifecycle_events(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "agent.health.settings", SimpleNamespace(agent_health_check_enabled=True)
    )
    service = AgentHealthService(check_interval_seconds=30)
    service.set_leader_election(_Follower(), lock_name="agent_health_checker")

    await service.start()
    await service.start()
    await service.stop()

    [started] = _events(caplog, "job_started", prefix="agent.")
    assert started.levelno == logging.INFO
    assert started.job == "agent_health_checker"
    assert started.interval_seconds == 30
    assert started.timeout_seconds == 10.0
    assert started.max_consecutive_failure_count == 3
    assert started.initial_retry_seconds == 30.0
    [already] = _events(caplog, "job_already_started", prefix="agent.")
    assert already.job == "agent_health_checker"
    [stopped] = _events(caplog, "job_stopped", prefix="agent.")
    assert stopped.job == "agent_health_checker"


@pytest.mark.asyncio
async def test_disabled_health_loop_emits_job_start_skipped(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "agent.health.settings", SimpleNamespace(agent_health_check_enabled=False)
    )
    service = AgentHealthService(check_interval_seconds=30)
    service.set_leader_election(None, lock_name="agent_health_checker")

    await service.start()

    [skipped] = _events(caplog, "job_start_skipped", prefix="agent.")
    assert skipped.job == "agent_health_checker"
    assert skipped.reason == "disabled"


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _no_rooms() -> list[str]:
    return []


class _RecoveredRoomFiles:
    async def recover(self, *, max_age_hours: int) -> int:
        del max_age_hours
        return 0


async def _healthy_metrics() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "runs_by_status_window": {"completed": 10, "failed": 0},
        "projection_outbox_blocked": 0,
        "projection_outbox_blocked_oldest_at": None,
        "recovery_cycle_last_run_at": now,
        "observation_conflicts_open": 0,
        "collected_at": now,
    }


class _NoAgents:
    async def list_visible(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


def _ok_recovery() -> OrchestratorRecoveryJob:
    job = OrchestratorRecoveryJob(interval_seconds=30)
    job.set_recovery_deps(OrchestratorRecoveryDeps(recover_once=_noop))
    return job


def _ok_projection() -> OrchestratorProjectionJob:
    job = OrchestratorProjectionJob(interval_seconds=30)
    job.set_projection_deps(OrchestratorProjectionDeps(project_once=_noop))
    return job


def _ok_canary() -> OrchestratorCanaryJob:
    job = OrchestratorCanaryJob(interval_seconds=30)
    job.set_canary_deps(OrchestratorCanaryDeps(collect=_healthy_metrics))
    return job


def _ok_stale() -> StaleTaskChecker:
    job = StaleTaskChecker(check_interval_minutes=5)
    job.set_hitl_deps(StaleHITLDeps(reconcile_lifecycle=_noop))
    return job


def _ok_compaction() -> CompactionSweep:
    job = CompactionSweep(interval_minutes=30)
    job.set_sweep_deps(
        CompactionSweepDeps(
            list_room_ids_with_memory=_no_rooms,
            get_room_ids_with_non_terminal_runs=_no_rooms,
            context_compaction=_UnusedCompaction(),
        )
    )
    return job


def _ok_cleaner() -> OrphanedUploadCleaner:
    job = OrphanedUploadCleaner(interval_hours=24)
    job.set_cleanup_deps(OrphanedUploadCleanerDeps(room_files=_RecoveredRoomFiles()))
    return job


def _ok_health() -> AgentHealthService:
    service = AgentHealthService(check_interval_seconds=30, repository=_NoAgents())
    service.set_leader_election(None, lock_name="agent_health_checker")
    return service


OK_JOBS: list[tuple[Callable[[], Any], str, str]] = [
    (_ok_recovery, ORCHESTRATOR_RECOVERY, "jobs."),
    (_ok_projection, ORCHESTRATOR_PROJECTION, "jobs."),
    (_ok_canary, ORCHESTRATOR_CANARY, "jobs."),
    (_ok_stale, STALE_TASK_CHECKER, "jobs."),
    (_ok_compaction, COMPACTION_SWEEP, "jobs."),
    (_ok_cleaner, ORPHANED_UPLOAD_CLEANER, "jobs."),
    (_ok_health, "agent_health_checker", "agent."),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "job_name", "prefix"), OK_JOBS, ids=[name for _, name, _ in OK_JOBS]
)
async def test_successful_iteration_emits_job_run_completed(
    caplog, monkeypatch, factory, job_name, prefix
):
    monkeypatch.setattr("jobs.orchestrator_workers.settings", Settings())
    caplog.set_level(logging.DEBUG)
    job = factory()

    await job._run_one_iteration()
    await job._run_one_iteration()

    first, second = _events(caplog, "job_run_completed", prefix)
    assert first.levelno == logging.INFO
    assert second.levelno == logging.DEBUG
    assert first.job == job_name
    assert isinstance(first.duration_ms, (int, float))
    assert _events(caplog, "job_iteration_failed", prefix) == []
    state = _job_state(job_name)
    assert state.last_outcome == "ok"
    assert state.runs == 2


@pytest.mark.asyncio
async def test_unnamed_health_cycle_emits_no_job_run_completed(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(check_interval_seconds=30, repository=_NoAgents())

    await service._run_one_iteration()

    assert _events(caplog, "job_run_completed", "agent.") == []
    assert job_runs.snapshot() == []


class _RaisingAgents:
    async def list_visible(self, **_kwargs: Any) -> list[dict[str, Any]]:
        raise _DepsFailed


def _stale_failing() -> StaleTaskChecker:
    job = StaleTaskChecker(check_interval_minutes=5)
    job.set_hitl_deps(StaleHITLDeps(reconcile_lifecycle=_raise))
    return job


def _stale_unbound() -> StaleTaskChecker:
    return StaleTaskChecker(check_interval_minutes=5)


def _health_failing() -> AgentHealthService:
    service = AgentHealthService(check_interval_seconds=30, repository=_RaisingAgents())
    service.set_leader_election(None, lock_name="agent_health_checker")
    return service


def _health_unbound() -> AgentHealthService:
    service = AgentHealthService(check_interval_seconds=30)
    service.set_leader_election(None, lock_name="agent_health_checker")
    return service


FAILING_PASSES = [
    (_stale_failing, STALE_TASK_CHECKER, "_DepsFailed"),
    (_stale_unbound, STALE_TASK_CHECKER, "deps_unbound"),
    (_health_failing, "agent_health_checker", "_DepsFailed"),
    (_health_unbound, "agent_health_checker", "RuntimeError"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "job_name", "error_type"),
    FAILING_PASSES,
    ids=["stale-raises", "stale-unbound", "health-raises", "health-unbound"],
)
async def test_failed_pass_records_one_failure_and_no_completion(
    caplog, factory, job_name, error_type
):
    caplog.set_level(logging.DEBUG)
    job = factory()

    await job._run_one_iteration()

    assert _events(caplog, "job_run_completed", "") == []
    state = _job_state(job_name)
    assert state.last_outcome == "failed"
    assert state.last_error_type == error_type
    assert state.failures == 1
    assert state.consecutive_failures == 1
    assert state.runs == 0
