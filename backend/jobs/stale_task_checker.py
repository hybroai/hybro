"""
Stale Task Checker Background Job

This module provides a leader-gated background job that runs the durable
HITL lifecycle reconciliation passes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from common.observability import events as E
from common.observability import get_logger, traced_create_task
from jobs.constants import STALE_TASK_CHECKER

logger = get_logger(__name__)


class LeaderGate(Protocol):
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool: ...

    async def release(self, name: str) -> None: ...


@dataclass(frozen=True)
class StaleHITLDeps:
    reconcile_lifecycle: Callable[[], Awaitable[Any]]


class StaleTaskChecker:
    """Background job for durable HITL reconciliation.

    Canonical Run recovery and projection are owned exclusively by the
    mandatory orchestrator workers; this checker never creates a second
    Run/Task lifecycle.
    """

    def __init__(self, check_interval_minutes: int = 5):
        self.check_interval_minutes = check_interval_minutes
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._hitl_deps: StaleHITLDeps | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        """Attach a leader gate instance for distributed leader gating."""
        self._leader = leader

    def set_hitl_deps(self, deps: StaleHITLDeps) -> None:
        self._hitl_deps = deps

    async def start(self) -> None:
        """Start the background checker."""
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=STALE_TASK_CHECKER)
            return

        self._running = True
        self._task = traced_create_task(self._run_loop(), name="stale-task-checker")
        logger.emit(
            E.JOB_STARTED,
            job=STALE_TASK_CHECKER,
            interval_seconds=self.check_interval_minutes * 60,
        )

    async def stop(self) -> None:
        """Stop the background checker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.emit(E.JOB_STOPPED, job=STALE_TASK_CHECKER)

    async def _run_loop(self) -> None:
        """Main loop that runs the check periodically."""
        while self._running:
            try:
                await self._run_one_iteration()
            except Exception as exc:
                logger.emit(E.JOB_ITERATION_FAILED, exc=exc, job=STALE_TASK_CHECKER)

            # Wait for next check
            await asyncio.sleep(self.check_interval_minutes * 60)

    async def _run_one_iteration(self) -> None:
        """Run a single iteration, gated by leader election if available."""
        if self._leader:
            ttl = int(self.check_interval_minutes * 60 * 2)
            acquired = await self._leader.try_acquire(STALE_TASK_CHECKER, ttl)
            if not acquired:
                return  # another instance is the leader
            try:
                await self.check_stale_tasks()
            finally:
                await self._leader.release(STALE_TASK_CHECKER)
        else:
            await self.check_stale_tasks()

    async def check_stale_tasks(self) -> None:
        """Run one reconciliation pass; called periodically by the loop."""
        if self._hitl_deps is None:
            logger.emit(E.HITL_RECONCILIATION_SKIPPED, reason="deps_unbound")
            return
        try:
            await self._hitl_deps.reconcile_lifecycle()
        except Exception as exc:
            logger.emit(E.JOB_ITERATION_FAILED, exc=exc, job=STALE_TASK_CHECKER)


# Singleton instance. Application startup binds runtime dependencies.
stale_task_checker = StaleTaskChecker()
