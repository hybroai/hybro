"""
HTTP integration tests that exercise endpoints through the real FastAPI stack.

Unlike unit tests (test_api_*.py) which call endpoint functions directly,
these tests use AsyncClient to verify that auth, request parsing, response
serialization, and error handling all work through the HTTP transport layer.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from common.auth import (
    ClerkUser,
    get_current_user,
    get_current_user_or_service,
    get_optional_user,
)
from models.response import (
    AgentCenterResponse,
    RoomCenterRoomMessageResponse,
    RoomCenterRoomSettingResponse,
)


@pytest.fixture
def integration_user() -> ClerkUser:
    return ClerkUser(
        user_id="integ_user_001",
        session_id="integ_session_001",
        claims={"sub": "integ_user_001", "email": "integ@test.com"},
    )


@pytest.fixture
def integration_app(integration_user):
    """App with auth overrides -- only mock authentication, not business logic."""
    from main import app

    original_overrides = dict(app.dependency_overrides)

    async def _mock_auth():
        return integration_user

    app.dependency_overrides[get_current_user] = _mock_auth
    app.dependency_overrides[get_optional_user] = _mock_auth
    # /agent/registerAgent accepts a Clerk user OR the registrar service token.
    # Override it too, or tests using this fixture 401 before reaching the
    # behaviour they assert. test_register_agent_requires_auth clears the
    # overrides itself, so real auth stays covered.
    app.dependency_overrides[get_current_user_or_service] = _mock_auth

    yield app

    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


@pytest.fixture
async def http_client(integration_app):
    async with AsyncClient(
        transport=ASGITransport(app=integration_app),
        base_url="http://test",
    ) as client:
        yield client


# =============================================================================
# Auth Guard Integration Tests
# =============================================================================


class TestAuthGuardIntegration:
    """Verify that protected endpoints reject unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_register_agent_requires_auth(self):
        """POST /agent/registerAgent should 401 without auth."""
        from main import app

        original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides.clear()

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/agent/registerAgent",
                    json={"agent_url": "https://example.com"},
                )

            assert resp.status_code == 401
        finally:
            app.dependency_overrides.update(original_overrides)


# =============================================================================
# Room Center HTTP Integration Tests
# =============================================================================


class TestRoomCenterHTTPIntegration:
    """Verify room endpoints through the HTTP stack."""

    @pytest.mark.asyncio
    async def test_create_room_returns_json(
        self,
        http_client,
        integration_app,
        integration_user,
    ):
        """POST /roomCenter/createNewRoom should return well-formed JSON."""
        mock_rc = MagicMock()
        mock_rc.create_new_room = AsyncMock(
            return_value=RoomCenterRoomSettingResponse(
                success=True, room_id="room-http-001"
            )
        )

        from api_gateway.routes import room_routes as room_api

        integration_app.dependency_overrides[room_api.get_room_center] = lambda: mock_rc
        try:
            resp = await http_client.post(
                "/api/v1/roomCenter/createNewRoom",
                json={
                    "room_name": "HTTP Test Room",
                    "room_owner_name": "Tester",
                    "room_agent_set": {},
                },
            )
        finally:
            integration_app.dependency_overrides.pop(room_api.get_room_center, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["room_id"] == "room-http-001"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pagination",
        [
            {"limit": True},
            {"cursor": "not+base64"},
        ],
    )
    async def test_invalid_room_timeline_pagination_is_body_level_400(
        self,
        http_client,
        integration_app,
        integration_user,
        pagination,
    ):
        from models.room import Room
        from room.compat.runtime import RoomServices

        mock_room = Room(
            room_id="room-http-invalid-page",
            room_name="Test",
            room_owner_id=integration_user.user_id,
            room_owner_name="Tester",
            room_agent_set={},
        )
        mock_db = MagicMock()
        mock_db.get_room_by_room_id = AsyncMock(return_value=mock_room)

        from api_gateway.routes import room_routes as room_api

        integration_app.dependency_overrides[room_api.get_room_store] = lambda: mock_db
        integration_app.dependency_overrides[room_api.get_room_center] = RoomServices
        try:
            resp = await http_client.post(
                "/api/v1/roomCenter/inquiryRoomMessagesByRoomId",
                json={"room_id": mock_room.room_id, **pagination},
            )
        finally:
            integration_app.dependency_overrides.pop(room_api.get_room_store, None)
            integration_app.dependency_overrides.pop(room_api.get_room_center, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["status_code"] == 400

    @pytest.mark.asyncio
    async def test_inquiry_room_messages_returns_json(
        self,
        http_client,
        integration_app,
        integration_user,
    ):
        """POST /roomCenter/inquiryRoomMessagesByRoomId via HTTP."""
        from models.room import Room

        mock_room = Room(
            room_id="room-http-002",
            room_name="Test",
            room_owner_id=integration_user.user_id,
            room_owner_name="Tester",
            room_agent_set={},
        )

        mock_db = MagicMock()
        mock_db.get_room_by_room_id = AsyncMock(return_value=mock_room)

        mock_rc = MagicMock()
        mock_rc.inquiry_room_messages_by_room_id = AsyncMock(
            return_value=RoomCenterRoomMessageResponse(success=True, message_list=[])
        )

        from api_gateway.routes import room_routes as room_api

        integration_app.dependency_overrides[room_api.get_room_store] = lambda: mock_db
        integration_app.dependency_overrides[room_api.get_room_center] = lambda: mock_rc
        try:
            resp = await http_client.post(
                "/api/v1/roomCenter/inquiryRoomMessagesByRoomId",
                json={"room_id": "room-http-002"},
            )
        finally:
            integration_app.dependency_overrides.pop(room_api.get_room_store, None)
            integration_app.dependency_overrides.pop(room_api.get_room_center, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True


# =============================================================================
# Agent HTTP Integration Tests
# =============================================================================


class TestAgentHTTPIntegration:
    """Verify agent endpoints through the HTTP stack."""

    @pytest.mark.asyncio
    async def test_get_active_agents_returns_json(self, http_client, integration_app):
        """GET /agent/getAllAgents?active_only=true should serialize correctly."""
        mock_ac = MagicMock()
        mock_ac.list_visible_agents_for_route = AsyncMock(
            return_value=AgentCenterResponse(success=True, agents=[])
        )

        from api_gateway.routes import agent_routes as agent_api

        integration_app.dependency_overrides[agent_api.get_agent_center] = lambda: (
            mock_ac
        )
        try:
            resp = await http_client.get("/api/v1/agent/getAllAgents?active_only=true")
        finally:
            integration_app.dependency_overrides.pop(agent_api.get_agent_center, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        mock_ac.list_visible_agents_for_route.assert_awaited_once_with(
            user_id="integ_user_001",
            active_only=True,
        )

    @pytest.mark.asyncio
    async def test_register_agent_validates_missing_url(
        self, http_client, integration_app
    ):
        """POST /agent/registerAgent should 400 when agent_url missing."""
        from api_gateway.routes import agent_routes as agent_api

        integration_app.dependency_overrides[agent_api.get_agent_center] = lambda: (
            MagicMock()
        )
        try:
            resp = await http_client.post(
                "/api/v1/agent/registerAgent",
                json={},
            )
        finally:
            integration_app.dependency_overrides.pop(agent_api.get_agent_center, None)

        assert resp.status_code == 400
        body = resp.json()
        assert "agent_url" in body.get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_get_agent_validates_empty_id(self, http_client, integration_app):
        """GET /agent/getAgent/ with whitespace ID returns error response."""
        from api_gateway.routes import agent_routes as agent_api

        integration_app.dependency_overrides[agent_api.get_agent_center] = lambda: (
            MagicMock()
        )
        integration_app.dependency_overrides[agent_api.get_agent_liveness_checker] = (
            lambda: AsyncMock()
        )
        try:
            resp = await http_client.get("/api/v1/agent/getAgent/%20")
        finally:
            integration_app.dependency_overrides.pop(agent_api.get_agent_center, None)
            integration_app.dependency_overrides.pop(
                agent_api.get_agent_liveness_checker, None
            )

        # Through HTTP, whitespace is URL-decoded to " " which is truthy,
        # so the endpoint proceeds and the service returns success=False.
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False


# =============================================================================
# HITL HTTP Integration Tests
# =============================================================================


class TestHITLHTTPIntegration:
    """Verify HITL endpoints through the HTTP stack."""

    @pytest.mark.asyncio
    async def test_get_pending_through_http(
        self, http_client, integration_app, integration_user
    ):
        """GET /rooms/{room_id}/hitl/pending should return JSON array."""
        room_ownership_reader = MagicMock()
        room_ownership_reader.get_room_owner = AsyncMock(
            return_value=integration_user.user_id
        )

        mock_hitl = MagicMock()
        mock_hitl.get_pending_hitl = AsyncMock(return_value=[])

        from api_gateway.routes import hitl_routes as hitl_api

        integration_app.dependency_overrides[hitl_api.get_room_ownership_reader] = (
            lambda: room_ownership_reader
        )
        integration_app.dependency_overrides[hitl_api.get_hitl_manager] = lambda: (
            mock_hitl
        )
        try:
            resp = await http_client.get("/api/v1/rooms/room-hitl-http/hitl/pending")
        finally:
            integration_app.dependency_overrides.pop(
                hitl_api.get_room_ownership_reader, None
            )
            integration_app.dependency_overrides.pop(hitl_api.get_hitl_manager, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["requests"] == []
