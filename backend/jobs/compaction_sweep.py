"""
Background Compaction Sweep Job

Periodically scans all active rooms and runs lossless compaction on any
room whose conversation history exceeds the configured thresholds.

This catches rooms that grow without triggering inline compaction — e.g.
supervisor orchestration rooms, rooms with high-frequency direct chat, or rooms
where the inline trigger was skipped due to an error.

See docs/System-Architecture.md for compaction design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from common.config import settings
from common.dto import CompactionResult
from common.observability import events as E
from common.observability import get_logger, traced_create_task
from jobs.constants import COMPACTION_SWEEP

logger = get_logger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
MAX_CONCURRENT_COMPACTIONS = 5


@runtime_checkable
class CompactionPort(Protocol):
    async def compact_if_needed(
        self, conversation_id: str
    ) -> CompactionResult | None: ...


class LeaderGate(Protocol):
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool: ...

    async def release(self, name: str) -> None: ...


@dataclass(frozen=True)
class CompactionSweepDeps:
    list_room_ids_with_memory: Callable[[], Awaitable[list[str]]]
    get_room_ids_with_non_terminal_runs: Callable[[], Awaitable[list[str]]]
    context_compaction: CompactionPort


class CompactionSweep:
    """Background job that sweeps rooms for compaction eligibility."""

    def __init__(self, interval_minutes: int = DEFAULT_INTERVAL_MINUTES):
        self.interval_minutes = interval_minutes
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._deps: CompactionSweepDeps | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        """Attach a LeaderElection instance for distributed leader gating."""
        self._leader = leader

    def set_sweep_deps(self, deps: CompactionSweepDeps) -> None:
        self._deps = deps

    def _require_sweep_deps(self) -> CompactionSweepDeps:
        if self._deps is None:
            raise RuntimeError("Compaction sweep dependencies are not bound")
        return self._deps

    async def start(self) -> None:
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=COMPACTION_SWEEP)
            return
        if not settings.compaction_enabled:
            logger.emit(E.JOB_START_SKIPPED, job=COMPACTION_SWEEP, reason="disabled")
            return
        self._running = True
        self._task = traced_create_task(self._run_loop(), name="compaction-sweep")
        logger.emit(
            E.JOB_STARTED,
            job=COMPACTION_SWEEP,
            interval_seconds=self.interval_minutes * 60,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.emit(E.JOB_STOPPED, job=COMPACTION_SWEEP)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_minutes * 60)
                await self._run_one_iteration()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.emit(E.JOB_ITERATION_FAILED, exc=exc, job=COMPACTION_SWEEP)
                await asyncio.sleep(60)

    async def _run_one_iteration(self) -> None:
        """Run a single iteration, gated by leader election if available."""
        if self._leader:
            ttl = int(self.interval_minutes * 60 * 2)
            acquired = await self._leader.try_acquire(COMPACTION_SWEEP, ttl)
            if not acquired:
                return  # another instance is the leader
            try:
                await self.sweep()
            finally:
                await self._leader.release(COMPACTION_SWEEP)
        else:
            await self.sweep()

    async def sweep(self) -> dict:
        """Scan all rooms with memory and compact where needed.

        Skips rooms with a non-terminal canonical orchestrator Run, including
        suspended ones (``awaiting_user``, ``waiting_external``), to avoid
        read-modify-write races with the running turn.

        Uses a fixed-size worker pool to bound memory usage instead of
        creating one task per room.

        Returns a summary dict: {scanned, compacted, skipped, errors}.
        """
        deps = self._require_sweep_deps()
        stats = {"scanned": 0, "compacted": 0, "skipped": 0, "errors": 0}

        # This lookup is a global safety gate: if it fails, the sweep must stop
        # rather than treating every room as idle.
        ids = await deps.get_room_ids_with_non_terminal_runs()
        active_room_ids = {rid for rid in ids if rid}

        eligible_room_ids: list[str] = []
        for conversation_id in await deps.list_room_ids_with_memory():
            if not conversation_id:
                continue
            stats["scanned"] += 1

            if conversation_id in active_room_ids:
                stats["skipped"] += 1
                continue

            eligible_room_ids.append(conversation_id)

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _worker() -> None:
            while True:
                rid = await queue.get()
                if rid is None:
                    queue.task_done()
                    return
                try:
                    result = await deps.context_compaction.compact_if_needed(rid)
                    if result and result.compacted_count > 0:
                        stats["compacted"] += 1
                        logger.emit(
                            E.ROOM_COMPACTED,
                            conversation_id=rid,
                            compacted_turns=result.compacted_count,
                            tokens_saved=result.tokens_saved,
                        )
                except Exception as exc:
                    stats["errors"] += 1
                    logger.emit(E.ROOM_COMPACTION_FAILED, exc=exc, conversation_id=rid)
                finally:
                    queue.task_done()

        workers: list[asyncio.Task] = []
        try:
            for index in range(MAX_CONCURRENT_COMPACTIONS):
                workers.append(
                    traced_create_task(_worker(), name=f"compaction-worker-{index}")
                )

            for conversation_id in eligible_room_ids:
                await queue.put(conversation_id)

            # Signal workers to stop after all eligible rooms are processed.
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        logger.emit(
            E.COMPACTION_SWEEP_COMPLETED,
            scanned_rooms=stats["scanned"],
            compacted_rooms=stats["compacted"],
            skipped_rooms=stats["skipped"],
            failed_rooms=stats["errors"],
        )
        return stats


compaction_sweep = CompactionSweep()
