from __future__ import annotations

from agent.service import AgentService
from common.auth import resolve_provider_name
from models.request import AgentCenterRequest
from models.response import AgentCenterResponse


class AgentRouteAdapter:
    def __init__(self, service: AgentService):
        self.agent_service = service

    async def register_agent_from_route(
        self, *, agent_url: str, provider_id: str
    ) -> AgentCenterResponse:
        response = await self.register_agent(
            AgentCenterRequest(agent_url=agent_url, provider_id=provider_id)
        )
        return self.finalize_agent_response_for_route(response)

    async def get_agents_by_provider_for_route(
        self, *, provider_id: str
    ) -> AgentCenterResponse:
        response = await self.get_agents_by_provider_id(
            AgentCenterRequest(provider_id=provider_id)
        )
        return self.finalize_agent_response_for_route(response)

    async def delete_agent_from_route(
        self, *, agent_id: str, provider_id: str
    ) -> AgentCenterResponse:
        existing_agent = await self.agent_service.get_agent_by_agent_id(agent_id)
        if not existing_agent:
            return AgentCenterResponse(
                agent_id=agent_id,
                success=False,
                error="Agent not found",
                status_code=404,
            )
        if existing_agent.provider_id != provider_id:
            return AgentCenterResponse(
                agent_id=agent_id,
                success=False,
                error="You do not have permission to delete this agent",
                status_code=403,
            )
        response = await self.remove_agent(
            AgentCenterRequest(agent_id=agent_id, provider_id=provider_id)
        )
        return self.finalize_agent_response_for_route(response)

    async def get_agent_card_from_url_for_route(
        self, *, agent_url: str
    ) -> AgentCenterResponse:
        response = await self.get_agent_card_from_url(
            AgentCenterRequest(agent_url=agent_url)
        )
        return self.finalize_agent_response_for_route(response)

    async def get_visible_agent_for_route(
        self, *, agent_id: str, user_id: str | None
    ) -> AgentCenterResponse:
        response = await self.query_agent_by_agent_id(
            AgentCenterRequest(agent_id=agent_id, user_id=user_id)
        )
        return response

    async def list_visible_agents_for_route(
        self, *, user_id: str | None, active_only: bool = False
    ) -> AgentCenterResponse:
        request = AgentCenterRequest(user_id=user_id)
        if active_only:
            response = await self.get_all_active_agents(request)
        else:
            response = await self.get_all_agents(request)
        return self.finalize_agent_response_for_route(response)

    def finalize_agent_response_for_route(
        self, response: AgentCenterResponse
    ) -> AgentCenterResponse:
        if response.success and response.agents:
            for agent in response.agents:
                if (
                    not agent.agent_card.provider
                    or not agent.agent_card.provider.organization
                ):
                    agent.provider_name = resolve_provider_name(agent.provider_id)
        if response.success and response.agent:
            agent = response.agent
            if (
                not agent.agent_card.provider
                or not agent.agent_card.provider.organization
            ):
                agent.provider_name = resolve_provider_name(agent.provider_id)
        return self._mask_sensitive_information(
            response, ["agent_url", "agent_card.url"]
        )

    async def get_agent_card_from_url(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.get_agent_card_from_url(request)

    async def register_agent(self, request: AgentCenterRequest) -> AgentCenterResponse:
        return await self.agent_service.register_agent(request)

    async def remove_agent(self, request: AgentCenterRequest) -> AgentCenterResponse:
        return await self.agent_service.remove_agent(request)

    async def query_agent_by_agent_id(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.query_agent_by_agent_id(request)

    async def get_all_agents(self, request: AgentCenterRequest) -> AgentCenterResponse:
        return await self.agent_service.get_all_agents(request)

    async def get_all_active_agents(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.get_all_active_agents(request)

    async def get_agents_with_conditions(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.get_agents_with_conditions(request)

    async def query_similar_agents(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.query_similar_agents(request)

    async def get_agents_by_provider_id(
        self, request: AgentCenterRequest
    ) -> AgentCenterResponse:
        return await self.agent_service.get_agents_by_provider_id(request)

    def _mask_sensitive_information(
        self, response: AgentCenterResponse, fields: list[str]
    ) -> AgentCenterResponse:
        return self.agent_service._mask_sensitive_information(response, fields)


__all__ = ["AgentRouteAdapter"]
