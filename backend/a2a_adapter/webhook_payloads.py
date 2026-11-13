"""Parse A2A stream frames and push notifications into internal tasks.

Used by the direct stream wrapper, the shared facade-result conversion, and the
orchestrator's authenticated push ingress (a push body is one ``StreamResponse``
as the SDK push sender serializes it). An
A2A 1.0 ``StreamResponse`` is exactly one of ``task`` / ``statusUpdate`` /
``artifactUpdate`` / ``message``. Frames produced by ``client_facade`` are
already unwrapped from that member and tagged with ``kind``, Hybro's internal
envelope tag (``task``, ``status-update``, ``artifact-update``, ``message``);
it is not the pre-1.0 wire discriminator. A raw 1.0 frame that still wraps the
event under its member is accepted too.

Conversion to internal models happens through the shared boundary converters,
so this module never constructs SDK types and never guesses at protobuf shape
beyond the JSON member names the protocol defines.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from common.types import (
    Artifact,
    Message,
    MessageRole,
    Task,
    TaskState,
    TaskStatus,
)

from .message_factory import to_internal_part, to_internal_status

# 1.0 ProtoJSON state names -> internal spelling.
_PROTO_STATE_MAP: dict[str, str] = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_AUTH_REQUIRED": "auth-required",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_REJECTED": "rejected",
}
_STREAM_MEMBERS = ("task", "statusUpdate", "artifactUpdate", "message")


def parse_stream_response_payload(payload: dict[str, Any], message_id: str) -> Task:
    """Parse one A2A stream frame into an internal task.

    ``message_id`` only supplies a fallback identity when the frame omits one;
    agent-supplied ids always win.
    """
    return parse_stream_frame(payload, message_id)[1]


def parse_stream_frame(payload: dict[str, Any], message_id: str) -> tuple[str, Task]:
    """Parse one A2A stream frame into its frame kind and an internal task.

    The kind tells a delta (``status_update``, ``artifact_update``) from a full
    snapshot (``task``, ``message``).
    """
    frame = _unwrap_result(payload)
    member, event = _resolve_event(frame)

    if member == "task":
        task = task_from_task_frame(event)
    elif member == "statusUpdate":
        task = task_from_status_update(event, message_id)
    elif member == "artifactUpdate":
        task = task_from_artifact_update(event)
    elif member == "message":
        task = task_from_message_frame(event, message_id)
    else:
        raise ValueError(
            "Invalid StreamResponse: expected one of 'task', 'statusUpdate', "
            "'artifactUpdate', or 'message'"
        )
    return _STREAM_FRAMES[member], task


_PUSH_FRAMES = {
    "task": "task",
    "statusUpdate": "status_update",
    "artifactUpdate": "artifact_update",
}
_STREAM_FRAMES = {**_PUSH_FRAMES, "message": "message"}


def parse_push_notification_payload(payload: dict[str, Any]) -> tuple[str, Task]:
    """Parse one push notification into its frame kind and an internal task.

    A push always concerns an existing task, so a message-only frame or a frame
    without a task id is rejected; no fallback identity is synthesized.
    """
    frame = _unwrap_result(payload)
    member, event = _resolve_event(frame)
    if member == "task":
        task = task_from_task_frame(event)
    elif member == "statusUpdate":
        task = task_from_status_update(event, "")
    elif member == "artifactUpdate":
        task = task_from_artifact_update(event)
    else:
        raise ValueError(
            "Invalid push notification: expected 'task', 'statusUpdate', or "
            "'artifactUpdate'"
        )
    if not task.id:
        raise ValueError("Invalid push notification: missing task id")
    return _PUSH_FRAMES[member], task


# ---------------------------------------------------------------------------
# Frame shape
# ---------------------------------------------------------------------------


def _unwrap_result(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid StreamResponse: expected an object")
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    if "error" in payload and payload.get("error") is not None:
        raise ValueError(f"A2A stream frame is an error: {payload.get('error')}")
    return payload


def _resolve_event(frame: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Identify the event and return the object that carries it.

    Facade frames carry the event inline, tagged with Hybro's internal
    envelope ``kind``. A raw 1.0 ``StreamResponse`` dict instead wraps the
    event under the member that is present (``{"statusUpdate": {...}}``); that
    shape and its snake_case spelling stay accepted because callers and tests
    still pass raw frames.
    """
    for member in _STREAM_MEMBERS:
        value = frame.get(member)
        if isinstance(value, dict):
            return member, value

    kind = _string(frame.get("kind"))
    if kind:
        normalized = kind.replace("_", "-").lower()
        if normalized == "status-update":
            return "statusUpdate", frame
        if normalized == "artifact-update":
            return "artifactUpdate", frame
        if normalized in {"task", "message"}:
            return normalized, frame
    if "statusUpdate" in frame or "status_update" in frame:
        return "statusUpdate", _member(frame, "statusUpdate")
    if "artifactUpdate" in frame or "artifact_update" in frame:
        return "artifactUpdate", _member(frame, "artifactUpdate")
    return None, frame


def _member(frame: dict[str, Any], member: str) -> dict[str, Any]:
    value = frame.get(member)
    if value is None:
        value = frame.get(_to_snake(member))
    return value if isinstance(value, dict) else {}


def _to_snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


# ---------------------------------------------------------------------------
# Event -> internal Task
# ---------------------------------------------------------------------------


def task_from_task_frame(raw: dict[str, Any]) -> Task:
    return Task(
        id=_string(raw.get("id")) or "",
        context_id=_string(raw.get("contextId") or raw.get("context_id")),
        status=to_internal_status(raw.get("status") or {}),
        artifacts=_artifacts(raw.get("artifacts")),
        history=_history(raw.get("history")),
        metadata=_metadata(raw),
    )


def task_from_status_update(raw: dict[str, Any], message_id: str) -> Task:
    """Build a task carrying a status update.

    The status message stays where the protocol puts it: on ``status.message``.
    Synthesizing a response artifact from that text would double it in the
    observation (once from the status message, once from the artifact) and would
    misreport a status event as produced output.
    """
    status = to_internal_status(raw.get("status") or {})
    task_id = _string(raw.get("taskId") or raw.get("task_id")) or message_id
    return Task(
        id=task_id,
        context_id=_string(raw.get("contextId") or raw.get("context_id")),
        status=status,
        metadata=_metadata(raw),
    )


def task_from_artifact_update(raw: dict[str, Any]) -> Task:
    artifact_data = raw.get("artifact")
    if not isinstance(artifact_data, dict):
        raise ValueError("Invalid StreamResponse: artifactUpdate requires 'artifact'")

    append = raw.get("append")
    last_chunk = raw.get("lastChunk", raw.get("last_chunk"))
    artifact = Artifact(
        artifact_id=_string(
            artifact_data.get("artifactId") or artifact_data.get("artifact_id")
        ),
        name=_string(artifact_data.get("name")),
        description=_string(artifact_data.get("description")),
        parts=[to_internal_part(part) for part in artifact_data.get("parts") or []],
        metadata=_metadata(artifact_data),
        append=None if append is None else bool(append),
        lastChunk=None if last_chunk is None else bool(last_chunk),
    )
    return Task(
        id=_string(raw.get("taskId") or raw.get("task_id")) or "",
        context_id=_string(raw.get("contextId") or raw.get("context_id")),
        status=TaskStatus(state=TaskState.working),
        artifacts=[artifact],
        metadata=_metadata(raw),
    )


def task_from_message_frame(raw: dict[str, Any], message_id: str) -> Task:
    """Build a completed task from a message-only response."""
    message = _message(raw)
    return Task(
        id=str(uuid4()),
        context_id=message.context_id or message_id,
        status=TaskStatus(state=TaskState.completed),
        artifacts=[
            Artifact(
                artifact_id=str(uuid4()),
                name="response",
                parts=message.parts,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------


def _message(raw: Any) -> Message:
    if not isinstance(raw, dict):
        raise ValueError("Invalid StreamResponse: message must be an object")
    role = _string(raw.get("role"))
    return Message(
        message_id=_string(raw.get("messageId") or raw.get("message_id")),
        role=(
            MessageRole.USER
            if (role or "").upper() in {"ROLE_USER", "USER"}
            else MessageRole.AGENT
        ),
        context_id=_string(raw.get("contextId") or raw.get("context_id")),
        task_id=_string(raw.get("taskId") or raw.get("task_id")),
        parts=[to_internal_part(part) for part in raw.get("parts") or []],
        metadata=_metadata(raw),
    )


def _artifacts(raw: Any) -> list[Artifact] | None:
    if not isinstance(raw, list):
        return None
    artifacts = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        artifacts.append(
            Artifact(
                artifact_id=_string(item.get("artifactId") or item.get("artifact_id")),
                name=_string(item.get("name")),
                description=_string(item.get("description")),
                parts=[to_internal_part(part) for part in item.get("parts") or []],
                metadata=_metadata(item),
            )
        )
    return artifacts or None


def _history(raw: Any) -> list[Message] | None:
    if not isinstance(raw, list):
        return None
    messages = [_message(item) for item in raw if isinstance(item, dict)]
    return messages or None


def _message_text(message: Message | None) -> str:
    if message is None:
        return ""
    return "".join(
        part.root.text
        for part in message.parts or []
        if getattr(part.root, "kind", None) == "text"
    )


def _metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    metadata = value.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) and metadata else None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = [
    "parse_push_notification_payload",
    "parse_stream_frame",
    "parse_stream_response_payload",
    "task_from_artifact_update",
    "task_from_message_frame",
    "task_from_status_update",
    "task_from_task_frame",
]
