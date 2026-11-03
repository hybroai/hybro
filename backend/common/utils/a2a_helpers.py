"""Shared utilities for extracting content from A2A Task/Message objects.

These are stateless, pure functions used by both WorkflowCenter
and the execution path.
"""

import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol

from common.observability import events as E
from common.types import MessageRole
from common.utils.logger import get_logger

logger = get_logger(__name__)


class A2AArtifactFiles(Protocol):
    async def materialize_inline_file_parts(
        self,
        parts: list[dict],
        conversation_id: str,
        message_id: str,
        *,
        converted_so_far: int = 0,
        budget: dict[str, Any] | None = None,
        artifact_slot: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> int: ...

    async def materialize_artifacts(
        self,
        artifacts: list,
        conversation_id: str,
        message_id: str,
        *,
        converted_so_far: int = 0,
        report: dict[str, Any] | None = None,
    ) -> int: ...

    async def delete_superseded_agent_artifacts(
        self,
        *,
        conversation_id: str,
        message_id: str,
        file_ids: set[str],
    ) -> int: ...


a2a_artifact_storage: A2AArtifactFiles | None = None


def bind_a2a_artifact_files(storage: A2AArtifactFiles) -> None:
    global a2a_artifact_storage

    a2a_artifact_storage = storage


def _require_a2a_artifact_storage() -> A2AArtifactFiles:
    if a2a_artifact_storage is None:
        raise RuntimeError("A2A artifact storage dependency has not been bound")
    return a2a_artifact_storage


def _parts_need_artifact_storage(parts: list[dict]) -> bool:
    for part in parts:
        if part.get("kind") != "file":
            continue
        file_info = part.get("file")
        if not isinstance(file_info, dict):
            continue
        if file_info.get("bytes") or file_info.get("uri"):
            return True
    return False


def _artifacts_need_artifact_storage(artifacts: list) -> bool:
    for artifact in artifacts:
        for part in getattr(artifact, "parts", []) or []:
            root = getattr(part, "root", part)
            if getattr(root, "kind", None) != "file":
                continue
            file_info = getattr(root, "file", None)
            if file_info and (
                getattr(file_info, "bytes", None) or getattr(file_info, "uri", None)
            ):
                return True
    return False


@dataclass
class ExtractedParts:
    """Structured extraction result from A2A parts."""

    text_parts: list[str] = field(default_factory=list)
    file_parts: list[dict] = field(default_factory=list)
    data_parts: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def has_non_text(self) -> bool:
        return bool(self.file_parts or self.data_parts)


def extract_parts(parts: list) -> ExtractedParts:
    """Extract and classify all parts from an A2A parts list.

    Handles both direct part objects and discriminated union wrappers (part.root).
    Non-text parts are projected to dicts carrying a flat ``mime_type`` key so
    consumers do not have to read both the canonical field and its aliases.
    """
    result = ExtractedParts()
    for part in parts:
        root = getattr(part, "root", part)
        kind = getattr(root, "kind", None)
        if not isinstance(kind, str):
            direct_kind = getattr(part, "kind", None)
            direct_text = getattr(part, "text", None)
            if isinstance(direct_kind, str) or isinstance(direct_text, str):
                root = part
                kind = direct_kind

        if kind == "text":
            text = getattr(root, "text", None)
            if isinstance(text, str) and text:
                result.text_parts.append(text)
        elif kind == "file":
            result.file_parts.append(_part_dict(root))
        elif kind == "data":
            result.data_parts.append(_part_dict(root))
        else:
            text = getattr(root, "text", None)
            if isinstance(text, str) and text:
                result.text_parts.append(text)
            else:
                logger.emit(E.A2A_PART_SKIPPED, part_kind=kind)
    return result


def _part_dict(root: Any) -> dict[str, Any]:
    if hasattr(root, "model_dump"):
        data = root.model_dump()
    else:
        data = dict(vars(root))
    mime_type = data.get("mime_type") or data.get("mimeType")
    if mime_type and not data.get("mime_type"):
        data["mime_type"] = mime_type
    return data


def extract_parts_from_artifacts(artifacts: list) -> ExtractedParts:
    """Extract parts from a list of A2A artifacts."""
    result = ExtractedParts()
    for artifact in artifacts:
        if not artifact.parts:
            continue
        artifact_parts = extract_parts(artifact.parts)
        result.text_parts.extend(artifact_parts.text_parts)
        result.file_parts.extend(artifact_parts.file_parts)
        result.data_parts.extend(artifact_parts.data_parts)
    return result


def get_text_from_a2a_response(result: Any) -> str:
    """Extract text content from an A2A response (Task or Message).

    Args:
        result: A Task or Message object from A2A response

    Returns:
        Extracted text as a string, or empty string if no text found
    """
    if result.kind == "message" and hasattr(result, "parts") and result.parts:
        return get_text_from_message(result)
    elif result.kind == "task":
        message = get_message_from_task(result)
        return get_text_from_message(message) if message else ""
    return ""


def get_message_from_task(task: Any) -> Any | None:
    """Extract message from a Task object.

    Per A2A spec, task outputs should be in artifacts. We check:
    1. task.artifacts - A2A-compliant location for task outputs
    2. task.status.message - for agents that reply in the status message
    3. task.history - conversation history (fallback)
    """
    # Check task.artifacts first (A2A-compliant: task outputs go in artifacts)
    if task.artifacts:
        all_parts = []
        for artifact in task.artifacts:
            # Artifact.parts is a list of Part objects
            all_parts.extend(artifact.parts)
        if all_parts:
            logger.emit(
                E.TASK_MESSAGE_RESOLVED,
                source="artifacts",
                task_id=task.id,
                part_count=len(all_parts),
            )
            message = SimpleNamespace(
                kind="message",
                role=MessageRole.AGENT,
                message_id=str(uuid.uuid4()),
                task_id=task.id,
                parts=all_parts,
            )
            return message

    # Check task.status.message (for status updates, less common for final output)
    if task.status and task.status.message:
        logger.emit(E.TASK_MESSAGE_RESOLVED, source="status_message", task_id=task.id)
        return task.status.message

    # Check task.history for the last agent message (fallback)
    if task.history:
        for msg in reversed(task.history):
            role = getattr(msg, "role", None)
            if getattr(role, "value", role) == "agent":
                logger.emit(E.TASK_MESSAGE_RESOLVED, source="history", task_id=task.id)
                return msg

    logger.emit(E.TASK_MESSAGE_NOT_FOUND, task_id=task.id)
    return None


def get_text_from_message(message: Any | None) -> str:
    """Extract text from a Message object via ``extract_parts``."""
    if message is None:
        return ""
    return extract_parts(message.parts).text


def extract_text_from_artifacts(artifacts: list) -> str | None:
    """Extract text content from A2A artifacts via ``extract_parts``."""
    text = extract_parts_from_artifacts(artifacts).text
    return text if text else None


def extract_agent_text_from_room_message(msg: object) -> str | None:
    """Safely extract the agent's latest response text from a RoomAgentMessage.

    Checks storage locations in canonical-output priority order:
    1. ``message_content.message_task.artifacts`` (A2A-compliant task output).
    2. ``message_content.message_text`` (the backend's canonical display text).
    3. ``message_content.message_task.status.message`` (fallback for agents
       that put completed output in status.message).
    4. Agent-role entries in ``message_content.message_task.history`` (last
       fallback; history can contain intermediate status/progress messages).

    All parts of the latest agent-role message are joined so that multi-part
    responses are preserved (e.g. reasoning part + answer part).

    Returns ``None`` when no text can be found; never raises.
    """
    try:
        mc = getattr(msg, "message_content", None)
        if mc is None:
            return None

        task = getattr(mc, "message_task", None)
        if task is not None:
            artifacts = getattr(task, "artifacts", None)
            if isinstance(artifacts, list) and artifacts:
                text = extract_text_from_artifacts(artifacts)
                if text:
                    return text

        message_text = getattr(mc, "message_text", None)
        if isinstance(message_text, str) and message_text:
            return message_text

        if task is not None:
            status = getattr(task, "status", None)
            status_message = getattr(status, "message", None)
            status_parts = getattr(status_message, "parts", None)
            if isinstance(status_parts, list) and status_parts:
                text = extract_parts(status_parts).text
                if text:
                    return text

            history = getattr(task, "history", None)
            if history:
                for entry in reversed(history):
                    role = getattr(entry, "role", None)
                    if getattr(role, "value", role) == "agent":
                        parts = getattr(entry, "parts", None)
                        if parts:
                            text = extract_parts(parts).text
                            if text:
                                return text

        return None
    except (AttributeError, IndexError, TypeError):
        return None


def extract_error_message(task: Any) -> str | None:
    """Extract error message from task status."""
    if not task.status.message:
        return None
    if not task.status.message.parts:
        return None
    for part in task.status.message.parts:
        if hasattr(part, "text") and part.text:
            return part.text
        if hasattr(part, "root") and hasattr(part.root, "text"):
            return part.root.text
    return None


def extract_status_message(task: Any) -> str | None:
    """Extract human-readable status message."""
    return extract_error_message(task)  # Same extraction logic


def task_has_visible_content(task: Any) -> bool:
    """Return True when task has user-visible output (text or non-text parts)."""
    if not task.artifacts:
        return False
    extracted = extract_parts_from_artifacts(task.artifacts)
    return bool(extracted.text or extracted.has_non_text)


def _normalize_part_root(root: dict) -> dict | None:
    """Normalize a raw part dict so Pydantic's ``kind`` discriminator validates."""
    kind = root.get("kind")

    if kind == "text":
        if "text" not in root or root.get("text") is None:
            return None
        return root

    if kind == "file":
        if "file" not in root or root.get("file") is None:
            metadata = root.get("metadata")
            if isinstance(metadata, dict) and metadata.get("file_id"):
                file_id = metadata["file_id"]
                file_info: dict[str, Any] = {
                    "uri": f"/api/v1/files/{file_id}/content",
                }
                name = metadata.get("file_name") or metadata.get("name")
                if name:
                    file_info["name"] = name
                mime_type = metadata.get("mime_type") or metadata.get("mimeType")
                if mime_type:
                    file_info["mimeType"] = mime_type
                out = {"kind": "file", "file": file_info, "metadata": metadata}
                return out
            return None
        return root

    if kind == "data":
        if "data" not in root or root.get("data") is None:
            return None
        return root

    if (
        "metadata" in root
        and isinstance(root["metadata"], dict)
        and root["metadata"].get("file_id")
    ):
        meta = root["metadata"]
        file_id = meta["file_id"]
        file_info = {
            "uri": f"/api/v1/files/{file_id}/content",
        }
        name = meta.get("file_name") or meta.get("name")
        if name:
            file_info["name"] = name
        mime_type = meta.get("mime_type") or meta.get("mimeType")
        if mime_type:
            file_info["mimeType"] = mime_type
        return {"kind": "file", "file": file_info, "metadata": meta}

    if "text" in root and root.get("text") is not None:
        out: dict[str, Any] = {"kind": "text", "text": root["text"]}
        if "metadata" in root:
            out["metadata"] = root["metadata"]
        return out

    if "file" in root and root.get("file") is not None:
        out = {"kind": "file", "file": root["file"]}
        if "metadata" in root:
            out["metadata"] = root["metadata"]
        return out

    if "data" in root and root.get("data") is not None:
        out = {"kind": "data", "data": root["data"]}
        if "metadata" in root:
            out["metadata"] = root["metadata"]
        return out

    if "url" in root or "raw" in root:
        file_info: dict[str, Any] = {}
        if "raw" in root:
            file_info["bytes"] = root["raw"]
        if "url" in root:
            file_info["uri"] = root["url"]
        media_type = (
            root.get("mime_type") or root.get("mimeType") or root.get("mediaType")
        )
        if media_type:
            file_info["mimeType"] = media_type
        filename = root.get("filename") or root.get("name")
        if filename:
            file_info["name"] = filename
        if not file_info:
            return None
        out = {"kind": "file", "file": file_info}
        if "metadata" in root:
            out["metadata"] = root["metadata"]
        return out

    return None


def sanitize_artifact_parts(parts: list[dict]) -> list[dict]:
    """Remove malformed part dicts and normalize legacy shapes before persistence/read.

    Each A2A Part variant requires its discriminator + payload:
      - TextPart:  kind='text' + text (str)
      - FilePart:  kind='file' + file (dict)
      - DataPart:  kind='data' + data (dict)

    Legacy rows may omit ``kind`` (e.g. ``{"text": "hello"}``); those are
    coerced to the canonical shape so Pydantic validation succeeds on read.

    Returns a new list with invalid entries stripped.
    """
    cleaned: list[dict] = []
    for p in parts:
        if not isinstance(p, dict):
            logger.emit(
                E.INVALID_ARTIFACT_PART_DROPPED,
                part_type=type(p).__name__,
                reason="not_mapping",
            )
            continue
        root = p.get("root", p)
        if not isinstance(root, dict):
            logger.emit(
                E.INVALID_ARTIFACT_PART_DROPPED,
                part_type=type(root).__name__,
                reason="root_not_mapping",
            )
            continue
        normalized_root = _normalize_part_root(root)
        if normalized_root is None:
            logger.emit(
                E.INVALID_ARTIFACT_PART_DROPPED,
                level=logging.DEBUG,
                reason="unrecognized_shape",
            )
            continue
        if "root" in p and p.get("root") is root:
            cleaned.append({**p, "root": normalized_root})
        else:
            cleaned.append(normalized_root)
    return cleaned


def sanitize_task_dict(task: dict) -> dict:
    """Sanitize a raw Task dict from MongoDB before Pydantic validation."""
    for artifact in task.get("artifacts") or []:
        parts = artifact.get("parts")
        if parts and isinstance(parts, list):
            artifact["parts"] = sanitize_artifact_parts(parts)

    for msg in task.get("history") or []:
        parts = msg.get("parts")
        if parts and isinstance(parts, list):
            msg["parts"] = sanitize_artifact_parts(parts)

    status = task.get("status") or {}
    status_msg = status.get("message") or {}
    parts = status_msg.get("parts")
    if parts and isinstance(parts, list):
        status_msg["parts"] = sanitize_artifact_parts(parts)

    return task


def append_artifact_to_task_dict(
    existing_artifacts: list[dict] | None,
    new_artifact: dict,
    append: bool = False,
) -> list[dict]:
    """Append or merge an artifact into an existing artifacts list per A2A spec.

    Implements the A2A artifact streaming semantics:
    - If append=False: Create new artifact or replace existing with same artifactId
    - If append=True: Extend parts of existing artifact with same artifactId

    Args:
        existing_artifacts: Current list of artifact dicts (may be None)
        new_artifact: The new artifact dict to add/merge
        append: If True, extend parts of existing artifact; if False, replace/create

    Returns:
        Updated list of artifact dicts
    """
    if existing_artifacts is None:
        existing_artifacts = []

    artifact_id = new_artifact.get("artifactId") or new_artifact.get("artifact_id")
    if not artifact_id:
        logger.emit(E.TASK_ARTIFACT_ID_NOT_FOUND)
        existing_artifacts.append(new_artifact)
        return existing_artifacts

    existing_index = None
    for i, art in enumerate(existing_artifacts):
        art_id = art.get("artifactId") or art.get("artifact_id")
        if art_id == artifact_id:
            existing_index = i
            break

    if not append:
        if existing_index is not None:
            logger.emit(
                E.TASK_ARTIFACT_MERGED, artifact_id=artifact_id, merge_action="replaced"
            )
            existing_artifacts[existing_index] = new_artifact
        else:
            logger.emit(
                E.TASK_ARTIFACT_MERGED, artifact_id=artifact_id, merge_action="added"
            )
            existing_artifacts.append(new_artifact)
    elif existing_index is not None:
        logger.emit(
            E.TASK_ARTIFACT_MERGED, artifact_id=artifact_id, merge_action="appended"
        )
        existing_parts = existing_artifacts[existing_index].get("parts", [])
        new_parts = new_artifact.get("parts", [])
        existing_artifacts[existing_index]["parts"] = existing_parts + new_parts
    else:
        logger.emit(E.TASK_ARTIFACT_APPEND_TARGET_NOT_FOUND, artifact_id=artifact_id)
        existing_artifacts.append(new_artifact)

    return existing_artifacts


async def materialize_inline_file_parts(
    parts: list[dict],
    conversation_id: str,
    message_id: str,
    *,
    converted_so_far: int = 0,
    budget: dict[str, Any] | None = None,
    artifact_slot: str | None = None,
    report: dict[str, Any] | None = None,
) -> int:
    if not _parts_need_artifact_storage(parts):
        return converted_so_far

    storage = _require_a2a_artifact_storage()
    return await storage.materialize_inline_file_parts(
        parts,
        conversation_id,
        message_id,
        converted_so_far=converted_so_far,
        budget=budget,
        artifact_slot=artifact_slot,
        report=report,
    )


async def delete_superseded_agent_artifacts(
    *,
    conversation_id: str,
    message_id: str,
    file_ids: set[str],
) -> int:
    if not file_ids:
        return 0
    return await _require_a2a_artifact_storage().delete_superseded_agent_artifacts(
        conversation_id=conversation_id,
        message_id=message_id,
        file_ids=file_ids,
    )


async def materialize_artifacts(
    artifacts: list,
    conversation_id: str,
    message_id: str,
    *,
    converted_so_far: int = 0,
    report: dict[str, Any] | None = None,
) -> int:
    if not _artifacts_need_artifact_storage(artifacts):
        return converted_so_far

    storage = _require_a2a_artifact_storage()
    return await storage.materialize_artifacts(
        artifacts,
        conversation_id,
        message_id,
        converted_so_far=converted_so_far,
        report=report,
    )


def _state_value(state: Any) -> str:
    value = getattr(state, "value", state)
    return str(value)


def is_terminal_task_state_value(state: Any) -> bool:
    from common.a2a_constants import TERMINAL_STATES

    if state is None:
        return False
    return _state_value(state) in {item.value for item in TERMINAL_STATES}


def _part_dict_is_text(part: dict) -> bool:
    root = part.get("root", part)
    if isinstance(root, dict):
        kind = root.get("kind")
        if kind == "text":
            return True
        if "text" in root and kind not in ("file", "data"):
            return True
    kind = part.get("kind") if isinstance(part, dict) else None
    if kind == "text":
        return True
    if isinstance(part, dict) and "text" in part and kind not in ("file", "data"):
        return True
    return False


def filter_non_text_parts(parts: list[dict] | None) -> list[dict] | None:
    """Drop text parts so SSE ``parts`` carries only file/data payloads."""
    if not parts:
        return parts
    kept = [part for part in parts if not _part_dict_is_text(part)]
    return kept or None


def prepare_terminal_agent_content(
    *,
    message_text: str | None = None,
    artifacts: list[dict] | None = None,
    task_data: dict | None = None,
) -> tuple[str | None, list[dict] | None, dict | None]:
    """Resolve terminal message_text and sync artifact text parts (no markdown transform).

    Terminal contract: one canonical text part holds the full display body per
    artifact. Streaming chunks are collapsed; file/data parts are preserved.
    """
    import copy

    resolved_text = message_text
    if (not resolved_text or not resolved_text.strip()) and artifacts:
        resolved_text = extract_text_from_artifact_dicts(artifacts)

    resolved_artifacts = artifacts
    if resolved_text and resolved_artifacts:
        resolved_artifacts = sync_artifact_dicts_to_canonical_text(
            resolved_artifacts,
            resolved_text,
        )

    resolved_task = task_data
    if resolved_task is not None and resolved_artifacts is not None:
        resolved_task = copy.deepcopy(task_data)
        resolved_task["artifacts"] = resolved_artifacts

    return resolved_text, resolved_artifacts, resolved_task


def artifacts_to_dicts(artifacts: list | None) -> list[dict]:
    """Convert persisted A2A artifact models to plain dicts."""
    if not artifacts:
        return []
    result: list[dict] = []
    for artifact in artifacts:
        if isinstance(artifact, dict):
            result.append(artifact)
        elif hasattr(artifact, "model_dump"):
            result.append(artifact.model_dump(mode="json", by_alias=True))
    return result


def extract_text_from_artifact_dicts(artifacts: list[dict] | None) -> str | None:
    """Concatenate text parts from serialized artifact dicts."""
    if not artifacts:
        return None
    chunks: list[str] = []
    for artifact in artifacts:
        for part in artifact.get("parts") or []:
            root = part.get("root", part)
            if isinstance(root, dict):
                text = root.get("text")
            else:
                text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text:
                chunks.append(text)
    combined = "".join(chunks)
    return combined if combined else None


def sync_artifact_dicts_to_canonical_text(
    artifacts: list[dict],
    canonical_text: str,
) -> list[dict]:
    """Align artifact text payload with canonical terminal display text.

    Collapses multi-part streaming text into a single text part and removes
    empty text slots. Non-text parts (file, data) are preserved as-is.
    """
    import copy

    if not canonical_text.strip():
        return artifacts

    out = copy.deepcopy(artifacts)
    canonical_written = False

    for artifact in out:
        parts = artifact.get("parts") or []
        non_text_parts = [part for part in parts if not _part_dict_is_text(part)]
        if not canonical_written:
            non_text_parts.insert(0, {"kind": "text", "text": canonical_text})
            canonical_written = True
        artifact["parts"] = non_text_parts

    if canonical_written:
        return out

    first = (
        out[0] if out else {"artifactId": "response", "name": "response", "parts": []}
    )
    if not out:
        out = [first]
    first.setdefault("parts", []).insert(0, {"kind": "text", "text": canonical_text})
    return out


def resolve_terminal_sse_content(
    state: Any,
    *,
    message_text: str | None,
    artifact_text: str | None,
) -> str | None:
    """Pick terminal content for SSE (message_text wins on completed)."""
    from common.a2a_constants import CommonTaskState

    stored = message_text.strip() if message_text and message_text.strip() else None
    extracted = (
        artifact_text.strip() if artifact_text and artifact_text.strip() else None
    )
    if _state_value(state) == CommonTaskState.COMPLETED.value and stored:
        return stored
    return stored or extracted


def is_authoritative_a2a_id(value: Any) -> bool:
    """True when a task/context identifier is a remote authority, not a
    provisional local dispatch placeholder like ``pending-*``."""
    return bool(
        isinstance(value, str)
        and value.strip()
        and not value.startswith(("pending-", "relay-pending-"))
    )
