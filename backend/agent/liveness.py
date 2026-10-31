"""On-demand agent liveness check.

Called from the GET /agent/getAgent/{id} endpoint to probe agent
reachability *before* returning the response, ensuring the frontend
always sees an accurate agent status.
"""

from __future__ import annotations

from typing import Protocol

from common.config.loader import settings
from common.dto import AgentInfo
from common.types import AgentCard
from common.utils.logger import get_logger
from models.agent import AgentStatus

logger = get_logger(__name__)


class AgentHealthProbe(Protocol):
    async def check_agent_health(
        self,
        agent_id: str,
        agent_url: str,
        agent_name: str | None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, AgentCard | None]: ...

    async def sync_agent_card(
        self, agent_id: str, stored_card: dict, fetched_card: AgentCard
    ) -> None: ...

    async def update_agent_status(
        self, agent_id: str, new_status: AgentStatus
    ) -> bool: ...

    async def reactivate_agent(self, agent_id: str) -> bool: ...


class AgentLivenessService:
    def __init__(self, *, health_service: AgentHealthProbe) -> None:
        self._health_service = health_service

    async def __call__(self, agent: AgentInfo) -> AgentInfo:
        """Probe a cloud or bundled agent and sync its stored status if it changed.

        Local agents follow discovery and are returned unchanged.
        """
        if agent.source not in {"cloud", "bundled"}:
            return agent
        return await self._check_cloud_agent(agent)

    async def _check_cloud_agent(self, agent: AgentInfo) -> AgentInfo:
        health_service = self._health_service
        stored_url = agent.raw_card.get("url")
        agent_url = stored_url if isinstance(stored_url, str) and stored_url else None
        agent_url = agent_url or agent.url
        if not agent_url:
            return agent
        try:
            is_healthy, fetched_card = await health_service.check_agent_health(
                agent.agent_id,
                agent_url,
                agent.name,
                timeout=settings.cloud_health_check_timeout,
            )
        except Exception:
            logger.warning(
                "Liveness probe failed for cloud agent %s — returning stale status",
                agent.agent_id,
                exc_info=True,
            )
            return agent

        if is_healthy and fetched_card:
            await health_service.sync_agent_card(
                agent.agent_id, _normalized_card(agent.raw_card), fetched_card
            )

        if not is_healthy and agent.status == AgentStatus.active.value:
            await health_service.update_agent_status(
                agent.agent_id, AgentStatus.inactive
            )
            logger.info("Liveness: cloud agent %s marked inactive", agent.agent_id)
            return agent.model_copy(update={"status": AgentStatus.inactive.value})

        # A bundled agent gated by setup stays inactive while it is reachable;
        # the write rechecks that, since gating may run during the probe.
        if (
            is_healthy
            and agent.status == AgentStatus.inactive.value
            and not agent.inactive_reason
            and await health_service.reactivate_agent(agent.agent_id)
        ):
            logger.info(
                "Liveness: cloud agent %s recovered — marked active", agent.agent_id
            )
            return agent.model_copy(update={"status": AgentStatus.active.value})

        return agent


def _normalized_card(raw_card: dict) -> dict:
    """Dump the stored card the way the fetched card is dumped, for comparison."""
    try:
        return AgentCard.model_validate(raw_card).model_dump(mode="json", by_alias=True)
    except ValueError:
        return dict(raw_card)


__all__ = ["AgentLivenessService"]
