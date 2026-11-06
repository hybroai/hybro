"""Container wiring of the diagnostics store, snapshot sources and access."""

import asyncio
import inspect
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import container
from common.config.loader import Settings
from common.observability import get_instance_id
from common.observability.logging import StructuredFormatter
from common.observability.sinks import RingBufferSink, SinkHandler
from diagnostics.models import (
    EventingSection,
    HealthSection,
    ProviderSection,
    RecoveryRun,
    SseSection,
    StoreSection,
)
from diagnostics.store_sink import StoreSink
from orchestrator_composition import configured_public_secret_values


class _Repository:
    async def insert(self, collection, documents):
        del collection, documents


@pytest.mark.asyncio
async def test_store_is_not_started_when_not_ready():
    assert container.start_diagnostics_store(_Repository(), ready=False) is None


@pytest.mark.asyncio
async def test_store_is_not_started_without_capture(monkeypatch):
    monkeypatch.setattr(container, "log_sinks", lambda: None)

    assert container.start_diagnostics_store(_Repository(), ready=True) is None


@pytest.mark.asyncio
async def test_store_starts_on_the_sink_handler(monkeypatch):
    handler = SinkHandler(StructuredFormatter(output_format="json", environment="t"))
    ring = RingBufferSink()
    monkeypatch.setattr(container, "log_sinks", lambda: handler)
    monkeypatch.setattr(container, "ring_buffer", lambda: ring)

    sink = container.start_diagnostics_store(_Repository(), ready=True)
    try:
        assert isinstance(sink, StoreSink)
        assert sink.stats()["ready"] is True
        assert sink.accept in handler._sinks
    finally:
        await sink.stop()


def test_lifespan_stops_the_store_before_closing_mongo():
    source = inspect.getsource(container._runtime_lifespan)

    mongo_steps = [m.start() for m in re.finditer(r'\("mongo"', source)]
    store_steps = [m.start() for m in re.finditer(r'\("diagnostics-store"', source)]
    assert len(mongo_steps) == 2
    assert len(store_steps) == 2
    for store, mongo in zip(store_steps, mongo_steps, strict=True):
        assert store < mongo
        guard = source.rfind("if _diagnostics_sink is not None", 0, store)
        assert guard != -1 and source.count("\n", guard, store) <= 1


# ── Snapshot sources ──────────────────────────────────────────────────────

T0 = datetime(2030, 1, 1, tzinfo=UTC)


class _Probe:
    """An awaitable call that records, raises or hangs on demand."""

    def __init__(self, result: object = None) -> None:
        self.result = result
        self.calls = 0
        self.raises = False
        self.hangs = False

    async def __call__(self) -> object:
        self.calls += 1
        if self.raises:
            raise ConnectionError("down")
        if self.hangs:
            await asyncio.sleep(10)
        return self.result


def _dead_letter(stage: str, second: int) -> SimpleNamespace:
    return SimpleNamespace(
        failure_stage=stage, timestamp=T0 + timedelta(seconds=second)
    )


def _fakes(**overrides) -> dict:
    delivery = SimpleNamespace(
        refresh_health=_Probe(),
        delivery_pubsub_connected=True,
        delivery_kv_connected=True,
        room_connections={"r1": {"c1": object(), "c2": object()}, "r2": {}},
    )
    eventing = SimpleNamespace(
        refresh_health=_Probe(),
        is_connected=True,
        transport=object(),
        dead_letters=[
            _dead_letter("handler", 1),
            _dead_letter("publish", 3),
            _dead_letter("handler", 2),
        ],
        config=SimpleNamespace(dead_letter_memory_maxlen=1000),
    )

    class _Agents:
        def __init__(self) -> None:
            self.kwargs: dict | None = None

        async def list_visible(self, **kwargs):
            self.kwargs = kwargs
            return [{"agent_id": "a1"}]

    async def collect_canary() -> dict:
        return {
            "runs_by_status": {"running": 2, "queued": 1, "completed": 9},
            "runs_by_status_window": {},
            "projection_outbox_pending": 0,
            "projection_outbox_blocked": 1,
            "projection_outbox_blocked_oldest_at": None,
            "agent_calls_outstanding": 0,
            "observation_conflicts_open": 0,
            "recovery_cycle_last_run_at": None,
            "collected_at": T0,
        }

    values = {
        "app_state": SimpleNamespace(
            provider_runtime=SimpleNamespace(
                current=SimpleNamespace(state="credential_invalid"),
                last_error="config_invalid",
            ),
            agent_search_index_ready=True,
        ),
        "mongo": SimpleNamespace(ping=_Probe(True)),
        "delivery_facade": delivery,
        "eventing_bus": eventing,
        "redis_runtime": SimpleNamespace(
            command_client=SimpleNamespace(is_connected=True)
        ),
        "leader": object(),
        "agent_repository": _Agents(),
        "recovery_job": SimpleNamespace(last_completed_at=T0),
        "recovery_cycle": SimpleNamespace(last_failed_phases=["dispatch"]),
        "collect_canary": collect_canary,
        "settings": Settings(redis_url="redis://cache:6379/0"),
        "store_sink": None,
    }
    values.update(overrides)
    return values


async def test_health_reads_the_flags_after_both_refreshes():
    fakes = _fakes()

    health = await container.build_snapshot_sources(**fakes).health()

    assert fakes["delivery_facade"].refresh_health.calls == 1
    assert fakes["eventing_bus"].refresh_health.calls == 1
    assert fakes["mongo"].ping.calls == 1
    assert health == HealthSection(
        mongo_connected=True,
        redis_expected=True,
        delivery_pubsub_connected=True,
        delivery_kv_connected=True,
        eventing_connected=True,
        redis_runtime_connected=True,
        agent_search_index_ready=True,
    )


@pytest.mark.parametrize("failure", ["raises", "hangs"])
@pytest.mark.parametrize("subsystem", ["delivery_facade", "eventing_bus", "mongo"])
async def test_a_failed_probe_clears_only_its_own_flags(
    monkeypatch, failure, subsystem
):
    monkeypatch.setattr(container, "_HEALTH_PROBE_SECONDS", 0.05)
    fakes = _fakes()
    probe = (
        fakes[subsystem].ping
        if subsystem == "mongo"
        else fakes[subsystem].refresh_health
    )
    setattr(probe, failure, True)

    health = await container.build_snapshot_sources(**fakes).health()

    flags = {
        "delivery_facade": {"delivery_pubsub_connected", "delivery_kv_connected"},
        "eventing_bus": {"eventing_connected"},
        "mongo": {"mongo_connected"},
    }
    all_flags = set().union(*flags.values()) | {"redis_runtime_connected"}
    for flag in all_flags:
        assert getattr(health, flag) is (flag not in flags[subsystem]), flag


async def test_in_memory_eventing_counts_as_connected_without_redis():
    # /health's rule: without Redis the in-memory bus needs no connection.
    fakes = _fakes(settings=Settings(redis_url=""))
    fakes["eventing_bus"].is_connected = False

    health = await container.build_snapshot_sources(**fakes).health()

    assert health.eventing_connected is True
    assert health.redis_expected is False


async def test_mongo_ping_false_is_not_connected():
    fakes = _fakes(mongo=SimpleNamespace(ping=_Probe(False)))

    health = await container.build_snapshot_sources(**fakes).health()

    assert health.mongo_connected is False


def test_provider_section():
    provider = container.build_snapshot_sources(**_fakes()).provider()

    assert provider == ProviderSection(
        state="credential_invalid", last_error="config_invalid"
    )


async def test_agents_returns_the_raw_visible_records():
    fakes = _fakes()

    records = await container.build_snapshot_sources(**fakes).agents()

    assert records == [{"agent_id": "a1"}]
    assert fakes["agent_repository"].kwargs == {
        "query": {"agent_status": {"$ne": "deleted"}},
        "exhaust": True,
    }


@pytest.mark.parametrize(("leader", "enabled"), [(object(), True), (None, False)])
def test_leader_section(leader, enabled):
    section = container.build_snapshot_sources(**_fakes(leader=leader)).leader()

    assert section.enabled is enabled
    assert section.instance_id == get_instance_id()


def test_eventing_counts_dead_letters_by_stage():
    section = container.build_snapshot_sources(**_fakes()).eventing()

    assert section == EventingSection(
        connected=True,
        transport="redis",
        dead_letter_count=3,
        dead_letter_capacity=1000,
        dead_letters_by_stage={"handler": 2, "publish": 1},
        last_dead_letter_at=T0 + timedelta(seconds=3),
    )


def test_eventing_without_transport_is_memory():
    fakes = _fakes()
    fakes["eventing_bus"].transport = None
    fakes["eventing_bus"].is_connected = False
    fakes["eventing_bus"].dead_letters = []

    section = container.build_snapshot_sources(**fakes).eventing()

    assert section.transport == "memory"
    # As /health: the in-memory bus needs no connection.
    assert section.connected is True
    assert section.last_dead_letter_at is None
    assert section.dead_letters_by_stage == {}


def test_sse_counts_rooms_and_connections():
    section = container.build_snapshot_sources(**_fakes()).sse()

    assert section == SseSection(rooms=2, connections=2)


def test_recovery_run_reads_the_job_and_cycle():
    run = container.build_snapshot_sources(**_fakes()).recovery_run()

    assert run == RecoveryRun(
        last_cycle_completed_at=T0, last_cycle_failed_phases=["dispatch"]
    )


@pytest.mark.parametrize(
    "cycle", [None, SimpleNamespace(last_failed_phases=None)], ids=["none", "unrun"]
)
def test_recovery_run_without_a_cycle_has_no_failed_phases(cycle):
    fakes = _fakes(
        recovery_cycle=cycle, recovery_job=SimpleNamespace(last_completed_at=None)
    )

    run = container.build_snapshot_sources(**fakes).recovery_run()

    assert run == RecoveryRun(last_cycle_completed_at=None, last_cycle_failed_phases=[])


async def test_canary_reading_with_breaches_and_non_terminal_runs():
    sources = container.build_snapshot_sources(**_fakes())

    assert sources.canary is not None
    reading = await sources.canary()

    assert reading.job_enabled is False
    assert reading.non_terminal_runs == 3
    assert reading.metrics.runs_by_status["completed"] == 9
    assert [breach.metric for breach in reading.breaches] == [
        "projection_outbox_blocked"
    ]


def test_canary_is_none_without_a_collector():
    sources = container.build_snapshot_sources(**_fakes(collect_canary=None))

    assert sources.canary is None


def test_store_section_with_a_sink(monkeypatch):
    ring = RingBufferSink(capacity=50)
    monkeypatch.setattr(container, "ring_buffer", lambda: ring)
    sink = SimpleNamespace(
        stats=lambda: {
            "ready": True,
            "queued": 2,
            "dropped": 1,
            "written": 9,
            "write_failures": 3,
            "last_error_type": "AutoReconnect",
            "last_write_at": T0,
        }
    )

    section = container.build_snapshot_sources(**_fakes(store_sink=sink)).store()

    assert section == StoreSection(
        ready=True,
        queued=2,
        dropped=1,
        written=9,
        write_failures=3,
        last_error_type="AutoReconnect",
        last_write_at=T0,
        ring_size=0,
        ring_capacity=50,
    )


def test_store_section_without_a_sink(monkeypatch):
    monkeypatch.setattr(container, "ring_buffer", lambda: None)

    section = container.build_snapshot_sources(**_fakes()).store()

    assert section.ready is False
    assert section.ring_capacity == 0


def test_lifespan_binds_and_resets_the_diagnostics_access():
    source = inspect.getsource(container._runtime_lifespan)

    assert source.count("app.state.diagnostics_access = None") == 2
    assert "app.state.diagnostics_access = create_diagnostics_access(" in source
    bind = source.index("app.state.diagnostics_access = create_diagnostics_access(")
    assert source.index("_collect_canary = collect_orchestrator_canary") < bind
    assert bind < source.index("except BaseException:")


def test_create_diagnostics_access_reads_the_handoff_token(tmp_path):
    handoff = tmp_path / "diagnostics.json"
    handoff.write_text(json.dumps({"token": "c" * 64}))
    settings = Settings(
        diagnostics_handoff_file=str(handoff),
        auth_mode="clerk",
        admin_user_ids=["admin"],
    )

    access = container.create_diagnostics_access(
        settings=settings,
        sources=container.build_snapshot_sources(**_fakes()),
        repository=_Repository(),
    )

    assert access.token_matches("c" * 64) is True
    assert access.can_read("admin") is True
    assert access.can_read("user") is False


# ── The token never leaves the backend ────────────────────────────────────


def test_the_diagnostics_token_is_a_public_secret_value(tmp_path):
    handoff = tmp_path / "diagnostics.json"
    handoff.write_text(json.dumps({"token": "d" * 64}))
    settings = Settings(diagnostics_handoff_file=str(handoff))

    assert "d" * 64 in configured_public_secret_values(settings)
    assert str(handoff) not in configured_public_secret_values(settings)


@pytest.mark.parametrize("content", [None, json.dumps({"token": "not-hex"})])
def test_an_unusable_handoff_file_adds_no_secret_value(tmp_path, content):
    handoff = tmp_path / "diagnostics.json"
    if content is not None:
        handoff.write_text(content)
    settings = Settings(diagnostics_handoff_file=str(handoff))

    assert configured_public_secret_values(settings) == (
        configured_public_secret_values(Settings())
    )


def test_startup_completed_carries_neither_the_token_nor_the_handoff_path(
    tmp_path, caplog
):
    handoff = tmp_path / "diagnostics.json"
    handoff.write_text(json.dumps({"token": "e" * 64}))
    runtime = container.ApplicationRuntime(
        settings=Settings(diagnostics_handoff_file=str(handoff))
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_runtime=SimpleNamespace(
                current=SimpleNamespace(state="unconfigured")
            )
        )
    )
    caplog.set_level(logging.INFO)

    container.log_startup_completed(app, runtime)

    [record] = [r for r in caplog.records if r.getMessage() == "startup_completed"]
    line = StructuredFormatter(output_format="json", environment="t").format(record)
    assert "e" * 64 not in line
    assert "diagnostics.json" not in line
