"""Bundled sample agents: boot registration and availability gating.

The backend registers every enabled agent of the bundled manifest
(``default_agents/agents.yaml``) itself. A background task waits for each
agent card and reconciles exactly one record per agent, so repeated boots
converge. Each agent's ``requires`` value then decides whether the record is
active: an unmet requirement sets ``inactive_reason``, which the health check
respects, and every Provider pair swap re-applies the gating.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from agent.facade import AgentRepository
from agent.translators import _status_value
from agent.url_utils import normalize_agent_url
from common.dto.agent import AgentCardSnapshot
from common.observability import get_logger, traced_create_task
from common.protocols import AgentCardResolver

logger = get_logger(__name__)

Requirement = Literal["none", "text", "image"]
InactiveReason = Literal["provider_not_configured", "image_model_not_configured"]
BUNDLED_SOURCE = "bundled"
_REQUIREMENTS = frozenset(get_args(Requirement))


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """What the current Provider pair offers the bundled agents.

    ``configured`` is false for a missing Provider and for an invalid
    credential alike: no model call works in either state.
    """

    configured: bool
    image_model: bool


@dataclass(frozen=True, slots=True)
class BundledAgent:
    """One enabled manifest entry; ``name`` is its manifest key."""

    name: str
    service: str
    port: int
    requires: Requirement

    @property
    def compose_url(self) -> str:
        """The 0.3 Compose address, used only to find records 0.3 created."""
        return f"http://{self.service}:{self.port}"

    @property
    def url(self) -> str:
        """Where this backend reaches the agent: loopback in the same container."""
        return f"http://127.0.0.1:{self.port}"


def load_manifest(path: Path) -> tuple[BundledAgent, ...]:
    """Enabled agents of the manifest, in file order; raise ``ValueError`` if invalid."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Bundled agent manifest {path} is unreadable") from exc
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        raise ValueError(f"Bundled agent manifest {path} has no agents mapping")
    loaded: list[BundledAgent] = []
    for name, spec in agents.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Bundled agent '{name}' must be a mapping")
        if not spec.get("enabled", True):
            continue
        service, port, requires = (
            spec.get("service"),
            spec.get("port"),
            spec.get("requires"),
        )
        if not isinstance(service, str) or not service:
            raise ValueError(f"Bundled agent '{name}' needs a service name")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError(f"Bundled agent '{name}' needs a port from 1 to 65535")
        if requires not in _REQUIREMENTS:
            raise ValueError(
                f"Bundled agent '{name}' requires must be one of {sorted(_REQUIREMENTS)}"
            )
        loaded.append(BundledAgent(str(name), service, port, requires))
    return tuple(loaded)


def inactive_reason(
    requires: Requirement, available: ProviderAvailability
) -> InactiveReason | None:
    """Why an agent with this requirement cannot run, or None."""
    if requires == "none":
        return None
    if not available.configured:
        return "provider_not_configured"
    if requires == "image" and not available.image_model:
        return "image_model_not_configured"
    return None


class BundledAgentService:
    """Registers the bundled agents at boot and keeps their gating current."""

    def __init__(
        self,
        *,
        repository: AgentRepository,
        card_resolver: AgentCardResolver,
        agents: tuple[BundledAgent, ...],
        provider_id: str,
        availability: Callable[[], ProviderAvailability],
        id_factory: Callable[[], str],
        now: Callable[[], datetime],
        retry_seconds: float = 2.0,
        max_retry_seconds: float = 30.0,
    ) -> None:
        self._repository = repository
        self._card_resolver = card_resolver
        self._agents = agents
        self._provider_id = provider_id
        self._availability = availability
        self._id_factory = id_factory
        self._now = now
        self._retry_seconds = retry_seconds
        self._max_retry_seconds = max_retry_seconds
        # Registration and gating write the same fields; one at a time keeps the
        # status consistent with the pair read under the lock.
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    def start(self) -> None:
        """Register in the background so the API never waits for an agent."""
        for agent in self._agents:
            self._spawn(self._register_when_ready(agent), f"bundled-agent:{agent.name}")

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def on_provider_change(self) -> asyncio.Task[None]:
        """Re-gate in the background after a Provider pair swap."""
        return self._spawn(self.apply_gating(), "bundled-agent-gating")

    async def apply_gating(self) -> None:
        """Set each bundled record active or inactive from the current pair."""
        requirements = {agent.name: agent.requires for agent in self._agents}
        async with self._lock:
            available = self._availability()
            for record in await self._repository.get_by_source(BUNDLED_SOURCE):
                requires = requirements.get(record.get("bundled_name") or "")
                if requires is None:
                    continue
                updates = _gating_updates(record, inactive_reason(requires, available))
                if updates:
                    await self._repository.update(record["agent_id"], updates)

    async def reconcile(
        self, agent: BundledAgent, card: AgentCardSnapshot
    ) -> str | None:
        """Create or update the one record for ``agent``; return its agent_id.

        Returns None when a ``source: local`` record holds the agent's URL: a
        host agent found by discovery is never taken over.
        """
        url = normalize_agent_url(agent.url)
        async with self._lock:
            record = await self._find(agent, url)
            holder = await self._repository.find_by_normalized_url(url)
            if holder is not None and (
                record is None or holder["agent_id"] != record["agent_id"]
            ):
                logger.warning(
                    "bundled_agent_url_taken",
                    extra={
                        "bundled_name": agent.name,
                        "holder_source": holder.get("source", "cloud"),
                    },
                )
                return None
            now = self._now()
            reason = inactive_reason(agent.requires, self._availability())
            fields = {
                "provider_id": self._provider_id,
                "agent_card": _stored_card(card, url),
                "normalized_url": url,
                "source": BUNDLED_SOURCE,
                "bundled_name": agent.name,
                "capabilities": list(card.capabilities),
                "agent_status": "inactive" if reason else "active",
                "inactive_reason": reason,
                "inactive_since": None,
                "updated_at": now,
            }
            if record is None:
                agent_id = self._id_factory()
                await self._repository.upsert(
                    agent_id,
                    {
                        **fields,
                        "agent_id": agent_id,
                        "public_url": None,
                        "is_public": True,
                        "rate_limit_per_user_per_hour": None,
                        "rate_limit_system_per_hour": None,
                        "call_count": 0,
                        "call_success_count": 0,
                        "created_at": now,
                    },
                )
                logger.info(
                    "bundled_agent_registered", extra={"bundled_name": agent.name}
                )
                return agent_id
            # Keeping agent_id lets room history still resolve the agent.
            await self._repository.update(record["agent_id"], fields)
            return record["agent_id"]

    async def _find(self, agent: BundledAgent, url: str) -> dict | None:
        for record in await self._repository.get_by_source(BUNDLED_SOURCE):
            if record.get("bundled_name") == agent.name:
                return record
        # A record at the current URL, or one the 0.3 Compose registrar created.
        for candidate in dict.fromkeys((url, normalize_agent_url(agent.compose_url))):
            record = await self._repository.find_by_normalized_url(candidate)
            if record is not None and record.get("source", "cloud") != "local":
                return record
        return None

    async def _register_when_ready(self, agent: BundledAgent) -> None:
        url = agent.url
        delay = self._retry_seconds
        while True:
            try:
                card = await self._card_resolver.resolve_card(url)
                if card is not None:
                    await self.reconcile(agent, card)
                    return
            except Exception as exc:  # Retry; a database blip must not end the task.
                logger.warning(
                    "bundled_agent_registration_failed",
                    extra={
                        "bundled_name": agent.name,
                        "error_type": type(exc).__name__,
                    },
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_retry_seconds)

    def _spawn(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = traced_create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _gating_updates(record: dict, reason: InactiveReason | None) -> dict[str, Any]:
    current = record.get("inactive_reason")
    if reason is not None:
        status = _status_value(record.get("agent_status"), default=None)
        if current == reason and status == "inactive":
            return {}
        return {"agent_status": "inactive", "inactive_reason": reason}
    if current is None:
        # Not gated; a health-check status stays as it is.
        return {}
    return {"agent_status": "active", "inactive_reason": None}


def _stored_card(card: AgentCardSnapshot, url: str) -> dict[str, Any]:
    raw_card = dict(card.raw_card)
    raw_card.setdefault("name", card.name)
    raw_card.setdefault("description", card.description)
    # The health check probes the stored URL, so it is where this backend
    # reaches the agent, not what the agent advertises.
    raw_card["url"] = url
    return raw_card


__all__ = [
    "BUNDLED_SOURCE",
    "BundledAgent",
    "BundledAgentService",
    "InactiveReason",
    "ProviderAvailability",
    "Requirement",
    "inactive_reason",
    "load_manifest",
]
