"""Serve the ``/agent/*`` routes over the agent facade with public views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent.facade import AgentFacade, AgentRegistrationConflict
from agent.views import agent_card_view, agent_view
from common.auth import resolve_provider_name
from common.dto import AgentInfo, AgentRouteResponse, AgentView
from common.observability import events as E
from common.observability import get_logger

logger = get_logger(__name__)

_NOT_FOUND = "Agent not found"


class AgentCatalogService:
    def __init__(
        self,
        facade: AgentFacade,
        *,
        liveness_checker: Callable[[AgentInfo], Awaitable[AgentInfo]],
    ) -> None:
        self._facade = facade
        self._liveness_checker = liveness_checker

    async def register(self, *, agent_url: str, provider_id: str) -> AgentRouteResponse:
        try:
            info = await self._facade.register_agent(agent_url, provider_id)
        except AgentRegistrationConflict as exc:
            return AgentRouteResponse(
                success=False,
                error=str(exc),
                error_code=exc.code,
                status_code=409,
            )
        except ValueError as exc:
            return AgentRouteResponse(success=False, error=str(exc), status_code=500)
        except Exception as exc:
            logger.emit(E.AGENT_REGISTRATION_FAILED, exc=exc, provider_id=provider_id)
            return AgentRouteResponse(success=False, error=str(exc), status_code=500)
        return AgentRouteResponse(success=True, agent=_view(info))

    async def list_for_provider(self, *, provider_id: str) -> AgentRouteResponse:
        infos = await self._facade.list_agents(provider_id)
        return AgentRouteResponse(success=True, agents=[_view(info) for info in infos])

    async def delete(self, *, agent_id: str, provider_id: str) -> AgentRouteResponse:
        existing = await self._facade.get_agent(agent_id)
        if existing is None:
            return AgentRouteResponse(success=False, error=_NOT_FOUND, status_code=404)
        if existing.provider_id != provider_id:
            return AgentRouteResponse(
                success=False,
                error="You do not have permission to delete this agent",
                status_code=403,
            )
        if not await self._facade.delete_agent(agent_id, provider_id):
            return AgentRouteResponse(success=False, error=_NOT_FOUND, status_code=404)
        return AgentRouteResponse(success=True)

    async def resolve_card(self, *, agent_url: str) -> AgentRouteResponse:
        card = await self._facade.resolve_agent_card_from_url(agent_url)
        if card is None:
            return AgentRouteResponse(
                success=False,
                error="Agent card could not resolve",
                status_code=404,
            )
        return AgentRouteResponse(
            success=True,
            card=agent_card_view(
                card.raw_card, name=card.name, description=card.description
            ),
        )

    async def get_visible(
        self, *, agent_id: str, user_id: str | None
    ) -> AgentRouteResponse:
        info = await self._facade.get_agent(agent_id)
        if info is None or (
            not info.is_public and (user_id is None or info.provider_id != user_id)
        ):
            return AgentRouteResponse(success=False, error=_NOT_FOUND, status_code=404)
        info = await self._liveness_checker(info)
        return AgentRouteResponse(success=True, agent=_view(info))

    async def list_visible(
        self, *, user_id: str | None, active_only: bool = False
    ) -> AgentRouteResponse:
        try:
            infos = await self._facade.list_visible_agents(
                user_id=user_id,
                active_only=active_only,
            )
        except Exception as exc:
            logger.emit(
                E.VISIBLE_AGENTS_LIST_FAILED,
                exc=exc,
                user_id=user_id,
                active_only=active_only,
            )
            return AgentRouteResponse(success=False, error=str(exc), status_code=500)
        return AgentRouteResponse(success=True, agents=[_view(info) for info in infos])


def _view(info: AgentInfo) -> AgentView:
    view = agent_view(info)
    if view.provider_organization:
        return view
    return view.model_copy(
        update={"provider_name": resolve_provider_name(info.provider_id)}
    )


__all__ = ["AgentCatalogService"]
