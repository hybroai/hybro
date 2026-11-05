"""The read-only diagnostics surface ``api_gateway`` consumes."""

from __future__ import annotations

from typing import Protocol

from diagnostics.models import (
    ID_PATTERN,
    MAX_EVENTS,
    MAX_LIMIT,
    DiagnosticPage,
    DiagnosticsSnapshot,
    LevelName,
    LogSource,
    RecordQuery,
)


class InvalidCursor(ValueError):
    """A ``before`` cursor that does not name a record of the source."""


class StoreUnavailable(RuntimeError):
    """The store is not ready, or its query failed or timed out."""


class DiagnosticsAccess(Protocol):
    def can_read(self, user_id: str) -> bool: ...

    def token_matches(self, value: str) -> bool: ...

    async def snapshot(self) -> DiagnosticsSnapshot: ...

    async def events(self, query: RecordQuery) -> DiagnosticPage: ...

    async def logs(
        self, query: RecordQuery, *, source: LogSource
    ) -> DiagnosticPage: ...


__all__ = [
    "ID_PATTERN",
    "MAX_EVENTS",
    "MAX_LIMIT",
    "DiagnosticPage",
    "DiagnosticsAccess",
    "DiagnosticsSnapshot",
    "InvalidCursor",
    "LevelName",
    "LogSource",
    "RecordQuery",
    "StoreUnavailable",
]
