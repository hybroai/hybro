from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.facade import AgentFacade
from common.dto import AgentCardSnapshot, LocalAgentUpsertResult
from local_agents.card_probe import LocalAgentCardProbe
from local_agents.config import LocalAgentDiscoveryConfig
from local_agents.models import DiscoveryTrigger
from local_agents.port_scanner import HostPortScanner
from local_agents.service import LocalAgentService


def test_local_agents_keeps_a2a_sdk_behind_adapter_boundary():
    root = Path(__file__).resolve().parents[1] / "local_agents"
    violations: list[str] = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module == "a2a" or module.startswith("a2a."):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def _card(url: str = "http://localhost:9001") -> AgentCardSnapshot:
    return AgentCardSnapshot(
        agent_id="local-card",
        name="Local Writer",
        description="Writes locally",
        url=url,
        capabilities=["streaming"],
        raw_card={
            "name": "Local Writer",
            "description": "Writes locally",
            "url": url,
        },
    )


class Scanner:
    def __init__(self, ports: list[int] | None = None) -> None:
        self.ports = ports or []
        self.calls = 0

    async def scan_open_ports(self) -> list[int]:
        self.calls += 1
        return self.ports


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, AgentCardSnapshot]] = []

    async def probe_agent_cards(self, ports: list[int]):
        del ports
        return self.results


class Writer:
    def __init__(self) -> None:
        self.local_ids: list[str] = []
        self.inactive: list[str] = []

    async def upsert_local_agent(self, discovery_url, card):
        del discovery_url, card
        if "local-1" not in self.local_ids:
            self.local_ids.append("local-1")
            return LocalAgentUpsertResult(agent_id="local-1", managed=True, added=True)
        return LocalAgentUpsertResult(agent_id="local-1", managed=True)

    async def list_local_agent_ids(self):
        return list(self.local_ids)

    async def mark_local_agents_inactive(self, agent_ids):
        newly_inactive = [item for item in agent_ids if item not in self.inactive]
        self.inactive.extend(newly_inactive)
        return len(newly_inactive)


def _service(scanner: Scanner, probe: Probe, writer: Writer) -> LocalAgentService:
    return LocalAgentService(
        config=LocalAgentDiscoveryConfig(enabled=True, port_start=9001, port_end=9001),
        scanner=scanner,
        card_probe=probe,
        writer=writer,
    )


@pytest.mark.asyncio
async def test_host_port_scanner_finds_a_listening_port():
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    scanner = HostPortScanner(
        host="127.0.0.1",
        port_start=port,
        port_end=port,
        connect_timeout_seconds=5.0,
        excluded_ports=frozenset(),
    )
    try:
        assert await scanner.scan_open_ports() == [port]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_discovery_adds_then_deactivates_after_three_successful_misses():
    scanner = Scanner([9001])
    probe = Probe()
    probe.results = [("http://host.docker.internal:9001", _card())]
    writer = Writer()
    service = _service(scanner, probe, writer)

    first = await service.request_discovery(DiscoveryTrigger.MANUAL)
    assert first.agents_added == 1
    assert first.agents_found == 1

    probe.results = []
    for _ in range(2):
        result = await service.request_discovery(DiscoveryTrigger.SCHEDULED)
        assert result.agents_deactivated == 0

    result = await service.request_discovery(DiscoveryTrigger.SCHEDULED)
    assert result.agents_deactivated == 1
    assert writer.inactive == ["local-1"]


@pytest.mark.asyncio
async def test_manual_discovery_immediately_deactivates_missing_local_agents():
    writer = Writer()
    writer.local_ids = ["stale-local"]
    service = _service(Scanner(), Probe(), writer)

    result = await service.request_discovery(DiscoveryTrigger.MANUAL)

    assert result.agents_deactivated == 1
    assert writer.inactive == ["stale-local"]


@pytest.mark.asyncio
async def test_successful_discovery_resets_consecutive_misses():
    scanner = Scanner([9001])
    probe = Probe()
    probe.results = [("http://host.docker.internal:9001", _card())]
    writer = Writer()
    service = _service(scanner, probe, writer)

    await service.request_discovery(DiscoveryTrigger.MANUAL)
    probe.results = []
    for _ in range(2):
        await service.request_discovery(DiscoveryTrigger.SCHEDULED)

    probe.results = [("http://host.docker.internal:9001", _card())]
    await service.request_discovery(DiscoveryTrigger.SCHEDULED)
    probe.results = []
    for _ in range(2):
        result = await service.request_discovery(DiscoveryTrigger.SCHEDULED)
        assert result.agents_deactivated == 0

    result = await service.request_discovery(DiscoveryTrigger.SCHEDULED)
    assert result.agents_deactivated == 1


@pytest.mark.asyncio
async def test_card_probe_rejects_card_that_redirects_dispatch_off_host():
    class CardResolver:
        async def resolve_card(self, url):
            port = int(url.rsplit(":", 1)[1])
            if port == 9001:
                return _card("http://localhost:9001/a2a")
            return _card("http://private.example:9002/a2a")

    probe = LocalAgentCardProbe(
        host="host.docker.internal",
        resolver=CardResolver(),
    )

    discovered = await probe.probe_agent_cards([9001, 9002])

    assert [(url, card.url) for url, card in discovered] == [
        ("http://host.docker.internal:9001", "http://localhost:9001/a2a")
    ]


@pytest.mark.asyncio
async def test_discovery_reuses_an_inflight_cycle():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingScanner(Scanner):
        async def scan_open_ports(self):
            self.calls += 1
            started.set()
            await release.wait()
            return []

    scanner = BlockingScanner()
    service = _service(scanner, Probe(), Writer())

    first_task = asyncio.create_task(
        service.request_discovery(DiscoveryTrigger.SCHEDULED)
    )
    await started.wait()
    second_task = asyncio.create_task(
        service.request_discovery(DiscoveryTrigger.MANUAL)
    )
    release.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert first.trigger == DiscoveryTrigger.MANUAL
    assert first.reused_running_discovery is False
    assert second.reused_running_discovery is True
    assert second.trigger == DiscoveryTrigger.MANUAL
    assert scanner.calls == 1


class FacadeRepository:
    def __init__(self, existing: dict | None = None) -> None:
        self.docs = {existing["agent_id"]: existing} if existing else {}

    async def get_by_id(self, agent_id):
        return self.docs.get(agent_id)

    async def find_by_normalized_url(self, normalized_url, provider_id=None):
        del provider_id
        return next(
            (
                doc
                for doc in self.docs.values()
                if doc.get("normalized_url") == normalized_url
            ),
            None,
        )

    async def upsert(self, agent_id, data):
        self.docs[agent_id] = data

    async def update(self, agent_id, updates):
        self.docs[agent_id].update(updates)
        return self.docs[agent_id]

    async def get_by_source(self, source):
        return [doc for doc in self.docs.values() if doc.get("source") == source]

    async def mark_agents_inactive(self, agent_ids, *, source):
        count = 0
        for agent_id in agent_ids:
            doc = self.docs.get(agent_id)
            if (
                doc
                and doc.get("source") == source
                and doc.get("agent_status") != "inactive"
            ):
                doc["agent_status"] = "inactive"
                count += 1
        return count


class Resolver:
    async def resolve_card(self, url):
        del url
        return None


def _facade(repository: FacadeRepository) -> AgentFacade:
    return AgentFacade(
        repository=repository,
        card_resolver=Resolver(),
        id_factory=lambda: "unused",
        now=lambda: datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_manual_registration_wins_over_discovered_url():
    existing = {
        "agent_id": "manual-1",
        "provider_id": "owner",
        "source": "cloud",
        "normalized_url": "http://localhost:9001",
        "agent_status": "inactive",
        "agent_card": {"name": "Manual", "url": "http://localhost:9001"},
    }
    repository = FacadeRepository(existing)

    result = await _facade(repository).upsert_local_agent(
        "http://host.docker.internal:9001",
        _card(),
    )

    assert result.managed is False
    assert result.agent_id == "manual-1"
    assert repository.docs["manual-1"] == existing


@pytest.mark.asyncio
async def test_local_agent_fills_empty_advertised_url_with_discovery_endpoint():
    repository = FacadeRepository()
    facade = _facade(repository)
    card = AgentCardSnapshot(
        agent_id="empty-url-card",
        name="Empty URL",
        description="",
        url="http://host.docker.internal:9001",
        raw_card={"name": "Empty URL", "description": "", "url": ""},
    )

    result = await facade.upsert_local_agent(
        "http://host.docker.internal:9001",
        card,
    )

    assert repository.docs[result.agent_id]["agent_card"]["url"] == card.url


@pytest.mark.asyncio
async def test_local_agent_id_is_stable_and_reactivation_preserves_record():
    repository = FacadeRepository()
    facade = _facade(repository)

    first = await facade.upsert_local_agent(
        "http://host.docker.internal:9001",
        _card(),
    )
    repository.docs[first.agent_id]["agent_status"] = "inactive"
    second = await facade.upsert_local_agent(
        "http://host.docker.internal:9001",
        _card(),
    )

    assert first.agent_id == second.agent_id
    assert first.added is True
    assert second.reactivated is True
    assert repository.docs[first.agent_id]["source"] == "local"
    assert repository.docs[first.agent_id]["is_public"] is True
    assert await facade.is_directly_callable(first.agent_id) is True
