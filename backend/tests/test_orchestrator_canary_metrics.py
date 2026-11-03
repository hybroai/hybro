"""Tests for the step-8 orchestrator canary observability surface."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from common.config.loader import Settings
from execution.adapters.canary_metrics import (
    CanaryBreach,
    collect_metrics,
    evaluate_canary_thresholds,
)
from execution.orchestrator.a2a_runtime.models import A2AObservationConflictRecord
from execution.repository.orchestrator.run_store import MongoOrchestratorRunStore
from execution.repository.orchestrator.stores import (
    MongoAgentCallLedgerStore,
    MongoObservationConflictStore,
)
from jobs.constants import ALL_JOB_NAMES, ORCHESTRATOR_CANARY
from jobs.orchestrator_workers import (
    OrchestratorCanaryDeps,
    OrchestratorCanaryJob,
    OrchestratorRecoveryDeps,
    OrchestratorRecoveryJob,
)

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import NOW, make_run

BASE_TIME = datetime(2030, 1, 1, tzinfo=UTC)


class _Cursor:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = [dict(doc) for doc in docs]

    async def to_list(self, *, length=None) -> list[dict]:
        del length
        return list(self.docs)


class _Collection:
    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = [dict(doc) for doc in (docs or [])]

    def find(self, query: dict) -> _Cursor:
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one(self, query: dict) -> dict | None:
        return next((dict(doc) for doc in self.docs if _matches(doc, query)), None)

    async def insert_one(self, document: dict) -> SimpleNamespace:
        self.docs.append(dict(document))
        return SimpleNamespace(inserted_id=len(self.docs))

    async def replace_one(self, query: dict, document: dict, *, upsert=False):
        del upsert
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs[index] = dict(document)
                return SimpleNamespace(modified_count=1, matched_count=1)
        return SimpleNamespace(modified_count=0, matched_count=0)

    async def delete_many(self, query: dict) -> SimpleNamespace:
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not _matches(doc, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def aggregate(self, pipeline: list[dict]) -> _Cursor:
        del pipeline
        return _Cursor(self.docs)


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key.startswith("$"):
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            continue
        if actual != expected:
            return False
    return True


def _run_doc(status: str, updated_at: datetime, intents: list[dict] | None = None):
    return {
        "run_id": f"run-{status}",
        "status": status,
        "updated_at": updated_at,
        "projection_outbox": intents or [],
    }


def _conflict(conflict_id: str, status: str) -> A2AObservationConflictRecord:
    return A2AObservationConflictRecord(
        conflict_id=conflict_id,
        conversation_id="room-1",
        room_epoch=1,
        source_identity=f"source-{conflict_id}",
        accepted_observation_id=f"obs-{conflict_id}",
        accepted_payload_digest="accepted-digest",
        conflicting_payload_digest="conflicting-digest",
        binding_scope="endpoint",
        received_at=BASE_TIME,
        status=status,
    )


@pytest.mark.asyncio
async def test_collect_metrics_derives_counts_from_seeded_collections():
    now = BASE_TIME + timedelta(hours=1)
    runs = _Collection(
        [
            _run_doc(
                "completed",
                now - timedelta(seconds=60),
                [{"status": "pending"}, {"status": "completed"}],
            ),
            _run_doc(
                "failed",
                now - timedelta(seconds=10),
                [{"status": "blocked"}],
            ),
            _run_doc("running", now - timedelta(seconds=10)),
            _run_doc("budget_exhausted", now - timedelta(seconds=400)),
        ]
    )
    calls = _Collection(
        [
            {"state": "working"},
            {"state": "completed"},
            {"state": "failed"},
            {"state": "dispatching"},
        ]
    )
    conflicts = _Collection(
        [
            {"status": "open"},
            {"status": "resolved"},
            {"status": "open"},
        ]
    )

    metrics = await collect_metrics(
        runs,
        calls,
        conflicts,
        recovery_cycle_last_run_at=BASE_TIME,
        window_seconds=300,
        now=now,
    )

    assert metrics["runs_by_status"]["completed"] == 1
    assert metrics["runs_by_status"]["failed"] == 1
    assert metrics["runs_by_status"]["running"] == 1
    assert metrics["runs_by_status"]["budget_exhausted"] == 1
    # The 400-second-old terminal run is outside the window.
    assert metrics["runs_by_status_window"]["completed"] == 1
    assert metrics["runs_by_status_window"]["failed"] == 1
    assert metrics["runs_by_status_window"]["running"] == 1
    assert metrics["runs_by_status_window"]["budget_exhausted"] == 0
    assert metrics["projection_outbox_pending"] == 1
    assert metrics["projection_outbox_blocked"] == 1
    assert metrics["projection_outbox_blocked_oldest_at"] == now - timedelta(seconds=10)
    assert metrics["agent_calls_outstanding"] == 2
    assert metrics["observation_conflicts_open"] == 2
    assert metrics["recovery_cycle_last_run_at"] == BASE_TIME
    assert metrics["collected_at"] == now


@pytest.mark.asyncio
async def test_collect_metrics_defaults_recovery_cycle_time_to_none():
    metrics = await collect_metrics(
        _Collection(), _Collection(), _Collection(), now=BASE_TIME
    )

    assert metrics["recovery_cycle_last_run_at"] is None
    assert metrics["agent_calls_outstanding"] == 0
    assert metrics["observation_conflicts_open"] == 0


@pytest.mark.asyncio
async def test_collect_metrics_reads_through_real_mongo_stores():
    run_store = MongoOrchestratorRunStore(_Collection())
    await run_store.create(make_run(), command_id="create-1")
    call_ledger = MongoAgentCallLedgerStore(_Collection())
    await call_ledger.insert(ledger_record(state="working"))
    conflicts = MongoObservationConflictStore(_Collection())
    await conflicts.insert(_conflict("conflict-1", "open"))
    await conflicts.insert(_conflict("conflict-2", "resolved"))

    metrics = await collect_metrics(
        run_store.collection,
        call_ledger.collection,
        conflicts.collection,
        now=NOW,
    )

    assert metrics["runs_by_status"]["running"] == 1
    assert metrics["agent_calls_outstanding"] == 1
    assert metrics["observation_conflicts_open"] == 1


def test_evaluate_canary_thresholds_reports_breaches():
    settings = Settings()
    now = BASE_TIME
    metrics = {
        "runs_by_status_window": {
            "completed": 90,
            "failed": 2,
            "budget_exhausted": 1,
            "canceled": 7,
        },
        "projection_outbox_blocked": 1,
        "projection_outbox_blocked_oldest_at": now - timedelta(seconds=601),
        "recovery_cycle_last_run_at": now - timedelta(seconds=61),
        "observation_conflicts_open": 2,
        "collected_at": now,
    }

    breaches = evaluate_canary_thresholds(metrics, settings)

    assert breaches == [
        CanaryBreach(
            metric="run_failure_rate",
            threshold=0.01,
            observed=0.03,
            counts={"failed_runs": 3, "terminal_runs": 100},
        ),
        CanaryBreach(
            metric="projection_outbox_blocked_age_seconds",
            threshold=600,
            observed=601.0,
            counts={"blocked_intents": 1},
        ),
        CanaryBreach(metric="recovery_cycle_age_seconds", threshold=60, observed=61.0),
        CanaryBreach(metric="observation_conflicts_open", threshold=0, observed=2),
    ]


def test_evaluate_canary_thresholds_reports_blocked_intents_without_age():
    metrics = {
        "projection_outbox_blocked": 3,
        "projection_outbox_blocked_oldest_at": None,
        "collected_at": BASE_TIME,
    }

    assert evaluate_canary_thresholds(metrics, Settings()) == [
        CanaryBreach(metric="projection_outbox_blocked", threshold=0, observed=3)
    ]


def test_evaluate_canary_thresholds_is_quiet_when_healthy():
    settings = Settings()
    now = BASE_TIME
    metrics = {
        "runs_by_status_window": {"completed": 100, "failed": 0},
        "projection_outbox_blocked": 0,
        "projection_outbox_blocked_oldest_at": None,
        "recovery_cycle_last_run_at": now - timedelta(seconds=30),
        "observation_conflicts_open": 0,
        "collected_at": now,
    }

    assert evaluate_canary_thresholds(metrics, settings) == []


def test_canary_settings_defaults():
    settings = Settings()

    assert settings.orchestrator_canary_enabled is False
    assert settings.orchestrator_canary_run_failure_rate_max == 0.01
    assert settings.orchestrator_canary_run_failure_window_seconds == 300
    assert settings.orchestrator_canary_blocked_intent_max_age_seconds == 600
    assert settings.orchestrator_canary_recovery_cycle_max_age_seconds == 60
    assert settings.orchestrator_canary_observation_conflicts_max == 0


def test_canary_job_name_is_registered_for_leader_release_all():
    assert ORCHESTRATOR_CANARY == "orchestrator_canary"
    assert ORCHESTRATOR_CANARY in ALL_JOB_NAMES


class _FakeLeader:
    def __init__(self) -> None:
        self.acquired: list[tuple[str, int]] = []
        self.released: list[str] = []

    async def try_acquire(self, name: str, ttl_seconds: int) -> bool:
        self.acquired.append((name, ttl_seconds))
        return True

    async def release(self, name: str) -> None:
        self.released.append(name)


@pytest.mark.asyncio
async def test_canary_job_logs_warning_on_breach(monkeypatch, caplog):
    monkeypatch.setattr("jobs.orchestrator_workers.settings", Settings())
    leader = _FakeLeader()

    async def collect() -> dict:
        return {
            "runs_by_status_window": {"completed": 1, "failed": 1},
            "projection_outbox_blocked": 0,
            "projection_outbox_blocked_oldest_at": None,
            "recovery_cycle_last_run_at": None,
            "observation_conflicts_open": 0,
            "collected_at": BASE_TIME,
        }

    job = OrchestratorCanaryJob(interval_seconds=30)
    job.set_leader_election(leader)
    job.set_canary_deps(OrchestratorCanaryDeps(collect=collect))

    with caplog.at_level("WARNING"):
        await job._run_one_iteration()

    assert leader.acquired == [(ORCHESTRATOR_CANARY, 60)]
    assert leader.released == [ORCHESTRATOR_CANARY]
    [breach] = [
        record
        for record in caplog.records
        if record.getMessage() == "orchestrator_canary_threshold_breached"
    ]
    assert breach.levelno == logging.WARNING
    assert breach.metric == "run_failure_rate"
    assert breach.threshold == 0.01
    assert breach.observed == 0.5
    assert breach.failed_runs == 1
    assert breach.terminal_runs == 2


@pytest.mark.asyncio
async def test_canary_job_skips_leader_gate_when_no_leader_bound():
    called = []

    async def collect() -> dict:
        called.append(True)
        return {
            "runs_by_status_window": {},
            "projection_outbox_blocked": 0,
            "projection_outbox_blocked_oldest_at": None,
            "recovery_cycle_last_run_at": None,
            "observation_conflicts_open": 0,
            "collected_at": BASE_TIME,
        }

    job = OrchestratorCanaryJob(interval_seconds=30)
    job.set_canary_deps(OrchestratorCanaryDeps(collect=collect))
    await job._run_one_iteration()

    assert called == [True]


@pytest.mark.asyncio
async def test_recovery_job_records_last_completed_at():
    leader = _FakeLeader()
    ran = []

    async def recover_once() -> None:
        ran.append(True)

    job = OrchestratorRecoveryJob(interval_seconds=30)
    job.set_leader_election(leader)
    job.set_recovery_deps(OrchestratorRecoveryDeps(recover_once=recover_once))
    assert job.last_completed_at is None

    await job._run_one_iteration()

    assert ran == [True]
    assert job.last_completed_at is not None
