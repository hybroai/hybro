"""Background job loops emit the shared job lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent.health import AgentHealthService
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
    await job.stop()

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

    [skipped] = _events(caplog, "job_skipped_not_leader")
    assert skipped.levelno == logging.DEBUG
    assert skipped.job == job_name
    assert _events(caplog, "job_iteration_failed") == []
    assert leader.released == []


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

    [skipped] = _events(caplog, "job_skipped_not_leader", prefix="agent.")
    assert skipped.levelno == logging.DEBUG
    assert skipped.job == "agent_health_checker"


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
