"""Background recovery for durable room files."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from common.observability import events as E
from common.observability import get_logger, traced_create_task
from jobs.constants import ORPHANED_UPLOAD_CLEANER

logger = get_logger(__name__)

DEFAULT_INTERVAL_HOURS = 24
MAX_AGE_HOURS = 24


class LeaderGate(Protocol):
    async def try_acquire(self, name: str, ttl_seconds: int) -> bool: ...

    async def release(self, name: str) -> None: ...


@dataclass(frozen=True)
class OrphanedUploadCleanerDeps:
    room_files: object


class OrphanedUploadCleaner:
    def __init__(self, interval_hours: int = DEFAULT_INTERVAL_HOURS):
        self.interval_hours = interval_hours
        self._running = False
        self._task: asyncio.Task | None = None
        self._leader: LeaderGate | None = None
        self._deps: OrphanedUploadCleanerDeps | None = None

    def set_leader_election(self, leader: LeaderGate | None) -> None:
        """Attach a LeaderElection instance for distributed leader gating."""
        self._leader = leader

    def set_cleanup_deps(self, deps: OrphanedUploadCleanerDeps) -> None:
        self._deps = deps

    def _require_cleanup_deps(self) -> OrphanedUploadCleanerDeps:
        if self._deps is None:
            raise RuntimeError("Orphaned upload cleaner dependencies are not bound")
        return self._deps

    async def start(self) -> None:
        if self._running:
            logger.emit(E.JOB_ALREADY_STARTED, job=ORPHANED_UPLOAD_CLEANER)
            return
        self._running = True
        self._task = traced_create_task(
            self._run_loop(),
            name="orphaned-upload-cleanup",
        )
        logger.emit(
            E.JOB_STARTED,
            job=ORPHANED_UPLOAD_CLEANER,
            interval_seconds=self.interval_hours * 3600,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.emit(E.JOB_STOPPED, job=ORPHANED_UPLOAD_CLEANER)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_one_iteration()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.emit(
                    E.JOB_ITERATION_FAILED, exc=exc, job=ORPHANED_UPLOAD_CLEANER
                )

            try:
                await asyncio.sleep(self.interval_hours * 3600)
            except asyncio.CancelledError:
                break

    async def _run_one_iteration(self) -> None:
        """Run a single iteration, gated by leader election if available."""
        if self._leader:
            ttl = int(self.interval_hours * 3600 * 2)
            acquired = await self._leader.try_acquire(ORPHANED_UPLOAD_CLEANER, ttl)
            if not acquired:
                logger.emit(E.JOB_SKIPPED_NOT_LEADER, job=ORPHANED_UPLOAD_CLEANER)
                return  # another instance is the leader
            try:
                deleted = await self.cleanup_orphaned_uploads()
                if deleted:
                    logger.emit(E.ORPHANED_UPLOADS_CLEANED, deleted_uploads=deleted)
            finally:
                await self._leader.release(ORPHANED_UPLOAD_CLEANER)
        else:
            deleted = await self.cleanup_orphaned_uploads()
            if deleted:
                logger.emit(E.ORPHANED_UPLOADS_CLEANED, deleted_uploads=deleted)

    async def cleanup_orphaned_uploads(self, max_age_hours: int = MAX_AGE_HOURS) -> int:
        """Recover stale writes and remove unreferenced uploads."""
        deps = self._require_cleanup_deps()
        cleanup = deps.room_files.recover
        return int(await cleanup(max_age_hours=max_age_hours))


orphaned_upload_cleaner = OrphanedUploadCleaner()
