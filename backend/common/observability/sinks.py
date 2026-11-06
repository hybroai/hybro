"""Log sinks: every captured record rendered once, redacted, for later readers.

``SinkHandler`` sits on the root logger beside the stdout handler. It renders
each record through ``StructuredFormatter.event_dict`` (so a sink only ever
sees redacted fields) into a frozen ``LogEntry`` and hands it to every
registered ``LogSink``. A sink runs on the emitting thread: it must only
buffer, return quickly, and never log.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from common.observability.catalog import SYSTEM_MARKER
from common.observability.logging import StructuredFormatter

# Keys of ``event_dict`` that become ``LogEntry`` attributes.
_ENTRY_KEYS = frozenset({"timestamp", "level", "service", "event", "logger"})


@dataclass(frozen=True, slots=True)
class LogEntry:
    sequence: int
    timestamp: datetime
    level: str
    level_no: int
    service: str
    event: str
    logger: str
    system: bool
    fields: Mapping[str, Any]


LogSink = Callable[[LogEntry], None]


class SinkHandler(logging.Handler):
    """Render each record once and pass it to every sink; never raise."""

    def __init__(
        self, renderer: StructuredFormatter, ring: RingBufferSink | None = None
    ) -> None:
        super().__init__()
        self.ring = ring
        # Kept private: ``formatter`` stays None so this handler is never
        # mistaken for the stdout handler.
        self._renderer = renderer
        self._sinks: tuple[LogSink, ...] = ()
        self._sinks_lock = threading.Lock()
        self._sequence = itertools.count(1)

    def set_renderer(self, renderer: StructuredFormatter) -> None:
        self._renderer = renderer

    def add_sink(self, sink: LogSink) -> None:
        with self._sinks_lock:
            self._sinks = (*self._sinks, sink)

    def remove_sink(self, sink: LogSink) -> None:
        with self._sinks_lock:
            self._sinks = tuple(s for s in self._sinks if s != sink)

    def emit(self, record: logging.LogRecord) -> None:
        sinks = self._sinks
        if not sinks:
            return
        try:
            entry = self._entry(record)
        except Exception:
            return
        for sink in sinks:
            try:
                sink(entry)
            except Exception:
                pass

    def _entry(self, record: logging.LogRecord) -> LogEntry:
        values = self._renderer.event_dict(record)
        return LogEntry(
            sequence=next(self._sequence),
            timestamp=datetime.fromtimestamp(record.created, tz=UTC),
            level=str(values["level"]),
            level_no=record.levelno,
            service=str(values["service"]),
            event=str(values["event"]),
            logger=str(values["logger"]),
            system=getattr(record, SYSTEM_MARKER, False) is True,
            fields=MappingProxyType(
                {k: v for k, v in values.items() if k not in _ENTRY_KEYS}
            ),
        )


class RingBufferSink:
    """The newest ``capacity`` entries of this process, oldest first."""

    def __init__(self, capacity: int = 2000) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._entries.maxlen or 0

    def accept(self, entry: LogEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["LogEntry", "LogSink", "RingBufferSink", "SinkHandler"]
