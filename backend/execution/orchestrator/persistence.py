"""Mongo collection and index metadata for orchestrator persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from common.run_status import TERMINAL_RUN_STATUSES as TERMINAL_RUN_STATUSES

if TYPE_CHECKING:
    from execution.orchestrator.models import OrchestratorRunState

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


# An ended Run takes writes only to its bookkeeping: projection settlement,
# projection intents, the recovery claim and the CAS fields. A late Agent
# outcome or a pass that kept running after the end must not change it.
ENDED_RUN_BOOKKEEPING_FIELDS = frozenset(
    {
        "projection_state",
        "projection_outbox",
        "recovery_claim",
        "state_version",
        "updated_at",
        "processed_command_ids",
    }
)


def ended_run_change_allowed(
    current: OrchestratorRunState, candidate: OrchestratorRunState
) -> bool:
    if current.status not in TERMINAL_RUN_STATUSES:
        return True
    return current.model_dump(exclude=ENDED_RUN_BOOKKEEPING_FIELDS) == (
        candidate.model_dump(exclude=ENDED_RUN_BOOKKEEPING_FIELDS)
    )


# A suspended Run waits for the owner or an external Agent. Its deadline clock
# is stopped while it waits (``BudgetState.suspended_at``), so it does not end
# at the profile deadline; it ends on an answer, a late result, a cancel, or a
# call that exceeds its wait ceiling.
SUSPENDED_RUN_STATUSES = frozenset({"waiting_external", "awaiting_user"})

# Suspended Runs keep a finite far-future recovery schedule while dormant and
# are re-armed at their extended deadline when they resume.
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
