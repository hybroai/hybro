"""Public, persistence-safe projections for A2A task data.

This module is common-owned so HTTP routes and runtime modules can apply the
same privacy boundary without importing one another's implementations.
"""

from typing import Any
from uuid import uuid4

from common.types import Message, Part, Task, TaskState, TextPart
from common.types import MessageRole as Role

_TRUSTED_LOCAL_HITL_METADATA_KEYS = frozenset(
    {
        "hitl_request_id",
        "hitl_prompt",
        "hitl_prompt_type",
        "hitl_choices",
        "hitl_a2a_task_id",
        "hitl_a2a_context_id",
        "hitl_interaction_id",
        "hitl_question_count",
        "hitl_question_index",
        "user_answer",
    }
)
_PUBLIC_SAFE_STATUS_TEXT = {
    "failed": "Task failed",
    "rejected": "Task was rejected by the agent",
    "canceled": "Task was canceled",
    "expired": "Task expired",
}
_PUBLIC_METADATA_KEYS = frozenset(
    {"file_id", "file_name", "mime_type", "size_bytes", "sha256"}
)
_COMPLETED_STATE = "completed"
_OUTPUT_DELIVERY_FAILURE_CODE = "artifact_delivery_failed"


def _plain_model_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def _state_value_from_task_data(task_data: dict[str, Any]) -> str | None:
    status = task_data.get("status")
    if not isinstance(status, dict):
        return None
    raw_state = status.get("state")
    if raw_state is None:
        return None
    return raw_state.value if hasattr(raw_state, "value") else str(raw_state)


def _public_metadata_subset(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    public = {
        key: value
        for key, value in metadata.items()
        if key in _PUBLIC_METADATA_KEYS and value is not None
    }
    return public or None


def public_part_data(part: Any) -> dict[str, Any] | None:
    part_data = _plain_model_data(part)
    root = part_data.get("root")
    if isinstance(root, dict):
        public_root = _public_part_payload(root)
        if public_root is None:
            return None
        part_data["root"] = public_root
        return part_data
    return _public_part_payload(part_data)


def _public_part_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    public_payload = dict(payload)
    public_metadata = _public_metadata_subset(public_payload.get("metadata"))
    public_payload["metadata"] = public_metadata

    file_payload = public_payload.get("file")
    if isinstance(file_payload, dict):
        if not public_metadata or not public_metadata.get("file_id"):
            return None
        # A2A FileContent is a transport type. Persisted/API projections keep
        # only the stable room-file reference and never retain bytes or URI.
        public_payload.pop("file", None)
    elif public_payload.get("kind") == "file" and (
        not public_metadata or not public_metadata.get("file_id")
    ):
        return None
    return public_payload


def public_artifact_data(artifact: Any) -> dict[str, Any]:
    artifact_data = _plain_model_data(artifact)
    artifact_data["metadata"] = None
    artifact_data["parts"] = _public_parts_data(artifact_data.get("parts"))
    return artifact_data


def _public_parts_data(parts: Any) -> list[dict[str, Any]]:
    public_parts = []
    for part in parts or []:
        public_part = public_part_data(part)
        if public_part is not None:
            public_parts.append(public_part)
    return public_parts


def _public_unavailable_artifacts(artifacts: list[Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for artifact in artifacts:
        public_artifact = public_artifact_data(artifact)
        unavailable_parts = []
        for part in public_artifact.get("parts") or []:
            root = part.get("root", part)
            data = root.get("data") if isinstance(root, dict) else None
            if isinstance(data, dict) and data.get("type") == "file_unavailable":
                unavailable_parts.append(part)
        if unavailable_parts:
            public_artifact["parts"] = unavailable_parts
            projected.append(public_artifact)
    return projected


def _public_status_message(text: str) -> dict[str, Any]:
    return Message(
        role=Role.AGENT,
        parts=[Part(root=TextPart(text=text))],
        message_id=str(uuid4()),
    ).model_dump(mode="json")


def public_persisted_task_data(
    task: Task,
    *,
    trusted_local_hitl_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_data = _plain_model_data(task)
    state_value = _state_value_from_task_data(task_data)

    artifacts = task_data.get("artifacts")
    metadata = task_data.get("metadata")
    output_delivery_failed = (
        state_value == "failed"
        and isinstance(metadata, dict)
        and metadata.get("output_failure_code") == _OUTPUT_DELIVERY_FAILURE_CODE
    )
    if state_value == _COMPLETED_STATE and isinstance(artifacts, list):
        task_data["artifacts"] = [
            public_artifact_data(artifact) for artifact in artifacts
        ]
    elif output_delivery_failed and isinstance(artifacts, list):
        task_data["artifacts"] = _public_unavailable_artifacts(artifacts) or None
    else:
        task_data["artifacts"] = None

    task_data["history"] = None

    status = task_data.get("status")
    if isinstance(status, dict) and isinstance(status.get("message"), dict):
        if state_value == _COMPLETED_STATE:
            status["message"] = None
        elif state_value in _PUBLIC_SAFE_STATUS_TEXT:
            status["message"] = _public_status_message(
                _PUBLIC_SAFE_STATUS_TEXT[state_value]
            )
        else:
            status["message"] = None
    elif isinstance(status, dict) and state_value in _PUBLIC_SAFE_STATUS_TEXT:
        status["message"] = _public_status_message(
            _PUBLIC_SAFE_STATUS_TEXT[state_value]
        )

    if isinstance(trusted_local_hitl_metadata, dict):
        trusted_metadata = {
            key: value
            for key, value in trusted_local_hitl_metadata.items()
            if key in _TRUSTED_LOCAL_HITL_METADATA_KEYS
        }
        task_data["metadata"] = trusted_metadata or None
    elif output_delivery_failed:
        task_data["metadata"] = {
            "output_failure_code": _OUTPUT_DELIVERY_FAILURE_CODE,
            "remote_task_state": _COMPLETED_STATE,
        }
    else:
        task_data["metadata"] = None
    return task_data


def _extract_text_from_message(message: Message) -> str:
    texts = []
    for part in message.parts or []:
        if hasattr(part, "text") and part.text:
            texts.append(part.text)
        elif hasattr(part, "root") and hasattr(part.root, "text"):
            texts.append(part.root.text)
    return "".join(texts)


def _state_value(state: TaskState) -> str:
    return state.value if hasattr(state, "value") else str(state)


def extract_public_completed_status_text(task: Task) -> str | None:
    """Extract agent-authored public text from a completed A2A Task status."""
    if _state_value(task.status.state) != _COMPLETED_STATE:
        return None
    message = task.status.message
    if message is None or message.role != Role.AGENT:
        return None
    text = _extract_text_from_message(message).strip()
    return text or None


def resolve_public_task_label(extend_info: Any, agent_name: str) -> str:
    if isinstance(extend_info, dict):
        public_task_label = extend_info.get("public_task_label")
        if isinstance(public_task_label, str) and public_task_label.strip():
            return public_task_label.strip()
    return f"Requesting {agent_name}"


def resolve_public_agent_response_text(
    room_agent_message: Any,
    *,
    preferred_text: str | None = None,
    fallback_text: str | None = None,
) -> str | None:
    """Resolve the public agent-authored body without exposing dispatch seeds.

    Modern A2A responses may carry their human-readable text separately from a
    DataPart-only artifact.  Legacy rows can instead have the dispatched task
    copied into ``message_text``; those values are not agent responses and must
    not be rendered as one.
    """

    if isinstance(preferred_text, str) and preferred_text.strip():
        return preferred_text.strip()

    message_content = getattr(room_agent_message, "message_content", None)
    stored_message_text = getattr(message_content, "message_text", None)
    stored_message_text = (
        stored_message_text.strip()
        if isinstance(stored_message_text, str) and stored_message_text.strip()
        else None
    )

    extend_info = getattr(room_agent_message, "extend_info", None)
    public_label = resolve_public_task_label(
        extend_info,
        getattr(room_agent_message, "agent_id", None) or "agent",
    )
    public_dispatch_text = (
        extend_info.get("public_dispatch_text")
        if isinstance(extend_info, dict)
        else None
    )
    dispatch_seed_texts = {
        candidate.strip()
        for candidate in (
            getattr(room_agent_message, "task_content", None),
            public_label,
            public_dispatch_text,
        )
        if isinstance(candidate, str) and candidate.strip()
    }
    if stored_message_text and stored_message_text not in dispatch_seed_texts:
        return stored_message_text

    if isinstance(fallback_text, str) and fallback_text.strip():
        return fallback_text.strip()
    return None


__all__ = [
    "extract_public_completed_status_text",
    "public_artifact_data",
    "public_part_data",
    "public_persisted_task_data",
    "resolve_public_agent_response_text",
    "resolve_public_task_label",
]
