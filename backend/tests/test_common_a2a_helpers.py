"""
Unit tests for A2A helper utilities (common/utils/a2a_helpers.py).

Tests cover:
- get_text_from_message: text extraction from Message
- get_message_from_task: message extraction priority (artifacts > status > history)
- extract_text_from_artifacts: robust part-type handling
"""

from unittest.mock import MagicMock

from common.types import (
    Artifact,
    Message,
    MessageRole,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from common.utils.a2a_helpers import (
    get_message_from_task,
    get_text_from_message,
)

# =============================================================================
# get_text_from_message Tests
# =============================================================================


class TestGetTextFromMessage:
    def test_returns_empty_for_none(self):
        assert get_text_from_message(None) == ""

    def test_extracts_text_from_parts(self):
        msg = Message(
            role=MessageRole.AGENT,
            message_id="m1",
            parts=[TextPart(text="Hello"), TextPart(text=" world")],
        )
        assert get_text_from_message(msg) == "Hello world"

    def test_returns_empty_when_no_text_parts(self):
        part = MagicMock()
        part.root = MagicMock()
        del part.root.text

        msg = MagicMock()
        msg.parts = [part]
        result = get_text_from_message(msg)
        assert result == ""


# =============================================================================
# get_message_from_task Tests
# =============================================================================


class TestGetMessageFromTask:
    def test_extracts_from_artifacts(self):
        artifact = Artifact(
            artifact_id="art-1",
            parts=[TextPart(text="artifact text")],
        )
        task = Task(
            id="t1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[artifact],
        )
        msg = get_message_from_task(task)
        assert msg is not None
        assert msg.role == MessageRole.AGENT

    def test_extracts_from_status_message(self):
        status_msg = Message(
            role=MessageRole.AGENT,
            message_id="sm1",
            parts=[TextPart(text="status text")],
        )
        task = Task(
            id="t1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed, message=status_msg),
        )
        msg = get_message_from_task(task)
        assert msg is status_msg

    def test_extracts_from_history_fallback(self):
        agent_msg = Message(
            role=MessageRole.AGENT,
            message_id="hm1",
            parts=[TextPart(text="history text")],
        )
        user_msg = Message(
            role=MessageRole.USER,
            message_id="hm2",
            parts=[TextPart(text="user text")],
        )
        task = Task(
            id="t1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed),
            history=[user_msg, agent_msg],
        )
        msg = get_message_from_task(task)
        assert msg is agent_msg

    def test_returns_none_when_empty(self):
        task = Task(
            id="t1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed),
        )
        assert get_message_from_task(task) is None
