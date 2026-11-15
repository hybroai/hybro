"""
Unit tests for Agent API endpoints.

Tests cover:
- Agent registration
- Agent retrieval (by ID, by provider, all agents)
- Agent deletion
- Public vs private agent visibility
- Authorization checks
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api_gateway.routes.agent_routes import (
    delete_agent,
    discover_local_agents,
    get_agent,
    get_agent_by_provider,
    get_agent_card_from_url,
    get_agent_list,
    register_agent,
)
from common.dto import AgentRouteResponse, AgentView
from local_agents.models import DiscoveryTrigger, LocalAgentDiscoveryResult
from models.request import InspectionCenterRequest

# =============================================================================
# Local Agent Discovery Tests
# =============================================================================


@pytest.mark.asyncio
async def test_manual_local_agent_discovery_requires_bound_service(mock_user):
    with pytest.raises(HTTPException) as exc_info:
        await discover_local_agents(mock_user, discovery=None)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_manual_local_agent_discovery_waits_for_result(mock_user):
    discovery = AsyncMock()
    discovery.request_discovery.return_value = LocalAgentDiscoveryResult(
        trigger=DiscoveryTrigger.MANUAL,
        agents_found=2,
    )

    result = await discover_local_agents(mock_user, discovery=discovery)

    assert result.agents_found == 2
    discovery.request_discovery.assert_awaited_once_with(DiscoveryTrigger.MANUAL)


# =============================================================================
# Agent Route Tests
# =============================================================================


def _request(payload: dict) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)
    return request


def _view(agent_id: str = "agent-1") -> AgentView:
    return AgentView(agent_id=agent_id, name="Test Agent", description="", version="1")


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_registers_agent_with_user_as_provider(
        self, mock_user, patch_agent_deps
    ):
        patch_agent_deps.register.return_value = AgentRouteResponse(
            success=True, agent=_view()
        )

        response = await register_agent(
            _request({"agent_url": "https://agent.example"}),
            mock_user,
            catalog=patch_agent_deps,
        )

        assert response.agent.agent_id == "agent-1"
        patch_agent_deps.register.assert_awaited_once_with(
            agent_url="https://agent.example", provider_id=mock_user.user_id
        )

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_url_missing(self, mock_user, patch_agent_deps):
        with pytest.raises(HTTPException) as exc_info:
            await register_agent(_request({}), mock_user, catalog=patch_agent_deps)

        assert exc_info.value.status_code == 400
        assert "agent_url is required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_typed_409_for_registration_conflict(
        self, mock_user, patch_agent_deps
    ):
        patch_agent_deps.register.return_value = AgentRouteResponse(
            success=False,
            error="Another provider has already registered an agent at this URL",
            error_code="owned_by_another_provider",
            status_code=409,
        )

        with pytest.raises(HTTPException) as exc_info:
            await register_agent(
                _request({"agent_url": "https://agent.example"}),
                mock_user,
                catalog=patch_agent_deps,
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {
            "code": "owned_by_another_provider",
            "message": "Another provider has already registered an agent at this URL",
        }


class TestGetAgentByProvider:
    @pytest.mark.asyncio
    async def test_returns_agents_for_authenticated_user(
        self, mock_user, patch_agent_deps
    ):
        patch_agent_deps.list_for_provider.return_value = AgentRouteResponse(
            success=True, agents=[_view()]
        )

        response = await get_agent_by_provider(mock_user, catalog=patch_agent_deps)

        assert len(response.agents) == 1
        patch_agent_deps.list_for_provider.assert_awaited_once_with(
            provider_id=mock_user.user_id
        )


class TestGetAgent:
    @pytest.mark.asyncio
    async def test_returns_public_agent_without_auth(self, patch_agent_deps):
        patch_agent_deps.get_visible.return_value = AgentRouteResponse(
            success=True, agent=_view()
        )

        response = await get_agent("agent-1", user=None, catalog=patch_agent_deps)

        assert response.agent.agent_id == "agent-1"
        patch_agent_deps.get_visible.assert_awaited_once_with(
            agent_id="agent-1", user_id=None
        )

    @pytest.mark.asyncio
    async def test_passes_user_id_for_visibility_check(
        self, mock_user, patch_agent_deps
    ):
        patch_agent_deps.get_visible.return_value = AgentRouteResponse(success=True)

        await get_agent("agent-1", user=mock_user, catalog=patch_agent_deps)

        patch_agent_deps.get_visible.assert_awaited_once_with(
            agent_id="agent-1", user_id=mock_user.user_id
        )

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_id_empty(self, mock_user, patch_agent_deps):
        with pytest.raises(HTTPException) as exc_info:
            await get_agent("", user=mock_user, catalog=patch_agent_deps)

        assert exc_info.value.status_code == 400


class TestGetAgentList:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("active_only", [False, True])
    async def test_lists_visible_agents(self, mock_user, patch_agent_deps, active_only):
        patch_agent_deps.list_visible.return_value = AgentRouteResponse(
            success=True, agents=[_view()]
        )

        response = await get_agent_list(
            active_only=active_only, user=mock_user, catalog=patch_agent_deps
        )

        assert response.success is True
        patch_agent_deps.list_visible.assert_awaited_once_with(
            user_id=mock_user.user_id, active_only=active_only
        )


class TestDeleteAgent:
    @pytest.mark.asyncio
    async def test_deletes_agent_owned_by_user(self, mock_user, patch_agent_deps):
        patch_agent_deps.delete.return_value = AgentRouteResponse(success=True)

        response = await delete_agent(
            _request({"agent_id": "agent-1"}), mock_user, catalog=patch_agent_deps
        )

        assert response.success is True
        patch_agent_deps.delete.assert_awaited_once_with(
            agent_id="agent-1", provider_id=mock_user.user_id
        )

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_id_missing(self, mock_user, patch_agent_deps):
        with pytest.raises(HTTPException) as exc_info:
            await delete_agent(_request({}), mock_user, catalog=patch_agent_deps)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 404])
    async def test_raises_catalog_rejections(self, mock_user, patch_agent_deps, status):
        patch_agent_deps.delete.return_value = AgentRouteResponse(
            success=False, error="rejected", status_code=status
        )

        with pytest.raises(HTTPException) as exc_info:
            await delete_agent(
                _request({"agent_id": "agent-1"}), mock_user, catalog=patch_agent_deps
            )

        assert exc_info.value.status_code == status


class TestGetAgentCardFromUrl:
    @pytest.mark.asyncio
    async def test_returns_agent_card(self, patch_agent_deps, mock_user):
        patch_agent_deps.resolve_card.return_value = AgentRouteResponse(success=True)

        response = await get_agent_card_from_url(
            InspectionCenterRequest(agent_url="https://agent.example"),
            user=mock_user,
            catalog=patch_agent_deps,
        )

        assert response.success is True
        patch_agent_deps.resolve_card.assert_awaited_once_with(
            agent_url="https://agent.example"
        )

    def test_body_requires_agent_url(self):
        with pytest.raises(ValidationError):
            InspectionCenterRequest.model_validate({})
