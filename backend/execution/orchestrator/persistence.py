"""Mongo collection and index metadata for orchestrator persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

ORCHESTRATOR_RUNS_COLLECTION = "orchestrator_runs"
ORCHESTRATOR_RUN_EVENTS_COLLECTION = "orchestrator_run_events"
ORCHESTRATOR_RECOVERY_LEASES_COLLECTION = "orchestrator_recovery_leases"

OBSOLETE_ORCHESTRATOR_RUN_INDEXES = ("orchestrator_active_room_unique",)

NON_TERMINAL_RUN_STATUSES = (
    "queued",
    "running",
    "waiting_external",
    "awaiting_user",
    "canceling",
    "finalizing",
)

# Suspended Runs remain dormant until ``recovery_claim.next_attempt_at``.  At
# the profile deadline generic recovery must terminalize even when no HITL
# answer/cancel arrives, otherwise accepted public Tool children stay open
# forever.
RECOVERY_ELIGIBLE_RUN_STATUSES = (
    "queued",
    "running",
    "waiting_external",
    "awaiting_user",
    "canceling",
    "finalizing",
)


@dataclass(frozen=True, slots=True)
class MongoIndexDefinition:
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False
    partial_filter: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MongoCollectionDefinition:
    name: str
    indexes: tuple[MongoIndexDefinition, ...] = field(default_factory=tuple)


ORCHESTRATOR_RUN_INDEXES = (
    MongoIndexDefinition(
        name="orchestrator_run_id_unique", keys=(("run_id", 1),), unique=True
    ),
    MongoIndexDefinition(
        name="orchestrator_active_room_unique_canceling",
        keys=(("room_id", 1),),
        unique=True,
        partial_filter=MappingProxyType(
            {"status": {"$in": list(NON_TERMINAL_RUN_STATUSES)}}
        ),
    ),
    MongoIndexDefinition(
        name="orchestrator_client_request",
        keys=(("room_id", 1), ("client_request_id", 1)),
        unique=True,
        partial_filter=MappingProxyType({"client_request_id": {"$type": "string"}}),
    ),
    MongoIndexDefinition(
        name="orchestrator_tool_call_id",
        keys=(("run_id", 1), ("tool_batches.entries.call_id", 1)),
        unique=True,
    ),
    MongoIndexDefinition(
        name="orchestrator_canceling_recovery",
        keys=(("updated_at", 1), ("run_id", 1)),
        partial_filter=MappingProxyType({"status": "canceling"}),
    ),
    MongoIndexDefinition(
        name="orchestrator_recovery_due",
        keys=(
            ("recovery_claim.next_attempt_at", 1),
            ("recovery_claim.lease_expires_at", 1),
        ),
    ),
    MongoIndexDefinition(
        name="orchestrator_projection_due",
        keys=(
            ("projection_outbox.status", 1),
            ("projection_outbox.next_attempt_at", 1),
            ("projection_outbox.claim_expires_at", 1),
        ),
    ),
)

ORCHESTRATOR_EVENT_INDEXES = (
    MongoIndexDefinition(
        name="orchestrator_event_id_unique", keys=(("event_id", 1),), unique=True
    ),
    MongoIndexDefinition(
        name="orchestrator_event_sequence_unique",
        keys=(("run_id", 1), ("sequence", 1)),
        unique=True,
    ),
    MongoIndexDefinition(
        name="orchestrator_event_epoch_cleanup",
        keys=(("room_id", 1), ("room_epoch", 1)),
    ),
)

ORCHESTRATOR_COLLECTIONS = (
    MongoCollectionDefinition(
        name=ORCHESTRATOR_RUNS_COLLECTION, indexes=ORCHESTRATOR_RUN_INDEXES
    ),
    MongoCollectionDefinition(
        name=ORCHESTRATOR_RECOVERY_LEASES_COLLECTION,
        indexes=(
            MongoIndexDefinition(
                name="orchestrator_recovery_lease_run_unique",
                keys=(("run_id", 1),),
                unique=True,
            ),
            MongoIndexDefinition(
                name="orchestrator_recovery_lease_due",
                keys=(
                    ("quarantined_at", 1),
                    ("next_attempt_at", 1),
                    ("lease_expires_at", 1),
                    ("run_id", 1),
                ),
            ),
        ),
    ),
    MongoCollectionDefinition(
        name=ORCHESTRATOR_RUN_EVENTS_COLLECTION, indexes=ORCHESTRATOR_EVENT_INDEXES
    ),
)
