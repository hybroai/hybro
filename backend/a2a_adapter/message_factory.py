"""Explicit boundary converters between internal A2A models and the SDK.

A2A 1.0 moved the SDK onto protobuf types: enums are ``SCREAMING_SNAKE_CASE``,
``Part`` is a single unified message discriminated by which content member is
set, and metadata is a ``google.protobuf.Struct``. This module is the single
place that maps those representations onto Hybro's internal (SDK-free) models
and back. Nothing here inspects objects for SDK-like shape or re-validates a
serialized internal model as an SDK model.

Two deliberate fidelity rules
-----------------------------
``FileContent.bytes`` stays a base64 string, matching the persisted internal and
storage contracts. The boundary decodes exactly once when sending and encodes
exactly once when receiving, so payloads are never base64-ed twice.

``google.protobuf.Value`` has no integer type, so ProtoJSON renders ``1`` as
``1.0``. Inbound conversion restores integral floats to ``int`` so structured
data and metadata keep the value shape the pre-1.0 JSON pipeline produced and
observation digests stay stable across the upgrade. Outbound conversion cannot
avoid the protocol's double encoding; that limitation is inherent to A2A 1.0.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from uuid import uuid4

from a2a.types import (
    Artifact as SDKArtifact,
)
from a2a.types import (
    Message as SDKMessage,
)
from a2a.types import (
    Part as SDKPart,
)
from a2a.types import (
    Role as SDKRole,
)
from a2a.types import (
    TaskState as SDKTaskState,
)
from a2a.types import (
    TaskStatus as SDKTaskStatus,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct

from common import types as internal

_ROLE_TO_SDK = {"user": SDKRole.ROLE_USER, "agent": SDKRole.ROLE_AGENT}
_ROLE_FROM_SDK = {
    "ROLE_USER": internal.MessageRole.USER,
    "ROLE_AGENT": internal.MessageRole.AGENT,
    "user": internal.MessageRole.USER,
    "agent": internal.MessageRole.AGENT,
}

# Internal state spelling (lowercase, hyphenated) <-> 1.0 enum name.
_STATE_TO_SDK = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "rejected": "TASK_STATE_REJECTED",
}
_STATE_FROM_SDK = {name: value for value, name in _STATE_TO_SDK.items()}


# ---------------------------------------------------------------------------
# Struct / value helpers
# ---------------------------------------------------------------------------


def _normalize_numbers(value: Any) -> Any:
    """Restore integral floats produced by ``google.protobuf.Value``."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    return value


def struct_to_dict(value: Any) -> dict[str, Any]:
    """Convert protobuf Struct metadata into a plain dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return _normalize_numbers(value)
    if not _struct_has_fields(value):
        return {}
    return _normalize_numbers(MessageToDict(value))


def dict_to_struct(data: dict[str, Any] | None) -> Struct:
    """Convert a plain dict into protobuf Struct metadata."""
    struct = Struct()
    if data:
        ParseDict(data, struct)
    return struct


def _struct_has_fields(value: Any) -> bool:
    try:
        return len(value) > 0
    except TypeError:
        return False


def _assign_struct(target: Any, data: dict[str, Any] | None) -> None:
    if not data:
        return
    target.CopyFrom(dict_to_struct(data))


def _encode_bytes(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode_bytes(encoded: str, *, name: str | None) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"invalid base64 encoding in inline A2A file part {name!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def to_sdk_part(part: Any) -> SDKPart:
    """Map one internal part onto the unified 1.0 SDK part."""
    if isinstance(part, SDKPart):
        return part

    root = getattr(part, "root", part)
    metadata = _field(root, "metadata")
    kind = _field(root, "kind")

    sdk_part = SDKPart()
    if kind == "file" or _field(root, "file") is not None:
        file_content = _field(root, "file") or {}
        raw_bytes = _field(file_content, "bytes")
        uri = _field(file_content, "uri")
        sdk_part.media_type = str(_field(file_content, "mimeType", "mime_type") or "")
        sdk_part.filename = str(_field(file_content, "name") or "")
        if raw_bytes:
            sdk_part.raw = _decode_bytes(
                str(raw_bytes), name=_field(file_content, "name")
            )
        elif uri:
            sdk_part.url = str(uri)
    elif kind == "data" or (kind is None and _field(root, "data") is not None):
        sdk_part.data.CopyFrom(ParseDict(_field(root, "data") or {}, _new_value()))
        sdk_part.media_type = str(_field(root, "mime_type", "mimeType") or "")
    else:
        sdk_part.text = str(_field(root, "text") or "")
        sdk_part.media_type = str(_field(root, "media_type", "mediaType") or "")
    _assign_struct(sdk_part.metadata, metadata)
    return sdk_part


def to_internal_part(part: Any) -> internal.Part:
    """Map one SDK part (or wire dict) onto an internal part."""
    if isinstance(part, internal.Part):
        return part

    root = getattr(part, "root", part)
    metadata = _part_metadata(root)

    if isinstance(root, SDKPart):
        content = root.WhichOneof("content")
        text = root.text
        raw = root.raw
        url = root.url
    else:
        content, text, raw, url = _wire_part_content(root)

    media_type = _string(_field(root, "media_type", "mediaType"))
    filename = _string(_field(root, "filename", "name"))

    if content == "raw":
        return internal.Part(
            root=internal.RoomArtifactPart(
                file=internal.FileContent(
                    name=filename,
                    mimeType=media_type,
                    bytes=_encode_bytes(raw),
                ),
                metadata=metadata,
            )
        )
    if content == "url":
        return internal.Part(
            root=internal.RoomArtifactPart(
                file=internal.FileContent(
                    name=filename,
                    mimeType=media_type,
                    uri=url,
                ),
                metadata=metadata,
            )
        )
    if content == "data":
        return internal.Part(
            root=internal.DataPart(
                data=_data_value(root),
                metadata=metadata,
                mime_type=media_type,
            )
        )
    return internal.Part(root=internal.TextPart(text=text or "", metadata=metadata))


def _new_value():
    from google.protobuf.struct_pb2 import Value

    return Value()


def _data_value(root: Any) -> dict[str, Any]:
    if isinstance(root, SDKPart):
        return (
            _normalize_numbers(MessageToDict(root.data))
            if root.HasField("data")
            else {}
        )
    data = _field(root, "data")
    return _normalize_numbers(data) if isinstance(data, dict) else {}


def _wire_part_content(root: Any) -> tuple[str | None, str | None, bytes, str | None]:
    """Resolve a 1.0 wire part's content member from a plain dict."""
    for member in ("text", "raw", "url", "data"):
        if member not in root:
            continue
        if member == "raw":
            encoded = root["raw"]
            return "raw", None, base64.b64decode(encoded) if encoded else b"", None
        if member == "url":
            return "url", None, b"", _string(root["url"])
        if member == "data":
            return "data", None, b"", None
        return "text", _string(root["text"]), b"", None
    return "text", _string(_field(root, "text")), b"", None


def _part_metadata(root: Any) -> dict[str, Any] | None:
    metadata = _field(root, "metadata")
    if metadata is None:
        return None
    if isinstance(metadata, dict):
        return _normalize_numbers(metadata)
    return struct_to_dict(metadata) or None


def to_sdk_parts(parts: Any) -> list[SDKPart]:
    return [to_sdk_part(part) for part in parts or []]


def to_internal_parts(parts: Any) -> list[internal.Part]:
    return [to_internal_part(part) for part in parts or []]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _role_name(value: Any) -> str:
    """Resolve a role field to ``"user"`` or ``"agent"``.

    Three spellings reach this boundary: the internal ``MessageRole`` enum
    (``str()`` of which is not its wire value), the 1.0 name (``ROLE_AGENT``),
    and the pre-1.0 lowercase name (``agent``).
    """
    raw = _field(value, "role")
    name = _enum_value(raw)
    if name is None:
        name = raw if isinstance(raw, str) else None
    if not name:
        return "user"
    normalized = name.lower().removeprefix("role_")
    return normalized if normalized in _ROLE_TO_SDK else "user"


def to_sdk_message(value: Any) -> SDKMessage:
    """Map an internal message or internal message dict onto a 1.0 SDK message."""
    if isinstance(value, SDKMessage):
        return value

    message = SDKMessage(
        message_id=_string(_field(value, "message_id", "messageId")) or uuid4().hex,
        role=_ROLE_TO_SDK[_role_name(value)],
        task_id=_string(_field(value, "task_id", "taskId")) or "",
        context_id=_string(_field(value, "context_id", "contextId")) or "",
    )
    message.parts.extend(to_sdk_parts(_field(value, "parts")))
    _assign_struct(message.metadata, _field(value, "metadata"))
    return message


def from_sdk_message(value: Any) -> internal.Message:
    """Map a 1.0 SDK message (or wire dict) onto the internal message model."""
    if isinstance(value, internal.Message):
        return value

    role = _enum_name(_field(value, "role"), SDKRole)
    return internal.Message(
        role=_ROLE_FROM_SDK.get(role or "", internal.MessageRole.AGENT),
        message_id=_string(_field(value, "message_id", "messageId")),
        context_id=_string(_field(value, "context_id", "contextId")),
        task_id=_string(_field(value, "task_id", "taskId")),
        parts=to_internal_parts(_field(value, "parts")),
        metadata=_part_metadata(value),
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _enum_name(value: Any, enum_type: Any) -> str | None:
    """Resolve a protobuf enum field to its name.

    Protobuf enum fields read back as plain ints, ProtoJSON reads back as the
    name string, and pre-1.0 Pydantic read back as a member whose ``value`` is
    the lowercase wire name. All three are normalized here.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            return enum_type.Name(value)
        except (ValueError, TypeError):
            return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    inner = getattr(value, "value", None)
    return inner if isinstance(inner, str) else None


def _sdk_state(state: Any) -> Any:
    name = _enum_name(state, SDKTaskState)
    if name and hasattr(SDKTaskState, name):
        return getattr(SDKTaskState, name)
    mapped = _STATE_TO_SDK.get(name or "")
    return getattr(SDKTaskState, mapped, SDKTaskState.TASK_STATE_WORKING)


def to_sdk_status(status: Any) -> SDKTaskStatus:
    sdk_status = SDKTaskStatus(state=_sdk_state(_field(status, "state")))
    message = _field(status, "message")
    if message is not None:
        sdk_status.message.CopyFrom(to_sdk_message(message))
    timestamp = _timestamp_string(_field(status, "timestamp"))
    if timestamp:
        sdk_status.timestamp = timestamp
    return sdk_status


def _internal_state(status: Any) -> internal.TaskState:
    name = _enum_name(_field(status, "state"), SDKTaskState)
    if not name:
        return internal.TaskState.working
    internal_name = _STATE_FROM_SDK.get(name, name)
    try:
        return internal.TaskState(internal_name)
    except ValueError:
        return internal.TaskState.working


def to_internal_status(status: Any) -> internal.TaskStatus:
    message = _field(status, "message")
    timestamp = _timestamp_string(_field(status, "timestamp"))
    return internal.TaskStatus(
        state=_internal_state(status),
        message=from_sdk_message(message) if message is not None else None,
        timestamp=timestamp or None,
    )


def to_sdk_artifact(artifact: Any) -> SDKArtifact:
    sdk_artifact = SDKArtifact(
        artifact_id=_string(_field(artifact, "artifact_id", "artifactId")) or "",
        name=_string(_field(artifact, "name")) or "",
        description=_string(_field(artifact, "description")) or "",
    )
    sdk_artifact.parts.extend(to_sdk_parts(_field(artifact, "parts")))
    _assign_struct(sdk_artifact.metadata, _field(artifact, "metadata"))
    return sdk_artifact


def _internal_artifact(artifact: Any) -> internal.Artifact:
    return internal.Artifact(
        artifact_id=_string(_field(artifact, "artifact_id", "artifactId")),
        name=_string(_field(artifact, "name")),
        description=_string(_field(artifact, "description")),
        parts=to_internal_parts(_field(artifact, "parts")),
        metadata=_part_metadata(artifact),
        append=_optional_bool(_field(artifact, "append")),
        lastChunk=_optional_bool(_field(artifact, "last_chunk", "lastChunk")),
    )


def to_internal_task(value: Any) -> internal.Task:
    """Map a 1.0 SDK task (or wire dict) onto the internal task model."""
    if isinstance(value, internal.Task):
        return value

    return internal.Task(
        id=str(_field(value, "id") or ""),
        context_id=_string(_field(value, "context_id", "contextId")),
        status=to_internal_status(_field(value, "status") or {}),
        artifacts=[
            _internal_artifact(item) for item in _field(value, "artifacts") or []
        ]
        or None,
        history=[from_sdk_message(item) for item in _field(value, "history") or []]
        or None,
        metadata=_part_metadata(value),
    )


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------


def _field(value: Any, name: str, *aliases: str) -> Any:
    """Read one named field from a mapping, model, or protobuf message."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in (name, *aliases):
            if key in value:
                return value[key]
        return None
    for key in (name, *aliases):
        if hasattr(value, key):
            return getattr(value, key)
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _enum_value(value: Any) -> str | None:
    """Read a str-enum's wire value (``str(Enum)`` is not its value)."""
    inner = getattr(value, "value", None)
    return inner if isinstance(inner, str) else None


def _timestamp_string(value: Any) -> str | None:
    """Read a protobuf Timestamp as ISO-8601, falling back to its string form."""
    if value is None:
        return None
    to_json = getattr(value, "ToJsonString", None)
    if callable(to_json):
        try:
            return to_json() or None
        except (ValueError, TypeError):
            return None
    return _string(value)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


__all__ = [
    "dict_to_struct",
    "from_sdk_message",
    "struct_to_dict",
    "to_internal_part",
    "to_internal_parts",
    "to_internal_status",
    "to_internal_task",
    "to_sdk_artifact",
    "to_sdk_message",
    "to_sdk_part",
    "to_sdk_parts",
    "to_sdk_status",
]
