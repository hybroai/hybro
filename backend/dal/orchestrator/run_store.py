"""Injected Mongo implementation of the generic OrchestratorRunStore port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pymongo.errors import DuplicateKeyError

from execution.orchestrator.a2a_runtime.errors import RecoverableAdapterError
from execution.orchestrator.models import (
    CancellationCause,
    OrchestratorRunState,
    ProjectionIntent,
    RecoveryClaim,
)
from execution.orchestrator.persistence import RECOVERY_ELIGIBLE_RUN_STATUSES
from execution.orchestrator.settlement import transition_projection_intent

from .stores import (
    AsyncMongoCollection,
    _bounded,
    _to_list,
    _without_mongo_id,
)


@dataclass(frozen=True, slots=True)
class MongoRunStoreResult:
    outcome: str
    run: OrchestratorRunState | None


class MongoOrchestratorRunStore:
    def __init__(
        self,
        collection: AsyncMongoCollection,
        recovery_collection: AsyncMongoCollection | None = None,
    ) -> None:
        self.collection = _bounded(collection)
        self._recovery_collection_name = getattr(recovery_collection, "name", None)
        self._recovery_collection = (
            _bounded(recovery_collection) if recovery_collection is not None else None
        )
        # Test/single-process fallback when an older constructor supplies only
        # the aggregate collection. Production always injects the durable
        # recovery collection from the composition root.
        self._recovery_claims: dict[str, RecoveryClaim] = {}

    async def create(
        self, run: OrchestratorRunState, *, command_id: str
    ) -> MongoRunStoreResult:
        processed_command_ids = list(run.processed_command_ids)
        if command_id not in processed_command_ids:
            processed_command_ids.append(command_id)
        candidate = _normalize_run_for_mongo(
            run.model_copy(update={"processed_command_ids": processed_command_ids})
        )
        existing = await self.load(run.run_id)
        if existing is not None:
            replay = command_id in existing.processed_command_ids
            return MongoRunStoreResult("replayed" if replay else "conflict", existing)
        duplicate = await self._load_client_request_duplicate(run)
        if duplicate is not None:
            replay = (
                duplicate.request.request_fingerprint == run.request.request_fingerprint
            )
            return MongoRunStoreResult("replayed" if replay else "conflict", duplicate)
        try:
            await self.collection.insert_one(candidate.model_dump(mode="python"))
        except DuplicateKeyError:
            existing = await self.load(run.run_id)
            if existing is not None:
                replay = command_id in existing.processed_command_ids
                return MongoRunStoreResult(
                    "replayed" if replay else "conflict", existing
                )
            duplicate = await self._load_client_request_duplicate(run)
            if duplicate is not None:
                replay = (
                    duplicate.request.request_fingerprint
                    == run.request.request_fingerprint
                )
                return MongoRunStoreResult(
                    "replayed" if replay else "conflict", duplicate
                )
            return MongoRunStoreResult("conflict", None)
        except RecoverableAdapterError:
            existing = await self.load(run.run_id)
            if existing is not None and command_id in existing.processed_command_ids:
                return MongoRunStoreResult("replayed", existing)
            raise
        return MongoRunStoreResult("accepted", candidate)

    async def load(self, run_id: str) -> OrchestratorRunState | None:
        value = await self.collection.find_one(
            {"run_id": run_id, "schema_version": {"$in": [5, 6]}}
        )
        if not value:
            return None
        run = _run_from_document(value)
        claim = await self._load_recovery_claim(run_id)
        return run.model_copy(update={"recovery_claim": claim}) if claim else run

    async def _load_recovery_claim(self, run_id: str) -> RecoveryClaim | None:
        if self._recovery_collection is None:
            return self._recovery_claims.get(run_id)
        value = await self._recovery_collection.find_one({"run_id": run_id})
        if value is None:
            return None
        return RecoveryClaim.model_validate(
            _restore_utc_datetimes(
                {
                    key: item
                    for key, item in value.items()
                    if key in RecoveryClaim.model_fields
                }
            )
        )

    async def _mirror_recovery_claim(self, run_id: str, claim: RecoveryClaim) -> None:
        """Refresh denormalized due-query fields without advancing Run CAS."""
        await self.collection.update_one(
            {"run_id": run_id},
            {"$set": {"recovery_claim": claim.model_dump(mode="python")}},
        )

    async def _claim_dedicated_recovery_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> RecoveryClaim | None:
        """Atomically acquire an absent, released, owned, or expired lease.

        ``owner_id`` includes the per-attempt token generated by the recovery
        worker, so the unique lease row is also the fencing record. A failed
        conditional update may insert only when the row is truly absent; the
        unique run index closes the concurrent-insert race.
        """
        assert self._recovery_collection is not None
        existing = await self._load_recovery_claim(run_id)
        if existing is not None and not _recovery_claim_is_due(
            existing, due_at=claimed_at
        ):
            return None
        claim = (existing or RecoveryClaim()).model_copy(
            update={
                "owner_id": owner_id,
                "lease_expires_at": lease_expires_at,
                "next_attempt_at": None,
            }
        )
        document = {"run_id": run_id, **claim.model_dump(mode="python")}
        query = {
            "run_id": run_id,
            "$and": [
                {
                    "$or": [
                        {"quarantined_at": None},
                        {"quarantined_at": {"$exists": False}},
                    ]
                },
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": claimed_at}},
                    ]
                },
                {
                    "$or": [
                        {"owner_id": None},
                        {"owner_id": {"$exists": False}},
                        {"lease_expires_at": None},
                        {"lease_expires_at": {"$exists": False}},
                        {"lease_expires_at": {"$lte": claimed_at}},
                    ]
                },
            ],
        }
        try:
            result = await self._recovery_collection.update_one(
                query,
                {"$set": claim.model_dump(mode="python")},
            )
            if int(getattr(result, "matched_count", 0)) == 1:
                return claim
            if await self._recovery_collection.find_one({"run_id": run_id}) is not None:
                return None
            await self._recovery_collection.insert_one(document)
            return claim
        except DuplicateKeyError:
            return None
        except RecoverableAdapterError:
            persisted = await self._load_recovery_claim(run_id)
            if persisted == claim:
                return claim
            raise

    async def _update_dedicated_recovery_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        claim: RecoveryClaim,
    ) -> bool:
        """Owner/token-fenced renewal or release of the dedicated lease row."""
        assert self._recovery_collection is not None
        try:
            result = await self._recovery_collection.update_one(
                {"run_id": run_id, "owner_id": owner_id},
                {"$set": claim.model_dump(mode="python")},
            )
            return int(getattr(result, "matched_count", 0)) == 1
        except RecoverableAdapterError:
            persisted = await self._load_recovery_claim(run_id)
            if persisted == claim:
                return True
            raise

    async def load_by_user_message_id(
        self, user_message_id: str
    ) -> OrchestratorRunState | None:
        """Correlate an orchestrator Run by its originating room user message."""
        value = await self.collection.find_one(
            {
                "request.user_message_id": user_message_id,
                "schema_version": {"$in": [5, 6]},
            }
        )
        return _run_from_document(value) if value else None

    async def _load_client_request_duplicate(
        self, run: OrchestratorRunState
    ) -> OrchestratorRunState | None:
        if run.client_request_id is None:
            return None
        value = await self.collection.find_one(
            {"room_id": run.room_id, "client_request_id": run.client_request_id}
        )
        return _run_from_document(value) if value else None

    async def cas_mutate(  # noqa: C901
        self,
        run: OrchestratorRunState,
        *,
        expected_state_version: int,
        command_id: str,
    ) -> MongoRunStoreResult:
        current = await self.load(run.run_id)
        if current is None:
            return MongoRunStoreResult("error", None)
        if command_id in current.processed_command_ids:
            return MongoRunStoreResult("replayed", current)
        if (
            current.state_version != expected_state_version
            or run.state_version != expected_state_version + 1
        ):
            return MongoRunStoreResult("conflict", current)
        processed_command_ids = list(run.processed_command_ids)
        if command_id not in processed_command_ids:
            processed_command_ids.append(command_id)
        candidate = _normalize_run_for_mongo(
            run.model_copy(update={"processed_command_ids": processed_command_ids})
        )
        if not _cancellation_transition_allowed(current, candidate):
            return MongoRunStoreResult("conflict", current)
        try:
            result = await self.collection.replace_one(
                {"run_id": run.run_id, "state_version": expected_state_version},
                candidate.model_dump(mode="python"),
            )
        except DuplicateKeyError:
            return MongoRunStoreResult("conflict", await self.load(run.run_id))
        except RecoverableAdapterError:
            winner = await self.load(run.run_id)
            if winner is not None and command_id in winner.processed_command_ids:
                return MongoRunStoreResult("replayed", winner)
            raise
        if int(getattr(result, "modified_count", 0)) != 1:
            winner = await self.load(run.run_id)
            replay = winner is not None and command_id in winner.processed_command_ids
            return MongoRunStoreResult("replayed" if replay else "conflict", winner)
        claim = await self._load_recovery_claim(run.run_id)
        if (
            claim is not None
            and self._recovery_collection is not None
            and candidate.recovery_claim.kind == "execution"
            and claim.kind == "execution"
            and candidate.recovery_claim.owner_id == claim.owner_id
        ):
            # Renewal may race a slow execution CAS. Overlay only metadata for
            # the same fencing token; never replace a candidate's new owner
            # with an older dedicated claim.
            await self._mirror_recovery_claim(run.run_id, claim)
            candidate = candidate.model_copy(update={"recovery_claim": claim})
        return MongoRunStoreResult("accepted", candidate)

    async def request_cancellation(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        command_id: str,
        cause: CancellationCause,
        requested_at: datetime,
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        if run is None:
            return MongoRunStoreResult("error", None)
        if command_id in run.processed_command_ids:
            await self._replace_cancellation_recovery(run_id, requested_at=requested_at)
            return MongoRunStoreResult("replayed", run)
        if run.state_version != expected_state_version or run.status not in {
            "queued",
            "running",
            "waiting_external",
            "awaiting_user",
        }:
            return MongoRunStoreResult("conflict", run)
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
        stored = await self.cas_mutate(
            candidate,
            expected_state_version=run.state_version,
            command_id=command_id,
        )
        if stored.outcome in {"accepted", "replayed"}:
            await self._replace_cancellation_recovery(run_id, requested_at=requested_at)
            latest = await self.load(run_id)
            if latest is not None:
                return MongoRunStoreResult(stored.outcome, latest)
        return stored

    async def _replace_cancellation_recovery(
        self, run_id: str, *, requested_at: datetime
    ) -> None:
        claim = RecoveryClaim(kind="cancellation", next_attempt_at=requested_at)
        if self._recovery_collection is None:
            self._recovery_claims[run_id] = claim
        else:
            await self._recovery_collection.replace_one(
                {"run_id": run_id},
                {"run_id": run_id, **claim.model_dump(mode="python")},
                upsert=True,
            )
        await self._mirror_recovery_claim(run_id, claim)

    async def repair_canceling_recovery(self, *, limit: int) -> int:
        if limit <= 0:
            return 0
        pipeline: list[dict[str, object]] = [
            {
                "$match": {
                    "schema_version": {"$in": [5, 6]},
                    "status": "canceling",
                }
            },
            {"$sort": {"updated_at": 1, "run_id": 1}},
        ]
        if isinstance(self._recovery_collection_name, str):
            pipeline.extend(
                [
                    {
                        "$lookup": {
                            "from": self._recovery_collection_name,
                            "localField": "run_id",
                            "foreignField": "run_id",
                            "as": "scheduling_rows",
                        }
                    },
                    {"$match": {"scheduling_rows.kind": {"$ne": "cancellation"}}},
                    {"$limit": limit},
                    {"$project": {"scheduling_rows": 0}},
                ]
            )
        documents = await _to_list(self.collection.aggregate(pipeline))
        repaired = 0
        for value in documents:
            run_id = value.get("run_id")
            if not isinstance(run_id, str):
                continue
            claim = await self._load_recovery_claim(run_id)
            if claim is not None and claim.kind == "cancellation":
                continue
            requested_at = value.get("cancellation_requested_at")
            if not isinstance(requested_at, datetime):
                requested_at = datetime.now(UTC)
            await self._replace_cancellation_recovery(run_id, requested_at=requested_at)
            repaired += 1
            if repaired >= limit:
                break
        return repaired

    async def claim_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or lease_expires_at <= claimed_at
            or not _recovery_claim_is_due(run.recovery_claim, due_at=claimed_at)
        ):
            return MongoRunStoreResult("conflict", run)
        claim = run.recovery_claim.model_copy(
            update={
                "owner_id": owner_id,
                "lease_expires_at": lease_expires_at,
                "next_attempt_at": None,
            }
        )
        if self._recovery_collection is None:
            candidate = run.model_copy(
                update={
                    "recovery_claim": claim,
                    "state_version": run.state_version + 1,
                }
            )
            claimed = await self.cas_mutate(
                candidate,
                expected_state_version=run.state_version,
                command_id=f"claim:{owner_id}:{run.state_version}",
            )
            if claimed.outcome in {"accepted", "replayed"}:
                self._recovery_claims[run_id] = claim
            return claimed

        acquired = await self._claim_dedicated_recovery_lease(
            run_id,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
            claimed_at=claimed_at,
        )
        if acquired is None:
            return MongoRunStoreResult("conflict", await self.load(run_id))
        candidate = run.model_copy(
            update={
                "recovery_claim": acquired,
                "state_version": run.state_version + 1,
            }
        )
        claimed = await self.cas_mutate(
            candidate,
            expected_state_version=run.state_version,
            command_id=f"claim:{owner_id}:{run.state_version}",
        )
        if claimed.outcome not in {"accepted", "replayed"}:
            released = run.recovery_claim.model_copy(
                update={"owner_id": None, "lease_expires_at": None}
            )
            if await self._update_dedicated_recovery_lease(
                run_id, owner_id=owner_id, claim=released
            ):
                await self._mirror_recovery_claim(run_id, released)
        return claimed

    async def renew_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or run.recovery_claim.owner_id != owner_id
        ):
            return MongoRunStoreResult("conflict", run)
        # Heartbeats live in their own durable document and deliberately do
        # not advance the execution aggregate's CAS version. This prevents a
        # lease renewal from invalidating a slow Kernel mutation.
        claim = run.recovery_claim.model_copy(
            update={"lease_expires_at": lease_expires_at}
        )
        if self._recovery_collection is None:
            self._recovery_claims[run_id] = claim
        elif not await self._update_dedicated_recovery_lease(
            run_id, owner_id=owner_id, claim=claim
        ):
            return MongoRunStoreResult("conflict", await self.load(run_id))
        await self._mirror_recovery_claim(run_id, claim)
        return MongoRunStoreResult(
            "accepted", run.model_copy(update={"recovery_claim": claim})
        )

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
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        if (
            run is None
            or run.state_version != expected_state_version
            or run.recovery_claim.owner_id != owner_id
        ):
            return MongoRunStoreResult("conflict", run)
        claim = RecoveryClaim(
            kind=run.recovery_claim.kind,
            next_attempt_at=next_attempt_at,
            failure_count=failure_count,
            quarantined_at=quarantined_at,
            quarantine_reason=quarantine_reason,
        )
        if self._recovery_collection is None:
            self._recovery_claims[run_id] = claim
        elif not await self._update_dedicated_recovery_lease(
            run_id, owner_id=owner_id, claim=claim
        ):
            return MongoRunStoreResult("conflict", await self.load(run_id))
        candidate = run.model_copy(
            update={
                "recovery_claim": claim,
                "state_version": run.state_version + 1,
            }
        )
        released = await self.cas_mutate(
            candidate,
            expected_state_version=run.state_version,
            command_id=f"release:{owner_id}:{run.state_version}",
        )
        if released.outcome not in {"accepted", "replayed"}:
            await self._mirror_recovery_claim(run_id, claim)
            latest = await self.load(run_id)
            if latest is not None and latest.recovery_claim == claim:
                # The dedicated lease is authoritative for recovery fencing.
                # Aggregate mirroring may lose a concurrent execution CAS, but
                # a durably released/quarantined token still converges.
                return MongoRunStoreResult("replayed", latest)
        return released

    async def list_due_runs(
        self, *, due_at: datetime, limit: int
    ) -> list[OrchestratorRunState]:
        """Return the authoritative union of never-leased and leased due Runs.

        The aggregate recovery claim is only a query mirror once a dedicated
        lease row exists. Union the indexed dedicated-due scan with aggregate-
        due Runs that have never acquired a dedicated row, then apply the
        caller limit. A stale or quarantined mirror can therefore neither
        bypass backoff nor consume the limit ahead of valid work.
        """
        if limit <= 0:
            return []
        dedicated_due_ids: set[str] = set()
        if self._recovery_collection is not None:
            dedicated_due_query = {
                "$and": [
                    {
                        "$or": [
                            {"quarantined_at": None},
                            {"quarantined_at": {"$exists": False}},
                        ]
                    },
                    {
                        "$or": [
                            {"next_attempt_at": None},
                            {"next_attempt_at": {"$exists": False}},
                            {"next_attempt_at": {"$lte": due_at}},
                        ]
                    },
                    {
                        "$or": [
                            {"lease_expires_at": None},
                            {"lease_expires_at": {"$exists": False}},
                            {"lease_expires_at": {"$lte": due_at}},
                        ]
                    },
                ]
            }
            dedicated_due_documents = await _to_list(
                self._recovery_collection.aggregate(
                    [
                        {"$match": dedicated_due_query},
                        {"$sort": {"next_attempt_at": 1, "run_id": 1}},
                    ]
                )
            )
            dedicated_due_ids = {
                value["run_id"]
                for value in dedicated_due_documents
                if isinstance(value.get("run_id"), str)
            }
        never_leased_query: dict[str, object] = {
            "schema_version": {"$in": [5, 6]},
            "status": {"$in": list(RECOVERY_ELIGIBLE_RUN_STATUSES)},
            "$and": [
                {
                    "$or": [
                        {"recovery_claim.quarantined_at": None},
                        {"recovery_claim.quarantined_at": {"$exists": False}},
                    ]
                },
                {
                    "$or": [
                        {"recovery_claim.next_attempt_at": None},
                        {"recovery_claim.next_attempt_at": {"$exists": False}},
                        {"recovery_claim.next_attempt_at": {"$lte": due_at}},
                    ]
                },
                {
                    "$or": [
                        {"recovery_claim.lease_expires_at": None},
                        {"recovery_claim.lease_expires_at": {"$exists": False}},
                        {"recovery_claim.lease_expires_at": {"$lte": due_at}},
                    ]
                },
            ],
        }
        never_leased_candidates = [
            _run_from_document(value)
            for value in await _to_list(
                self.collection.aggregate(
                    [
                        {"$match": never_leased_query},
                        {"$sort": {"updated_at": 1, "run_id": 1}},
                    ]
                )
            )
        ]
        due_by_id: dict[str, OrchestratorRunState] = {}
        for run in never_leased_candidates:
            # A dedicated row created between scans owns scheduling. Skipping it
            # here is safe; if due it is selected by this or the next dedicated
            # scan, while a new backoff can never be bypassed.
            if await self._load_recovery_claim(run.run_id) is not None:
                continue
            if _recovery_claim_is_due(run.recovery_claim, due_at=due_at):
                due_by_id[run.run_id] = run

        # Fetch dedicated-due aggregates independently; stale aggregate mirrors
        # are deliberately irrelevant to this branch.
        for run_id in sorted(dedicated_due_ids):
            value = await self.collection.find_one(
                {"run_id": run_id, "schema_version": {"$in": [5, 6]}}
            )
            if value is None:
                continue
            run = _run_from_document(value)
            dedicated = await self._load_recovery_claim(run_id)
            if (
                run.status in RECOVERY_ELIGIBLE_RUN_STATUSES
                and dedicated is not None
                and _recovery_claim_is_due(dedicated, due_at=due_at)
            ):
                due_by_id[run_id] = run.model_copy(update={"recovery_claim": dedicated})
        due = list(due_by_id.values())
        due.sort(
            key=lambda run: (
                run.recovery_claim.next_attempt_at or run.updated_at,
                run.run_id,
            )
        )
        return due[:limit]

    async def claim_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> MongoRunStoreResult:
        return await self._intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            to_status="claimed",
            command_id=f"claim-intent:{intent_id}:{expected_state_version}",
            claim_owner=owner_id,
            claim_expires_at=lease_expires_at,
        )

    async def complete_projection_intent(
        self, run_id: str, intent_id: str, *, expected_state_version: int, owner_id: str
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        item = _find_intent(run, intent_id)
        if item is None or item.claim_owner != owner_id:
            return MongoRunStoreResult("conflict", run)
        return await self._intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            to_status="completed",
            command_id=f"complete-intent:{intent_id}:{expected_state_version}",
        )

    async def block_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        reason: str,
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        item = _find_intent(run, intent_id)
        if item is None or item.claim_owner not in {None, owner_id}:
            return MongoRunStoreResult("conflict", run)
        return await self._intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            to_status="blocked",
            command_id=f"block-intent:{intent_id}:{expected_state_version}",
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
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        item = _find_intent(run, intent_id)
        if item is None or item.status != "claimed":
            return MongoRunStoreResult("conflict", run)
        lease_expired = (
            item.claim_expires_at is not None and item.claim_expires_at <= now
        )
        if item.claim_owner != owner_id and not lease_expired:
            return MongoRunStoreResult("conflict", run)
        return await self._intent(
            run_id,
            intent_id,
            expected_state_version=expected_state_version,
            to_status="pending",
            command_id=f"release-intent:{intent_id}:{expected_state_version}",
            next_attempt_at=next_attempt_at,
        )

    async def list_due_projection_intents(
        self, *, due_at: datetime, limit: int
    ) -> list[tuple[str, ProjectionIntent]]:
        if limit <= 0:
            return []
        # Dotted paths are relative to the Run document (post-$unwind match).
        due = {
            "$or": [
                {
                    "projection_outbox.status": "pending",
                    "$or": [
                        {"projection_outbox.next_attempt_at": None},
                        {"projection_outbox.next_attempt_at": {"$lte": due_at}},
                    ],
                },
                {
                    "projection_outbox.status": "claimed",
                    "projection_outbox.claim_expires_at": {"$lte": due_at},
                },
            ]
        }
        # Inside $elemMatch the paths are relative to the array ELEMENT, so the
        # pre-filter must drop the "projection_outbox." prefix.
        pre_filter = {
            "$or": [
                {
                    "status": "pending",
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": due_at}},
                    ],
                },
                {
                    "status": "claimed",
                    "claim_expires_at": {"$lte": due_at},
                },
            ]
        }
        pipeline: list[dict[str, object]] = [
            {"$match": {"projection_outbox": {"$elemMatch": pre_filter}}},
            {"$unwind": "$projection_outbox"},
            {"$match": due},
            {
                "$addFields": {
                    "_projection_due_at": {
                        "$ifNull": [
                            "$projection_outbox.next_attempt_at",
                            "$projection_outbox.claim_expires_at",
                            "$updated_at",
                        ]
                    }
                }
            },
            {"$sort": {"_projection_due_at": 1, "run_id": 1}},
            {"$limit": limit},
            {
                "$project": {
                    "_id": 0,
                    "run_id": 1,
                    "projection_outbox": 1,
                }
            },
        ]
        results: list[tuple[str, ProjectionIntent]] = []
        for value in await _to_list(self.collection.aggregate(pipeline), length=limit):
            run_id = value.get("run_id")
            intent_value = value.get("projection_outbox")
            if not isinstance(run_id, str) or not isinstance(intent_value, dict):
                continue
            results.append(
                (
                    run_id,
                    ProjectionIntent.model_validate(_without_mongo_id(intent_value)),
                )
            )
        return results

    async def _intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        to_status: str,
        command_id: str,
        **kwargs: object,
    ) -> MongoRunStoreResult:
        run = await self.load(run_id)
        if run is None or run.state_version != expected_state_version:
            return MongoRunStoreResult("conflict", run)
        index = next(
            (
                index
                for index, item in enumerate(run.projection_outbox)
                if item.intent_id == intent_id
            ),
            None,
        )
        if index is None:
            return MongoRunStoreResult("conflict", run)
        intents = list(run.projection_outbox)
        intents[index] = transition_projection_intent(
            intents[index], to_status=to_status, **kwargs
        )  # type: ignore[arg-type]
        candidate = run.model_copy(
            update={
                "projection_outbox": intents,
                "state_version": run.state_version + 1,
            }
        )
        return await self.cas_mutate(
            candidate, expected_state_version=run.state_version, command_id=command_id
        )


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


def _normalize_run_for_mongo(run: OrchestratorRunState) -> OrchestratorRunState:
    return OrchestratorRunState.model_validate(
        _truncate_datetimes_to_bson_precision(run.model_dump(mode="python"))
    )


def _truncate_datetimes_to_bson_precision(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)
    if isinstance(value, dict):
        return {
            key: _truncate_datetimes_to_bson_precision(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_truncate_datetimes_to_bson_precision(item) for item in value]
    return value


def _run_from_document(value: dict[str, Any]) -> OrchestratorRunState:
    return OrchestratorRunState.model_validate(
        _restore_utc_datetimes(_without_mongo_id(value))
    )


def _recovery_claim_is_due(claim: RecoveryClaim, *, due_at: datetime) -> bool:
    return (
        claim.quarantined_at is None
        and (claim.next_attempt_at is None or claim.next_attempt_at <= due_at)
        and (claim.lease_expires_at is None or claim.lease_expires_at <= due_at)
    )


def _restore_utc_datetimes(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: _restore_utc_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_utc_datetimes(item) for item in value]
    return value


def _find_intent(run: OrchestratorRunState | None, intent_id: str):
    if run is None:
        return None
    return next(
        (item for item in run.projection_outbox if item.intent_id == intent_id), None
    )
