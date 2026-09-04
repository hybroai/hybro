"""Async in-memory ports for kernel and session tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .events import canonicalize_orchestrator_event, evaluate_event_append
from .models import (
    CancellationCause,
    OrchestratorEvent,
    OrchestratorRunState,
    ProjectionIntent,
    RecoveryClaim,
)
from .persistence import RECOVERY_ELIGIBLE_RUN_STATUSES
from .settlement import transition_projection_intent, transition_projection_settlement


@dataclass(frozen=True, slots=True)
class InMemoryRunStoreResult:
    outcome: str
    run: OrchestratorRunState | None


class InMemoryOrchestratorRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, OrchestratorRunState] = {}
        self.commands: dict[tuple[str, str], OrchestratorRunState] = {}

    async def create(
        self, run: OrchestratorRunState, *, command_id: str
    ) -> InMemoryRunStoreResult:
        command_key = (run.run_id, command_id)
        if command_key in self.commands:
            return InMemoryRunStoreResult("replayed", self.commands[command_key])
        existing = self.runs.get(run.run_id)
        if existing is not None:
            return InMemoryRunStoreResult(
                "replayed" if existing == run else "conflict", existing
            )
        duplicate = next(
            (
                item
                for item in self.runs.values()
                if item.room_id == run.room_id
                and item.client_request_id == run.client_request_id
                and run.client_request_id is not None
            ),
            None,
        )
        if duplicate is not None:
            if duplicate.request.request_fingerprint != run.request.request_fingerprint:
                return InMemoryRunStoreResult("conflict", duplicate)
            return InMemoryRunStoreResult("replayed", duplicate)
        self.runs[run.run_id] = run
        self.commands[command_key] = run
        return InMemoryRunStoreResult("accepted", run)

    async def load(self, run_id: str) -> OrchestratorRunState | None:
        return self.runs.get(run_id)

    async def load_by_user_message_id(
        self, user_message_id: str
    ) -> OrchestratorRunState | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.request.user_message_id == user_message_id
            ),
            None,
        )

    async def cas_mutate(
        self,
        run: OrchestratorRunState,
        *,
        expected_state_version: int,
        command_id: str,
    ) -> InMemoryRunStoreResult:
        command_key = (run.run_id, command_id)
        if command_key in self.commands:
            return InMemoryRunStoreResult("replayed", self.commands[command_key])
        current = self.runs.get(run.run_id)
        if current is None:
            return InMemoryRunStoreResult("error", None)
        if current.state_version != expected_state_version:
            return InMemoryRunStoreResult("conflict", current)
        if run.state_version != expected_state_version + 1:
            return InMemoryRunStoreResult("error", current)
        if not _cancellation_transition_allowed(current, run):
            return InMemoryRunStoreResult("conflict", current)
        self.runs[run.run_id] = run
        self.commands[command_key] = run
        return InMemoryRunStoreResult("accepted", run)

    async def request_cancellation(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        command_id: str,
        cause: CancellationCause,
        requested_at: datetime,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        if run is None:
            return InMemoryRunStoreResult("error", None)
        if (run_id, command_id) in self.commands:
            return InMemoryRunStoreResult("replayed", run)
        if run.state_version != expected_state_version or run.status not in {
            "queued",
            "running",
            "waiting_external",
            "awaiting_user",
        }:
            return InMemoryRunStoreResult("conflict", run)
        candidate = run.model_copy(
            update={
                "status": "canceling",
                "cancellation_command_id": command_id,
                "cancellation_requested_at": requested_at,
                "cancellation_cause": cause,
                "recovery_claim": RecoveryClaim(
                    kind="cancellation", next_attempt_at=requested_at
                ),
                "state_version": run.state_version + 1,
                "updated_at": requested_at,
            }
        )
        return await self.cas_mutate(
            candidate,
            expected_state_version=run.state_version,
            command_id=command_id,
        )

    async def repair_canceling_recovery(self, *, limit: int) -> int:
        repaired = 0
        for run in sorted(
            self.runs.values(), key=lambda item: (item.updated_at, item.run_id)
        ):
            if repaired >= max(limit, 0):
                break
            if run.status != "canceling" or run.recovery_claim.kind == "cancellation":
                continue
            self.runs[run.run_id] = run.model_copy(
                update={
                    "recovery_claim": RecoveryClaim(
                        kind="cancellation", next_attempt_at=datetime.now(UTC)
                    )
                }
            )
            repaired += 1
        return repaired

    async def claim_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or lease_expires_at <= claimed_at
            or run.recovery_claim.quarantined_at is not None
            or (
                run.recovery_claim.next_attempt_at is not None
                and run.recovery_claim.next_attempt_at > claimed_at
            )
            or (
                run.recovery_claim.lease_expires_at is not None
                and run.recovery_claim.lease_expires_at > claimed_at
            )
        ):
            return InMemoryRunStoreResult("conflict", run)
        return await self._replace(
            run.model_copy(
                update={
                    "recovery_claim": run.recovery_claim.model_copy(
                        update={
                            "owner_id": owner_id,
                            "lease_expires_at": lease_expires_at,
                            "next_attempt_at": None,
                        }
                    ),
                    "state_version": run.state_version + 1,
                }
            ),
            run.state_version,
            f"claim:{owner_id}:{run.state_version}",
        )

    async def renew_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or run.recovery_claim.owner_id != owner_id
        ):
            return InMemoryRunStoreResult("conflict", run)
        # Lease heartbeats are fencing metadata, not execution mutations. Keep
        # the aggregate CAS version stable so a slow Kernel checkpoint built
        # from this version can still commit while the worker renews its lease.
        renewed = run.model_copy(
            update={
                "recovery_claim": run.recovery_claim.model_copy(
                    update={"lease_expires_at": lease_expires_at}
                )
            }
        )
        self.runs[run_id] = renewed
        return InMemoryRunStoreResult("accepted", renewed)

    async def release_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime | None,
        failure_count: int = 0,
        quarantined_at: datetime | None = None,
        quarantine_reason: Literal["terminal_invariant_conflict"] | None = None,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or run.recovery_claim.owner_id != owner_id
        ):
            return InMemoryRunStoreResult("conflict", run)
        return await self._replace(
            run.model_copy(
                update={
                    "recovery_claim": RecoveryClaim(
                        kind=run.recovery_claim.kind,
                        next_attempt_at=next_attempt_at,
                        failure_count=failure_count,
                        quarantined_at=quarantined_at,
                        quarantine_reason=quarantine_reason,
                    ),
                    "state_version": run.state_version + 1,
                }
            ),
            run.state_version,
            f"release:{owner_id}:{run.state_version}",
        )

    async def list_due_runs(
        self, *, due_at: datetime, limit: int
    ) -> list[OrchestratorRunState]:
        due = [
            run
            for run in self.runs.values()
            if run.status in RECOVERY_ELIGIBLE_RUN_STATUSES
            and run.recovery_claim.quarantined_at is None
            and (
                run.recovery_claim.next_attempt_at is None
                or run.recovery_claim.next_attempt_at <= due_at
            )
            and (
                run.recovery_claim.lease_expires_at is None
                or run.recovery_claim.lease_expires_at <= due_at
            )
        ]
        due.sort(
            key=lambda run: (
                run.recovery_claim.next_attempt_at or run.updated_at,
                run.run_id,
            )
        )
        return due[: max(limit, 0)]

    async def claim_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> InMemoryRunStoreResult:
        return await self._transition_intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            command_id=f"claim-intent:{intent_id}:{expected_state_version}",
            to_status="claimed",
            claim_owner=owner_id,
            claim_expires_at=lease_expires_at,
        )

    async def complete_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        item = _intent(run, intent_id)
        if item is None or item.claim_owner != owner_id:
            return InMemoryRunStoreResult("conflict", run)
        return await self._transition_intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            command_id=f"complete-intent:{intent_id}:{expected_state_version}",
            to_status="completed",
        )

    async def block_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        reason: str,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        item = _intent(run, intent_id)
        if item is None or item.claim_owner not in {None, owner_id}:
            return InMemoryRunStoreResult("conflict", run)
        return await self._transition_intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            command_id=f"block-intent:{intent_id}:{expected_state_version}",
            to_status="blocked",
            blocked_reason=reason,
        )

    async def release_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        item = _intent(run, intent_id)
        if item is None or item.status != "claimed":
            return InMemoryRunStoreResult("conflict", run)
        lease_expired = (
            item.claim_expires_at is not None and item.claim_expires_at <= now
        )
        if item.claim_owner != owner_id and not lease_expired:
            return InMemoryRunStoreResult("conflict", run)
        return await self._transition_intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            command_id=f"release-intent:{intent_id}:{expected_state_version}",
            to_status="pending",
            next_attempt_at=next_attempt_at,
        )

    async def list_due_projection_intents(
        self, *, due_at: datetime, limit: int
    ) -> list[tuple[str, ProjectionIntent]]:
        due: list[tuple[datetime, str, ProjectionIntent]] = []
        for run in self.runs.values():
            for intent in run.projection_outbox:
                if intent.status == "pending" and (
                    intent.next_attempt_at is None or intent.next_attempt_at <= due_at
                ):
                    due.append(
                        (intent.next_attempt_at or run.updated_at, run.run_id, intent)
                    )
                elif (
                    intent.status == "claimed"
                    and intent.claim_expires_at is not None
                    and intent.claim_expires_at <= due_at
                ):
                    due.append((intent.claim_expires_at, run.run_id, intent))
        due.sort(key=lambda item: (item[0], item[1]))
        return [(run_id, intent) for _, run_id, intent in due[:limit]]

    async def _transition_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        command_id: str,
        to_status: str,
        **kwargs: object,
    ) -> InMemoryRunStoreResult:
        run = self.runs.get(run_id)
        if run is None or run.state_version != expected_state_version:
            return InMemoryRunStoreResult("conflict", run)
        index = next(
            (
                index
                for index, item in enumerate(run.projection_outbox)
                if item.intent_id == intent_id
            ),
            None,
        )
        if index is None:
            return InMemoryRunStoreResult("conflict", run)
        intents = list(run.projection_outbox)
        intents[index] = transition_projection_intent(
            intents[index],
            to_status=to_status,
            **kwargs,  # type: ignore[arg-type]
        )
        return await self._replace(
            run.model_copy(
                update={
                    "projection_outbox": intents,
                    "state_version": run.state_version + 1,
                }
            ),
            expected_state_version,
            command_id,
        )

    async def _replace(
        self, run: OrchestratorRunState, expected: int, command: str
    ) -> InMemoryRunStoreResult:
        return await self.cas_mutate(
            run, expected_state_version=expected, command_id=command
        )


class InMemoryOrchestratorEventStore:
    """Event append/read port backed by the pure ordering evaluation."""

    def __init__(self) -> None:
        self.events: dict[str, list[OrchestratorEvent]] = {}

    async def append(self, event: OrchestratorEvent) -> str:
        event = canonicalize_orchestrator_event(event)
        existing = self.events.setdefault(event.run_id, [])
        evaluation = evaluate_event_append(existing, event)
        if evaluation.outcome == "accepted":
            self.events[event.run_id] = [*existing, event]
        return evaluation.outcome

    async def read(
        self, run_id: str, *, after_sequence: int = 0
    ) -> list[OrchestratorEvent]:
        return [
            event
            for event in self.events.get(run_id, [])
            if event.sequence > after_sequence
        ]

    async def delete_by_epoch(self, room_id: str, room_epoch: int) -> int:
        deleted = 0
        for run_id, events in list(self.events.items()):
            kept = [
                event
                for event in events
                if not (event.room_id == room_id and event.room_epoch == room_epoch)
            ]
            deleted += len(events) - len(kept)
            if kept:
                self.events[run_id] = kept
            else:
                self.events.pop(run_id, None)
        return deleted


class InMemoryProjectionDriver:
    """Claim and complete required intents without external side effects."""

    def __init__(self, run_store: InMemoryOrchestratorRunStore) -> None:
        self.run_store = run_store

    async def settle(self, run_id: str) -> OrchestratorRunState:
        run = await self.run_store.load(run_id)
        if run is None:
            raise KeyError(run_id)
        owner = "plan2-projection"
        for intent in list(run.projection_outbox):
            if not intent.required or intent.status == "completed":
                continue
            if intent.status == "pending":
                result = await self.run_store.claim_projection_intent(
                    run_id,
                    intent.intent_id,
                    expected_state_version=run.state_version,
                    owner_id=owner,
                    lease_expires_at=run.updated_at,
                )
                if result.run is None:
                    raise RuntimeError("projection claim failed")
                run = result.run
            result = await self.run_store.complete_projection_intent(
                run_id,
                intent.intent_id,
                expected_state_version=run.state_version,
                owner_id=owner,
            )
            if result.run is None:
                raise RuntimeError("projection completion failed")
            run = result.run
        transition = transition_projection_settlement(
            run, expected_state_version=run.state_version, updated_at=run.updated_at
        )
        if transition.outcome == "accepted":
            result = await self.run_store.cas_mutate(
                transition.run,
                expected_state_version=run.state_version,
                command_id=f"settle:{run.run_id}:{run.state_version}",
            )
            if result.run is not None:
                run = result.run
        return run


def _cancellation_transition_allowed(
    current: OrchestratorRunState, candidate: OrchestratorRunState
) -> bool:
    metadata = (
        "cancellation_command_id",
        "cancellation_requested_at",
        "cancellation_cause",
    )
    if current.cancellation_command_id is not None and any(
        getattr(current, field) != getattr(candidate, field) for field in metadata
    ):
        return False
    if current.status != "canceling":
        return True
    return candidate.status in {"canceling", "canceled"} and all(
        getattr(current, field) == getattr(candidate, field) for field in metadata
    )


def _intent(run: OrchestratorRunState | None, intent_id: str):
    if run is None:
        return None
    return next(
        (item for item in run.projection_outbox if item.intent_id == intent_id), None
    )


# concise aliases used by kernel/session tests
InMemoryRunStore = InMemoryOrchestratorRunStore

__all__ = [
    "InMemoryOrchestratorEventStore",
    "InMemoryOrchestratorRunStore",
    "InMemoryProjectionDriver",
    "InMemoryRunStore",
    "InMemoryRunStoreResult",
]
