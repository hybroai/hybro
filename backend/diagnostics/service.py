"""DiagnosticsService: the live snapshot, the stored records and who may read.

The snapshot's sources are zero-argument callables the container builds over
its runtime objects, so this module imports only ``common`` and its own
package. Every async source and every store query is bounded, so an outage is
reported within seconds instead of holding the request for Motor's
server-selection timeout.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from bson import ObjectId

from common.observability import sanitize_text
from common.observability.job_runs import job_runs
from common.observability.sinks import LogEntry, RingBufferSink
from common.utils.time import utcnow
from diagnostics.models import (
    AgentItem,
    AgentsSection,
    CanaryReading,
    CanarySection,
    DiagnosticPage,
    DiagnosticRecord,
    DiagnosticsSnapshot,
    EventingSection,
    HealthSection,
    JobRunView,
    LeaderSection,
    LogSource,
    ProviderSection,
    RecordQuery,
    RecoveryRun,
    RecoverySection,
    ServiceSection,
    SnapshotError,
    SseSection,
    StoreSection,
)
from diagnostics.protocols import InvalidCursor, StoreUnavailable
from diagnostics.repository.mongo import (
    EVENTS_COLLECTION,
    LOGS_COLLECTION,
    DiagnosticsMongoRepository,
    RecordFilter,
    to_document,
)

# Bound on each async snapshot source and each store query.
SOURCE_TIMEOUT_SECONDS = 3.0
# The canary reading scans three collections; reuse it this long.
CANARY_CACHE = timedelta(seconds=30)
AGENT_ITEMS_MAX = 200
AGENT_NAME_LIMIT = 80
# The smallest configured token ``token_matches`` accepts, in bytes.
MIN_TOKEN_BYTES = 32


@dataclass(frozen=True)
class SnapshotSources:
    """Zero-argument readers of the runtime, one per snapshot section.

    ``canary`` is None when the orchestrator runtime has no canary collector;
    the canary and the metric-backed recovery fields are then null.
    """

    health: Callable[[], Awaitable[HealthSection]]
    provider: Callable[[], ProviderSection]
    agents: Callable[[], Awaitable[list[dict[str, object]]]]
    leader: Callable[[], LeaderSection]
    eventing: Callable[[], EventingSection]
    sse: Callable[[], SseSection]
    recovery_run: Callable[[], RecoveryRun]
    canary: Callable[[], Awaitable[CanaryReading]] | None
    store: Callable[[], StoreSection]


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _agent_item(record: Mapping[str, object]) -> AgentItem:
    card = record.get("agent_card")
    name = card.get("name") if isinstance(card, Mapping) else None
    return AgentItem(
        agent_id=str(record.get("agent_id") or ""),
        name=sanitize_text(str(name or ""), limit=AGENT_NAME_LIMIT),
        source=_text(record.get("source")),
        status=_text(record.get("agent_status")),
        inactive_reason=_text(record.get("inactive_reason")),
    )


def _agents_section(records: list[dict[str, object]]) -> AgentsSection:
    statuses = Counter(record.get("agent_status") for record in records)
    sources = Counter(str(record.get("source") or "unknown") for record in records)
    return AgentsSection(
        total=len(records),
        active=statuses["active"],
        inactive=statuses["inactive"],
        by_source=dict(sources),
        items=[_agent_item(record) for record in records[:AGENT_ITEMS_MAX]],
        truncated=len(records) > AGENT_ITEMS_MAX,
    )


def _jobs() -> list[JobRunView]:
    return [JobRunView.model_validate(asdict(state)) for state in job_runs.snapshot()]


def _recovery_section(
    run: RecoveryRun | None, canary: CanaryReading | None
) -> RecoverySection | None:
    if run is None:
        return None
    metrics: dict[str, int] = {}
    if canary is not None:
        metrics = {
            "non_terminal_runs": canary.non_terminal_runs,
            "agent_calls_outstanding": canary.metrics.agent_calls_outstanding,
            "projection_outbox_pending": canary.metrics.projection_outbox_pending,
            "projection_outbox_blocked": canary.metrics.projection_outbox_blocked,
            "observation_conflicts_open": canary.metrics.observation_conflicts_open,
        }
    return RecoverySection(**run.model_dump(), **metrics)


def _canary_section(canary: CanaryReading | None) -> CanarySection | None:
    if canary is None:
        return None
    return CanarySection(
        job_enabled=canary.job_enabled,
        metrics=canary.metrics,
        breaches=canary.breaches,
    )


def _min_level_no(query: RecordQuery) -> int | None:
    if query.level is None:
        return None
    return logging.getLevelNamesMapping()[query.level.upper()]


def _record_filter(query: RecordQuery) -> RecordFilter:
    before_id = None
    if query.before is not None:
        if not ObjectId.is_valid(query.before):
            raise InvalidCursor("Invalid cursor.")
        before_id = ObjectId(query.before)
    return RecordFilter(
        service=query.service,
        events=query.event,
        min_level_no=_min_level_no(query),
        trace_id=query.trace_id,
        instance_id=query.instance_id,
        since=query.since,
        until=query.until,
        before_id=before_id,
        limit=query.limit,
    )


def _fields(fields_json: object) -> dict[str, object]:
    try:
        fields = json.loads(str(fields_json))
    except ValueError:
        return {}
    return fields if isinstance(fields, dict) else {}


def _record(document: Mapping[str, object], record_id: str) -> DiagnosticRecord:
    """A stored document (or a memory entry in that shape) as a record."""
    return DiagnosticRecord.model_validate(
        {
            "id": record_id,
            "timestamp": document["ts"],
            "level": document["level"],
            "service": document["service"],
            "event": document["event"],
            "logger": document["logger"],
            "system": document["system"],
            "instance_id": document.get("instance_id"),
            "version": document.get("version"),
            "trace_id": document.get("trace_id"),
            "request_id": document.get("request_id"),
            "fields": _fields(document.get("fields_json")),
        }
    )


def _page(items: list[DiagnosticRecord], limit: int) -> DiagnosticPage:
    full = len(items) == limit
    return DiagnosticPage(items=items, next_before=items[-1].id if full else None)


def _memory_cursor(query: RecordQuery) -> int | None:
    if query.before is None:
        return None
    if not (query.before.isascii() and query.before.isdecimal()):
        raise InvalidCursor("Invalid cursor.")
    return int(query.before)


def _entry_matches(entry: LogEntry, query: RecordQuery, min_level: int) -> bool:
    checks = (
        entry.level_no >= min_level,
        query.service is None or entry.service == query.service,
        not query.event or entry.event in query.event,
        query.trace_id is None or entry.fields.get("trace_id") == query.trace_id,
        query.instance_id is None
        or entry.fields.get("instance_id") == query.instance_id,
        query.since is None or entry.timestamp >= query.since,
        query.until is None or entry.timestamp < query.until,
    )
    return all(checks)


class DiagnosticsService:
    """The ``DiagnosticsAccess`` implementation."""

    def __init__(
        self,
        sources: SnapshotSources,
        *,
        repository: DiagnosticsMongoRepository,
        ring: RingBufferSink | None,
        token: str,
        auth_mode: str,
        admin_user_ids: frozenset[str],
        version: str,
        instance_id: str,
        environment: str,
        log_level: str,
        log_format: str,
        started_at: datetime,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sources = sources
        self._repository = repository
        self._ring = ring
        self._token = token
        self._auth_mode = auth_mode
        self._admins = admin_user_ids
        self._identity = {
            "version": version,
            "instance_id": instance_id,
            "environment": environment,
            "auth_mode": auth_mode,
            "log_level": log_level,
            "log_format": log_format,
        }
        self._started_at = started_at
        self._now = now
        self._canary_cache: tuple[datetime, CanaryReading] | None = None
        # One scan at a time: concurrent snapshots wait for it and share it.
        self._canary_lock = asyncio.Lock()

    # ── Authorisation ──

    def can_read(self, user_id: str) -> bool:
        # The rule of BrowserSetup.can_configure: mock mode trusts the caller.
        return self._auth_mode == "mock" or user_id in self._admins

    def token_matches(self, value: str) -> bool:
        expected = self._token.encode()
        if len(expected) < MIN_TOKEN_BYTES:
            return False
        try:
            given = value.encode()
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(given, expected)

    # ── Snapshot ──

    async def snapshot(self) -> DiagnosticsSnapshot:
        errors: list[SnapshotError] = []
        generated_at = self._now()
        health, agents, canary = await asyncio.gather(
            self._bounded("health", self._sources.health, errors),
            self._bounded("agents", self._agents, errors),
            self._canary(generated_at, errors),
        )
        sources = self._sources
        return DiagnosticsSnapshot(
            generated_at=generated_at,
            service=ServiceSection(
                **self._identity,
                started_at=self._started_at,
                uptime_seconds=(generated_at - self._started_at).total_seconds(),
            ),
            health=health,
            provider=self._read("provider", sources.provider, errors),
            agents=agents,
            jobs=self._read("jobs", _jobs, errors),
            leader=self._read("leader", sources.leader, errors),
            eventing=self._read("eventing", sources.eventing, errors),
            sse=self._read("sse", sources.sse, errors),
            recovery=_recovery_section(
                self._read("recovery", sources.recovery_run, errors), canary
            ),
            canary=_canary_section(canary),
            store=self._read("store", sources.store, errors),
            errors=sorted(errors, key=lambda error: error.section),
        )

    async def _agents(self) -> AgentsSection:
        return _agents_section(await self._sources.agents())

    async def _canary(
        self, now: datetime, errors: list[SnapshotError]
    ) -> CanaryReading | None:
        source = self._sources.canary
        if source is None:
            return None

        async def shared_reading() -> CanaryReading:
            async with self._canary_lock:
                cached = self._canary_cache
                if cached is not None and now - cached[0] < CANARY_CACHE:
                    return cached[1]
                reading = await source()
                self._canary_cache = (now, reading)
                return reading

        # One bound covers the wait for the lock and the scan together.
        return await self._bounded("canary", shared_reading, errors)

    @staticmethod
    async def _bounded[T](
        section: str, source: Callable[[], Awaitable[T]], errors: list[SnapshotError]
    ) -> T | None:
        try:
            return await asyncio.wait_for(source(), SOURCE_TIMEOUT_SECONDS)
        except Exception as exc:
            errors.append(SnapshotError(section=section, error_type=type(exc).__name__))
            return None

    @staticmethod
    def _read[T](
        section: str, source: Callable[[], T], errors: list[SnapshotError]
    ) -> T | None:
        try:
            return source()
        except Exception as exc:
            errors.append(SnapshotError(section=section, error_type=type(exc).__name__))
            return None

    # ── Records ──

    async def events(self, query: RecordQuery) -> DiagnosticPage:
        return await self._store_page(EVENTS_COLLECTION, query)

    async def logs(self, query: RecordQuery, *, source: LogSource) -> DiagnosticPage:
        if source == "memory":
            return self._memory_page(query)
        return await self._store_page(LOGS_COLLECTION, query)

    def _store_ready(self) -> bool:
        try:
            return self._sources.store().ready
        except Exception:
            return False

    async def _store_page(self, collection: str, query: RecordQuery) -> DiagnosticPage:
        record_filter = _record_filter(query)
        if not self._store_ready():
            raise StoreUnavailable("The diagnostics store is not ready.")
        try:
            documents = await asyncio.wait_for(
                self._repository.find(collection, record_filter),
                SOURCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise StoreUnavailable(type(exc).__name__) from exc
        items = [_record(document, str(document["_id"])) for document in documents]
        return _page(items, query.limit)

    def _memory_page(self, query: RecordQuery) -> DiagnosticPage:
        before = _memory_cursor(query)
        if self._ring is None:
            return DiagnosticPage(items=[], next_before=None)
        min_level = _min_level_no(query) or 0
        items: list[DiagnosticRecord] = []
        for entry in reversed(self._ring.entries()):
            if len(items) == query.limit:
                break
            if before is not None and entry.sequence >= before:
                continue
            if _entry_matches(entry, query, min_level):
                items.append(_record(to_document(entry), str(entry.sequence)))
        return _page(items, query.limit)


__all__ = [
    "CANARY_CACHE",
    "SOURCE_TIMEOUT_SECONDS",
    "DiagnosticsService",
    "SnapshotSources",
]
