"""Application-wide structured logging and correlation context."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import traceback
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common.observability import events
from common.observability.catalog import (
    BRIDGE_MARKER,
    EVENT_MARKER,
    FIXED_FIELDS,
    RESERVED_RECORD_FIELDS,
    EventLogger,
)

_INSTANCE_ID = uuid.uuid4().hex[:8]
_LOG_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "hybro_log_context",
    default=None,
)
_CONTEXT_FIELDS = frozenset(
    {
        "trace_id",
        "request_id",
        "client_request_id",
        "conversation_id",
        "run_id",
        "user_message_id",
        "message_id",
        "turn_id",
        "agent_id",
        "task_id",
        "dispatch_intent_id",
    }
)
# Library loggers (exact names) whose text is kept, sanitised, not blanked.
PASSTHROUGH_LOGGERS = ("uvicorn", "uvicorn.error", "py.warnings")
_SENSITIVE_FIELD_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "set_cookie",
        "token",
        "secret",
        "password",
        "api_key",
        "headers",
        "prompt",
        "body",
        "payload",
        "content",
        "base64",
        "bytes",
        "response_text",
        "message_text",
        "artifact_content",
        "task_content",
        "raw_failure_detail",
        "failure_detail",
        "error_detail",
        "error_message",
        "raw_request",
        "raw_response",
    }
)
_CANONICAL_SENSITIVE_FIELD_PARTS = frozenset(
    re.sub(r"[^a-z0-9]", "", part) for part in _SENSITIVE_FIELD_PARTS
)
_CANONICAL_CREDENTIAL_FIELD_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "token",
        "secret",
        "password",
        "apikey",
    }
)
_CONTEXT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
# Matches suffixed keys (`client_secret`, `refresh_token`) and JSON-quoted pairs.
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|token|secret|password))"
    r"[\"']?\s*[=:]\s*[\"']?[^\s,;\"'}]+"
)
_EMBEDDED_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|redis|mongodb(?:\+srv)?):[^\s\"'<>]+"
)
_UNSAFE_FIELD_KEY_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_FIELD_CHARS = 2 * 1024
_MAX_EXCEPTION_CHARS = 16 * 1024
_MAX_EXCEPTION_CHAIN = 8
_MAX_STACK_FRAMES = 24
_configured_handler: logging.Handler | None = None


def release_version(start: Path) -> str:
    """Return the release version from the nearest ``VERSION`` file above ``start``.

    The repository root (checkout) and ``/app`` (image) hold the release
    ``VERSION``; an unreadable file must not block startup.
    """

    for directory in start.parents:
        candidate = directory / "VERSION"
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8").strip() or "unknown"
            except (OSError, UnicodeDecodeError):
                return "unknown"
    return "unknown"


def _is_sensitive_key(key: str) -> bool:
    leaf = key.rsplit(".", 1)[-1].lower().replace("-", "_")
    canonical_leaf = re.sub(r"[^a-z0-9]", "", leaf)
    canonical_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if canonical_leaf in _CANONICAL_SENSITIVE_FIELD_PARTS:
        return True
    return any(
        canonical_key.endswith(sensitive)
        for sensitive in _CANONICAL_CREDENTIAL_FIELD_PARTS
    )


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        scheme = value.split(":", 1)[0].lower()
        if scheme in {"http", "https", "redis", "mongodb", "mongodb+srv"}:
            return f"{scheme}://[REDACTED]"
        return value
    if parsed.scheme not in {"http", "https", "redis", "mongodb", "mongodb+srv"}:
        return value
    hostname = parsed.hostname or ""
    if not hostname:
        return f"{parsed.scheme}://[REDACTED]"
    try:
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def _sanitize_string(value: str, *, limit: int = _MAX_FIELD_CHARS) -> str:
    value = value.encode("utf-8", errors="replace").decode("utf-8")

    def sanitize_embedded_url(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;)]}":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"{_sanitize_url(url)}{trailing}"

    value = _EMBEDDED_URL_PATTERN.sub(sanitize_embedded_url, value)
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    value = _CREDENTIAL_PATTERN.sub(r"\1=[REDACTED]", value)
    if len(value) > limit:
        return f"{value[:limit]}...[TRUNCATED {len(value) - limit} chars]"
    return value


def _safe_key_text(value: Any) -> str:
    try:
        raw = str(value)
    except Exception:
        raw = type(value).__name__
    return raw.encode("utf-8", errors="replace").decode("utf-8")


def _safe_field_key(value: Any) -> str:
    raw = _safe_key_text(value)
    normalized = _UNSAFE_FIELD_KEY_PATTERN.sub("_", raw).strip("._-")
    if not normalized:
        normalized = "field"
    if normalized != raw:
        suffix = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
        normalized = f"{normalized[:96]}_{suffix}"
    return normalized[:128]


def _safe_value(  # noqa: C901
    value: Any,
    *,
    key: str,
    seen: set[int],
) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite>"
    if isinstance(value, bytes):
        return f"<bytes length={len(value)}>"
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Enum):
        return _safe_value(value.value, key=key, seen=seen)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseException):
        return type(value).__name__

    value_id = id(value)
    if value_id in seen:
        return "<circular>"
    if isinstance(value, Mapping):
        seen.add(value_id)
        try:
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                raw_child_key = _safe_key_text(child_key)
                safe_child_key = _safe_field_key(child_key)
                nested_key = f"{key}.{raw_child_key}" if key else raw_child_key
                result[safe_child_key] = _safe_value(
                    child_value,
                    key=nested_key,
                    seen=seen,
                )
            return result
        finally:
            seen.remove(value_id)
    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(value_id)
        try:
            return [_safe_value(item, key=key, seen=seen) for item in list(value)[:100]]
        finally:
            seen.remove(value_id)
    try:
        rendered = str(value)
    except Exception:
        rendered = f"<unserializable {type(value).__name__}>"
    return _sanitize_string(rendered)


def _flatten_mapping(
    value: Mapping[str, Any],
    *,
    prefix: str = "",
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = output if output is not None else {}
    for key, child in value.items():
        flat_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, Mapping) and child:
            _flatten_mapping(child, prefix=flat_key, output=output)
        else:
            output[flat_key] = child
    return output


def _safe_message_arg(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, Enum):
        return _safe_message_arg(value.value)
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, str):
        sanitized_url = _sanitize_url(value)
        return sanitized_url if sanitized_url != value else "[REDACTED]"
    return f"<{type(value).__name__}>"


def _safe_record_message(record: logging.LogRecord) -> str:
    try:
        raw_template = str(record.msg)
    except Exception:
        raw_template = f"<unserializable {type(record.msg).__name__}>"
    template = _sanitize_string(raw_template)
    if not record.args:
        return template if _EVENT_PATTERN.fullmatch(template) else "[REDACTED]"
    if isinstance(record.args, Mapping):
        safe_args: Any = {
            key: _safe_message_arg(value) for key, value in record.args.items()
        }
    else:
        safe_args = tuple(_safe_message_arg(value) for value in record.args)
    try:
        return _sanitize_string(template % safe_args)
    except (KeyError, TypeError, ValueError):
        return template


def _passthrough_message(record: logging.LogRecord) -> str:
    try:
        return _sanitize_string(record.getMessage())
    except Exception:
        return _safe_record_message(record)


def _compact_frame(filename: str, lineno: int, function: str) -> str:
    parts = PurePath(filename).parts
    safe_path = "/".join(parts[-3:]) if parts else "<unknown>"
    return f"{safe_path}:{lineno}:{function}"


def safe_exception_metadata(error: BaseException) -> dict[str, str]:
    """Return actionable exception metadata without messages, args, or locals."""

    error_types: list[str] = []
    frames: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()

    while (
        current is not None
        and id(current) not in seen
        and len(error_types) < _MAX_EXCEPTION_CHAIN
    ):
        seen.add(id(current))
        error_types.append(type(current).__name__)
        extracted = traceback.extract_tb(current.__traceback__)
        frames.extend(
            _compact_frame(frame.filename, frame.lineno, frame.name)
            for frame in extracted[-_MAX_STACK_FRAMES:]
        )
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error

    bounded_frames = frames[-_MAX_STACK_FRAMES:]
    error_stack = " <- ".join(bounded_frames) if bounded_frames else "<no traceback>"
    error_chain = " <- ".join(error_types)
    fingerprint_source = "\n".join([error_chain, *bounded_frames])
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    return {
        "error_type": error_types[0] if error_types else type(error).__name__,
        "error_chain": error_chain,
        "error_fingerprint": fingerprint,
        "error_stack": _sanitize_string(
            error_stack,
            limit=_MAX_EXCEPTION_CHARS,
        ),
    }


class StructuredFormatter(logging.Formatter):
    """Render one structured event as JSON or logfmt."""

    def __init__(
        self,
        *,
        output_format: str,
        environment: str,
        service: str = "hybro-backend",
        service_version: str | None = None,
    ) -> None:
        super().__init__()
        self.output_format = output_format
        self.environment = environment
        self.service = service
        self.service_version = service_version or release_version(Path(__file__))

    def _event(self, record: logging.LogRecord, message: str) -> str:
        if not (
            getattr(record, EVENT_MARKER, False)
            or getattr(record, BRIDGE_MARKER, False)
        ):
            return "library_message"
        explicit = getattr(record, "event", None)
        if isinstance(explicit, str) and _EVENT_PATTERN.fullmatch(explicit):
            return explicit
        if _EVENT_PATTERN.fullmatch(message):
            return message
        return "log_message"

    def _exception_fields(self, record: logging.LogRecord) -> dict[str, str]:
        if not record.exc_info:
            return {}
        error = record.exc_info[1]
        if error is None:
            return {"error_type": "Exception"}
        return safe_exception_metadata(error)

    def event_dict(self, record: logging.LogRecord) -> dict[str, Any]:
        is_library = not (
            getattr(record, EVENT_MARKER, False)
            or getattr(record, BRIDGE_MARKER, False)
        )
        if is_library and record.name in PASSTHROUGH_LOGGERS:
            message = _passthrough_message(record)
        else:
            message = _safe_record_message(record)
        event = self._event(record, message)
        values: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=UTC,
            )
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "environment": self.environment,
            "version": self.service_version,
            "instance_id": _INSTANCE_ID,
            "event": event,
            "logger": record.name,
        }
        if message != event:
            values["message"] = message
        if message == "[REDACTED]":
            values["message_redacted"] = True
            values["source"] = _compact_frame(
                record.pathname,
                record.lineno,
                record.funcName,
            )

        combined: dict[str, Any] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in RESERVED_RECORD_FIELDS
            and key not in FIXED_FIELDS
            and key not in {"event", "exc_info", "exc_text", "stack_info"}
            and not key.startswith("_")
        }
        combined.update(get_log_context())
        combined.update(self._exception_fields(record))
        safe: dict[str, Any] = {}
        for key, value in combined.items():
            raw_key = _safe_key_text(key)
            safe_key = _safe_field_key(key)
            safe[safe_key] = _safe_value(value, key=raw_key, seen=set())
        values.update(_flatten_mapping(safe))
        return values

    def format(self, record: logging.LogRecord) -> str:
        values = self.event_dict(record)
        if self.output_format == "json":
            return json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        return " ".join(
            f"{key}={_logfmt_value(value)}" for key, value in values.items()
        )


def _logfmt_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    rendered = str(value)
    if not rendered or any(
        char.isspace() or char in {'"', "=", "\\"} for char in rendered
    ):
        return json.dumps(rendered, ensure_ascii=False)
    return rendered


def _resolve_output_format(settings: Any) -> str:
    configured = str(getattr(settings, "log_format", "auto")).lower()
    if configured == "auto":
        environment = str(getattr(settings, "app_env", "development")).lower()
        return "logfmt" if environment == "development" else "json"
    return configured


def configure_logging(settings: Any = None) -> None:
    """Configure the process-wide stdout logging pipeline idempotently."""

    global _configured_handler

    environment = str(getattr(settings, "app_env", "development"))
    level_name = str(getattr(settings, "log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = StructuredFormatter(
        output_format=_resolve_output_format(settings),
        environment=environment,
    )
    root = logging.getLogger()

    for handler in list(root.handlers):
        if handler is not _configured_handler:
            root.removeHandler(handler)
    if _configured_handler is None:
        _configured_handler = logging.StreamHandler(sys.stdout)
    if _configured_handler not in root.handlers:
        root.addHandler(_configured_handler)

    _configured_handler.setLevel(level)
    _configured_handler.setFormatter(formatter)
    root.setLevel(level)
    logging.captureWarnings(True)

    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    for logger_name in ("httpx", "httpcore", "pymongo", "motor", "redis"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.WARNING)


def get_logger(name: str) -> EventLogger:
    """Return an event logger bound to the backend catalog.

    Configuration belongs to the app shell.
    """

    return EventLogger(
        logging.getLogger(name),
        events.CATALOG,
        exception_fields=safe_exception_metadata,
        context=get_log_context,
    )


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Bind allowed correlation fields for the current async context."""

    invalid = set(fields) - _CONTEXT_FIELDS
    if invalid:
        raise ValueError(f"Unsupported log context fields: {sorted(invalid)}")
    current = dict(_LOG_CONTEXT.get() or {})
    for key, value in fields.items():
        if not isinstance(value, str) or not _CONTEXT_VALUE_PATTERN.fullmatch(value):
            current.pop(key, None)
        else:
            current[key] = value
    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def get_log_context() -> Mapping[str, str]:
    """Return a copy of the current correlation context."""

    return dict(_LOG_CONTEXT.get() or {})


def get_instance_id() -> str:
    """Return the process-wide instance identifier."""

    return _INSTANCE_ID


__all__ = [
    "StructuredFormatter",
    "bind_log_context",
    "configure_logging",
    "get_log_context",
    "get_instance_id",
    "get_logger",
    "release_version",
    "safe_exception_metadata",
]
