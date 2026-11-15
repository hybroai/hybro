"""
Unit tests for Pydantic models.

Tests cover:
- Model validation
- Serialization/deserialization
- Default values
- Field constraints
"""

from datetime import datetime
from uuid import uuid4

from common.types import Task, TaskState, TaskStatus
from models.agent import Agent, AgentStatus
from models.hitl import HITLPromptType, HITLRequest, HITLStatus
from models.room import (
    CoordinatorAgentId,
    MessageContent,
    Room,
    RoomAgentMessage,
    RoomUserMessage,
)

# =============================================================================
# Room Model Tests
# =============================================================================


class TestTaskPayloadSerialization:
    """Tests for task payload serialization inside room message content."""

    def test_task_payloads_serialize_with_runtime_serializer(self):
        task = Task(
            id="task-1",
            contextId="ctx-1",
            status=TaskStatus(state=TaskState.working),
        )

        content = MessageContent(message_task=task)
        status = content.model_dump(mode="json")["message_task"]["status"]
        assert status["message"] is None
        assert status["state"] == "working"
        # The internal status model stamps its own timestamp on construction,
        # so the serializer always renders an ISO-8601 string here. The SDK
        # shape this used to pin (a proto Timestamp that serialized to None)
        # no longer exists.
        assert isinstance(status["timestamp"], str)


class TestRoomModel:
    """Tests for Room model."""

    def test_creates_room_with_defaults(self):
        """Should create room with default values."""
        room = Room(
            room_name="Test Room",
            room_owner_id="user-123",
            room_owner_name="Test User",
        )

        assert room.conversation_id is not None
        assert room.room_name == "Test Room"
        assert room.room_owner_id == "user-123"
        assert room.room_agent_set == {}

    def test_creates_room_with_agents(self):
        """Should create room with agent set."""
        agent_set = {"agent-1": "Agent One", "agent-2": "Agent Two"}
        room = Room(
            room_name="Test Room",
            room_owner_id="user-123",
            room_owner_name="Test User",
            room_agent_set=agent_set,
        )

        assert room.room_agent_set == agent_set
        assert len(room.room_agent_set) == 2

    def test_room_serialization(self):
        """Should serialize room to dict."""
        room = Room(
            room_name="Test Room",
            room_owner_id="user-123",
            room_owner_name="Test User",
        )

        data = room.model_dump()

        assert "conversation_id" in data
        assert "room_name" in data
        assert data["room_name"] == "Test Room"


class TestRoomUserMessageModel:
    """Tests for RoomUserMessage model."""

    def test_creates_user_message(self):
        """Should create user message with correct type."""
        message = RoomUserMessage(
            conversation_id="room-123",
            message_id="msg-456",
            user_id="user-789",
            message_content=MessageContent(message_text="Hello"),
        )

        assert message.message_type == "user"
        assert message.conversation_id == "room-123"
        assert message.message_content.message_text == "Hello"

    def test_user_message_with_extend_info(self):
        """Should support extend_info field."""
        message = RoomUserMessage(
            conversation_id="room-123",
            message_id="msg-456",
            message_content=MessageContent(message_text="Hello"),
            extend_info={"custom_field": "custom_value"},
        )

        assert message.extend_info["custom_field"] == "custom_value"


class TestRoomAgentMessageModel:
    """Tests for RoomAgentMessage model."""

    def test_creates_agent_message(self):
        """Should create agent message with correct type."""
        message = RoomAgentMessage(
            conversation_id="room-123",
            message_id="msg-456",
            agent_id="agent-789",
            message_content=MessageContent(message_text="Response"),
        )

        assert message.message_type == "agent"
        assert message.agent_id == "agent-789"

    def test_agent_message_with_task_tracking(self):
        """Should support task tracking fields."""
        task = Task(
            id="task-123",
            contextId="context-123",
            status=TaskStatus(state=TaskState.working),
        )

        message = RoomAgentMessage(
            conversation_id="room-123",
            message_id="msg-456",
            agent_id="agent-789",
            message_content=MessageContent(
                message_text="Processing",
                message_task=task,
            ),
            has_task_tracking=True,
            task_created_at=datetime.now(),
        )

        assert message.has_task_tracking is True
        assert message.message_content.message_task is not None

    def test_agent_message_step_tracking(self):
        """Should support step tracking fields."""
        message = RoomAgentMessage(
            conversation_id="room-123",
            message_id="msg-456",
            agent_id="agent-789",
            message_content=MessageContent(message_text="Step 1"),
            step_number=1,
            total_steps=3,
        )

        assert message.step_number == 1
        assert message.total_steps == 3


class TestCoordinatorAgentId:
    """Tests for CoordinatorAgentId enum."""

    def test_coordinator_agent_ids(self):
        """Should have expected coordinator agent IDs."""
        assert CoordinatorAgentId.SUPERVISOR_ERROR == "supervisor_error"
        assert CoordinatorAgentId.SYSTEM == "system"
        # Deprecated but still present for backward compat


# =============================================================================
# Agent Model Tests
# =============================================================================


class TestAgentModel:
    """Tests for Agent model."""

    def test_creates_agent_with_defaults(self, sample_agent_card):
        """Should create agent with default values."""
        agent = Agent(
            is_public=True,
            agent_id=str(uuid4()),
            agent_card=sample_agent_card,
        )

        assert agent.agent_status == AgentStatus.active
        assert agent.is_public is True
        assert agent.call_count == 0
        assert agent.like_count == 0

    def test_agent_status_serialization(self, sample_agent_card):
        """Should serialize agent status to string."""
        agent = Agent(
            is_public=True,
            agent_id=str(uuid4()),
            agent_card=sample_agent_card,
            agent_status=AgentStatus.inactive,
        )

        data = agent.model_dump()
        assert data["agent_status"] == "inactive"

    def test_agent_rate_limits(self, sample_agent_card):
        """Should support rate limit configuration."""
        agent = Agent(
            is_public=True,
            agent_id=str(uuid4()),
            agent_card=sample_agent_card,
            rate_limit_per_user_per_hour=100,
            rate_limit_system_per_hour=1000,
        )

        assert agent.rate_limit_per_user_per_hour == 100
        assert agent.rate_limit_system_per_hour == 1000


class TestAgentStatus:
    """Tests for AgentStatus enum."""

    def test_agent_status_values(self):
        """Should have expected status values."""
        assert AgentStatus.active.value == "active"
        assert AgentStatus.inactive.value == "inactive"
        assert AgentStatus.deleted.value == "deleted"


# =============================================================================
# HITL Model Tests
# =============================================================================


def _hitl_identity(source: str) -> dict:
    return {
        "schema_version": 3,
        "interaction_id": "interaction-1",
        "question_index": 0,
        "question_count": 1,
        "application_route": "supervisor_run",
        "public_source": source,
        "evidence_origin": source,
    }


class TestHITLRequestModel:
    """Tests for HITLRequest model."""

    def test_creates_hitl_request(self):
        """Should create HITL request with defaults."""
        request = HITLRequest(
            conversation_id="room-123",
            user_message_id="msg-456",
            **_hitl_identity("supervisor"),
            prompt="Please clarify",
        )

        assert request.request_id is not None
        assert request.status == HITLStatus.PENDING
        assert request.prompt_type == HITLPromptType.TEXT

    def test_hitl_request_with_choices(self):
        """Should support choice prompt type."""
        request = HITLRequest(
            conversation_id="room-123",
            user_message_id="msg-456",
            **_hitl_identity("supervisor"),
            prompt="Choose an option",
            prompt_type=HITLPromptType.CHOICE,
            choices=["Option A", "Option B", "Option C"],
        )

        assert request.prompt_type == HITLPromptType.CHOICE
        assert len(request.choices) == 3

    def test_hitl_request_agent_source(self):
        """Should support agent as source."""
        request = HITLRequest(
            conversation_id="room-123",
            user_message_id="msg-456",
            **_hitl_identity("agent"),
            prompt="Need more information",
            agent_id="agent-789",
            agent_name="TestAgent",
        )

        assert request.public_source.value == "agent"
        assert request.agent_id == "agent-789"

    def test_hitl_request_serialization(self):
        """Should serialize to JSON-compatible dict."""
        request = HITLRequest(
            conversation_id="room-123",
            user_message_id="msg-456",
            **_hitl_identity("supervisor"),
            prompt="Test prompt",
        )

        data = request.model_dump(mode="json")

        assert isinstance(data["created_at"], str)
        assert data["status"] == "pending"


class TestHITLStatus:
    """Tests for HITLStatus enum."""

    def test_hitl_status_values(self):
        """Should have expected status values."""
        assert HITLStatus.PENDING.value == "pending"
        assert "PROCESSING" not in HITLStatus.__members__
        assert HITLStatus.RESPONDED.value == "responded"
        assert HITLStatus.EXPIRED.value == "expired"
        assert HITLStatus.CANCELED.value == "canceled"
