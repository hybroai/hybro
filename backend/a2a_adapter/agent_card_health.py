"""SDK-confined agent-card health probing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from common.observability import events as E
from common.observability import get_logger
from common.types import AgentCard

from .card_data import build_agent_card
from .constants import AGENT_CARD_WELL_KNOWN_PATH, PREV_AGENT_CARD_WELL_KNOWN_PATH
from .docker_host_fallback import with_docker_host_url_fallback

logger = get_logger(__name__)


@dataclass(frozen=True)
class AgentCardHealthResult:
    is_healthy: bool
    card: AgentCard | None
    status_code: int | None = None
    error: str | None = None


async def fetch_agent_card_for_health(
    agent_url: str,
    client: Any,
) -> AgentCardHealthResult:
    """Fetch and parse an agent card for health sync without leaking SDK types."""
    base_url = agent_url.rstrip("/")
    response = await client.get(base_url + AGENT_CARD_WELL_KNOWN_PATH)

    if response.status_code == 404:
        response = await client.get(base_url + PREV_AGENT_CARD_WELL_KNOWN_PATH)

    is_healthy = response.status_code < 400
    if not is_healthy:
        return AgentCardHealthResult(
            is_healthy=False,
            card=None,
            status_code=response.status_code,
        )

    try:
        from .translators import a2a_card_to_snapshot

        snapshot = a2a_card_to_snapshot(build_agent_card(response.json()), base_url)
        card = AgentCard.model_validate(snapshot.raw_card)
    except Exception:
        card = None

    return AgentCardHealthResult(
        is_healthy=True,
        card=card,
        status_code=response.status_code,
    )


async def probe_agent_card_for_health(
    agent_url: str,
    *,
    timeout: float = 30.0,
) -> AgentCardHealthResult:
    """Fetch an agent card with Docker host fallback and normalized failures."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await with_docker_host_url_fallback(
                str(agent_url),
                lambda candidate_url: fetch_agent_card_for_health(
                    candidate_url,
                    client,
                ),
            )
    except httpx.TimeoutException as exc:
        logger.emit(
            E.AGENT_CARD_HEALTH_PROBE_TIMED_OUT,
            exc=exc,
            agent_url=agent_url,
            timeout_seconds=timeout,
        )
        return AgentCardHealthResult(
            is_healthy=False,
            card=None,
            error=str(exc),
        )
    except httpx.RequestError as exc:
        logger.emit(E.AGENT_CARD_HEALTH_PROBE_FAILED, exc=exc, agent_url=agent_url)
        return AgentCardHealthResult(
            is_healthy=False,
            card=None,
            error=str(exc),
        )
    except Exception as exc:
        logger.emit(E.AGENT_CARD_HEALTH_PROBE_FAILED, exc=exc, agent_url=agent_url)
        return AgentCardHealthResult(
            is_healthy=False,
            card=None,
            error=str(exc),
        )


__all__ = [
    "AgentCardHealthResult",
    "fetch_agent_card_for_health",
    "probe_agent_card_for_health",
]
