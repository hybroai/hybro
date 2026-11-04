"""Room and message document parsing shared by the conversation repositories."""

from __future__ import annotations

from typing import Any

from common.observability import events as E
from common.utils.a2a_helpers import sanitize_task_dict
from common.utils.logger import get_logger
from models.room import Room, RoomAgentMessage, RoomUserMessage

logger = get_logger(__name__)


def _stored_conversation_id(doc: dict) -> Any:
    return doc.get("room_id", doc.get("conversation_id"))


def _log_message_parse_failure(
    document_kind: str, doc: dict, exc: BaseException
) -> None:
    """Log an unreadable stored message by kind and ids, never its content."""
    logger.emit(
        E.CONVERSATION_DOCUMENT_PARSE_FAILED,
        exc=exc,
        document_kind=document_kind,
        conversation_id=_stored_conversation_id(doc),
        message_id=doc.get("message_id"),
    )


def _safe_parse_room(doc: dict | None) -> Room | None:
    if doc is None:
        return None
    try:
        return Room.model_validate(doc)
    except Exception as exc:
        logger.emit(
            E.CONVERSATION_DOCUMENT_PARSE_FAILED,
            exc=exc,
            document_kind="room",
            conversation_id=_stored_conversation_id(doc),
        )
        return None


def _safe_parse_agent_message(doc: dict | None) -> RoomAgentMessage | None:
    if doc is None:
        return None
    try:
        mc = doc.get("message_content")
        if mc and isinstance(mc, dict):
            task = mc.get("message_task")
            if task and isinstance(task, dict):
                sanitize_task_dict(task)
        return RoomAgentMessage.model_validate(doc)
    except Exception as exc:
        _log_message_parse_failure("room_agent_message", doc, exc)
        return None


def _strip_unset_task_tracking_fields(update_data: dict[str, Any]) -> dict[str, Any]:
    task_tracking_fields = {
        "last_notified_state",
        "agent_url",
        "task_created_at",
        "task_updated_at",
        "task_content",
    }
    for field in task_tracking_fields:
        if update_data.get(field) is None:
            update_data.pop(field, None)
    if update_data.get("has_task_tracking") is False:
        update_data.pop("has_task_tracking", None)
    return update_data


def _extract_text_from_artifact_parts(parts: list[dict]) -> str:
    chunks: list[str] = []
    for part in parts:
        root = part.get("root", part)
        if isinstance(root, dict) and isinstance(root.get("text"), str):
            chunks.append(root["text"])
    return "".join(chunks)


def _mongo_update_succeeded(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    modified_count = getattr(result, "modified_count", None)
    upserted_id = getattr(result, "upserted_id", None)
    if modified_count is not None:
        return modified_count > 0 or upserted_id is not None
    return bool(result)


def _modified_count(result: Any) -> int:
    if isinstance(result, bool):
        return int(result)
    if isinstance(result, int):
        return result
    modified_count = getattr(result, "modified_count", None)
    if modified_count is not None:
        return int(modified_count)
    return int(bool(result))


def _safe_parse_user_message(doc: dict | None) -> RoomUserMessage | None:
    if doc is None:
        return None
    try:
        return RoomUserMessage.model_validate(doc)
    except Exception as exc:
        _log_message_parse_failure("room_user_message", doc, exc)
        return None


def _strip_file_urls(doc: dict) -> None:
    target = doc.get("$set", doc)
    content = target.get("message_content")
    if not content:
        return
    for attachment in content.get("attachments") or []:
        attachment.pop("file_url", None)
