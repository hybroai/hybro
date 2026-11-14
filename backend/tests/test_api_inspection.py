"""
Unit tests for api_gateway/routes/inspection_routes.py endpoints.

Tests cover:
- request body validation
- delegation to the inspection service
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api_gateway.routes.inspection_routes import inspect_a2a_connection, inspect_agent
from models.request import InspectionCenterRequest


@pytest.mark.parametrize("body", [{}, {"agent_url": ""}, {"agent_url": "   "}])
def test_body_rejects_missing_agent_url(body):
    with pytest.raises(ValidationError):
        InspectionCenterRequest.model_validate(body)


class TestInspectAgent:
    @pytest.mark.asyncio
    async def test_delegates_to_inspection_center(self):
        expected = {"name": "TestAgent", "status": "ok"}

        mock_ic = MagicMock()
        mock_ic.inspect_agent_card = AsyncMock(return_value=expected)
        result = await inspect_agent(
            InspectionCenterRequest(agent_url="https://agent.example.com"),
            center=mock_ic,
        )

        assert result == expected
        mock_ic.inspect_agent_card.assert_called_once()
        call_arg = mock_ic.inspect_agent_card.call_args[0][0]
        assert call_arg.agent_url == "https://agent.example.com"


class TestInspectA2AConnection:
    @pytest.mark.asyncio
    async def test_delegates_to_inspection_center(self):
        expected = {"connected": True}

        mock_ic = MagicMock()
        mock_ic.inspect_a2a_connection = AsyncMock(return_value=expected)
        result = await inspect_a2a_connection(
            InspectionCenterRequest(agent_url="https://agent.example.com"),
            center=mock_ic,
        )

        assert result == expected
        mock_ic.inspect_a2a_connection.assert_called_once()
