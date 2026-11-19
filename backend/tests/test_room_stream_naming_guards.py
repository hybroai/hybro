"""Naming guards and pinned contracts for the Room Stream Snapshot plan (§10)."""

from pathlib import Path

import pytest

from delivery.translator import to_sse_frame

ROOT = Path(__file__).resolve().parents[1]
OWNED_ROOTS = [
    ROOT / "delivery",
    ROOT / "execution",
    ROOT / "api_gateway",
]

VERSIONED_BRANDING = (
    "sse_v2",
    "stream_v2",
    "protocol_v2",
    "sse-v2",
    "stream-v2",
    "protocol-v2",
    "SSE V2",
)


def test_no_versioned_branding_in_owned_surfaces():
    offenders = []
    for root in OWNED_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text()
            for branding in VERSIONED_BRANDING:
                if branding in text:
                    offenders.append((str(path.relative_to(ROOT)), branding))
    assert offenders == []


def test_pinned_sse_endpoint_paths_are_present():
    from main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/sse/room/{conversation_id}/stream" in paths
    assert "/api/v1/sse/room/{conversation_id}/status" in paths


def test_sse_status_endpoint_is_deprecated_in_openapi():
    from main import app

    paths = app.openapi()["paths"]
    status = paths["/api/v1/sse/room/{conversation_id}/status"]["get"]
    stream = paths["/api/v1/sse/room/{conversation_id}/stream"]["get"]
    assert status.get("deprecated") is True
    assert "deprecated" not in stream


@pytest.mark.parametrize(
    "event_type",
    [
        "processing_status",
        "run_event",
        "agent_response_partial",
        "agent_response",
        "task_submitted",
        "task_update",
        "artifact_update",
        "error",
        "cancellation",
        "hitl_request",
        "hitl_response",
    ],
)
def test_pinned_delta_frame_type_strings(event_type):
    """Every existing frame ``type`` string is a pinned contract (§10)."""

    from common.dto import (
        AgentMessageFinal,
        AgentMessagePartial,
        ArtifactUpdateEvent,
        CancellationEvent,
        ErrorEvent,
        HITLRequestEvent,
        HITLResolvedEvent,
        ProcessingStatusEvent,
        RunEventNotification,
        TaskSubmittedEvent,
        TaskUpdateEvent,
    )
    from common.utils.time import utcnow

    samples = {
        "processing_status": ProcessingStatusEvent(
            conversation_id="r", message_id="m", status="processing"
        ),
        "run_event": RunEventNotification(
            conversation_id="r",
            event_id="e",
            run_id="run",
            seq=1,
            run_event_type="turn_start",
            payload={"internal_turn_id": "turn-1", "attempt": 1},
            correlation_id="c",
        ),
        "agent_response_partial": AgentMessagePartial(
            conversation_id="r", message_id="m", agent_id="a", content_delta="d"
        ),
        "agent_response": AgentMessageFinal(
            conversation_id="r", message_id="m", agent_id="a", content={}
        ),
        "task_submitted": TaskSubmittedEvent(
            conversation_id="r", message_id="m", task_id="t", agent_name="a"
        ),
        "task_update": TaskUpdateEvent(
            conversation_id="r", message_id="m", status="working"
        ),
        "artifact_update": ArtifactUpdateEvent(
            conversation_id="r", message_id="m", agent_id="a", artifact={}
        ),
        "error": ErrorEvent(conversation_id="r", error="boom"),
        "cancellation": CancellationEvent(
            conversation_id="r", message_id="m", reason="why"
        ),
        "hitl_request": HITLRequestEvent(
            conversation_id="r",
            request_id="h",
            message_id="m",
            prompt="p",
            prompt_type="text",
            source="agent",
            question_count=1,
            question_index=0,
        ),
        "hitl_response": HITLResolvedEvent(
            conversation_id="r",
            request_id="h",
            message_id="m",
            source="agent",
            status="responded",
            question_count=1,
            question_index=0,
        ),
    }
    frame = to_sse_frame(samples[event_type], timestamp=utcnow())
    assert frame["type"] == event_type


def test_room_events_schema_pinned_fields():
    """The room_events doc shape (§5) is a durable contract."""

    from common.utils.time import utcnow
    from delivery.room_events import _room_event_doc

    doc = _room_event_doc(
        _id="k",
        conversation_id="r",
        kind="task_update",
        payload_public={"a": 1},
        event_id="e",
        parent_event_id="p",
        run_id="run",
        persist_state="settled",
        ts=utcnow(),
    )
    assert set(doc) == {
        "_id",
        "room_id",
        "kind",
        "event_id",
        "parent_event_id",
        "run_id",
        "ts",
        "payload_public",
        "persist_state",
    }
