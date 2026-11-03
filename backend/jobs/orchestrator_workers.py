"""
Leader-elected orchestrator background workers.

The recovery cycle drains A2A recovery work (cancellation, inbox, call
delivery, artifacts, projection), the projection worker drains the durable
projection outbox, and the canary job reads the durable stores and emits
``orchestrator_canary_threshold_breached`` when a §8.2 threshold is exceeded. Recovery and projection are
mandatory canonical lifecycle workers; the canary remains independently
configurable observability.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from common.config import settings
from common.observability import events as E
from common.observability import get_logger, traced_create_task
from execution.adapters.canary_metrics import evaluate_canary_thresholds
from jobs.constants import (
    ORCHESTRATOR_CANARY,
    ORCHESTRATOR_PROJECTION,
    ORCHESTRATOR_RECOVERY,
)

logger = get_logger(__name__)


class LeaderGate(Protocol):
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool: ...

    async def release(self, name: str) -> None: ...


@dataclass(frozen=True)
class OrchestratorRecoveryDeps:
    recover_once: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class OrchestratorProjectionDeps:
    project_once: Callable[[], Awaitable[int]]


@dataclass(frozen=True)
class OrchestratorCanaryDeps:
    collect: Callable[[], Awaitable[dict[str, Any]]]


class OrchestratorRecoveryJob:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._deps: OrchestratorRecoveryDeps | None = None
        self.last_completed_at: datetime | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        self._leader = leader

    def set_recovery_deps(self, deps: OrchestratorRecoveryDeps) -> None:
        self._deps = deps

    def _require_deps(self) -> OrchestratorRecoveryDeps:
        if self._deps is None:
            raise RuntimeError("Orchestrator recovery dependencies are not bound")
        return self._deps

    async def start(self) -> None:
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=ORCHESTRATOR_RECOVERY)
            return
        self._require_deps()
        self._running = True
        self._task = traced_create_task(self._run_loop(), name="orchestrator-recovery")
        logger.emit(
            E.JOB_STARTED,
            job=ORCHESTRATOR_RECOVERY,
            interval_seconds=self.interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.emit(E.JOB_STOPPED, job=ORCHESTRATOR_RECOVERY)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self._run_one_iteration()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.emit(E.JOB_ITERATION_FAILED, exc=exc, job=ORCHESTRATOR_RECOVERY)
                await asyncio.sleep(60)

    async def _run_one_iteration(self) -> None:
        deps = self._require_deps()
        if self._leader:
            ttl = int(self.interval_seconds * 2)
            acquired = await self._leader.try_acquire(ORCHESTRATOR_RECOVERY, ttl)
            if not acquired:
                logger.emit(E.JOB_SKIPPED_NOT_LEADER, job=ORCHESTRATOR_RECOVERY)
                return
            try:
                await deps.recover_once()
            finally:
                await self._leader.release(ORCHESTRATOR_RECOVERY)
        else:
            await deps.recover_once()
        self.last_completed_at = datetime.now(UTC)


class OrchestratorProjectionJob:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._deps: OrchestratorProjectionDeps | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        self._leader = leader

    def set_projection_deps(self, deps: OrchestratorProjectionDeps) -> None:
        self._deps = deps

    def _require_deps(self) -> OrchestratorProjectionDeps:
        if self._deps is None:
            raise RuntimeError("Orchestrator projection dependencies are not bound")
        return self._deps

    async def start(self) -> None:
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=ORCHESTRATOR_PROJECTION)
            return
        self._require_deps()
        self._running = True
        self._task = traced_create_task(
            self._run_loop(), name="orchestrator-projection"
        )
        logger.emit(
            E.JOB_STARTED,
            job=ORCHESTRATOR_PROJECTION,
            interval_seconds=self.interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.emit(E.JOB_STOPPED, job=ORCHESTRATOR_PROJECTION)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self._run_one_iteration()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.emit(
                    E.JOB_ITERATION_FAILED, exc=exc, job=ORCHESTRATOR_PROJECTION
                )
                await asyncio.sleep(60)

    async def _run_one_iteration(self) -> None:
        deps = self._require_deps()
        if self._leader:
            ttl = int(self.interval_seconds * 2)
            acquired = await self._leader.try_acquire(ORCHESTRATOR_PROJECTION, ttl)
            if not acquired:
                logger.emit(E.JOB_SKIPPED_NOT_LEADER, job=ORCHESTRATOR_PROJECTION)
                return
            try:
                await deps.project_once()
            finally:
                await self._leader.release(ORCHESTRATOR_PROJECTION)
        else:
            await deps.project_once()


class OrchestratorCanaryJob:
    """Leader-elected canary threshold watcher (flag-gated, default OFF)."""

    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._deps: OrchestratorCanaryDeps | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        self._leader = leader

    def set_canary_deps(self, deps: OrchestratorCanaryDeps) -> None:
        self._deps = deps

    def _require_deps(self) -> OrchestratorCanaryDeps:
        if self._deps is None:
            raise RuntimeError("Orchestrator canary dependencies are not bound")
        return self._deps

    async def start(self) -> None:
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=ORCHESTRATOR_CANARY)
            return
        if not settings.orchestrator_canary_enabled:
            logger.emit(E.JOB_START_SKIPPED, job=ORCHESTRATOR_CANARY, reason="disabled")
            return
        self._running = True
        self._task = traced_create_task(self._run_loop(), name="orchestrator-canary")
        logger.emit(
            E.JOB_STARTED,
            job=ORCHESTRATOR_CANARY,
            interval_seconds=self.interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.emit(E.JOB_STOPPED, job=ORCHESTRATOR_CANARY)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self._run_one_iteration()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.emit(E.JOB_ITERATION_FAILED, exc=exc, job=ORCHESTRATOR_CANARY)
                await asyncio.sleep(60)

    async def _run_one_iteration(self) -> None:
        deps = self._require_deps()
        if self._leader:
            ttl = int(self.interval_seconds * 2)
            acquired = await self._leader.try_acquire(ORCHESTRATOR_CANARY, ttl)
            if not acquired:
                logger.emit(E.JOB_SKIPPED_NOT_LEADER, job=ORCHESTRATOR_CANARY)
                return
            try:
                await self._evaluate(deps)
            finally:
                await self._leader.release(ORCHESTRATOR_CANARY)
        else:
            await self._evaluate(deps)

    async def _evaluate(self, deps: OrchestratorCanaryDeps) -> None:
        metrics = await deps.collect()
        for breach in evaluate_canary_thresholds(metrics, settings):
            logger.emit(
                E.ORCHESTRATOR_CANARY_THRESHOLD_BREACHED,
                metric=breach.metric,
                threshold=breach.threshold,
                observed=breach.observed,
                **breach.counts,
            )


orchestrator_recovery_job = OrchestratorRecoveryJob()
orchestrator_projection_job = OrchestratorProjectionJob()
orchestrator_canary_job = OrchestratorCanaryJob()

__all__ = [
    "OrchestratorCanaryDeps",
    "OrchestratorCanaryJob",
    "OrchestratorProjectionDeps",
    "OrchestratorProjectionJob",
    "OrchestratorRecoveryDeps",
    "OrchestratorRecoveryJob",
    "orchestrator_canary_job",
    "orchestrator_projection_job",
    "orchestrator_recovery_job",
]
