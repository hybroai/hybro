"""Log event catalog and the logger that emits catalog events.

This module imports only the standard library so the MCP server can reuse it;
callers inject the exception-metadata and log-context callables.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

# ``LogRecord`` attributes: ``Logger.makeRecord`` raises on these in ``extra``.
RESERVED_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}
# Keys the formatter writes itself and drops from ``extra``.
FIXED_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "environment",
        "version",
        "instance_id",
        "event",
        "logger",
    }
)
EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
# Record attribute set on catalog events; the formatter drops ``_`` keys.
EVENT_MARKER = "_hybro_event"
# Record attribute set by the migration pass-throughs on unmigrated app code.
BRIDGE_MARKER = "_hybro_app"

ExceptionFields = Callable[[BaseException], Mapping[str, Any]]
LogContext = Callable[[], Mapping[str, Any]]


class CatalogViolation(Exception):
    """An emit call that does not match its catalog (raised only when strict)."""


@dataclass(frozen=True)
class EventSpec:
    name: str
    level: int
    required: frozenset[str] = field(default_factory=frozenset)
    # Marks events a later phase copies into the system event store.
    system: bool = False


class Catalog:
    """A registry of event specs; ``strict`` makes violations raise."""

    def __init__(self) -> None:
        self._specs: dict[str, EventSpec] = {}
        self.strict = False

    def define(
        self,
        name: str,
        level: int,
        *,
        required: Iterable[str] = (),
        system: bool = False,
    ) -> EventSpec:
        if not EVENT_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid event name: {name!r}")
        if name in self._specs:
            raise ValueError(f"Event {name!r} is a duplicate")
        required_fields = frozenset(required)
        reserved = required_fields & (RESERVED_RECORD_FIELDS | FIXED_FIELDS)
        if reserved:
            raise ValueError(
                f"Event {name!r} requires reserved fields {sorted(reserved)}"
            )
        spec = EventSpec(name, level, required_fields, system)
        self._specs[name] = spec
        return spec

    def __contains__(self, spec: object) -> bool:
        return isinstance(spec, EventSpec) and self._specs.get(spec.name) is spec

    def __iter__(self) -> Iterator[EventSpec]:
        return iter(tuple(self._specs.values()))


def _no_context() -> Mapping[str, Any]:
    return {}


class EventLogger:
    """Emit catalog events through a wrapped standard-library logger."""

    def __init__(
        self,
        stdlib_logger: logging.Logger,
        catalog: Catalog,
        *,
        exception_fields: ExceptionFields,
        context: LogContext = _no_context,
    ) -> None:
        self._stdlib = stdlib_logger
        self.catalog = catalog
        self._exception_fields = exception_fields
        self._context = context

    @property
    def name(self) -> str:
        return self._stdlib.name

    def emit(
        self,
        spec: EventSpec,
        /,
        *,
        level: int | None = None,
        exc: BaseException | None = None,
        **fields: Any,
    ) -> None:
        extra: dict[str, Any] = {}
        reserved = sorted(
            key
            for key in fields
            if key in RESERVED_RECORD_FIELDS or key in FIXED_FIELDS
        )
        for key, value in fields.items():
            extra[f"field_{key}" if key in reserved else key] = value

        violation: str | None = None
        if spec not in self.catalog:
            violation = "unknown_event"
        else:
            missing = sorted(spec.required - fields.keys() - self._context().keys())
            if missing:
                violation = "missing_fields"
                extra["missing_fields"] = missing
        if reserved:
            violation = violation or "reserved_field"
            extra["reserved_fields"] = reserved
        if violation is not None:
            if self.catalog.strict:
                raise CatalogViolation(f"{violation}: {spec.name} {sorted(fields)}")
            extra["catalog_violation"] = violation

        resolved_level = spec.level if level is None else level
        if not self._stdlib.isEnabledFor(resolved_level):
            return
        if exc is not None:
            extra.update(self._exception_fields(exc))
        extra[EVENT_MARKER] = True
        self._stdlib.log(resolved_level, spec.name, extra=extra, stacklevel=2)

    # Migration bridge: stdlib-style pass-throughs for unmigrated call sites.
    # Removed together with the guard's migration ratchet.

    def _bridge(
        self,
        level: int,
        msg: object,
        args: tuple[object, ...],
        kwargs: dict[str, Any],
    ) -> None:
        extra = dict(kwargs.pop("extra", None) or {})
        extra[BRIDGE_MARKER] = True
        stacklevel = kwargs.pop("stacklevel", 1) + 2
        self._stdlib.log(
            level, msg, *args, extra=extra, stacklevel=stacklevel, **kwargs
        )

    def debug(self, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(logging.DEBUG, msg, args, kwargs)

    def info(self, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(logging.INFO, msg, args, kwargs)

    def warning(self, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(logging.WARNING, msg, args, kwargs)

    def error(self, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(logging.ERROR, msg, args, kwargs)

    def exception(self, msg: object, *args: object, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._bridge(logging.ERROR, msg, args, kwargs)

    def critical(self, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(logging.CRITICAL, msg, args, kwargs)

    def log(self, level: int, msg: object, *args: object, **kwargs: Any) -> None:
        self._bridge(level, msg, args, kwargs)


__all__ = [
    "BRIDGE_MARKER",
    "EVENT_MARKER",
    "FIXED_FIELDS",
    "RESERVED_RECORD_FIELDS",
    "Catalog",
    "CatalogViolation",
    "EventLogger",
    "EventSpec",
]
