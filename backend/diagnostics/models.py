"""Read models of the diagnostics API: record queries, pages and the snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue

from common.dto.base import FrozenDTO

LevelName = Literal["debug", "info", "warning", "error", "critical"]
LogSource = Literal["store", "memory"]
# Trace, request and instance ids accepted as filters.
ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
MAX_EVENTS = 20
MAX_LIMIT = 500


class _Model(FrozenDTO):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecordQuery(_Model):
    """Filters of ``/events`` and ``/logs``; ``level`` is a threshold."""

    service: str | None = None
    level: LevelName | None = None
    event: tuple[str, ...] = Field(default=(), max_length=MAX_EVENTS)
    trace_id: str | None = Field(default=None, pattern=ID_PATTERN)
    instance_id: str | None = Field(default=None, pattern=ID_PATTERN)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    limit: int = Field(default=100, ge=1, le=MAX_LIMIT)
    before: str | None = None


class DiagnosticRecord(_Model):
    id: str
    timestamp: datetime
    level: str
    service: str
    event: str
    logger: str
    system: bool
    instance_id: str | None
    version: str | None
    trace_id: str | None
    request_id: str | None
    fields: dict[str, JsonValue]


class DiagnosticPage(_Model):
    """Newest first; ``next_before`` is set when the page is full."""

    items: list[DiagnosticRecord]
    next_before: str | None


class SnapshotError(_Model):
    section: str
    error_type: str


class ServiceSection(_Model):
    version: str
    instance_id: str
    environment: str
    started_at: datetime
    uptime_seconds: float
    auth_mode: str
    log_level: str
    log_format: str


class HealthSection(_Model):
    """Raw connection flags; the derived status stays with ``/health``."""

    mongo_connected: bool
    redis_expected: bool
    delivery_pubsub_connected: bool
    delivery_kv_connected: bool
    eventing_connected: bool
    redis_runtime_connected: bool
    agent_search_index_ready: bool


class ProviderSection(_Model):
    state: str
    last_error: str | None


class AgentItem(_Model):
    agent_id: str
    name: str
    source: str | None
    status: str | None
    inactive_reason: str | None


class AgentsSection(_Model):
    total: int
    active: int
    inactive: int
    by_source: dict[str, int]
    items: list[AgentItem]
    truncated: bool


class JobRunView(_Model):
    """One job's ``common.observability.job_runs`` state."""

    job: str
    running: bool
    interval_seconds: float | None
    last_outcome: Literal["ok", "failed", "skipped_not_leader"] | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_duration_ms: float | None
    last_skipped_at: datetime | None
    runs: int
    failures: int
    skips: int
    consecutive_failures: int
    last_error_type: str | None


class LeaderSection(_Model):
    enabled: bool
    instance_id: str


class EventingSection(_Model):
    connected: bool
    transport: Literal["redis", "memory"]
    dead_letter_count: int
    dead_letter_capacity: int
    dead_letters_by_stage: dict[str, int]
    last_dead_letter_at: datetime | None


class SseSection(_Model):
    rooms: int
    connections: int


class RecoveryRun(_Model):
    last_cycle_completed_at: datetime | None
    last_cycle_failed_phases: list[str]


class RecoverySection(_Model):
    """The last cycle, plus canary metrics (null when no reading exists)."""

    last_cycle_completed_at: datetime | None
    last_cycle_failed_phases: list[str]
    non_terminal_runs: int | None = None
    agent_calls_outstanding: int | None = None
    projection_outbox_pending: int | None = None
    projection_outbox_blocked: int | None = None
    observation_conflicts_open: int | None = None


class CanaryMetrics(_Model):
    """The keys ``execution.adapters.canary_metrics.collect_metrics`` returns."""

    runs_by_status: dict[str, int]
    runs_by_status_window: dict[str, int]
    projection_outbox_pending: int
    projection_outbox_blocked: int
    projection_outbox_blocked_oldest_at: datetime | None
    agent_calls_outstanding: int
    observation_conflicts_open: int
    recovery_cycle_last_run_at: datetime | None
    collected_at: datetime


class CanaryBreachView(_Model):
    metric: str
    threshold: float
    observed: float


class CanaryReading(_Model):
    job_enabled: bool
    metrics: CanaryMetrics
    breaches: list[CanaryBreachView]
    non_terminal_runs: int


class CanarySection(_Model):
    job_enabled: bool
    metrics: CanaryMetrics
    breaches: list[CanaryBreachView]


class StoreSection(_Model):
    ready: bool
    queued: int
    dropped: int
    written: int
    write_failures: int
    last_error_type: str | None
    last_write_at: datetime | None
    ring_size: int
    ring_capacity: int


class DiagnosticsSnapshot(_Model):
    """Live state of this process; a section whose source failed is null."""

    generated_at: datetime
    service: ServiceSection
    health: HealthSection | None = None
    provider: ProviderSection | None = None
    agents: AgentsSection | None = None
    jobs: list[JobRunView] | None = None
    leader: LeaderSection | None = None
    eventing: EventingSection | None = None
    sse: SseSection | None = None
    recovery: RecoverySection | None = None
    canary: CanarySection | None = None
    store: StoreSection | None = None
    errors: list[SnapshotError]


__all__ = [
    "ID_PATTERN",
    "MAX_EVENTS",
    "MAX_LIMIT",
    "AgentItem",
    "AgentsSection",
    "CanaryBreachView",
    "CanaryMetrics",
    "CanaryReading",
    "CanarySection",
    "DiagnosticPage",
    "DiagnosticRecord",
    "DiagnosticsSnapshot",
    "EventingSection",
    "HealthSection",
    "JobRunView",
    "LeaderSection",
    "LevelName",
    "LogSource",
    "ProviderSection",
    "RecordQuery",
    "RecoveryRun",
    "RecoverySection",
    "ServiceSection",
    "SnapshotError",
    "SseSection",
    "StoreSection",
]
