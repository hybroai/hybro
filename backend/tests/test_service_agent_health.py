from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from a2a_adapter import agent_card_health
from a2a_adapter.agent_card_health import (
    fetch_agent_card_for_health,
    probe_agent_card_for_health,
)
from a2a_adapter.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PREV_AGENT_CARD_WELL_KNOWN_PATH,
)
from agent.health import AgentHealthService
from common.types import AgentCard
from models.agent import Agent


def _card_payload(url: str = "https://agent.example", **overrides):
    payload = {
        "name": "Health Agent",
        "description": "Checks health",
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "version": "1.0.0",
        "capabilities": {},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "health",
                "name": "Health",
                "description": "Reports health",
                "tags": ["health"],
            }
        ],
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    async def get(self, url: str):
        self.urls.append(url)
        return self.responses.pop(0)


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _Repo:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update(self, agent_id: str, updates: dict):
        self.updates.append((agent_id, updates))
        return {}


@pytest.mark.asyncio
async def test_fetch_agent_card_for_health_returns_common_card_from_current_path():
    client = _Client([_Response(200, _card_payload())])

    result = await fetch_agent_card_for_health("https://agent.example/", client)

    assert result.is_healthy is True
    assert isinstance(result.card, AgentCard)
    assert result.card.name == "Health Agent"
    assert client.urls == ["https://agent.example" + AGENT_CARD_WELL_KNOWN_PATH]


@pytest.mark.asyncio
async def test_fetch_agent_card_for_health_falls_back_after_current_path_404():
    client = _Client(
        [
            _Response(404, {}),
            _Response(200, _card_payload(name="Legacy Health Agent")),
        ]
    )

    result = await fetch_agent_card_for_health("https://agent.example", client)

    assert result.is_healthy is True
    assert result.card is not None
    assert result.card.name == "Legacy Health Agent"
    assert client.urls == [
        "https://agent.example" + AGENT_CARD_WELL_KNOWN_PATH,
        "https://agent.example" + PREV_AGENT_CARD_WELL_KNOWN_PATH,
    ]


@pytest.mark.asyncio
async def test_fetch_agent_card_for_health_does_not_fallback_for_non_404_failure():
    client = _Client([_Response(503, {})])

    result = await fetch_agent_card_for_health("https://agent.example", client)

    assert result.is_healthy is False
    assert result.card is None
    assert client.urls == ["https://agent.example" + AGENT_CARD_WELL_KNOWN_PATH]


@pytest.mark.asyncio
async def test_fetch_agent_card_for_health_keeps_healthy_invalid_card_nonfatal():
    client = _Client([_Response(200, ValueError("not json"))])

    result = await fetch_agent_card_for_health("https://agent.example", client)

    assert result.is_healthy is True
    assert result.card is None


@pytest.mark.asyncio
async def test_probe_agent_card_for_health_retries_host_gateway_for_loopback_url(
    monkeypatch,
    caplog,
):
    class _LoopbackClient:
        def __init__(self):
            self.urls: list[str] = []

        async def get(self, url: str):
            self.urls.append(url)
            if url.startswith("http://127.0.0.1:9060/"):
                raise httpx.ConnectError("All connection attempts failed")
            return _Response(200, _card_payload(url="http://127.0.0.1:9060"))

    client = _LoopbackClient()
    monkeypatch.setattr(
        agent_card_health.httpx,
        "AsyncClient",
        lambda *, timeout: _AsyncClientContext(client),
    )

    with caplog.at_level(logging.DEBUG):
        result = await probe_agent_card_for_health(
            "http://127.0.0.1:9060",
            timeout=1,
        )

    assert result.is_healthy is True
    assert result.card is not None
    assert client.urls == [
        "http://127.0.0.1:9060" + AGENT_CARD_WELL_KNOWN_PATH,
        "http://host.docker.internal:9060" + AGENT_CARD_WELL_KNOWN_PATH,
    ]
    assert "a2a_docker_host_fallback_selected" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "event"),
    [
        (httpx.ReadTimeout("slow"), "agent_card_health_probe_timed_out"),
        (httpx.ConnectError("refused"), "agent_card_health_probe_failed"),
        (RuntimeError("unexpected"), "agent_card_health_probe_failed"),
    ],
)
async def test_probe_agent_card_for_health_logs_failure_event(
    monkeypatch, caplog, error, event
):
    class _FailingClient:
        async def get(self, url: str):
            raise error

    monkeypatch.setattr(
        agent_card_health.httpx,
        "AsyncClient",
        lambda *, timeout: _AsyncClientContext(_FailingClient()),
    )

    with caplog.at_level(logging.WARNING, logger="a2a_adapter.agent_card_health"):
        result = await probe_agent_card_for_health("https://agent.example", timeout=2.5)

    assert result.is_healthy is False
    [record] = [r for r in caplog.records if r.getMessage() == event]
    assert record.levelno == logging.WARNING
    assert record.agent_url == "https://agent.example"
    assert record.error_type == type(error).__name__
    if event == "agent_card_health_probe_timed_out":
        assert record.timeout_seconds == 2.5


@pytest.mark.asyncio
async def test_check_agent_health_delegates_to_adapter_probe(monkeypatch):
    captured = {}
    live_card = AgentCard(**_card_payload(name="Live Health Agent"))

    async def _probe(agent_url: str, *, timeout: float):
        captured["agent_url"] = agent_url
        captured["timeout"] = timeout
        return SimpleNamespace(
            is_healthy=True,
            card=live_card,
            status_code=200,
            error=None,
        )

    monkeypatch.setattr(
        "agent.health.probe_agent_card_for_health",
        _probe,
    )
    service = AgentHealthService(repository=_Repo(), timeout_seconds=9)
    assert await service.check_agent_health(
        "agent-1", "https://agent.example", "Agent", timeout=2
    ) == (True, live_card)
    assert captured == {
        "agent_url": "https://agent.example",
        "timeout": 2,
    }


@pytest.mark.asyncio
async def test_check_agent_health_returns_unhealthy_on_adapter_errors(monkeypatch):
    async def _raise_adapter_error(*_args, **_kwargs):
        raise RuntimeError("adapter broke")

    monkeypatch.setattr(
        "agent.health.probe_agent_card_for_health",
        _raise_adapter_error,
    )
    service = AgentHealthService(repository=_Repo())
    assert await service.check_agent_health(
        "agent-1", "https://agent.example", "Agent"
    ) == (False, None)


@pytest.mark.asyncio
async def test_sync_agent_card_preserves_blocklisted_fields():
    repo = _Repo()
    service = AgentHealthService(repository=repo)
    agent = Agent(
        agent_id="agent-1",
        is_public=True,
        agent_card=AgentCard(
            **_card_payload(
                name="Stored Agent",
                iconUrl="https://registered.example/icon.png",
            ),
            url="https://registered.example",
        ),
    )
    fetched = AgentCard(
        **_card_payload(
            name="Live Agent",
            iconUrl="https://live.example/icon.png",
        ),
        url="https://live.example",
    )

    await service.sync_agent_card(
        agent.agent_id,
        agent.agent_card.model_dump(mode="json", by_alias=True),
        fetched,
    )

    assert repo.updates == [
        (
            "agent-1",
            {
                "agent_card.name": "Live Agent",
                "agent_card.iconUrl": "https://live.example/icon.png",
            },
        )
    ]


@pytest.mark.asyncio
async def test_sync_agent_card_syncs_extra_sdk_card_fields():
    repo = _Repo()
    service = AgentHealthService(repository=repo)
    agent = Agent(
        agent_id="agent-1",
        is_public=True,
        agent_card=AgentCard(**_card_payload()),
    )
    fetched = AgentCard(
        **_card_payload(
            protocolVersion="0.3.0",
            preferredTransport="JSONRPC",
            additionalInterfaces=[{"url": "https://agent.example/a2a"}],
            securitySchemes={"bearer": {"type": "http", "scheme": "bearer"}},
            supportsAuthenticatedExtendedCard=True,
        )
    )

    await service.sync_agent_card(
        agent.agent_id,
        agent.agent_card.model_dump(mode="json", by_alias=True),
        fetched,
    )

    assert repo.updates == [
        (
            "agent-1",
            {
                "agent_card.protocolVersion": "0.3.0",
                "agent_card.preferredTransport": "JSONRPC",
                "agent_card.additionalInterfaces": [
                    {"url": "https://agent.example/a2a"}
                ],
                "agent_card.securitySchemes": {
                    "bearer": {"type": "http", "scheme": "bearer"}
                },
                "agent_card.supportsAuthenticatedExtendedCard": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_health_cycle_is_gated_by_the_injected_leader_lock():
    calls: list[tuple[str, str]] = []

    class Leader:
        async def try_acquire(self, name, ttl):
            calls.append(("acquire", name))
            return True

        async def release(self, name):
            calls.append(("release", name))

    service = AgentHealthService(repository=_Repo())
    cycles = []

    async def cycle():
        cycles.append(True)

    service.run_health_check_cycle = cycle
    service.set_leader_election(Leader(), lock_name="agent_health_checker")

    await service._run_one_iteration()

    assert cycles == [True]
    assert calls == [
        ("acquire", "agent_health_checker"),
        ("release", "agent_health_checker"),
    ]


def test_agent_health_does_not_import_jobs():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("agent/health.py").read_text())
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.split(".")[0] == "jobs" for module in modules)
