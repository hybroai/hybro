"""JobRunRecorder: last-run state per background job."""

import logging

import pytest

from common.observability.job_runs import JobRunRecorder


@pytest.fixture
def recorder() -> JobRunRecorder:
    return JobRunRecorder()


def _state(recorder: JobRunRecorder, job: str):
    [state] = [state for state in recorder.snapshot() if state.job == job]
    return state


def test_completed_is_info_on_change_then_debug(recorder):
    assert recorder.completed("j", 5) == logging.INFO
    assert recorder.completed("j", 6) == logging.DEBUG


def test_failure_then_completion_is_info_and_resets_consecutive_failures(recorder):
    recorder.completed("j", 1)
    recorder.failed("j", "RuntimeError")
    recorder.failed("j", "RuntimeError")
    assert _state(recorder, "j").consecutive_failures == 2

    assert recorder.completed("j", 1) == logging.INFO
    state = _state(recorder, "j")
    assert state.consecutive_failures == 0
    assert state.failures == 2
    assert state.last_outcome == "ok"


def test_skipped_is_info_on_change_then_debug(recorder):
    assert recorder.skipped("j") == logging.INFO
    assert recorder.skipped("j") == logging.DEBUG
    recorder.completed("j", 1)
    assert recorder.skipped("j") == logging.INFO


def test_snapshot_reports_counters_and_timestamps(recorder):
    recorder.started("j", 30)
    recorder.completed("j", 12)
    recorder.skipped("j")
    recorder.failed("j", "TimeoutError")

    state = _state(recorder, "j")
    assert state.running is True
    assert state.interval_seconds == 30
    assert state.runs == 1
    assert state.skips == 1
    assert state.failures == 1
    assert state.consecutive_failures == 1
    assert state.last_outcome == "failed"
    assert state.last_error_type == "TimeoutError"
    assert state.last_duration_ms == 12
    assert state.last_started_at is not None
    assert state.last_started_at.tzinfo is not None
    assert state.last_completed_at is not None
    assert state.last_skipped_at is not None
    assert state.last_failed_at is not None

    recorder.stopped("j")
    assert _state(recorder, "j").running is False


def test_failed_stamps_its_own_time_and_keeps_last_completed(recorder):
    recorder.failed("j", "RuntimeError")
    state = _state(recorder, "j")
    assert state.last_failed_at is not None
    assert state.last_completed_at is None

    recorder.completed("j", 1)
    completed_at = _state(recorder, "j").last_completed_at
    recorder.failed("j", "RuntimeError")
    state = _state(recorder, "j")
    assert state.last_completed_at == completed_at
    assert state.last_failed_at >= completed_at


def test_snapshot_is_a_copy(recorder):
    recorder.completed("j", 1)
    [state] = recorder.snapshot()
    recorder.completed("j", 2)
    assert state.last_duration_ms == 1


def test_none_job_name_is_ignored(recorder):
    recorder.started(None, 30)
    recorder.failed(None, "X")
    recorder.stopped(None)
    assert recorder.completed(None, 1) == logging.DEBUG
    assert recorder.skipped(None) == logging.DEBUG
    assert recorder.snapshot() == []


def test_reset_clears(recorder):
    recorder.completed("j", 1)
    recorder.reset()
    assert recorder.snapshot() == []
