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

from api_gateway.routes.agent_routes import (
    delete_agent,
    discover_local_agents,
    get_agent,
    get_agent_by_provider,
    get_agent_card_from_url,
    get_agent_list,
    register_agent,
)
from local_agents.models import DiscoveryTrigger, LocalAgentDiscoveryResult
from models.response import AgentCenterResponse

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
# Agent Registration Tests
# =============================================================================


class TestRegisterAgent:
    """Tests for register_agent endpoint."""

    @pytest.mark.asyncio
    async def test_registers_agent_with_user_as_provider(
        self, mock_user, patch_agent_deps, sample_agent_card
    ):
        """Should register agent with authenticated user as provider."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_url": "https://test-agent.example.com/.well-known/agent.json",
            }
        )

        expected_response = AgentCenterResponse(
            success=True,
            agent_id="new-agent-id",
            status_code=200,
        )
        patch_agent_deps.register_agent.return_value = expected_response

        response = await register_agent(
            mock_request, mock_user, center=patch_agent_deps
        )

        assert response.success is True

        # Verify provider_id is set to user's ID
        call_args = patch_agent_deps.register_agent.call_args[0][0]
        assert call_args.provider_id == mock_user.user_id

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_url_missing(self, mock_user, patch_agent_deps):
        """Should raise 400 when agent_url is not provided."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={})

        with pytest.raises(HTTPException) as exc_info:
            await register_agent(mock_request, mock_user, center=patch_agent_deps)

        assert exc_info.value.status_code == 400
        assert "agent_url is required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_400_for_duplicate_agent(self, mock_user, patch_agent_deps):
        """Should raise 400 when agent URL is already registered."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_url": "https://existing-agent.example.com/.well-known/agent.json",
            }
        )

        duplicate_response = AgentCenterResponse(
            success=False,
            error="Agent with this URL already exists",
            status_code=400,
        )
        patch_agent_deps.register_agent.return_value = duplicate_response

        with pytest.raises(HTTPException) as exc_info:
            await register_agent(mock_request, mock_user, center=patch_agent_deps)

        assert exc_info.value.status_code == 400


# =============================================================================
# Agent Retrieval Tests
# =============================================================================


class TestGetAgentByProvider:
    """Tests for get_agent_by_provider endpoint."""

    @pytest.mark.asyncio
    async def test_returns_agents_for_authenticated_user(
        self, mock_user, patch_agent_deps, sample_agent
    ):
        """Should return agents owned by the authenticated user."""
        expected_response = AgentCenterResponse(
            success=True,
            agents=[sample_agent],
        )
        patch_agent_deps.get_agents_by_provider_id.return_value = expected_response

        response = await get_agent_by_provider(mock_user, center=patch_agent_deps)

        assert response.success is True
        assert len(response.agents) == 1

    @pytest.mark.asyncio
    async def test_populates_provider_name_when_agent_card_has_no_provider(
        self, mock_user, patch_agent_deps, sample_agent
    ):
        """Should resolve and set provider_name when agent_card.provider is absent."""
        sample_agent.agent_card.provider = None
        expected_response = AgentCenterResponse(
            success=True,
            agents=[sample_agent],
        )
        sample_agent.provider_name = "Test User"
        patch_agent_deps.get_agents_by_provider_id.return_value = expected_response

        response = await get_agent_by_provider(mock_user, center=patch_agent_deps)

        assert response.agents[0].provider_name == "Test User"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_provider_name_when_organization_is_set(
        self, mock_user, patch_agent_deps, sample_agent
    ):
        """Should not set provider_name when agent_card.provider.organization is already set."""
        from a2a.types import AgentProvider

        sample_agent.agent_card.provider = AgentProvider(
            organization="Existing Org", url="http://example.com"
        )
        expected_response = AgentCenterResponse(
            success=True,
            agents=[sample_agent],
        )
        patch_agent_deps.get_agents_by_provider_id.return_value = expected_response

        response = await get_agent_by_provider(mock_user, center=patch_agent_deps)

        assert response.agents[0].provider_name is None


class TestGetAgent:
    """Tests for get_agent endpoint."""

    @pytest.mark.asyncio
    async def test_returns_public_agent_without_auth(
        self, patch_agent_deps, sample_agent
    ):
        """Should return public agent even without authentication."""
        expected_response = AgentCenterResponse(
            success=True,
            agent=sample_agent,
        )
        patch_agent_deps.query_agent_by_agent_id.return_value = expected_response

        response = await get_agent(
            sample_agent.agent_id,
            user=None,
            center=patch_agent_deps,
            liveness_checker=AsyncMock(side_effect=lambda agent: agent),
        )

        assert response.success is True
        assert response.agent.agent_id == sample_agent.agent_id

    @pytest.mark.asyncio
    async def test_passes_user_id_for_visibility_check(
        self, mock_user, patch_agent_deps, sample_agent
    ):
        """Should pass user_id for private agent visibility check."""
        expected_response = AgentCenterResponse(
            success=True,
            agent=sample_agent,
        )
        patch_agent_deps.query_agent_by_agent_id.return_value = expected_response

        await get_agent(
            sample_agent.agent_id,
            user=mock_user,
            center=patch_agent_deps,
            liveness_checker=AsyncMock(side_effect=lambda agent: agent),
        )

        # Verify user_id was passed in request
        call_args = patch_agent_deps.query_agent_by_agent_id.call_args[0][0]
        assert call_args.user_id == mock_user.user_id

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_id_empty(self, mock_user, patch_agent_deps):
        """Should raise 400 when agent_id is empty."""
        with pytest.raises(HTTPException) as exc_info:
            await get_agent(
                "",
                user=mock_user,
                center=patch_agent_deps,
                liveness_checker=AsyncMock(side_effect=lambda agent: agent),
            )

        assert exc_info.value.status_code == 400


class TestGetAgentList:
    """Tests for get_agent_list endpoint."""

    @pytest.mark.asyncio
    async def test_returns_all_agents(self, patch_agent_deps, sample_agent):
        expected_response = AgentCenterResponse(
            success=True,
            agents=[sample_agent],
        )
        patch_agent_deps.get_all_agents.return_value = expected_response

        response = await get_agent_list(
            active_only=False,
            user=None,
            center=patch_agent_deps,
        )

        assert response.success is True
        patch_agent_deps.list_visible_agents_for_route.assert_awaited_once_with(
            user_id=None,
            active_only=False,
        )

    @pytest.mark.asyncio
    async def test_filters_active_agents_for_owner(
        self, mock_user, patch_agent_deps, sample_agent, sample_private_agent
    ):
        expected_response = AgentCenterResponse(
            success=True,
            agents=[sample_agent, sample_private_agent],
        )
        patch_agent_deps.get_all_active_agents.return_value = expected_response

        response = await get_agent_list(
            active_only=True,
            user=mock_user,
            center=patch_agent_deps,
        )

        assert response.success is True
        patch_agent_deps.list_visible_agents_for_route.assert_awaited_once_with(
            user_id=mock_user.user_id,
            active_only=True,
        )


# =============================================================================
# Agent Deletion Tests
# =============================================================================


class TestDeleteAgent:
    """Tests for delete_agent endpoint."""

    @pytest.mark.asyncio
    async def test_deletes_agent_owned_by_user(
        self, mock_user, patch_agent_deps, sample_agent
    ):
        """Should delete agent when user is the owner."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_id": sample_agent.agent_id,
            }
        )

        mock_agent_service = MagicMock()
        mock_agent_service.get_agent_by_agent_id = AsyncMock(return_value=sample_agent)

        expected_response = AgentCenterResponse(success=True)
        patch_agent_deps.remove_agent.return_value = expected_response

        response = await delete_agent(mock_request, mock_user, center=patch_agent_deps)

        assert response.success is True

    @pytest.mark.asyncio
    async def test_raises_400_when_agent_id_missing(self, mock_user, patch_agent_deps):
        """Should raise 400 when agent_id is not provided."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={})

        with pytest.raises(HTTPException) as exc_info:
            await delete_agent(mock_request, mock_user, center=patch_agent_deps)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_raises_404_when_agent_not_found(self, mock_user, patch_agent_deps):
        """Should raise 404 when agent doesn't exist."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_id": "nonexistent-agent",
            }
        )

        patch_agent_deps.delete_agent_from_route.side_effect = None
        patch_agent_deps.delete_agent_from_route.return_value = AgentCenterResponse(
            success=False,
            error="Agent not found",
            status_code=404,
        )

        with pytest.raises(HTTPException) as exc_info:
            await delete_agent(mock_request, mock_user, center=patch_agent_deps)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_403_when_not_owner(
        self, mock_user_2, sample_agent, patch_agent_deps
    ):
        """Should raise 403 when user is not the agent owner."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_id": sample_agent.agent_id,
            }
        )

        patch_agent_deps.delete_agent_from_route.side_effect = None
        patch_agent_deps.delete_agent_from_route.return_value = AgentCenterResponse(
            success=False,
            error="You do not have permission to delete this agent",
            status_code=403,
        )

        with pytest.raises(HTTPException) as exc_info:
            await delete_agent(mock_request, mock_user_2, center=patch_agent_deps)

        assert exc_info.value.status_code == 403


# =============================================================================
# Agent Card from URL Tests
# =============================================================================


class TestGetAgentCardFromUrl:
    """Tests for get_agent_card_from_url endpoint."""

    @pytest.mark.asyncio
    async def test_returns_agent_card(self, patch_agent_deps, sample_agent_card):
        """Should return agent card from URL."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "agent_url": "https://test-agent.example.com/.well-known/agent.json",
            }
        )

        expected_response = AgentCenterResponse(
            success=True,
            agent_card=sample_agent_card,
        )
        patch_agent_deps.get_agent_card_from_url.return_value = expected_response

        response = await get_agent_card_from_url(mock_request, center=patch_agent_deps)

        assert response.success is True
        assert response.agent_card.name == sample_agent_card.name

    @pytest.mark.asyncio
    async def test_raises_400_when_url_missing(self, patch_agent_deps):
        """Should raise 400 when agent_url is not provided."""
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={})

        with pytest.raises(HTTPException) as exc_info:
            await get_agent_card_from_url(mock_request, center=patch_agent_deps)

        assert exc_info.value.status_code == 400
