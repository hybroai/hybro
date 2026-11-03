"""Background job loops emit the shared job lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

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


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("jobs.") and record.getMessage() == event
    ]


async def _wait_for(caplog, event: str) -> list[logging.LogRecord]:
    for _ in range(200):
        found = _events(caplog, event)
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
