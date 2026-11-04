"""The agent package emits catalog events with ids, never exception text."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent.catalog import AgentCatalogService
from agent.health import AgentHealthService
from agent.inspection import AgentInspectionService
from agent.liveness import AgentLivenessService
from agent.repository.documents import _safe_parse_agent, _safe_parse_agent_group
from agent.repository.runtime_store import AgentRuntimeStore
from agent.selection_service import AgentSelectionService
from common.dto import AgentInfo
from common.types import AgentCard
from models.agent import Agent, AgentStatus
from models.agent_group import AgentGroup
from models.error import AgentNotFoundError
from models.request import InspectionCenterRequest
from tests.test_agent_facade import Repository, _doc, _facade
from tests.test_bundled_agents import ECHO, Resolver, card, service

SECRET = "secret-detail-must-not-be-logged"


class _Failed(RuntimeError):
    pass


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("agent.") and record.getMessage() == event
    ]


def _one(caplog, event: str) -> logging.LogRecord:
    [record] = _events(caplog, event)
    return record


def _assert_no_secret(caplog) -> None:
    for record in caplog.records:
        assert record.exc_info is None
        assert SECRET not in repr(record.__dict__)


async def _raise(*_args, **_kwargs):
    raise _Failed(SECRET)


def _agent(agent_id: str = "agent-1", status: AgentStatus = AgentStatus.active):
    return Agent(
        agent_id=agent_id,
        is_public=True,
        agent_status=status,
        agent_card=AgentCard(
            name="Health Agent",
            description="Checks health",
            url="https://agent.example",
            version="1.0.0",
            capabilities={},
            defaultInputModes=["text"],
            defaultOutputModes=["text"],
            skills=[],
        ),
    )


class _HealthRepo:
    def __init__(self, agents=(), *, fail: bool = False) -> None:
        self.agents = list(agents)
        self.fail = fail

    async def list_visible(self, *, query=None, **kwargs):
        del query, kwargs
        if self.fail:
            raise _Failed(SECRET)
        return self.agents

    async def get_by_id(self, agent_id):
        if self.fail:
            raise _Failed(SECRET)
        return next(a for a in self.agents if a.agent_id == agent_id)

    async def update(self, agent_id, updates):
        if self.fail:
            raise _Failed(SECRET)
        return {}

    async def update_where(self, agent_id, conditions, updates):
        if self.fail:
            raise _Failed(SECRET)
        return True


def _probe(monkeypatch, *, healthy: bool, status_code: int | None = None):
    async def probe(agent_url, *, timeout):
        del agent_url, timeout
        return SimpleNamespace(
            is_healthy=healthy, card=None, status_code=status_code, error=None
        )

    monkeypatch.setattr("agent.health.probe_agent_card_for_health", probe)


# ── health ──


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [503, None])
async def test_unhealthy_probe_emits_agent_health_probe_failed(
    caplog, monkeypatch, status_code
):
    caplog.set_level(logging.DEBUG)
    _probe(monkeypatch, healthy=False, status_code=status_code)

    await AgentHealthService(repository=_HealthRepo()).check_agent_health(
        "agent-1", "https://agent.example", "Agent"
    )

    record = _one(caplog, "agent_health_probe_failed")
    assert record.levelno == logging.WARNING
    assert record.agent_id == "agent-1"
    assert record.status_code == status_code


@pytest.mark.asyncio
async def test_health_check_error_emits_agent_health_check_failed(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("agent.health.probe_agent_card_for_health", _raise)

    await AgentHealthService(repository=_HealthRepo()).check_agent_health(
        "agent-1", "https://agent.example", "Agent"
    )

    record = _one(caplog, "agent_health_check_failed")
    assert record.levelno == logging.ERROR
    assert record.agent_id == "agent-1"
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_card_sync_error_emits_agent_card_sync_failed(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(repository=_HealthRepo(fail=True))

    await service.sync_agent_card("agent-1", {}, _agent().agent_card)

    record = _one(caplog, "agent_card_sync_failed")
    assert record.levelno == logging.WARNING
    assert record.agent_id == "agent-1"
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_status_write_errors_emit_agent_status_update_failed(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(repository=_HealthRepo(fail=True))

    assert await service.update_agent_status("agent-1", AgentStatus.inactive) is False
    assert await service.reactivate_agent("agent-1") is False

    records = _events(caplog, "agent_status_update_failed")
    assert [(r.agent_id, r.agent_status) for r in records] == [
        ("agent-1", "inactive"),
        ("agent-1", "active"),
    ]
    assert {r.levelno for r in records} == {logging.ERROR}
    assert {r.error_type for r in records} == {"_Failed"}
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_health_cycle_deactivates_after_consecutive_failures(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    _probe(monkeypatch, healthy=False, status_code=500)
    service = AgentHealthService(
        repository=_HealthRepo([_agent()]), max_consecutive_failures=1
    )

    await service.run_health_check_cycle()

    started = _one(caplog, "agent_health_cycle_started")
    assert started.agent_count == 1
    record = _one(caplog, "agent_deactivated")
    assert record.levelno == logging.WARNING
    assert record.agent_id == "agent-1"
    assert record.trigger == "health_check"
    assert record.consecutive_failure_count == 1


@pytest.mark.asyncio
async def test_health_cycle_reactivates_recovered_agent(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    _probe(monkeypatch, healthy=True, status_code=200)
    service = AgentHealthService(
        repository=_HealthRepo([_agent(status=AgentStatus.inactive)])
    )

    await service.run_health_check_cycle()

    record = _one(caplog, "agent_reactivated")
    assert record.levelno == logging.INFO
    assert record.agent_id == "agent-1"
    assert record.trigger == "health_check"


@pytest.mark.asyncio
async def test_health_cycle_error_emits_agent_health_cycle_failed(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(repository=_HealthRepo(fail=True))

    await service.run_health_check_cycle()

    record = _one(caplog, "agent_health_cycle_failed")
    assert record.levelno == logging.ERROR
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_health_retry_error_emits_agent_health_retry_failed(caplog):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(
        repository=_HealthRepo(fail=True), initial_retry_delay=0
    )
    service._running = True
    service._failure_counts["agent-1"] = 1

    await service._retry_agent_check("agent-1")

    scheduled = _one(caplog, "agent_health_retry_scheduled")
    assert scheduled.agent_id == "agent-1"
    assert scheduled.consecutive_failure_count == 1
    record = _one(caplog, "agent_health_retry_failed")
    assert record.levelno == logging.ERROR
    assert record.agent_id == "agent-1"
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


# ── liveness ──


def _info(**overrides) -> AgentInfo:
    values = {
        "agent_id": "a1",
        "name": "Weather",
        "url": "https://agent.example/a2a",
        "provider_id": "owner",
        "raw_card": {"name": "Weather", "url": "https://agent.example/a2a"},
    }
    values.update(overrides)
    return AgentInfo(**values)


def _health(is_healthy: bool, *, fail: bool = False) -> MagicMock:
    check = AsyncMock(side_effect=_Failed(SECRET) if fail else None)
    if not fail:
        check.return_value = (is_healthy, None)
    return MagicMock(
        check_agent_health=check,
        sync_agent_card=AsyncMock(),
        update_agent_status=AsyncMock(return_value=True),
        reactivate_agent=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_liveness_probe_error_emits_agent_liveness_probe_failed(caplog):
    caplog.set_level(logging.DEBUG)

    await AgentLivenessService(health_service=_health(False, fail=True))(_info())

    record = _one(caplog, "agent_liveness_probe_failed")
    assert record.levelno == logging.WARNING
    assert record.agent_id == "a1"
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_liveness_transitions_emit_deactivated_and_reactivated(caplog):
    caplog.set_level(logging.DEBUG)

    await AgentLivenessService(health_service=_health(False))(_info())
    await AgentLivenessService(health_service=_health(True))(_info(status="inactive"))

    deactivated = _one(caplog, "agent_deactivated")
    assert (deactivated.agent_id, deactivated.trigger) == ("a1", "liveness")
    assert deactivated.levelno == logging.WARNING
    reactivated = _one(caplog, "agent_reactivated")
    assert (reactivated.agent_id, reactivated.trigger) == ("a1", "liveness")
    assert reactivated.levelno == logging.INFO


# ── bundled ──


class _FailingResolver:
    async def resolve_card(self, agent_url):
        raise _Failed(SECRET)


@pytest.mark.asyncio
async def test_bundled_registration_error_emits_registration_failed(caplog):
    caplog.set_level(logging.DEBUG)
    bundled, _collection, _availability = service()
    bundled._card_resolver = _FailingResolver()
    bundled._max_retry_seconds = 0.01

    task = bundled._spawn(bundled._register_when_ready(ECHO), "test")
    for _ in range(100):
        if _events(caplog, "bundled_agent_registration_failed"):
            break
        await asyncio.sleep(0.005)
    task.cancel()

    record = _events(caplog, "bundled_agent_registration_failed")[0]
    assert record.levelno == logging.WARNING
    assert record.bundled_name == "echo_agent"
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_bundled_reconcile_emits_registered_and_url_taken(caplog):
    caplog.set_level(logging.DEBUG)
    bundled, _collection, _availability = service(resolver=Resolver())

    agent_id = await bundled.reconcile(ECHO, card(ECHO.name, ECHO.compose_url))

    registered = _one(caplog, "bundled_agent_registered")
    assert (registered.bundled_name, registered.agent_id) == ("echo_agent", agent_id)

    holder = {
        "agent_id": "local-1",
        "source": "local",
        "normalized_url": "http://localhost:7004",
        "agent_card": {"name": "Echo", "url": "http://127.0.0.1:7004"},
    }
    taken, _collection, _availability = service([holder])
    caplog.clear()
    assert await taken.reconcile(ECHO, card(ECHO.name, ECHO.url)) is None

    record = _one(caplog, "bundled_agent_url_taken")
    assert record.levelno == logging.WARNING
    assert record.bundled_name == "echo_agent"
    assert record.agent_id == "local-1"
    assert record.holder_source == "local"


# ── catalog ──


@pytest.mark.asyncio
async def test_catalog_failures_emit_events_without_messages(caplog):
    caplog.set_level(logging.DEBUG)
    facade = MagicMock(
        register_agent=AsyncMock(side_effect=_Failed(SECRET)),
        list_visible_agents=AsyncMock(side_effect=_Failed(SECRET)),
    )
    catalog = AgentCatalogService(facade, liveness_checker=AsyncMock())

    await catalog.register(agent_url="https://agent.example", provider_id="owner")
    await catalog.list_visible(user_id="user-1", active_only=True)

    registration = _one(caplog, "agent_registration_failed")
    assert registration.levelno == logging.ERROR
    assert registration.provider_id == "owner"
    assert registration.error_type == "_Failed"
    listing = _one(caplog, "visible_agents_list_failed")
    assert listing.levelno == logging.ERROR
    assert (listing.user_id, listing.active_only) == ("user-1", True)
    assert listing.error_type == "_Failed"
    _assert_no_secret(caplog)


# ── inspection ──


@pytest.mark.asyncio
async def test_inspection_failures_emit_agent_inspection_failed(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def connect_error(*_args, **_kwargs):
        raise httpx.ConnectError(SECRET)

    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.fetch_agent_card_for_inspection",
        connect_error,
    )
    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.inspect_a2a_connection", _raise
    )
    request = InspectionCenterRequest(agent_url="https://agent.example")
    inspection = AgentInspectionService()

    await inspection.inspect_agent_card(request)
    await inspection.inspect_a2a_connection(request)

    records = _events(caplog, "agent_inspection_failed")
    assert [(r.inspection, r.error_type) for r in records] == [
        ("agent_card", "ConnectError"),
        ("a2a_connection", "_Failed"),
    ]
    assert {r.levelno for r in records} == {logging.ERROR}
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_inspection_without_url_emits_request_rejected(caplog):
    caplog.set_level(logging.DEBUG)
    request = SimpleNamespace(agent_url=None)

    with pytest.raises(AgentNotFoundError):
        await AgentInspectionService().inspect_agent_card(request)

    record = _one(caplog, "agent_inspection_request_rejected")
    assert record.levelno == logging.WARNING
    assert record.inspection == "agent_card"


# ── repository ──


def test_invalid_documents_emit_agent_document_parse_failed(caplog):
    caplog.set_level(logging.DEBUG)

    assert _safe_parse_agent({"agent_id": "a1", "agent_card": SECRET}) is None
    assert _safe_parse_agent_group({"group_id": "g1", "name": None}) is None

    records = _events(caplog, "agent_document_parse_failed")
    assert [(r.document_kind, r.levelno) for r in records] == [
        ("agent", logging.WARNING),
        ("agent_group", logging.WARNING),
    ]
    assert records[0].agent_id == "a1"
    assert records[1].group_id == "g1"
    assert {r.error_type for r in records} == {"ValidationError"}
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_runtime_store_errors_emit_operation_failed(caplog):
    caplog.set_level(logging.DEBUG)
    groups = MagicMock(insert_one=AsyncMock(side_effect=_Failed(SECRET)))
    agents = MagicMock(
        increment_agent_call_count=AsyncMock(side_effect=_Failed(SECRET))
    )
    store = AgentRuntimeStore(agent_groups=groups, agent_repository=agents)

    group = AgentGroup(group_id="g1", name="G", type="user")
    assert await store.add_agent_group(group) is False
    await store.increment_agent_call_count("a1", success=True)

    records = _events(caplog, "agent_runtime_store_operation_failed")
    assert [(r.operation, r.levelno) for r in records] == [
        ("add_agent_group", logging.ERROR),
        ("increment_agent_call_count", logging.ERROR),
    ]
    assert records[0].group_id == "g1"
    assert records[1].agent_id == "a1"
    assert {r.error_type for r in records} == {"_Failed"}
    _assert_no_secret(caplog)


# ── facade and selection ──


@pytest.mark.asyncio
async def test_text_search_error_emits_agent_text_search_failed(caplog):
    caplog.set_level(logging.DEBUG)

    class BrokenRepository(Repository):
        async def text_search(self, agent_ids, query, limit):
            raise _Failed(SECRET)

    repo = BrokenRepository([_doc("writer", "Writer"), _doc("other", "Other")])
    await _facade(repo).match_agents("writer")

    record = _one(caplog, "agent_text_search_failed")
    assert record.levelno == logging.WARNING
    assert record.candidate_count == 2
    assert record.error_type == "_Failed"
    _assert_no_secret(caplog)


def _matched(agent_id: str):
    return SimpleNamespace(
        agent=_info(agent_id=agent_id), lexical_score=0.5, final_score=0.5
    )


@pytest.mark.asyncio
async def test_selection_rerank_error_emits_rerank_failed(caplog):
    caplog.set_level(logging.DEBUG)
    matcher = MagicMock(
        match=AsyncMock(
            return_value=SimpleNamespace(
                agents=[_matched("a1"), _matched("a2")], total_candidates=4
            )
        )
    )
    reranker = MagicMock(rank_agents_for_task=AsyncMock(side_effect=_Failed(SECRET)))

    await AgentSelectionService(
        matcher=matcher, llm_reranker=reranker
    ).select_agents_for_message(SECRET, user_id="user-1")

    record = _one(caplog, "agent_selection_rerank_failed")
    assert record.levelno == logging.WARNING
    assert record.candidate_count == 2
    assert record.error_type == "_Failed"
    completed = _one(caplog, "agent_selection_completed")
    assert completed.levelno == logging.INFO
    assert completed.selected_agent_count == 2
    assert completed.candidate_count == 4
    assert completed.user_id == "user-1"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_selection_without_matches_completes_at_warning(caplog):
    caplog.set_level(logging.DEBUG)
    matcher = MagicMock(
        match=AsyncMock(return_value=SimpleNamespace(agents=[], total_candidates=3))
    )

    await AgentSelectionService(matcher=matcher).select_agents_for_message(
        SECRET, user_id=None
    )

    started = _one(caplog, "agent_selection_started")
    assert started.message_char_count == len(SECRET)
    record = _one(caplog, "agent_selection_completed")
    assert record.levelno == logging.WARNING
    assert record.selected_agent_count == 0
    assert record.candidate_count == 3
    _assert_no_secret(caplog)


# ── transitions follow only a successful write ──


class _NoWriteRepo(_HealthRepo):
    async def update_where(self, agent_id, conditions, updates):
        return False


@pytest.mark.asyncio
async def test_health_cycle_without_status_write_emits_no_transition(
    caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    service = AgentHealthService(
        repository=_HealthRepo([_agent()]), max_consecutive_failures=1
    )
    monkeypatch.setattr(service, "update_agent_status", AsyncMock(return_value=False))
    _probe(monkeypatch, healthy=False, status_code=500)

    await service.run_health_check_cycle()

    _probe(monkeypatch, healthy=True, status_code=200)
    service._repository = _NoWriteRepo([_agent(status=AgentStatus.inactive)])
    await service.run_health_check_cycle()

    assert _events(caplog, "agent_deactivated") == []
    assert _events(caplog, "agent_reactivated") == []


@pytest.mark.asyncio
async def test_liveness_without_status_write_emits_no_transition(caplog):
    caplog.set_level(logging.DEBUG)
    down = _health(False)
    down.update_agent_status.return_value = False
    up = _health(True)
    up.reactivate_agent.return_value = False

    await AgentLivenessService(health_service=down)(_info())
    await AgentLivenessService(health_service=up)(_info(status="inactive"))

    assert _events(caplog, "agent_deactivated") == []
    assert _events(caplog, "agent_reactivated") == []
