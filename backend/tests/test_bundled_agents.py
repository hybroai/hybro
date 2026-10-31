"""Bundled agents: boot registration, reconciliation, and availability gating.

Agent cards come from a local fake resolver and records live in the fake Mongo
collection; nothing reaches an agent, a database, or a model Provider.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.bundled import (
    BundledAgent,
    BundledAgentService,
    ProviderAvailability,
    inactive_reason,
    load_manifest,
)
from agent.health import AgentHealthService
from agent.liveness import AgentLivenessService
from agent.translators import agent_info_from_doc
from agent.views import agent_view
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore
from common.dto.agent import AgentCardSnapshot
from llm_gateway.provider_runtime import ProviderRuntime
from tests.test_agent_repository import _repo

REPO_MANIFEST = Path(__file__).resolve().parents[2] / "default_agents" / "agents.yaml"
NOW = datetime(2026, 10, 1, tzinfo=UTC)
ECHO = BundledAgent("echo_agent", "echo-agent", 7004, "none")
WEATHER = BundledAgent("weather_agent", "weather-agent", 7001, "text")
IMAGE = BundledAgent("image_generator_agent", "image-generator-agent", 6002, "image")
AGENTS = (ECHO, WEATHER, IMAGE)

UNCONFIGURED = ProviderAvailability(configured=False, image_model=False)
TEXT_ONLY = ProviderAvailability(configured=True, image_model=False)
TEXT_AND_IMAGE = ProviderAvailability(configured=True, image_model=True)


def card(name: str, url: str) -> AgentCardSnapshot:
    return AgentCardSnapshot(
        agent_id="",
        url=url,
        name=name,
        description=f"{name} description",
        capabilities=[name],
        raw_card={
            "name": name,
            "description": f"{name} description",
            "version": "1.0.0",
            "capabilities": {},
            "skills": [{"id": name, "name": name, "tags": []}],
        },
    )


class Resolver:
    """Serves a card for every URL in ``up``; records each request."""

    def __init__(self, up: set[str] | None = None) -> None:
        self.up = up if up is not None else set()
        self.requests: list[str] = []

    async def resolve_card(self, agent_url: str) -> AgentCardSnapshot | None:
        self.requests.append(agent_url)
        return card(agent_url, agent_url) if agent_url in self.up else None


class Availability:
    def __init__(self, value: ProviderAvailability) -> None:
        self.value = value

    def __call__(self) -> ProviderAvailability:
        return self.value


def service(
    docs=None,
    *,
    available: ProviderAvailability = UNCONFIGURED,
    resolver: Resolver | None = None,
    agents=AGENTS,
):
    repository, collection = _repo(docs)
    ids = (f"agent-{n}" for n in itertools.count(1))
    availability = Availability(available)
    bundled = BundledAgentService(
        repository=repository,
        card_resolver=resolver or Resolver(),
        agents=agents,
        provider_id="Hybro AI",
        availability=availability,
        id_factory=lambda: next(ids),
        now=lambda: NOW,
        retry_seconds=0.01,
        max_retry_seconds=0.01,
    )
    return bundled, collection, availability


async def reconcile_all(bundled: BundledAgentService):
    return [
        await bundled.reconcile(agent, card(agent.name, agent.compose_url))
        for agent in AGENTS
    ]


def by_name(collection) -> dict[str, dict]:
    return {doc.get("bundled_name") or doc["agent_id"]: doc for doc in collection.docs}


def compose_record(agent: BundledAgent, agent_id: str) -> dict:
    """A record the removed Compose registrar created."""
    return {
        "agent_id": agent_id,
        "provider_id": "Hybro AI",
        "agent_card": {"name": agent.name, "url": f"{agent.compose_url}/"},
        "normalized_url": agent.compose_url,
        "public_url": "https://legacy.hybro.ai",
        "agent_status": "active",
        "is_public": True,
        "source": "cloud",
        "call_count": 7,
    }


# ── Manifest ──


def test_repository_manifest_lists_every_sample_agent_with_its_requirement():
    agents = {agent.name: agent for agent in load_manifest(REPO_MANIFEST)}

    assert {name: agent.requires for name, agent in agents.items()} == {
        "echo_agent": "none",
        "weather_agent": "text",
        "image_generator_agent": "image",
        "travel_planner_agent": "text",
        "story_agent": "text",
    }
    assert agents["echo_agent"].url == "http://127.0.0.1:7004"
    assert agents["echo_agent"].compose_url == "http://echo-agent:7004"


def test_manifest_skips_disabled_agents_and_rejects_invalid_entries(tmp_path):
    manifest = tmp_path / "agents.yaml"
    manifest.write_text(
        "agents:\n"
        "  kept: {dir: a, service: a, port: 1, requires: none}\n"
        "  skipped: {dir: b, service: b, port: 2, requires: text, enabled: false}\n"
    )
    assert [agent.name for agent in load_manifest(manifest)] == ["kept"]

    manifest.write_text(
        "agents:\n  a: {dir: a, service: a, port: 1, requires: video}\n"
    )
    with pytest.raises(ValueError, match="requires"):
        load_manifest(manifest)
    manifest.write_text("agents:\n  a: {dir: a, service: a, port: 0, requires: none}\n")
    with pytest.raises(ValueError, match="port"):
        load_manifest(manifest)
    with pytest.raises(ValueError, match="unreadable"):
        load_manifest(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("requires", "available", "reason"),
    [
        ("none", UNCONFIGURED, None),
        ("text", UNCONFIGURED, "provider_not_configured"),
        ("image", UNCONFIGURED, "provider_not_configured"),
        ("text", TEXT_ONLY, None),
        ("image", TEXT_ONLY, "image_model_not_configured"),
        ("image", TEXT_AND_IMAGE, None),
    ],
)
def test_inactive_reason_follows_the_requirement(requires, available, reason):
    assert inactive_reason(requires, available) == reason


# ── Reconciliation ──


async def test_reconcile_from_empty_creates_one_gated_record_per_agent_and_converges():
    bundled, collection, _ = service()

    first = await reconcile_all(bundled)
    second = await reconcile_all(bundled)

    assert first == second == ["agent-1", "agent-2", "agent-3"]
    records = by_name(collection)
    assert set(records) == {"echo_agent", "weather_agent", "image_generator_agent"}
    weather = records["weather_agent"]
    assert weather["source"] == "bundled"
    assert weather["provider_id"] == "Hybro AI"
    assert weather["normalized_url"] == "http://localhost:7001"
    assert weather["agent_card"]["url"] == "http://localhost:7001"
    assert (weather["agent_status"], weather["inactive_reason"]) == (
        "inactive",
        "provider_not_configured",
    )
    echo = records["echo_agent"]
    assert (echo["agent_status"], echo["inactive_reason"]) == ("active", None)


async def test_reconcile_takes_over_compose_registrar_records_in_place():
    docs = [
        compose_record(WEATHER, "legacy-weather"),
        compose_record(ECHO, "legacy-echo"),
    ]
    bundled, collection, _ = service(docs, available=TEXT_ONLY)

    await reconcile_all(bundled)
    await reconcile_all(bundled)

    records = by_name(collection)
    assert len(collection.docs) == 3
    weather = records["weather_agent"]
    # The agent_id is kept, so room history still resolves the agent.
    assert weather["agent_id"] == "legacy-weather"
    assert weather["source"] == "bundled"
    assert weather["normalized_url"] == "http://localhost:7001"
    assert weather["agent_card"]["url"] == "http://localhost:7001"
    assert weather["call_count"] == 7
    assert (weather["agent_status"], weather["inactive_reason"]) == ("active", None)
    assert records["echo_agent"]["agent_id"] == "legacy-echo"
    assert records["image_generator_agent"]["inactive_reason"] == (
        "image_model_not_configured"
    )


async def test_local_record_at_a_matching_url_is_never_taken_over():
    url = "http://localhost:7001"
    local = {
        "agent_id": "host-agent",
        "provider_id": None,
        "agent_card": {"name": "Someone's agent", "url": url},
        "normalized_url": url,
        "agent_status": "active",
        "is_public": True,
        "source": "local",
    }
    bundled, collection, _ = service([local])

    assert await bundled.reconcile(WEATHER, card("w", url)) is None

    assert collection.docs == [local]


async def test_registration_runs_in_the_background_until_the_card_appears():
    resolver = Resolver()
    bundled, collection, _ = service(resolver=resolver, agents=(ECHO,))

    bundled.start()  # Returns at once: health and startup never wait for it.
    assert collection.docs == []
    while len(resolver.requests) < 3:
        await asyncio.sleep(0.005)
    assert collection.docs == []

    resolver.up.add(ECHO.url)
    for _ in range(200):
        if collection.docs:
            break
        await asyncio.sleep(0.005)
    assert [doc["bundled_name"] for doc in collection.docs] == ["echo_agent"]
    await bundled.stop()


async def test_stop_cancels_registration_that_never_finds_a_card():
    bundled, _, _ = service(agents=(ECHO, WEATHER))
    bundled.start()
    await asyncio.sleep(0.02)

    await asyncio.wait_for(bundled.stop(), timeout=1)


# ── Gating ──


async def test_gating_follows_provider_changes():
    bundled, collection, availability = service()
    await reconcile_all(bundled)

    def states():
        return {
            name: (doc["agent_status"], doc["inactive_reason"])
            for name, doc in by_name(collection).items()
        }

    availability.value = TEXT_ONLY
    await bundled.apply_gating()
    assert states() == {
        "echo_agent": ("active", None),
        "weather_agent": ("active", None),
        "image_generator_agent": ("inactive", "image_model_not_configured"),
    }

    availability.value = TEXT_AND_IMAGE
    await bundled.apply_gating()
    assert states()["image_generator_agent"] == ("active", None)

    # An invalid credential gates like a missing Provider.
    availability.value = UNCONFIGURED
    await bundled.apply_gating()
    assert states() == {
        "echo_agent": ("active", None),
        "weather_agent": ("inactive", "provider_not_configured"),
        "image_generator_agent": ("inactive", "provider_not_configured"),
    }


async def test_gating_leaves_a_health_check_status_alone():
    bundled, collection, _ = service(available=TEXT_ONLY)
    await reconcile_all(bundled)
    weather = by_name(collection)["weather_agent"]
    weather["agent_status"] = "inactive"  # Unreachable, not gated.

    await bundled.apply_gating()

    assert by_name(collection)["weather_agent"]["agent_status"] == "inactive"


async def test_provider_pair_swap_regates_through_the_runtime_listener(tmp_path):
    from container import create_bundled_agent_service

    store = RuntimeConfigStore(tmp_path / "runtime")
    runtime = ProviderRuntime(store)
    repository, collection = _repo()
    manifest = tmp_path / "agents.yaml"
    manifest.write_text(
        "agents:\n"
        "  weather_agent: {dir: w, service: weather-agent, port: 7001, requires: text}\n"
        "  image_generator_agent:\n"
        "    {dir: i, service: image-generator-agent, port: 6002, requires: image}\n"
    )
    settings = SimpleNamespace(
        bundled_agents_manifest=str(manifest),
        default_agent_provider_id="Hybro AI",
    )
    bundled = create_bundled_agent_service(
        settings,
        repository=repository,
        card_resolver=Resolver(),
        provider_runtime=runtime,
    )
    for agent in load_manifest(manifest):
        await bundled.reconcile(agent, card(agent.name, agent.compose_url))
    assert {doc["inactive_reason"] for doc in collection.docs} == {
        "provider_not_configured"
    }

    store.save(
        RuntimeConfig(
            provider=RuntimeProvider(id="openai", auth="api_key"),
            models=RuntimeModels(text="gpt-4o-mini"),
        ),
        ApiKeyCredential(provider="openai", api_key="fixture-not-a-provider-key"),
        {},
    )
    await runtime.reload()  # A setup save; it returns once the gating is written.

    records = by_name(collection)
    assert records["weather_agent"]["inactive_reason"] is None
    assert records["image_generator_agent"]["inactive_reason"] == (
        "image_model_not_configured"
    )
    await bundled.stop()


# ── Health check and liveness ──


def gated_record(reason: str | None) -> dict:
    return {
        "agent_id": "gated",
        "provider_id": "Hybro AI",
        "agent_card": {
            "name": "Weather",
            "url": "http://weather-agent:7001",
            "version": "1.0.0",
            "capabilities": {},
        },
        "normalized_url": "http://weather-agent:7001",
        "agent_status": "inactive",
        "is_public": True,
        "source": "bundled",
        "bundled_name": "weather_agent",
        "inactive_reason": reason,
    }


def healthy_health_service(repository) -> AgentHealthService:
    health = AgentHealthService(repository=repository, check_interval_seconds=60)

    async def check_agent_health(agent_id, agent_url, agent_name, *, timeout=None):
        return True, None

    health.check_agent_health = check_agent_health
    return health


@pytest.mark.parametrize(
    ("reason", "status"), [("provider_not_configured", "inactive"), (None, "active")]
)
async def test_health_check_does_not_reactivate_a_gated_agent(reason, status):
    repository, collection = _repo([gated_record(reason)])
    health = healthy_health_service(repository)

    await health.run_health_check_cycle()
    assert collection.docs[0]["agent_status"] == status

    collection.docs[0]["agent_status"] = "inactive"
    agent = health._coerce_agent(collection.docs[0])
    await health._handle_retry_success("gated", agent, None)
    assert collection.docs[0]["agent_status"] == status


@pytest.mark.parametrize(
    ("reason", "status"), [("image_model_not_configured", "inactive"), (None, "active")]
)
async def test_liveness_does_not_reactivate_a_gated_agent(reason, status):
    repository, collection = _repo([gated_record(reason)])
    liveness = AgentLivenessService(health_service=healthy_health_service(repository))

    checked = await liveness(agent_info_from_doc(collection.docs[0]))

    assert checked.status == status
    assert collection.docs[0]["agent_status"] == status


def gating_during_probe(repository, collection) -> AgentHealthService:
    """A healthy probe during which provider gating disables the agent."""
    health = AgentHealthService(repository=repository, check_interval_seconds=60)

    async def check_agent_health(agent_id, agent_url, agent_name, *, timeout=None):
        collection.docs[0]["inactive_reason"] = "provider_not_configured"
        return True, None

    health.check_agent_health = check_agent_health
    return health


async def test_health_check_in_flight_does_not_undo_gating():
    # Unreachable, not gated, when the cycle reads it.
    repository, collection = _repo([gated_record(None)])
    health = gating_during_probe(repository, collection)

    await health.run_health_check_cycle()
    assert collection.docs[0]["agent_status"] == "inactive"

    collection.docs[0]["inactive_reason"] = None
    agent = health._coerce_agent(collection.docs[0])
    collection.docs[0]["inactive_reason"] = "provider_not_configured"
    await health._handle_retry_success("gated", agent, None)
    assert collection.docs[0]["agent_status"] == "inactive"


async def test_liveness_in_flight_does_not_undo_gating():
    repository, collection = _repo([gated_record(None)])
    info = agent_info_from_doc(collection.docs[0])
    liveness = AgentLivenessService(
        health_service=gating_during_probe(repository, collection)
    )

    checked = await liveness(info)

    assert checked.status == "inactive"
    assert collection.docs[0]["agent_status"] == "inactive"


def test_agent_view_carries_the_inactive_reason():
    view = agent_view(agent_info_from_doc(gated_record("provider_not_configured")))

    assert view.source == "bundled"
    assert view.inactive_reason == "provider_not_configured"
