"""
Pytest configuration and shared fixtures for the test suite.

This module provides:
- Mock fixtures for database services
- Mock fixtures for authentication
- Sample data factories for common models
- Async test configuration
- Centralized patch targets for maintainability
"""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Sample data uses the internal (SDK-free) A2A models: the domain layer never
# holds SDK objects, which are confined to ``a2a_adapter``. Cards and tasks
# arriving from the wire are converted at that boundary.
from common.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)

# Configuration must be isolated before any application module is imported.
# isort: split
from tests.fakes import config_bootstrap as _config_bootstrap  # noqa: F401

# isort: split
from common.auth import ClerkUser
from common.dto.hitl import HITLApplicationRoute, HITLEvidenceOrigin, HITLPublicSource
from common.observability import events as _observability_events
from models.agent import Agent, AgentStatus
from models.hitl import HITLPromptType, HITLRequest, HITLStatus
from models.room import MessageContent, Room, RoomAgentMessage, RoomUserMessage

# A catalog violation (unknown event, missing field, reserved key) raises in
# tests and only annotates the line in production.
_observability_events.CATALOG.strict = True

FROZEN_TIME = datetime(2026, 1, 15, 12, 0, 0)


# =============================================================================
# Pytest Configuration
# =============================================================================


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use default event loop policy for async tests."""
    import asyncio

    return asyncio.DefaultEventLoopPolicy()


# =============================================================================
# Authentication Fixtures
# =============================================================================


@pytest.fixture
def mock_user() -> ClerkUser:
    """Create a mock authenticated user."""
    return ClerkUser(
        user_id="user_test_123",
        session_id="session_test_456",
        claims={
            "sub": "user_test_123",
            "email": "test@example.com",
            "username": "testuser",
        },
    )


@pytest.fixture
def mock_user_2() -> ClerkUser:
    """Create a second mock user for ownership tests."""
    return ClerkUser(
        user_id="user_test_789",
        session_id="session_test_012",
        claims={
            "sub": "user_test_789",
            "email": "other@example.com",
            "username": "otheruser",
        },
    )


@pytest.fixture
def mock_auth(mock_user):
    """Mock the authentication dependency."""

    async def override_get_current_user():
        return mock_user

    return override_get_current_user


@pytest.fixture
def mock_optional_auth(mock_user):
    """Mock the optional authentication dependency."""

    async def override_get_optional_user():
        return mock_user

    return override_get_optional_user


@pytest.fixture
def mock_no_auth():
    """Mock optional auth returning None (unauthenticated)."""

    async def override_get_optional_user():
        return None

    return override_get_optional_user


# =============================================================================
# Database Service Mocks
# =============================================================================


@pytest.fixture
def mock_db_service():
    """Create a mock database service with common methods."""
    mock = MagicMock()

    # Room operations
    mock.get_room_by_room_id = AsyncMock(return_value=None)
    mock.create_room = AsyncMock(return_value=True)
    mock.update_room = AsyncMock(return_value=True)
    mock.delete_room = AsyncMock(return_value=True)
    mock.get_rooms_by_owner_id = AsyncMock(return_value=[])

    # Agent operations
    mock.get_agent_by_agent_id = AsyncMock(return_value=None)
    mock.get_all_agents = AsyncMock(return_value=[])
    mock.get_all_active_agents = AsyncMock(return_value=[])
    mock.create_agent = AsyncMock(return_value=True)
    mock.update_agent = AsyncMock(return_value=True)
    mock.delete_agent = AsyncMock(return_value=True)
    mock.get_agents_by_provider_id = AsyncMock(return_value=[])

    # Message operations
    mock.get_room_user_message_by_message_id = AsyncMock(return_value=None)
    mock.get_room_agent_message_by_message_id = AsyncMock(return_value=None)
    mock.get_room_agent_messages_by_related_message_id = AsyncMock(return_value=[])
    mock.create_room_user_message = AsyncMock(return_value=True)
    mock.create_room_agent_message = AsyncMock(return_value=True)
    mock.get_room_messages_by_room_id = AsyncMock(return_value=[])

    # Memory operations
    mock.create_room_memory = AsyncMock(return_value=True)
    mock.update_room_memory = AsyncMock(return_value=True)
    mock.compact_turns_bulk = AsyncMock(return_value=True)

    # HITL operations
    mock.create_hitl_request = AsyncMock(return_value=True)
    mock.get_hitl_request = AsyncMock(return_value=None)
    mock.update_hitl_request = AsyncMock(return_value=True)
    mock.get_pending_hitl_requests = AsyncMock(return_value=[])

    return mock


@pytest.fixture
def mock_mongodb():
    """Create a mock MongoDB client."""
    mock = MagicMock()
    mock.cancel_message = AsyncMock(return_value=True)
    return mock


# =============================================================================
# Sample Data Factories
# =============================================================================


@pytest.fixture
def sample_agent_card() -> AgentCard:
    """Create a sample A2A agent card."""
    return AgentCard(
        name="TestAgent",
        description="A test agent for unit testing",
        url="https://test-agent.example.com/.well-known/agent.json",
        version="1.0.0",
        skills=[
            AgentSkill(
                id="test-skill",
                name="Test Skill",
                description="A test skill",
                tags=["test", "unit-testing"],
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            pushNotifications=True,
        ),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
    )


@pytest.fixture
def sample_agent(sample_agent_card, mock_user) -> Agent:
    """Create a sample agent."""
    return Agent(
        agent_id="test-agent-001",
        provider_id=mock_user.user_id,
        agent_card=sample_agent_card,
        normalized_url="test-agent.example.com",
        agent_status=AgentStatus.active,
        is_public=True,
    )


@pytest.fixture
def sample_private_agent(sample_agent_card, mock_user) -> Agent:
    """Create a sample private agent."""
    return Agent(
        agent_id="test-private-agent-001",
        provider_id=mock_user.user_id,
        agent_card=sample_agent_card,
        normalized_url="private-agent.example.com",
        agent_status=AgentStatus.active,
        is_public=False,
    )


@pytest.fixture
def sample_room(mock_user, sample_agent) -> Room:
    """Create a sample room with all fields populated (normal/non-debate mode)."""
    return Room(
        conversation_id="test-room-001",
        room_name="Test Room",
        room_owner_id=mock_user.user_id,
        room_owner_name="Test User",
        room_agent_set={sample_agent.agent_id: sample_agent.agent_card.name},
        room_created_at=FROZEN_TIME,
        extend_info={
            "debateMode": False,
            "use_supervisor": False,
        },
    )


@pytest.fixture
def sample_debate_room(mock_user, sample_agent) -> Room:
    """Create a sample room with debate mode enabled."""
    return Room(
        conversation_id="test-debate-room-001",
        room_name="Debate Room",
        room_owner_id=mock_user.user_id,
        room_owner_name="Test User",
        room_agent_set={sample_agent.agent_id: sample_agent.agent_card.name},
        room_created_at=FROZEN_TIME,
        extend_info={
            "debateMode": True,
            "use_supervisor": False,
        },
    )


@pytest.fixture
def sample_supervisor_room(mock_user, sample_agent) -> Room:
    """Create a sample room with supervisor mode enabled."""
    return Room(
        conversation_id="test-supervisor-room-001",
        room_name="Supervisor Room",
        room_owner_id=mock_user.user_id,
        room_owner_name="Test User",
        room_agent_set={sample_agent.agent_id: sample_agent.agent_card.name},
        room_created_at=FROZEN_TIME,
        extend_info={
            "debateMode": False,
            "use_supervisor": True,
        },
    )


@pytest.fixture
def sample_user_message(sample_room, mock_user) -> RoomUserMessage:
    """Create a sample user message."""
    return RoomUserMessage(
        conversation_id=sample_room.conversation_id,
        message_id="test-user-msg-001",
        user_id=mock_user.user_id,
        message_content=MessageContent(message_text="Hello, this is a test message"),
    )


@pytest.fixture
def sample_agent_message(
    sample_room, sample_agent, sample_user_message
) -> RoomAgentMessage:
    """Create a sample agent message."""
    return RoomAgentMessage(
        conversation_id=sample_room.conversation_id,
        message_id="test-agent-msg-001",
        agent_id=sample_agent.agent_id,
        related_message_id=sample_user_message.message_id,
        message_content=MessageContent(message_text="Hello! I'm the test agent."),
    )


@pytest.fixture
def sample_task() -> Task:
    """Create a sample A2A task."""
    return Task(
        id="test-task-001",
        contextId="test-context-001",
        status=TaskStatus(state=TaskState.completed),
    )


@pytest.fixture
def sample_agent_message_with_task(
    sample_room, sample_agent, sample_user_message, sample_task
) -> RoomAgentMessage:
    """Create a sample agent message with task tracking."""
    return RoomAgentMessage(
        conversation_id=sample_room.conversation_id,
        message_id="test-agent-task-msg-001",
        agent_id=sample_agent.agent_id,
        related_message_id=sample_user_message.message_id,
        user_id=sample_user_message.user_id,
        message_content=MessageContent(
            message_text="Processing...",
            message_task=sample_task,
        ),
        has_task_tracking=True,
        task_created_at=FROZEN_TIME,
        task_updated_at=FROZEN_TIME,
    )


@pytest.fixture
def sample_hitl_request(sample_room, sample_user_message) -> HITLRequest:
    """Create a sample HITL request."""
    return HITLRequest(
        schema_version=3,
        request_id="test-hitl-req-001",
        interaction_id="test-hitl-req-001",
        question_index=0,
        question_count=1,
        conversation_id=sample_room.conversation_id,
        user_message_id=sample_user_message.message_id,
        application_route=HITLApplicationRoute.SUPERVISOR_RUN,
        public_source=HITLPublicSource.SUPERVISOR,
        evidence_origin=HITLEvidenceOrigin.SUPERVISOR,
        prompt="Could you please clarify what you mean?",
        prompt_type=HITLPromptType.TEXT,
        status=HITLStatus.PENDING,
        created_at=FROZEN_TIME,
    )


# =============================================================================
# FastAPI Test Client Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def app():
    """Create a FastAPI app instance for testing (imported once per session)."""
    from main import app as main_app

    return main_app


# =============================================================================
# Service Mocks
# =============================================================================


@pytest.fixture
def mock_sse_transport():
    """Create a mock SSE transport."""
    transport = MagicMock()
    transport.add_connection = AsyncMock()
    transport.remove_connection = AsyncMock()
    transport.broadcast_to_room = AsyncMock()
    transport.cancel_message = MagicMock()
    transport.cancel_message_and_broadcast = AsyncMock()
    transport.clear_cancellation = MagicMock()
    transport.get_room_status = MagicMock(return_value={"connections": 0})
    return transport


@pytest.fixture
def mock_hitl_service():
    """Create a mock HITL service."""
    mock = MagicMock()
    mock.get_pending_requests = AsyncMock(return_value=[])
    mock.cancel_request = AsyncMock()
    mock.cancel_requests_for_message = AsyncMock()
    mock.get_pending_hitl = AsyncMock(return_value=[])
    mock.cancel_hitl_interaction = AsyncMock(return_value=1)
    return mock


@pytest.fixture
def mock_a2a_service():
    """Create a mock A2A service."""
    mock = MagicMock()
    mock.send_task = AsyncMock()
    mock.get_task_status = AsyncMock()
    mock.cancel_task = AsyncMock()
    return mock


@pytest.fixture
def mock_room_message_center():
    """Create a mock room message center."""
    mock = MagicMock()
    mock.process_room_user_message = AsyncMock()
    return mock


@pytest.fixture
def mock_agent_catalog():
    """Create a mock agent catalog."""
    mock = MagicMock()
    mock.register = AsyncMock()
    mock.list_for_provider = AsyncMock()
    mock.delete = AsyncMock()
    mock.resolve_card = AsyncMock()
    mock.get_visible = AsyncMock()
    mock.list_visible = AsyncMock()
    return mock


@pytest.fixture
def mock_chat_rooms():
    """Create a mock ChatRooms port."""
    mock = MagicMock()
    mock.create_room = AsyncMock()
    mock.get_room = AsyncMock()
    mock.list_rooms_for_owner = AsyncMock()
    mock.list_room_history = AsyncMock()
    mock.rename_room = AsyncMock()
    mock.update_history_fields = AsyncMock()
    mock.update_room_default_mode = AsyncMock(return_value=True)
    mock.delete_room = AsyncMock()
    mock.read_messages = AsyncMock()
    return mock


# =============================================================================
# Settings Mocks
# =============================================================================


@pytest.fixture
def mock_settings():
    """Create mock settings for tests."""
    with patch("common.config.loader.settings") as mock:
        mock.clerk_secret_key = "test_secret_key"
        mock.mongodb_uri = "mongodb://localhost:27017"
        mock.mongodb_database = "test_db"
        mock.openai_api_key = "test_openai_key"
        mock.compaction_enabled = True
        mock.compaction_max_full_turns = 20
        mock.compaction_max_total_tokens = 80000
        mock.compaction_preserve_recent = 10
        mock.compaction_content_ttl_days = 0
        yield mock


# =============================================================================
# Utility Functions
# =============================================================================


def create_mock_response(success: bool = True, data: Any = None, error: str = None):
    """Helper to create mock response objects."""
    response = MagicMock()
    response.success = success
    response.data = data
    response.error = error
    response.status_code = 200 if success else 400
    return response


# =============================================================================
# Composite Patch Fixtures (flatten deeply nested patches)
# =============================================================================


@pytest.fixture
def make_api_gateway_deps(
    mock_db_service,
    mock_agent_catalog,
    mock_chat_rooms,
    mock_sse_transport,
    mock_hitl_service,
    mock_s3_service,
):
    """Factory for complete APIGatewayDeps instances."""
    from api_gateway.dependencies import APIGatewayDeps
    from common.dto import ExecutionAck

    def _make(**overrides):
        execution_engine = MagicMock()
        execution_engine.execute = AsyncMock(
            return_value=ExecutionAck(success=True, message_id="new-message-id")
        )
        execution_engine.start_orchestration = AsyncMock()
        execution_engine.schedule_orchestration = MagicMock()
        execution_engine.get_runs_for_room = AsyncMock(return_value=[])
        execution_engine.cancel = AsyncMock(return_value=True)

        agent_service = MagicMock()
        agent_service.get_agent = AsyncMock(return_value=None)
        agent_service.get_agent_by_agent_id = AsyncMock(return_value=None)
        agent_service.get_agents_by_ids = AsyncMock(return_value=[])

        capability_issue_service = MagicMock()
        capability_issue_service.get_issues_for_agent = AsyncMock(return_value=[])
        capability_issue_service.get_issue_by_id = AsyncMock(return_value=None)
        capability_issue_service.resolve_all_for_agent = AsyncMock(return_value=0)
        capability_issue_service.resolve_issue = AsyncMock(return_value=None)

        room_ownership_reader = MagicMock()
        room_ownership_reader.get_room_owner = AsyncMock(return_value=None)

        agent_selection_service = MagicMock()
        agent_selection_service.suggest_agents = AsyncMock()

        defaults = {
            "agent_catalog": mock_agent_catalog,
            "agent_service": agent_service,
            "agent_matcher": MagicMock(),
            "capability_issue_service": capability_issue_service,
            "agent_group_store": mock_db_service,
            "file_storage": mock_s3_service,
            "room_ownership_reader": room_ownership_reader,
            "hitl_manager": mock_hitl_service,
            "inspection_center": MagicMock(),
            "chat_rooms": mock_chat_rooms,
            "room_store": mock_db_service,
            "agent_selection_service": agent_selection_service,
            "execution_engine": execution_engine,
            "sse_store": mock_db_service,
            "sse_transport": mock_sse_transport,
            "session_facade": MagicMock(),
        }
        defaults.update(overrides)
        return APIGatewayDeps(**defaults)

    return _make


@pytest.fixture
def patch_sse_deps(
    mock_db_service,
    mock_sse_transport,
    mock_mongodb,
    mock_hitl_service,
    make_api_gateway_deps,
):
    """Return explicit SSE endpoint dependencies."""
    execution_engine = MagicMock()
    execution_engine.cancel = AsyncMock(return_value=True)
    return {
        "db_service": mock_db_service,
        "sse_transport": mock_sse_transport,
        "mongodb": mock_mongodb,
        "hitl_service": mock_hitl_service,
        "execution_engine": execution_engine,
        "deps": make_api_gateway_deps(
            sse_store=mock_db_service,
            sse_transport=mock_sse_transport,
            hitl_manager=mock_hitl_service,
            execution_engine=execution_engine,
        ),
    }


@pytest.fixture
def patch_chat_rooms_deps(mock_db_service, mock_chat_rooms, make_api_gateway_deps):
    """Return explicit room endpoint dependencies."""
    from common.dto import ExecutionAck

    execution_engine = MagicMock()
    execution_engine.execute = AsyncMock(
        return_value=ExecutionAck(success=True, message_id="new-message-id")
    )
    execution_engine.start_orchestration = AsyncMock()
    execution_engine.get_runs_for_room = AsyncMock(return_value=[])
    execution_engine.cancel = AsyncMock(return_value=True)
    return {
        "db_service": mock_db_service,
        "chat_rooms": mock_chat_rooms,
        "execution_engine": execution_engine,
        "deps": make_api_gateway_deps(
            room_store=mock_db_service,
            chat_rooms=mock_chat_rooms,
            execution_engine=execution_engine,
        ),
    }


@pytest.fixture
def patch_agent_deps(mock_agent_catalog):
    """Return the agent catalog dependency used by the agent routes."""
    return mock_agent_catalog


# =============================================================================
# S3 / File Upload Fixtures
# =============================================================================


@pytest.fixture
def mock_s3_service():
    """Create a mock S3 service with common methods."""
    mock = AsyncMock()
    mock.upload_file = AsyncMock(return_value="uploads/room1/f1/test.png")
    mock.generate_presigned_url = AsyncMock(
        return_value="https://s3.example.com/presigned"
    )
    mock.batch_presigned_urls = AsyncMock(
        return_value={"uploads/room1/f1/test.png": "https://s3.example.com/presigned"}
    )
    mock.delete_file = AsyncMock()
    mock.delete_prefix = AsyncMock()
    mock.head_file = AsyncMock(return_value={"ContentLength": 1024})
    mock.download_text = AsyncMock(return_value="text content")
    return mock


@pytest.fixture
def sample_file_upload_metadata():
    """Factory for file upload metadata dicts (as stored in MongoDB)."""

    def _make(
        file_id="f_test_001",
        conversation_id="room_test_123",
        user_id="user_test_123",
        mime_type="image/png",
        file_name="test.png",
        size_bytes=2048,
    ):
        return {
            "file_id": file_id,
            "room_id": conversation_id,
            "user_id": user_id,
            "s3_key": f"uploads/{conversation_id}/{file_id}/{file_name}",
            "mime_type": mime_type,
            "file_name": file_name,
            "size_bytes": size_bytes,
            "uploaded_at": FROZEN_TIME.isoformat(),
        }

    return _make
