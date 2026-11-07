"""DiagnosticsService: the snapshot, the record queries and authorisation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from bson import ObjectId

from common.observability.sinks import LogEntry, RingBufferSink
from diagnostics import service as service_module
from diagnostics.models import (
    CanaryBreachView,
    CanaryMetrics,
    CanaryReading,
    EventingSection,
    HealthSection,
    LeaderSection,
    ProviderSection,
    RecordQuery,
    RecoveryRun,
    SseSection,
    StoreSection,
)
from diagnostics.protocols import InvalidCursor, StoreUnavailable
from diagnostics.repository.mongo import EVENTS_COLLECTION, LOGS_COLLECTION
from diagnostics.service import DiagnosticsService, SnapshotSources

T0 = datetime(2030, 1, 1, tzinfo=UTC)
TOKEN = "a" * 64


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _health() -> HealthSection:
    return HealthSection(
        mongo_connected=True,
        redis_expected=False,
        delivery_pubsub_connected=True,
        delivery_kv_connected=False,
        eventing_connected=False,
        redis_runtime_connected=False,
        agent_search_index_ready=True,
    )


def _metrics(**overrides) -> CanaryMetrics:
    values = {
        "runs_by_status": {"running": 2, "completed": 5},
        "runs_by_status_window": {"completed": 1},
        "projection_outbox_pending": 3,
        "projection_outbox_blocked": 1,
        "projection_outbox_blocked_oldest_at": None,
        "agent_calls_outstanding": 4,
        "observation_conflicts_open": 0,
        "recovery_cycle_last_run_at": None,
        "collected_at": T0,
    }
    values.update(overrides)
    return CanaryMetrics.model_validate(values)


def _reading() -> CanaryReading:
    return CanaryReading(
        job_enabled=False,
        metrics=_metrics(),
        breaches=[
            CanaryBreachView(
                metric="projection_outbox_blocked", threshold=0, observed=1
            )
        ],
        non_terminal_runs=2,
    )


def _store(ready: bool = True) -> StoreSection:
    return StoreSection(
        ready=ready,
        queued=0,
        dropped=0,
        written=7,
        write_failures=0,
        last_error_type=None,
        last_write_at=None,
        ring_size=3,
        ring_capacity=2000,
    )


class _Sources:
    """Mutable fakes behind a SnapshotSources."""

    def __init__(self) -> None:
        self.agent_records: list[dict[str, object]] = []
        self.canary_calls = 0
        self.canary_delay = 0.0
        self.health_delay = 0.0
        self.agents_delay = 0.0
        self.store_ready = True
        self.raising: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.raising:
            raise RuntimeError(f"{name} down")

    async def health(self) -> HealthSection:
        self._check("health")
        await asyncio.sleep(self.health_delay)
        return _health()

    def provider(self) -> ProviderSection:
        self._check("provider")
        return ProviderSection(state="unconfigured", last_error="config_invalid")

    async def agents(self) -> list[dict[str, object]]:
        self._check("agents")
        await asyncio.sleep(self.agents_delay)
        return self.agent_records

    def leader(self) -> LeaderSection:
        self._check("leader")
        return LeaderSection(enabled=False, instance_id="ab12cd34")

    def eventing(self) -> EventingSection:
        self._check("eventing")
        return EventingSection(
            connected=False,
            transport="memory",
            dead_letter_count=0,
            dead_letter_capacity=1000,
            dead_letters_by_stage={},
            last_dead_letter_at=None,
        )

    def sse(self) -> SseSection:
        self._check("sse")
        return SseSection(rooms=1, connections=2)

    def recovery_run(self) -> RecoveryRun:
        self._check("recovery")
        return RecoveryRun(last_cycle_completed_at=T0, last_cycle_failed_phases=["x"])

    async def canary(self) -> CanaryReading:
        self.canary_calls += 1
        self._check("canary")
        await asyncio.sleep(self.canary_delay)
        return _reading()

    def store(self) -> StoreSection:
        self._check("store")
        return _store(self.store_ready)

    def build(self, *, canary: bool = True) -> SnapshotSources:
        return SnapshotSources(
            health=self.health,
            provider=self.provider,
            agents=self.agents,
            leader=self.leader,
            eventing=self.eventing,
            sse=self.sse,
            recovery_run=self.recovery_run,
            canary=self.canary if canary else None,
            store=self.store,
        )


class _Repository:
    def __init__(self, documents: list[dict] | None = None) -> None:
        self.documents = documents or []
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None
        self.delay = 0.0

    async def find(self, collection, record_filter):
        self.calls.append((collection, record_filter))
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.documents[: record_filter.limit])


def _service(
    sources: _Sources | None = None,
    *,
    repository: _Repository | None = None,
    ring: RingBufferSink | None = None,
    token: str = TOKEN,
    auth_mode: str = "clerk",
    clock: _Clock | None = None,
    canary: bool = True,
    log_level: Callable[[], str] = lambda: "INFO",
) -> DiagnosticsService:
    sources = sources or _Sources()
    return DiagnosticsService(
        sources.build(canary=canary),
        repository=repository or _Repository(),
        ring=ring,
        token=token,
        auth_mode=auth_mode,
        admin_user_ids=frozenset({"admin"}),
        version="0.4.4",
        instance_id="ab12cd34",
        environment="test",
        log_level=log_level,
        log_format="json",
        started_at=T0,
        now=clock or _Clock(),
    )


# ── Snapshot ──────────────────────────────────────────────────────────────


async def test_snapshot_reports_the_current_log_level():
    level = ["INFO"]
    service = _service(log_level=lambda: level[0])
    level[0] = "DEBUG"  # The Settings API changed it live.
    assert (await service.snapshot()).service.log_level == "DEBUG"


async def test_snapshot_maps_every_section():
    sources = _Sources()
    sources.agent_records = [
        {
            "agent_id": "a1",
            "agent_card": {"name": "Echo"},
            "source": "bundled",
            "agent_status": "active",
        }
    ]
    snapshot = await _service(sources).snapshot()

    assert snapshot.errors == []
    assert snapshot.generated_at == T0
    assert snapshot.service.version == "0.4.4"
    assert snapshot.service.instance_id == "ab12cd34"
    assert snapshot.service.environment == "test"
    assert snapshot.service.auth_mode == "clerk"
    assert snapshot.health == _health()
    assert snapshot.provider == ProviderSection(
        state="unconfigured", last_error="config_invalid"
    )
    assert snapshot.agents is not None
    assert snapshot.agents.total == 1
    assert snapshot.agents.items[0].name == "Echo"
    assert snapshot.leader == LeaderSection(enabled=False, instance_id="ab12cd34")
    assert snapshot.eventing is not None and snapshot.eventing.transport == "memory"
    assert snapshot.sse == SseSection(rooms=1, connections=2)
    assert snapshot.recovery is not None
    assert snapshot.recovery.last_cycle_completed_at == T0
    assert snapshot.recovery.last_cycle_failed_phases == ["x"]
    assert snapshot.recovery.non_terminal_runs == 2
    assert snapshot.recovery.agent_calls_outstanding == 4
    assert snapshot.recovery.projection_outbox_pending == 3
    assert snapshot.recovery.projection_outbox_blocked == 1
    assert snapshot.recovery.observation_conflicts_open == 0
    assert snapshot.canary is not None
    assert snapshot.canary.metrics == _metrics()
    assert [b.metric for b in snapshot.canary.breaches] == ["projection_outbox_blocked"]
    assert snapshot.store == _store()
    assert isinstance(snapshot.jobs, list)


@pytest.mark.parametrize(
    ("raising", "section"),
    [
        ("health", "health"),
        ("provider", "provider"),
        ("agents", "agents"),
        ("leader", "leader"),
        ("eventing", "eventing"),
        ("sse", "sse"),
        ("recovery", "recovery"),
        ("canary", "canary"),
        ("store", "store"),
    ],
)
async def test_a_raising_source_is_null_with_an_error(raising, section):
    sources = _Sources()
    sources.raising = {raising}

    snapshot = await _service(sources).snapshot()

    assert getattr(snapshot, section) is None
    assert [(e.section, e.error_type) for e in snapshot.errors] == [
        (section, "RuntimeError")
    ]
    others = {"health", "provider", "agents", "leader", "eventing", "sse", "store"}
    for other in others - {section}:
        assert getattr(snapshot, other) is not None


async def test_a_failed_canary_nulls_only_the_metric_backed_recovery_fields():
    sources = _Sources()
    sources.raising = {"canary"}

    snapshot = await _service(sources).snapshot()

    assert snapshot.recovery is not None
    assert snapshot.recovery.last_cycle_completed_at == T0
    assert snapshot.recovery.non_terminal_runs is None
    assert snapshot.recovery.agent_calls_outstanding is None


async def test_without_a_canary_source_canary_is_null_without_an_error():
    snapshot = await _service(canary=False).snapshot()

    assert snapshot.canary is None
    assert snapshot.errors == []
    assert snapshot.recovery is not None
    assert snapshot.recovery.projection_outbox_pending is None


async def test_uptime_follows_the_clock():
    clock = _Clock()
    service = _service(clock=clock)

    clock.now = T0 + timedelta(seconds=5)
    first = await service.snapshot()
    clock.now = T0 + timedelta(seconds=65)
    second = await service.snapshot()

    assert first.service.uptime_seconds == 5
    assert second.service.uptime_seconds == 65
    assert second.generated_at == T0 + timedelta(seconds=65)
    assert second.service.started_at == T0


@pytest.mark.parametrize("slow", ["health", "agents"])
async def test_a_slow_source_becomes_an_error_within_the_bound(monkeypatch, slow):
    monkeypatch.setattr(service_module, "SOURCE_TIMEOUT_SECONDS", 0.05)
    sources = _Sources()
    setattr(sources, f"{slow}_delay", 5.0)

    started = asyncio.get_running_loop().time()
    snapshot = await _service(sources).snapshot()

    assert asyncio.get_running_loop().time() - started < 2.0
    assert getattr(snapshot, slow) is None
    assert [(e.section, e.error_type) for e in snapshot.errors] == [
        (slow, "TimeoutError")
    ]


async def test_agent_names_are_sanitised_and_items_truncated():
    sources = _Sources()
    long_name = "N" * 200
    sources.agent_records = [
        {
            "agent_id": f"a{i}",
            "agent_card": {"name": long_name if i == 0 else f"agent {i}"},
            "source": "bundled" if i % 2 else "local",
            "agent_status": "active" if i < 150 else "inactive",
            "inactive_reason": None if i < 150 else "provider_missing",
        }
        for i in range(250)
    ]
    sources.agent_records.append(
        {
            "agent_id": "secret",
            "agent_card": {"name": "see https://user:hunter2@example.test/x"},
            "source": "cloud",
            "agent_status": "active",
        }
    )

    agents = (await _service(sources).snapshot()).agents

    assert agents is not None
    assert agents.total == 251
    assert agents.active == 151
    assert agents.inactive == 100
    assert agents.by_source == {"local": 125, "bundled": 125, "cloud": 1}
    assert len(agents.items) == 200
    assert agents.truncated is True
    first = agents.items[0]
    assert first.name.startswith("N" * 80)
    assert "TRUNCATED" in first.name
    assert "N" * 81 not in first.name
    assert agents.items[199].status == "inactive"
    assert agents.items[199].inactive_reason == "provider_missing"


async def test_agent_names_are_redacted():
    sources = _Sources()
    sources.agent_records = [
        {
            "agent_id": "a1",
            "agent_card": {"name": "see https://user:hunter2@example.test/x"},
            "source": "cloud",
            "agent_status": "active",
        }
    ]

    agents = (await _service(sources).snapshot()).agents

    assert agents is not None
    assert agents.truncated is False
    assert "hunter2" not in agents.items[0].name


async def test_canary_reading_is_cached_for_thirty_seconds():
    sources = _Sources()
    clock = _Clock()
    service = _service(sources, clock=clock)

    await service.snapshot()
    clock.now = T0 + timedelta(seconds=29)
    await service.snapshot()
    assert sources.canary_calls == 1

    clock.now = T0 + timedelta(seconds=31)
    await service.snapshot()
    assert sources.canary_calls == 2


async def test_a_slow_canary_times_out_into_errors(monkeypatch):
    monkeypatch.setattr(service_module, "SOURCE_TIMEOUT_SECONDS", 0.05)
    sources = _Sources()
    sources.canary_delay = 5.0

    snapshot = await _service(sources).snapshot()

    assert snapshot.canary is None
    assert [(e.section, e.error_type) for e in snapshot.errors] == [
        ("canary", "TimeoutError")
    ]


# ── Store queries ─────────────────────────────────────────────────────────


def _document(index: int, **overrides) -> dict:
    document = {
        "_id": ObjectId(),
        "ts": T0 + timedelta(seconds=index),
        "level": "warning",
        "level_no": logging.WARNING,
        "service": "hybro-backend",
        "event": "agent_deactivated",
        "logger": "agent.health",
        "system": True,
        "instance_id": "ab12cd34",
        "version": "0.4.4",
        "trace_id": "t-1",
        "request_id": None,
        "fields_json": json.dumps({"agent_id": "a1", "n": index}),
    }
    document.update(overrides)
    return document


async def test_events_pass_the_filters_to_the_repository():
    repository = _Repository()
    before = ObjectId()
    query = RecordQuery(
        service="hybro-backend",
        level="warning",
        event=("a", "b"),
        trace_id="t-1",
        instance_id="ab12cd34",
        since=T0,
        until=T0 + timedelta(hours=1),
        limit=10,
        before=str(before),
    )

    await _service(repository=repository).events(query)

    [(collection, record_filter)] = repository.calls
    assert collection == EVENTS_COLLECTION
    assert record_filter.service == "hybro-backend"
    assert record_filter.min_level_no == logging.WARNING
    assert record_filter.events == ("a", "b")
    assert record_filter.trace_id == "t-1"
    assert record_filter.instance_id == "ab12cd34"
    assert record_filter.since == T0
    assert record_filter.until == T0 + timedelta(hours=1)
    assert record_filter.before_id == before
    assert record_filter.limit == 10


async def test_logs_from_the_store_read_the_log_collection():
    repository = _Repository()

    await _service(repository=repository).logs(RecordQuery(), source="store")

    [(collection, record_filter)] = repository.calls
    assert collection == LOGS_COLLECTION
    assert record_filter.min_level_no is None
    assert record_filter.before_id is None
    assert record_filter.limit == 100


async def test_documents_map_to_records():
    documents = [_document(2), _document(1)]
    repository = _Repository(documents)

    page = await _service(repository=repository).events(RecordQuery(limit=5))

    assert [record.id for record in page.items] == [
        str(document["_id"]) for document in documents
    ]
    record = page.items[0]
    assert record.timestamp == T0 + timedelta(seconds=2)
    assert record.fields == {"agent_id": "a1", "n": 2}
    assert record.trace_id == "t-1"
    assert record.system is True
    assert page.next_before is None
    rendered = page.model_dump(mode="json")
    assert datetime.fromisoformat(rendered["items"][0]["timestamp"]).tzinfo


async def test_next_before_is_set_only_on_a_full_page():
    documents = [_document(3), _document(2)]
    repository = _Repository(documents)

    page = await _service(repository=repository).events(RecordQuery(limit=2))

    assert page.next_before == str(documents[-1]["_id"])


async def test_invalid_cursor_raises():
    with pytest.raises(InvalidCursor):
        await _service().events(RecordQuery(before="not-an-object-id"))


async def test_a_raising_repository_is_store_unavailable():
    repository = _Repository()
    repository.error = ConnectionError("down")

    with pytest.raises(StoreUnavailable):
        await _service(repository=repository).events(RecordQuery())


async def test_a_slow_repository_is_store_unavailable(monkeypatch):
    monkeypatch.setattr(service_module, "SOURCE_TIMEOUT_SECONDS", 0.05)
    repository = _Repository()
    repository.delay = 5.0

    with pytest.raises(StoreUnavailable):
        await _service(repository=repository).logs(RecordQuery(), source="store")


async def test_a_store_that_is_not_ready_is_store_unavailable():
    sources = _Sources()
    sources.store_ready = False
    repository = _Repository()

    with pytest.raises(StoreUnavailable):
        await _service(sources, repository=repository).events(RecordQuery())
    assert repository.calls == []


# ── Memory logs ───────────────────────────────────────────────────────────


def _entry(
    sequence: int,
    *,
    level_no: int = logging.INFO,
    event: str = "e",
    service: str = "hybro-backend",
    trace_id: str | None = None,
    instance_id: str = "ab12cd34",
) -> LogEntry:
    fields = {"instance_id": instance_id, "version": "0.4.4", "n": sequence}
    if trace_id is not None:
        fields["trace_id"] = trace_id
    return LogEntry(
        sequence=sequence,
        timestamp=T0 + timedelta(seconds=sequence),
        level=logging.getLevelName(level_no).lower(),
        level_no=level_no,
        service=service,
        event=event,
        logger="test",
        system=False,
        fields=MappingProxyType(fields),
    )


def _ring(*entries: LogEntry) -> RingBufferSink:
    ring = RingBufferSink()
    for entry in entries:
        ring.accept(entry)
    return ring


async def test_memory_logs_are_newest_first_with_all_levels():
    ring = _ring(_entry(1, level_no=logging.DEBUG), _entry(2), _entry(3))

    page = await _service(ring=ring).logs(RecordQuery(), source="memory")

    assert [record.id for record in page.items] == ["3", "2", "1"]
    assert page.items[0].fields == {"n": 3}
    assert page.items[0].instance_id == "ab12cd34"
    rendered = page.model_dump(mode="json")["items"][0]["timestamp"]
    assert datetime.fromisoformat(rendered).tzinfo


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (RecordQuery(level="warning"), ["4", "3"]),
        (RecordQuery(event=("x",)), ["4"]),
        (RecordQuery(trace_id="t-2"), ["2"]),
        (RecordQuery(service="hybro-mcp"), ["5"]),
        (RecordQuery(instance_id="other"), ["5"]),
        (RecordQuery(since=T0 + timedelta(seconds=2)), ["5", "4", "3", "2"]),
        (RecordQuery(until=T0 + timedelta(seconds=2)), ["1"]),
        (RecordQuery(before="4"), ["3", "2", "1"]),
        (RecordQuery(limit=2), ["5", "4"]),
    ],
)
async def test_memory_logs_apply_every_filter(query, expected):
    ring = _ring(
        _entry(1),
        _entry(2, trace_id="t-2"),
        _entry(3, level_no=logging.WARNING),
        _entry(4, level_no=logging.ERROR, event="x"),
        _entry(5, service="hybro-mcp", instance_id="other"),
    )

    page = await _service(ring=ring).logs(query, source="memory")

    assert [record.id for record in page.items] == expected


async def test_memory_next_before_pages_through_the_buffer():
    ring = _ring(*(_entry(i) for i in range(1, 6)))
    service = _service(ring=ring)

    first = await service.logs(RecordQuery(limit=2), source="memory")
    second = await service.logs(
        RecordQuery(limit=2, before=first.next_before), source="memory"
    )

    assert first.next_before == "4"
    assert [record.id for record in second.items] == ["3", "2"]


async def test_memory_invalid_cursor_raises():
    with pytest.raises(InvalidCursor):
        await _service(ring=_ring()).logs(RecordQuery(before="abc"), source="memory")


async def test_memory_logs_without_capture_are_empty():
    page = await _service(ring=None).logs(RecordQuery(), source="memory")

    assert page.items == []
    assert page.next_before is None


# ── Authorisation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("auth_mode", "user_id", "allowed"),
    [
        ("mock", "anyone", True),
        ("clerk", "admin", True),
        ("clerk", "user", False),
    ],
)
def test_can_read_follows_the_mock_or_admin_rule(auth_mode, user_id, allowed):
    assert _service(auth_mode=auth_mode).can_read(user_id) is allowed


def test_token_matches_the_configured_token():
    assert _service().token_matches(TOKEN) is True


@pytest.mark.parametrize(
    ("configured", "value"),
    [
        ("", ""),
        ("", "anything"),
        ("short", "short"),
        ("b" * 31, "b" * 31),
        (TOKEN, "b" * 64),
        (TOKEN, ""),
        (TOKEN, "é" * 64),
        (TOKEN, "\ud800"),
    ],
)
def test_token_does_not_match(configured, value):
    assert _service(token=configured).token_matches(value) is False


async def test_jobs_section_maps_the_recorded_runs():
    from common.observability.job_runs import job_runs

    job_runs.reset()
    try:
        job_runs.started("test_job", 30.0)
        job_runs.completed("test_job", 12.5)
        job_runs.failed("test_job", "RuntimeError")

        snapshot = await _service().snapshot()
    finally:
        job_runs.reset()

    assert snapshot.jobs is not None
    [view] = snapshot.jobs
    assert view.job == "test_job"
    assert view.running is True
    assert view.interval_seconds == 30.0
    assert view.last_outcome == "failed"
    assert view.last_duration_ms == 12.5
    assert view.runs == 1
    assert view.failures == 1
    assert view.consecutive_failures == 1
    assert view.last_error_type == "RuntimeError"
    assert snapshot.errors == []


async def test_a_raising_jobs_source_is_null_with_an_error(monkeypatch):
    def broken():
        raise RuntimeError("jobs")

    monkeypatch.setattr(service_module, "_jobs", broken)

    snapshot = await _service().snapshot()

    assert snapshot.jobs is None
    assert [(e.section, e.error_type) for e in snapshot.errors] == [
        ("jobs", "RuntimeError")
    ]


async def test_concurrent_snapshots_share_one_canary_reading():
    sources = _Sources()
    sources.canary_delay = 0.05
    service = _service(sources)

    await asyncio.gather(service.snapshot(), service.snapshot(), service.snapshot())

    assert sources.canary_calls == 1


async def test_waiting_for_a_hanging_canary_is_bounded(monkeypatch):
    monkeypatch.setattr(service_module, "SOURCE_TIMEOUT_SECONDS", 0.05)
    sources = _Sources()
    sources.canary_delay = 10
    service = _service(sources)
    loop = asyncio.get_running_loop()
    started = loop.time()

    snapshots = await asyncio.gather(*(service.snapshot() for _ in range(6)))

    # Serialised waits would take at least 6 bounds (0.3 s).
    assert loop.time() - started < 0.1
    for snapshot in snapshots:
        assert snapshot.canary is None
        assert ("canary", "TimeoutError") in [
            (e.section, e.error_type) for e in snapshot.errors
        ]
