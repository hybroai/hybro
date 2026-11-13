"""Regression tests for A2A JSON-RPC SSE StreamResponse parsing."""

from __future__ import annotations

import pytest

from a2a_adapter.webhook_payloads import (
    parse_stream_frame,
    parse_stream_response_payload,
)
from common.types import TaskState


def test_parse_kind_based_task_frame():
    payload = {
        "id": "1",
        "jsonrpc": "2.0",
        "result": {
            "contextId": "ctx-1",
            "id": "task-1",
            "kind": "task",
            "status": {"state": "submitted"},
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.submitted


def test_parse_kind_based_working_status_update():
    payload = {
        "id": "1",
        "jsonrpc": "2.0",
        "result": {
            "contextId": "ctx-1",
            "final": False,
            "kind": "status-update",
            "status": {"state": "working"},
            "taskId": "task-1",
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.working


def test_parse_kind_based_artifact_update():
    payload = {
        "id": "1",
        "jsonrpc": "2.0",
        "result": {
            "append": False,
            "artifact": {
                "artifactId": "art-1",
                "parts": [{"kind": "text", "text": "Hawaii itinerary"}],
            },
            "contextId": "ctx-1",
            "kind": "artifact-update",
            "lastChunk": True,
            "taskId": "task-1",
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.working
    assert task.artifacts is not None
    assert len(task.artifacts) == 1
    assert task.artifacts[0].parts[0].root.text == "Hawaii itinerary"


def test_parse_kind_based_completed_status_without_message_id():
    """SDK agents often omit messageId on embedded status messages."""
    payload = {
        "id": "1",
        "jsonrpc": "2.0",
        "result": {
            "contextId": "ctx-1",
            "final": True,
            "kind": "status-update",
            "status": {
                "message": {
                    "parts": [{"kind": "text", "text": "Trip plan ready"}],
                    "role": "agent",
                },
                "state": "completed",
            },
            "taskId": "task-1",
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.status.state == TaskState.completed
    # A status-update frame carries its text on the status message. No response
    # artifact is synthesized from it, because the observation already reads it
    # from ``status.message`` and an artifact would duplicate the text.
    assert task.artifacts is None
    assert task.status.message is not None
    assert task.status.message.parts[0].root.text == "Trip plan ready"


def test_parse_input_required_status_preserves_interaction_metadata():
    """Omitting messageId must not drop hybro.ai/a2a/interaction metadata."""
    payload = {
        "id": "1",
        "jsonrpc": "2.0",
        "result": {
            "contextId": "ctx-1",
            "final": True,
            "kind": "status-update",
            "status": {
                "message": {
                    "parts": [
                        {
                            "kind": "text",
                            "text": "How many days will you stay in NYC?",
                        }
                    ],
                    "role": "agent",
                    "metadata": {
                        "hybro.ai/a2a/interaction": {
                            "schema_version": 1,
                            "interaction_id": "travel-planner:abc123",
                            "questions": [
                                {
                                    "question_id": "travel-details:abc123",
                                    "interaction_kind": "questionnaire",
                                    "prompt": "How many days will you stay in NYC?",
                                    "answer_kind": "text",
                                    "required": True,
                                }
                            ],
                        }
                    },
                },
                "state": "input-required",
            },
            "taskId": "task-1",
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.status.state == TaskState.input_required
    assert task.status.message is not None
    assert task.status.message.metadata is not None
    assert "hybro.ai/a2a/interaction" in task.status.message.metadata


def test_parse_legacy_wrapped_status_update_still_works():
    payload = {
        "statusUpdate": {
            "contextId": "ctx-1",
            "final": True,
            "taskId": "task-1",
            "status": {"state": "completed"},
        }
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.status.state == TaskState.completed


def test_parse_legacy_wrapped_artifact_update_still_works():
    payload = {
        "artifactUpdate": {
            "contextId": "ctx-1",
            "taskId": "task-1",
            "artifact": {
                "artifactId": "art-1",
                "parts": [{"kind": "text", "text": "chunk"}],
            },
        }
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.status.state == TaskState.working
    assert task.artifacts[0].parts[0].root.text == "chunk"


def test_parse_travel_planner_stream_sequence():
    """Exact frame shapes that previously stalled the travel planner use case."""
    frames = [
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "append": False,
                "artifact": {
                    "artifactId": "tp-current-result",
                    "name": "current_result",
                    "parts": [{"kind": "text", "text": "7-day Hawaii plan"}],
                },
                "contextId": "ctx-tp",
                "kind": "artifact-update",
                "lastChunk": True,
                "taskId": "task-tp",
            },
        },
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "contextId": "ctx-tp",
                "final": True,
                "kind": "status-update",
                "status": {
                    "message": {
                        "parts": [{"kind": "text", "text": "7-day Hawaii plan"}],
                        "role": "agent",
                    },
                    "state": "completed",
                },
                "taskId": "task-tp",
            },
        },
    ]

    artifact_task = parse_stream_response_payload(frames[0], "msg-tp")
    terminal_task = parse_stream_response_payload(frames[1], "msg-tp")

    assert artifact_task.status.state == TaskState.working
    assert artifact_task.artifacts[0].parts[0].root.text == "7-day Hawaii plan"
    # The terminal status frame completes the task and carries the final text on
    # its status message; the artifact text arrived on the artifact frame above.
    assert terminal_task.status.state == TaskState.completed
    assert terminal_task.artifacts is None
    assert terminal_task.status.message.parts[0].root.text == "7-day Hawaii plan"


def test_parse_weather_agent_stream_sequence():
    frames = [
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "contextId": "ctx-wx",
                "id": "task-wx",
                "kind": "task",
                "status": {"state": "submitted"},
            },
        },
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "contextId": "ctx-wx",
                "final": False,
                "kind": "status-update",
                "status": {"state": "working"},
                "taskId": "task-wx",
            },
        },
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "append": False,
                "artifact": {
                    "artifactId": "art-wx",
                    "parts": [{"kind": "text", "text": "Honolulu is clear"}],
                },
                "contextId": "ctx-wx",
                "kind": "artifact-update",
                "lastChunk": True,
                "taskId": "task-wx",
            },
        },
        {
            "id": "1",
            "jsonrpc": "2.0",
            "result": {
                "contextId": "ctx-wx",
                "final": True,
                "kind": "status-update",
                "status": {
                    "message": {
                        "contextId": "ctx-wx",
                        "kind": "message",
                        "messageId": "m-wx",
                        "parts": [{"kind": "text", "text": "Honolulu is clear"}],
                        "role": "agent",
                        "taskId": "task-wx",
                    },
                    "state": "completed",
                },
                "taskId": "task-wx",
            },
        },
    ]

    states = [
        parse_stream_response_payload(frame, "msg-wx").status.state for frame in frames
    ]

    assert states == [
        TaskState.submitted,
        TaskState.working,
        TaskState.working,
        TaskState.completed,
    ]


def test_parse_rejects_unknown_payload():
    with pytest.raises(ValueError, match="Invalid StreamResponse"):
        parse_stream_response_payload({"foo": "bar"}, "msg-1")


# ---------------------------------------------------------------------------
# A2A 1.0 frames: the member that is present identifies the event, states are
# SCREAMING_SNAKE, and parts are unified ({text, mediaType}).
# ---------------------------------------------------------------------------


def test_parse_v1_task_frame():
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "task": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "messageId": "status-1",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "done", "mediaType": "text/plain"}],
                    },
                },
            }
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.completed
    assert task.status.message.parts[0].root.text == "done"


def test_parse_v1_status_update_frame():
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "messageId": "status-2",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "Which city?", "mediaType": "text/plain"}],
                        "metadata": {"hybro.ai/a2a/interaction": {"schema_version": 1}},
                    },
                },
            }
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.id == "task-1"
    assert task.status.state == TaskState.input_required
    assert task.status.message.metadata is not None
    assert "hybro.ai/a2a/interaction" in task.status.message.metadata


def test_parse_v1_artifact_update_frame():
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "artifactUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "append": False,
                "lastChunk": True,
                "artifact": {
                    "artifactId": "art-1",
                    "name": "response",
                    "parts": [{"text": "chunk", "mediaType": "text/plain"}],
                },
            }
        },
    }

    task = parse_stream_response_payload(payload, "msg-1")

    assert task.status.state == TaskState.working
    assert task.artifacts[0].parts[0].root.text == "chunk"


def test_parse_v1_message_frame():
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "message": {
                "messageId": "m-1",
                "role": "ROLE_AGENT",
                "parts": [{"text": "answered directly", "mediaType": "text/plain"}],
            }
        },
    }

    task = parse_stream_response_payload(payload, "msg-fallback")

    # A message-only response is wrapped in a synthetic task; the frame's own
    # message id is not a task id, so a fresh one is minted.
    assert task.context_id == "msg-fallback"
    assert task.status.state == TaskState.completed
    # A message-only response still produces the response artifact.
    assert task.artifacts[0].parts[0].root.text == "answered directly"


def test_parse_rejects_unknown_frame():
    with pytest.raises(ValueError, match="Invalid StreamResponse"):
        parse_stream_response_payload({"jsonrpc": "2.0", "result": {"nope": {}}}, "m")


@pytest.mark.parametrize(
    ("result", "frame"),
    [
        ({"task": {"id": "t", "status": {"state": "TASK_STATE_WORKING"}}}, "task"),
        (
            {
                "statusUpdate": {
                    "taskId": "t",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
            "status_update",
        ),
        (
            {"artifactUpdate": {"taskId": "t", "artifact": {"artifactId": "a"}}},
            "artifact_update",
        ),
        ({"message": {"messageId": "m", "role": "ROLE_AGENT"}}, "message"),
        # The internal ``kind`` tag ``client_facade`` sets names the same frames.
        ({"kind": "status-update", "taskId": "t", "status": {}}, "status_update"),
    ],
)
def test_parse_stream_frame_tells_deltas_from_snapshots(result, frame):
    kind, task = parse_stream_frame({"jsonrpc": "2.0", "result": result}, "msg-1")

    assert kind == frame
    assert task.status.state in TaskState
