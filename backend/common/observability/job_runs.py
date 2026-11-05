"""Last-run state of the background jobs in this process.

Each job records its runs here; ``completed`` and ``skipped`` return the level
for the matching event: INFO when the outcome changed, DEBUG otherwise, so a
job that repeats on a timer logs transitions at INFO and steady state at
DEBUG. Imports only the standard library so any module may use it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

JobOutcome = Literal["ok", "failed", "skipped_not_leader"]


@dataclass
class JobRunState:
    job: str
    running: bool = False
    interval_seconds: float | None = None
    last_outcome: JobOutcome | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_duration_ms: float | None = None
    last_skipped_at: datetime | None = None
    runs: int = 0
    failures: int = 0
    skips: int = 0
    consecutive_failures: int = 0
    last_error_type: str | None = None


class JobRunRecorder:
    """Per-job run state; every method ignores a ``None`` job name."""

    def __init__(self) -> None:
        self._states: dict[str, JobRunState] = {}
        self._lock = threading.Lock()

    def _state(self, job: str) -> JobRunState:
        state = self._states.get(job)
        if state is None:
            state = self._states[job] = JobRunState(job=job)
        return state

    def started(self, job: str | None, interval_seconds: float) -> None:
        if job is None:
            return
        with self._lock:
            state = self._state(job)
            state.running = True
            state.interval_seconds = interval_seconds
            state.last_started_at = datetime.now(UTC)

    def stopped(self, job: str | None) -> None:
        if job is None:
            return
        with self._lock:
            self._state(job).running = False

    def completed(self, job: str | None, duration_ms: float) -> int:
        if job is None:
            return logging.DEBUG
        with self._lock:
            state = self._state(job)
            level = self._transition(state, "ok")
            state.runs += 1
            state.consecutive_failures = 0
            state.last_duration_ms = duration_ms
            state.last_completed_at = datetime.now(UTC)
            return level

    def skipped(self, job: str | None) -> int:
        if job is None:
            return logging.DEBUG
        with self._lock:
            state = self._state(job)
            level = self._transition(state, "skipped_not_leader")
            state.skips += 1
            state.last_skipped_at = datetime.now(UTC)
            return level

    def failed(self, job: str | None, error_type: str) -> None:
        if job is None:
            return
        with self._lock:
            state = self._state(job)
            state.last_outcome = "failed"
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error_type = error_type

    def snapshot(self) -> list[JobRunState]:
        with self._lock:
            return [replace(state) for state in self._states.values()]

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    @staticmethod
    def _transition(state: JobRunState, outcome: JobOutcome) -> int:
        level = logging.INFO if state.last_outcome != outcome else logging.DEBUG
        state.last_outcome = outcome
        return level


# Process-wide, like the job singletons that record into it.
job_runs = JobRunRecorder()

__all__ = ["JobOutcome", "JobRunRecorder", "JobRunState", "job_runs"]
