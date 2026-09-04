from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pymongo.errors import AutoReconnect, DuplicateKeyError

from common.dto.hitl import (
    A2AInteractionSpec,
    HITLInteractionKind,
    HITLQuestionAnswer,
    HITLQuestionSpec,
    HITLRouteSnapshotV2,
    HITLTextAnswer,
)
from dal.orchestrator.hitl import MongoHITLApplicationStore
from dal.orchestrator.run_store import MongoOrchestratorRunStore
from dal.orchestrator.stores import (
    MongoAgentCallLedgerStore,
    MongoObservationConflictStore,
    MongoObservationInboxStore,
    MongoRoomEpochStore,
)
from execution.adapters.hitl import InMemoryHITLApplicationStore
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryRoomEpochStore,
)
from execution.orchestrator.a2a_runtime.models import (
    A2AObservationConflictRecord,
    A2AObservationInboxRecord,
    A2ARuntimePolicy,
    DurableHITLAnswerRecord,
    NormalizedA2AObservation,
)
from execution.orchestrator.in_memory import InMemoryOrchestratorRunStore
from execution.orchestrator.models import ProjectionIntent, RecoveryClaim

from ._orchestrator_a2a_helpers import ledger_record
from ._orchestrator_helpers import NOW, make_run


class Cursor:
    def __init__(self, values):
        self.values = values

    async def to_list(self, length=None):
        return deepcopy(self.values if length is None else self.values[:length])


class FakeCollection:
    def __init__(self):
        self.values = []

    async def find_one(self, query):
        return next(
            (deepcopy(item) for item in self.values if _matches(item, query)), None
        )

    async def insert_one(self, document):
        self.values.append(deepcopy(document))
        return SimpleNamespace(inserted_id=len(self.values))

    async def replace_one(self, query, document, *, upsert=False):
        for index, item in enumerate(self.values):
            if _matches(item, query):
                modified = item != document
                self.values[index] = deepcopy(document)
                return SimpleNamespace(
                    modified_count=int(modified), matched_count=1, upserted_id=None
                )
        if upsert:
            self.values.append(deepcopy(document))
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=1)
        return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)

    async def update_one(self, query, update, *, upsert=False):
        del upsert  # HITL answers use plain filters only; no upsert path needed.
        matched = 0
        modified = 0
        for item in self.values:
            if not _matches(item, query):
                continue
            matched += 1
            before = deepcopy(item)
            if "$set" in update:
                for dotted, value in update["$set"].items():
                    _set(item, dotted, value)
            if item != before:
                modified += 1
        return SimpleNamespace(
            matched_count=matched, modified_count=modified, upserted_id=None
        )

    async def delete_many(self, query):
        before = len(self.values)
        self.values = [item for item in self.values if not _matches(item, query)]
        return SimpleNamespace(deleted_count=before - len(self.values))

    def find(self, query):
        return Cursor([deepcopy(item) for item in self.values if _matches(item, query)])

    def aggregate(self, pipeline):  # noqa: C901
        values = deepcopy(self.values)
        for stage in pipeline:
            if "$match" in stage:
                values = [item for item in values if _matches(item, stage["$match"])]
            elif "$addFields" in stage:
                for field, expression in stage["$addFields"].items():
                    candidates = expression["$ifNull"]
                    for item in values:
                        item[field] = next(
                            (
                                _get(item, candidate.removeprefix("$"))
                                for candidate in candidates
                                if _get(item, candidate.removeprefix("$")) is not None
                            ),
                            None,
                        )
            elif "$unwind" in stage:
                field = stage["$unwind"].removeprefix("$")
                unwound = []
                for item in values:
                    entries = _get(item, field) or []
                    for entry in entries:
                        copied = deepcopy(item)
                        copied[field] = entry
                        unwound.append(copied)
                values = unwound
            elif "$sort" in stage:
                fields = list(stage["$sort"])
                values.sort(
                    key=lambda item: tuple(
                        # Mongo sorts null/missing before any value.
                        datetime.min.replace(tzinfo=UTC)
                        if _get(item, field) is None
                        else _get(item, field)
                        for field in fields
                    )
                )
            elif "$limit" in stage:
                values = values[: stage["$limit"]]
            elif "$project" in stage:
                projection = stage["$project"]
                # Mirror Mongo's rule: only `_id` may be excluded alongside
                # inclusions; a mixed projection is rejected by the server.
                excluded = [
                    field for field, included in projection.items() if not included
                ]
                non_id_exclusions = [field for field in excluded if field != "_id"]
                inclusions = [
                    field for field, included in projection.items() if included
                ]
                if non_id_exclusions and inclusions:
                    raise AssertionError(
                        "mixed projection is invalid in Mongo aggregation: "
                        f"{projection!r}"
                    )
                for item in values:
                    for field in excluded:
                        item.pop(field, None)
                    if inclusions:
                        for key in list(item):
                            if key not in inclusions:
                                item.pop(key, None)
        return Cursor(values)


class DuplicateAfterWriteCollection(FakeCollection):
    def __init__(self, *, fail_insert=False, fail_upsert=False):
        super().__init__()
        self.fail_insert = fail_insert
        self.fail_upsert = fail_upsert

    async def insert_one(self, document):
        result = await super().insert_one(document)
        if self.fail_insert:
            self.fail_insert = False
            raise DuplicateKeyError("duplicate key after concurrent winner")
        return result

    async def replace_one(self, query, document, *, upsert=False):
        result = await super().replace_one(query, document, upsert=upsert)
        if upsert and self.fail_upsert:
            self.fail_upsert = False
            raise DuplicateKeyError("duplicate key after concurrent upsert winner")
        return result


class ConcurrentClientRequestWinnerCollection(FakeCollection):
    async def insert_one(self, document):
        winner = deepcopy(document)
        winner["run_id"] = "run-concurrent-winner"
        winner["processed_command_ids"] = ["create:concurrent-winner"]
        self.values.append(winner)
        raise DuplicateKeyError("client request winner used another run ID")


class ConcurrentAnswerCollection(FakeCollection):
    """Release two ``update_one`` callers together to simulate a CAS race."""

    def __init__(self):
        super().__init__()
        self._waiters = 0
        self._both_waiting = asyncio.Event()
        self._release = asyncio.Event()

    async def update_one(self, query, update, *, upsert=False):
        self._waiters += 1
        if self._waiters == 2:
            self._both_waiting.set()
        await self._release.wait()
        return await super().update_one(query, update, upsert=upsert)


class MongoPrecisionCollection(FakeCollection):
    def __init__(self):
        super().__init__()
        self.lose_replace_ack = False
        self.advance_after_replace_ack_loss = False

    async def insert_one(self, document):
        return await super().insert_one(_bson_roundtrip(document))

    async def replace_one(self, query, document, *, upsert=False):
        result = await super().replace_one(
            query, _bson_roundtrip(document), upsert=upsert
        )
        if self.advance_after_replace_ack_loss:
            self.advance_after_replace_ack_loss = False
            winner = next(
                item for item in self.values if item["run_id"] == document["run_id"]
            )
            winner["state_version"] += 1
            winner["processed_command_ids"].append("later:command")
            raise AutoReconnect("replace acknowledgement lost before later mutation")
        if self.lose_replace_ack:
            self.lose_replace_ack = False
            raise AutoReconnect("replace acknowledgement lost")
        return result


def _matches(document, query):  # noqa: C901
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(document, item) for item in expected):
                return False
            continue
        actual = _get(document, key)
        if isinstance(expected, dict):
            if "$elemMatch" in expected:
                if not isinstance(actual, list) or not any(
                    _matches(item, expected["$elemMatch"]) for item in actual
                ):
                    return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$in" in expected:
                actual_values = actual if isinstance(actual, list) else [actual]
                if not set(actual_values).intersection(expected["$in"]):
                    return False
            if "$exists" in expected and (actual is not None) != expected["$exists"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$lte" in expected and actual is not None:
                boundary = expected["$lte"]
                # Mirror Mongo's BSON type order: a string date is never
                # less-than-or-equal to a Date boundary.
                if isinstance(actual, str) and isinstance(boundary, datetime):
                    return False
                if isinstance(actual, datetime) and isinstance(boundary, datetime):
                    actual = actual.replace(tzinfo=actual.tzinfo or UTC)
                    boundary = boundary.replace(tzinfo=boundary.tzinfo or UTC)
                if actual > boundary:
                    return False
            continue
        if actual != expected:
            return False
    return True


def _get(document, dotted):
    value = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _set(document, dotted, value):
    parts = dotted.split(".")
    current = document
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _bson_roundtrip(value):
    if isinstance(value, datetime):
        return value.replace(
            microsecond=(value.microsecond // 1000) * 1000,
            tzinfo=None,
        )
    if isinstance(value, dict):
        return {key: _bson_roundtrip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bson_roundtrip(item) for item in value]
    return deepcopy(value)


def _inbox_record():
    observation = NormalizedA2AObservation(
        observation_id="observation-1",
        source_kind="webhook",
        source_identity="source-1",
        binding_scope="endpoint",
        event_kind="working",
        observed_at=NOW,
    )
    return A2AObservationInboxRecord(
        observation_id=observation.observation_id,
        source_kind=observation.source_kind,
        source_identity=observation.source_identity,
        payload_digest="digest",
        received_at=NOW,
        binding_scope=observation.binding_scope,
        room_id="room-1",
        room_epoch=1,
        event_kind=observation.event_kind,
        observation=observation,
    )


class AsyncListCollection:
    """Fake collection shaped like the production ``MongoCollectionAdapter``.

    ``find``/``aggregate`` are async and return materialized lists (no
    ``to_list``); ``replace_one`` returns a bool and ``delete_many`` an int.
    The orchestrator DAL must tolerate this shape for reads (``_to_list``),
    which is why the production composition passes raw Motor collections for
    write paths whose results carry ``modified_count``/``deleted_count``.
    """

    def __init__(self, values=None):
        self.values = deepcopy(values or [])

    async def find_one(self, query):
        return next(
            (deepcopy(item) for item in self.values if _matches(item, query)), None
        )

    async def find(self, query):
        return [deepcopy(item) for item in self.values if _matches(item, query)]

    async def aggregate(self, pipeline):
        del pipeline  # Shape-only fake: the caller must not depend on filtering.
        return deepcopy(self.values)


async def test_mongo_parity_accepts_adapter_shaped_async_list_find():
    record = ledger_record()
    collection = AsyncListCollection([record.model_dump(mode="python")])
    ledger = MongoAgentCallLedgerStore(collection)

    due = await ledger.list_due(due_at=NOW, limit=5)

    assert [item.call_record_id for item in due] == [record.call_record_id]


async def test_mongo_parity_accepts_adapter_shaped_async_list_aggregate():
    intent = ProjectionIntent(
        intent_id="intent-1",
        kind="deliver_final_message",
        target="room-1",
        dedupe_key="dedupe-1",
        required=True,
        event_id="event-1",
        event_sequence=1,
        causation_id="cause-1",
        payload={},
        status="pending",
    )
    collection = AsyncListCollection(
        [
            {
                "run_id": "run-1",
                "projection_outbox": intent.model_dump(mode="python"),
            }
        ]
    )
    store = MongoOrchestratorRunStore(collection)

    due = await store.list_due_projection_intents(due_at=NOW, limit=5)

    assert [(run_id, item.intent_id) for run_id, item in due] == [("run-1", "intent-1")]


async def test_mongo_ledger_string_dates_never_match_due_queries():
    """Mongo compares BSON types: a string date is never <= a Date boundary.

    Persisting records with ``mode="json"`` turns datetimes into ISO strings,
    which silently excludes working calls from every recovery ``list_due``
    scan and stalls them forever. The fake mirrors Mongo's type order so a
    regression here fails the parity suite.
    """
    record = ledger_record(state="working").model_copy(update={"next_attempt_at": NOW})
    collection = AsyncListCollection([record.model_dump(mode="json")])
    ledger = MongoAgentCallLedgerStore(collection)

    due = await ledger.list_due(due_at=NOW, limit=5)

    assert due == []


async def test_mongo_ledger_python_mode_dates_match_due_queries():
    record = ledger_record(state="working")
    collection = AsyncListCollection([record.model_dump(mode="python")])
    ledger = MongoAgentCallLedgerStore(collection)

    due = await ledger.list_due(due_at=NOW, limit=5)

    assert [item.call_record_id for item in due] == [record.call_record_id]
    assert isinstance(due[0].accepted_at, datetime)


async def test_mongo_run_create_exact_retry_replays_persisted_candidate():
    store = MongoOrchestratorRunStore(FakeCollection())
    run = make_run()

    accepted = await store.create(run, command_id="create:run-1")
    replayed = await store.create(run, command_id="create:run-1")

    assert accepted.outcome == "accepted"
    assert replayed.outcome == "replayed"
    assert replayed.run == accepted.run
    assert replayed.run.processed_command_ids == ["create:run-1"]


async def test_mongo_recovery_lease_takeover_fences_stale_owner():
    runs = FakeCollection()
    leases = FakeCollection()
    store = MongoOrchestratorRunStore(runs, leases)
    created = await store.create(make_run(), command_id="create:lease-takeover")
    assert created.run is not None

    claim_a_at = created.run.recovery_claim.next_attempt_at
    assert claim_a_at is not None
    owner_a = "instance-a:token-a"
    claimed_a = await store.claim_recovery(
        created.run.run_id,
        expected_state_version=created.run.state_version,
        owner_id=owner_a,
        lease_expires_at=claim_a_at + timedelta(seconds=1),
        claimed_at=claim_a_at,
    )
    assert claimed_a.outcome == "accepted"
    assert claimed_a.run is not None

    owner_b = "instance-b:token-b"
    claimed_b = await store.claim_recovery(
        created.run.run_id,
        expected_state_version=claimed_a.run.state_version,
        owner_id=owner_b,
        lease_expires_at=claim_a_at + timedelta(minutes=1),
        claimed_at=claim_a_at + timedelta(seconds=2),
    )
    assert claimed_b.outcome == "accepted"
    assert claimed_b.run is not None
    assert claimed_b.run.recovery_claim.owner_id == owner_b
    assert len(leases.values) == 1
    assert leases.values[0]["owner_id"] == owner_b

    stale_renewal = await store.renew_recovery(
        created.run.run_id,
        expected_state_version=claimed_b.run.state_version,
        owner_id=owner_a,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    stale_release = await store.release_recovery(
        created.run.run_id,
        expected_state_version=claimed_b.run.state_version,
        owner_id=owner_a,
        next_attempt_at=None,
    )

    assert stale_renewal.outcome == "conflict"
    assert stale_release.outcome == "conflict"
    latest = await store.load(created.run.run_id)
    assert latest is not None
    assert latest.recovery_claim.owner_id == owner_b
    assert leases.values[0]["owner_id"] == owner_b


async def test_mongo_recovery_lease_release_is_token_fenced():
    runs = FakeCollection()
    leases = FakeCollection()
    store = MongoOrchestratorRunStore(runs, leases)
    created = await store.create(make_run(), command_id="create:lease-release")
    assert created.run is not None
    owner = "instance:token"
    claimed_at = created.run.recovery_claim.next_attempt_at
    assert claimed_at is not None
    claimed = await store.claim_recovery(
        created.run.run_id,
        expected_state_version=created.run.state_version,
        owner_id=owner,
        lease_expires_at=claimed_at + timedelta(minutes=1),
        claimed_at=claimed_at,
    )
    assert claimed.run is not None

    released = await store.release_recovery(
        created.run.run_id,
        expected_state_version=claimed.run.state_version,
        owner_id=owner,
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=5),
    )

    assert released.outcome == "accepted"
    assert released.run is not None
    assert released.run.recovery_claim.owner_id is None
    assert leases.values[0]["owner_id"] is None


async def test_mongo_recovery_dedicated_backoff_is_authoritative_and_starvation_free():
    runs = FakeCollection()
    leases = FakeCollection()
    store = MongoOrchestratorRunStore(runs, leases)

    def due_run(run_id):
        run = make_run().model_copy(
            update={
                "run_id": run_id,
                "room_id": f"room-{run_id}",
                "session_id": f"room-{run_id}",
                "status": "running",
                "updated_at": NOW - timedelta(minutes=10),
                "recovery_claim": RecoveryClaim(
                    next_attempt_at=NOW - timedelta(minutes=1)
                ),
                "request": make_run().request.model_copy(
                    update={
                        "request_fingerprint": f"fingerprint-{run_id}",
                        "user_message_id": f"message-{run_id}",
                    }
                ),
            }
        )
        return run

    blocked = due_run("run-a-blocked")
    quarantined = due_run("run-b-quarantined")
    valid = due_run("run-z-valid")
    for run in (blocked, quarantined, valid):
        assert (await store.create(run, command_id=f"create:{run.run_id}")).run

    await leases.insert_one(
        {
            "run_id": blocked.run_id,
            **RecoveryClaim(
                next_attempt_at=NOW + timedelta(hours=1), failure_count=1
            ).model_dump(mode="python"),
        }
    )
    await leases.insert_one(
        {
            "run_id": quarantined.run_id,
            **RecoveryClaim(
                failure_count=3,
                quarantined_at=NOW - timedelta(seconds=1),
                quarantine_reason="terminal_invariant_conflict",
            ).model_dump(mode="python"),
        }
    )

    due = await store.list_due_runs(due_at=NOW, limit=1)
    assert [item.run_id for item in due] == [valid.run_id]

    stale = await store.load(blocked.run_id)
    assert stale is not None
    early = await store.claim_recovery(
        blocked.run_id,
        expected_state_version=stale.state_version,
        owner_id="worker:early",
        lease_expires_at=NOW + timedelta(minutes=1),
        claimed_at=NOW,
    )
    assert early.outcome == "conflict"

    due_at = NOW + timedelta(hours=1)
    eligible = await store.list_due_runs(due_at=due_at, limit=10)
    assert blocked.run_id in {item.run_id for item in eligible}
    current = await store.load(blocked.run_id)
    assert current is not None
    claimed = await store.claim_recovery(
        blocked.run_id,
        expected_state_version=current.state_version,
        owner_id="worker:due",
        lease_expires_at=due_at + timedelta(minutes=1),
        claimed_at=due_at,
    )
    assert claimed.outcome == "accepted"


async def test_mongo_recovery_naive_bson_dates_load_as_utc_and_allow_expired_takeover():
    runs = FakeCollection()
    leases = FakeCollection()
    store = MongoOrchestratorRunStore(runs, leases)
    run = make_run().model_copy(
        update={
            "status": "running",
            "recovery_claim": RecoveryClaim(next_attempt_at=NOW - timedelta(minutes=2)),
        }
    )
    created = await store.create(run, command_id="create:naive-lease")
    assert created.run is not None
    await leases.insert_one(
        {
            "run_id": run.run_id,
            "owner_id": "worker:expired",
            "lease_expires_at": (NOW - timedelta(minutes=1)).replace(tzinfo=None),
            "next_attempt_at": (NOW - timedelta(minutes=2)).replace(tzinfo=None),
            "failure_count": 2,
            "quarantined_at": None,
            "quarantine_reason": None,
        }
    )

    loaded = await store.load(run.run_id)
    assert loaded is not None
    assert loaded.recovery_claim.lease_expires_at.tzinfo is UTC
    assert loaded.recovery_claim.next_attempt_at.tzinfo is UTC
    claimed = await store.claim_recovery(
        run.run_id,
        expected_state_version=loaded.state_version,
        owner_id="worker:takeover",
        lease_expires_at=NOW + timedelta(minutes=1),
        claimed_at=NOW,
    )
    assert claimed.outcome == "accepted"

    leases.values[0].update(
        {
            "owner_id": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
            "quarantined_at": NOW.replace(tzinfo=None),
            "quarantine_reason": "terminal_invariant_conflict",
        }
    )
    quarantined = await store.load(run.run_id)
    assert quarantined is not None
    assert quarantined.recovery_claim.quarantined_at.tzinfo is UTC


async def test_mongo_recovery_quarantine_persists_in_dedicated_lease_and_is_not_due():
    runs = FakeCollection()
    leases = FakeCollection()
    store = MongoOrchestratorRunStore(runs, leases)
    run = make_run().model_copy(update={"status": "running"})
    created = await store.create(run, command_id="create:quarantine")
    assert created.run is not None
    owner = "instance:quarantine"
    claimed = await store.claim_recovery(
        run.run_id,
        expected_state_version=created.run.state_version,
        owner_id=owner,
        lease_expires_at=run.budget.deadline_at + timedelta(minutes=1),
        claimed_at=run.budget.deadline_at,
    )
    assert claimed.run is not None

    quarantined_at = NOW + timedelta(seconds=1)
    released = await store.release_recovery(
        run.run_id,
        expected_state_version=claimed.run.state_version,
        owner_id=owner,
        next_attempt_at=None,
        failure_count=3,
        quarantined_at=quarantined_at,
        quarantine_reason="terminal_invariant_conflict",
    )

    assert released.outcome == "accepted"
    assert released.run is not None
    assert released.run.recovery_claim.failure_count == 3
    assert released.run.recovery_claim.quarantined_at == quarantined_at
    assert leases.values[0]["quarantine_reason"] == "terminal_invariant_conflict"
    assert await store.list_due_runs(due_at=NOW + timedelta(days=3650), limit=10) == []
    reloaded = await store.load(run.run_id)
    assert reloaded is not None
    assert reloaded.recovery_claim == released.run.recovery_claim


async def test_mongo_run_replay_and_ack_loss_use_bson_datetime_precision():
    collection = MongoPrecisionCollection()
    store = MongoOrchestratorRunStore(collection)
    precise = NOW.replace(microsecond=456789)
    run = make_run().model_copy(update={"created_at": precise, "updated_at": precise})

    accepted = await store.create(run, command_id="create:precise")
    replayed = await store.create(run, command_id="create:precise")

    assert accepted.outcome == "accepted"
    assert replayed.outcome == "replayed"
    assert replayed.run == accepted.run
    assert accepted.run.updated_at.microsecond == 456000

    candidate = accepted.run.model_copy(
        update={
            "status": "running",
            "state_version": accepted.run.state_version + 1,
            "updated_at": precise + timedelta(seconds=1),
        }
    )
    collection.lose_replace_ack = True
    reconciled = await store.cas_mutate(
        candidate,
        expected_state_version=accepted.run.state_version,
        command_id="mutate:precise",
    )

    assert reconciled.outcome == "replayed"
    assert reconciled.run.updated_at.microsecond == 456000
    assert reconciled.run.processed_command_ids == [
        "create:precise",
        "mutate:precise",
    ]


async def test_mongo_run_cas_ack_loss_replays_after_later_advance():
    collection = MongoPrecisionCollection()
    store = MongoOrchestratorRunStore(collection)
    created = await store.create(make_run(), command_id="create:run-1")
    candidate = created.run.model_copy(
        update={
            "status": "running",
            "state_version": created.run.state_version + 1,
        }
    )
    collection.advance_after_replace_ack_loss = True

    replayed = await store.cas_mutate(
        candidate,
        expected_state_version=created.run.state_version,
        command_id="mutate:run-1",
    )

    assert replayed.outcome == "replayed"
    assert replayed.run.state_version == candidate.state_version + 1
    assert replayed.run.processed_command_ids == [
        "create:run-1",
        "mutate:run-1",
        "later:command",
    ]


async def test_mongo_run_create_does_not_duplicate_preapplied_command_id():
    store = MongoOrchestratorRunStore(FakeCollection())
    run = make_run().model_copy(update={"processed_command_ids": ["create:run-1"]})

    created = await store.create(run, command_id="create:run-1")

    assert created.outcome == "accepted"
    assert created.run.processed_command_ids == ["create:run-1"]
    assert await store.load(run.run_id) == created.run


async def test_mongo_run_create_retry_replays_after_aggregate_advances():
    store = MongoOrchestratorRunStore(FakeCollection())
    run = make_run()
    created = await store.create(run, command_id="create:run-1")
    advanced = created.run.model_copy(
        update={
            "status": "running",
            "state_version": created.run.state_version + 1,
        }
    )
    assert (
        await store.cas_mutate(
            advanced,
            expected_state_version=created.run.state_version,
            command_id="mutate:run-1",
        )
    ).outcome == "accepted"

    replayed = await store.create(run, command_id="create:run-1")

    assert replayed.outcome == "replayed"
    assert replayed.run.state_version == advanced.state_version
    assert replayed.run.processed_command_ids == ["create:run-1", "mutate:run-1"]


async def test_mongo_run_create_reloads_concurrent_client_request_winner():
    store = MongoOrchestratorRunStore(ConcurrentClientRequestWinnerCollection())
    run = make_run()

    replayed = await store.create(run, command_id="create:attempt")

    assert replayed.outcome == "replayed"
    assert replayed.run.run_id == "run-concurrent-winner"
    assert replayed.run.request.request_fingerprint == run.request.request_fingerprint


async def test_mongo_run_duplicate_request_ignores_mongo_id():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    run = make_run()
    assert (await store.create(run, command_id="create:run-1")).outcome == "accepted"
    collection.values[0]["_id"] = "mongo-generated-id"
    duplicate = run.model_copy(update={"run_id": "run-duplicate"})

    replayed = await store.create(duplicate, command_id="create:run-duplicate")

    assert replayed.outcome == "replayed"
    assert replayed.run.run_id == run.run_id


async def test_mongo_run_cas_does_not_duplicate_preapplied_command_id():
    store = MongoOrchestratorRunStore(FakeCollection())
    created = await store.create(make_run(), command_id="create:run-1")
    terminal = created.run.model_copy(
        update={
            "status": "failed",
            "terminal_reason": "test failure",
            "processed_command_ids": [
                *created.run.processed_command_ids,
                "complete:run-1",
            ],
            "state_version": created.run.state_version + 1,
        }
    )

    committed = await store.cas_mutate(
        terminal,
        expected_state_version=created.run.state_version,
        command_id="complete:run-1",
    )

    assert committed.outcome == "accepted"
    assert committed.run.processed_command_ids == ["create:run-1", "complete:run-1"]
    assert await store.load(terminal.run_id) == committed.run


async def test_mongo_due_run_listing_ignores_mongo_id():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    due_run = make_run().model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    created = await store.create(due_run, command_id="create:run-1")
    collection.values[0]["_id"] = "mongo-generated-id"

    due = await store.list_due_runs(due_at=NOW, limit=10)

    assert due == [created.run]


async def test_mongo_run_load_ignores_other_schema_documents():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    document = make_run().model_dump(mode="python")
    document["schema_version"] = 7
    collection.values.append(document)

    assert await store.load(document["run_id"]) is None


async def test_mongo_due_run_listing_ignores_other_schema_documents():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    due_run = make_run().model_copy(
        update={"recovery_claim": RecoveryClaim(next_attempt_at=NOW)}
    )
    created = await store.create(due_run, command_id="create:run-1")
    collection.values.append(
        {
            "run_id": "foreign-schema-run",
            "schema_version": 7,
            "status": "running",
            "updated_at": NOW - timedelta(days=1),
            "recovery_claim": {
                "owner_id": None,
                "lease_expires_at": None,
                "next_attempt_at": NOW,
                "failure_count": 0,
                "quarantined_at": None,
                "quarantine_reason": None,
            },
        }
    )

    due = await store.list_due_runs(due_at=NOW, limit=10)

    assert due == [created.run]


@pytest.mark.parametrize(
    "store",
    [
        InMemoryOrchestratorRunStore(),
        MongoOrchestratorRunStore(FakeCollection()),
    ],
)
async def test_awaiting_user_run_is_dormant_until_scheduled_deadline(store):
    deadline = NOW + timedelta(minutes=1)
    dormant = make_run().model_copy(
        update={
            "status": "awaiting_user",
            "recovery_claim": RecoveryClaim(next_attempt_at=deadline),
        }
    )
    assert (await store.create(dormant, command_id="create:dormant")).outcome == (
        "accepted"
    )

    assert await store.list_due_runs(due_at=NOW, limit=10) == []
    assert [
        run.run_id for run in await store.list_due_runs(due_at=deadline, limit=10)
    ] == [dormant.run_id]


async def test_mongo_due_run_listing_uses_contract_order_before_limit():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    base = make_run()

    def scheduled(run_id, *, next_attempt_at, updated_at):
        return base.model_copy(
            update={
                "run_id": run_id,
                "session_id": f"session-{run_id}",
                "room_id": f"room-{run_id}",
                "client_request_id": f"request-{run_id}",
                "request": base.request.model_copy(
                    update={"request_fingerprint": f"fingerprint-{run_id}"}
                ),
                "recovery_claim": base.recovery_claim.model_copy(
                    update={"next_attempt_at": next_attempt_at}
                ),
                "updated_at": updated_at,
            }
        )

    runs = [
        scheduled("third", next_attempt_at=NOW - timedelta(seconds=5), updated_at=NOW),
        scheduled(
            "second", next_attempt_at=NOW - timedelta(seconds=10), updated_at=NOW
        ),
        scheduled(
            "first", next_attempt_at=None, updated_at=NOW - timedelta(seconds=20)
        ),
    ]
    for run in runs:
        assert (
            await store.create(run, command_id=f"create:{run.run_id}")
        ).outcome == "accepted"

    due = await store.list_due_runs(due_at=NOW, limit=2)

    assert [run.run_id for run in due] == ["first", "second"]
    assert await store.list_due_runs(due_at=NOW, limit=0) == []


async def test_mongo_run_dates_remain_bson_datetimes_for_due_queries():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    scheduled = make_run().model_copy(
        update={
            "recovery_claim": make_run().recovery_claim.model_copy(
                update={"next_attempt_at": NOW - timedelta(seconds=1)}
            )
        }
    )

    created = await store.create(scheduled, command_id="create:scheduled")

    assert isinstance(
        collection.values[0]["recovery_claim"]["next_attempt_at"],
        type(NOW),
    )
    assert await store.list_due_runs(due_at=NOW, limit=10) == [created.run]


async def test_mongo_run_reads_restore_utc_to_bson_datetimes():
    collection = FakeCollection()
    store = MongoOrchestratorRunStore(collection)
    scheduled = make_run().model_copy(
        update={
            "recovery_claim": make_run().recovery_claim.model_copy(
                update={"next_attempt_at": NOW - timedelta(seconds=1)}
            )
        }
    )
    await store.create(scheduled, command_id="create:scheduled")
    collection.values[0]["created_at"] = scheduled.created_at.replace(tzinfo=None)
    collection.values[0]["updated_at"] = scheduled.updated_at.replace(tzinfo=None)
    collection.values[0]["recovery_claim"]["next_attempt_at"] = (
        scheduled.recovery_claim.next_attempt_at.replace(tzinfo=None)
    )

    loaded = await store.load(scheduled.run_id)
    due = await store.list_due_runs(due_at=NOW, limit=10)

    assert loaded.created_at.tzinfo is UTC
    assert loaded.updated_at.tzinfo is UTC
    assert loaded.recovery_claim.next_attempt_at.tzinfo is UTC
    assert due[0].recovery_claim.next_attempt_at.tzinfo is UTC


async def test_mongo_and_memory_call_lease_contracts_match():
    stores = [
        InMemoryAgentCallLedgerStore(),
        MongoAgentCallLedgerStore(FakeCollection()),
    ]
    for store in stores:
        record = ledger_record()
        assert await store.insert(record) == "accepted"
        assert (
            await store.claim(
                record.call_record_id,
                expected_state_version=0,
                owner_id="owner",
                lease_expires_at=NOW,
                claimed_at=NOW,
            )
            is None
        )
        claimed = await store.claim(
            record.call_record_id,
            expected_state_version=0,
            owner_id="owner",
            lease_expires_at=NOW + timedelta(seconds=10),
            claimed_at=NOW,
        )
        assert claimed is not None
        assert (
            await store.renew(
                record.call_record_id,
                expected_state_version=claimed.state_version,
                owner_id="owner",
                lease_expires_at=NOW + timedelta(seconds=5),
                renewed_at=NOW + timedelta(seconds=1),
            )
            is None
        )
        renewed = await store.renew(
            record.call_record_id,
            expected_state_version=claimed.state_version,
            owner_id="owner",
            lease_expires_at=NOW + timedelta(seconds=20),
            renewed_at=NOW + timedelta(seconds=1),
        )
        assert renewed is not None
        released = await store.release(
            record.call_record_id,
            expected_state_version=renewed.state_version,
            owner_id="owner",
            next_attempt_at=NOW + timedelta(seconds=30),
            released_at=NOW + timedelta(seconds=2),
        )
        assert released is not None


@pytest.mark.parametrize(
    "store",
    [
        InMemoryObservationInboxStore(),
        MongoObservationInboxStore(FakeCollection()),
    ],
)
async def test_due_observation_lookup_uses_denormalized_call_identity(store):
    record = _inbox_record().model_copy(update={"call_record_id": "call-1"})
    assert record.observation.call_record_id is None
    assert await store.insert(record) == "accepted"

    due = await store.list_due_for_call("call-1", due_at=NOW, limit=10)

    assert due == [record]


async def test_mongo_and_memory_inbox_claim_takeover_and_stale_fence_match():
    stores = [
        InMemoryObservationInboxStore(),
        MongoObservationInboxStore(FakeCollection()),
    ]
    for store in stores:
        record = _inbox_record()
        assert await store.insert(record) == "accepted"
        first = await store.claim(
            record.observation_id,
            expected_state_version=0,
            owner_id="owner-a",
            claim_token="token-a",
            lease_expires_at=NOW + timedelta(seconds=1),
            claimed_at=NOW,
        )
        second = await store.claim(
            record.observation_id,
            expected_state_version=first.state_version,
            owner_id="owner-b",
            claim_token="token-b",
            lease_expires_at=NOW + timedelta(seconds=5),
            claimed_at=NOW + timedelta(seconds=2),
        )
        assert second is not None
        stale = first.model_copy(
            update={"state": "completed", "state_version": first.state_version + 1}
        )
        assert (
            await store.cas(
                stale,
                expected_state_version=first.state_version,
                owner_id="owner-a",
                claim_token="token-a",
            )
            == "conflict"
        )


async def test_memory_and_mongo_boundaries_defensively_copy_nested_evidence():
    ledger_stores = [
        InMemoryAgentCallLedgerStore(),
        MongoAgentCallLedgerStore(FakeCollection()),
    ]
    for store in ledger_stores:
        record = ledger_record()
        await store.insert(record)
        loaded = await store.load_by_record_id(record.call_record_id)
        loaded.recent_observation_ids.append("mutated-without-cas")
        loaded.runtime_policy.__dict__["max_transport_attempts"] = 999
        persisted = await store.load_by_record_id(record.call_record_id)
        assert persisted.recent_observation_ids == []
        assert persisted.runtime_policy.max_transport_attempts == 3

    inbox_stores = [
        InMemoryObservationInboxStore(),
        MongoObservationInboxStore(FakeCollection()),
    ]
    for store in inbox_stores:
        record = _inbox_record()
        await store.insert(record)
        loaded = await store.load(record.observation_id)
        loaded.observation.content.append({"kind": "text", "text": "mutated"})
        assert (await store.load(record.observation_id)).observation.content == []

    policy = A2ARuntimePolicy()
    with pytest.raises(ValidationError):
        policy.max_transport_attempts = 999


async def test_inbox_and_conflict_retention_is_directly_room_epoch_queryable():
    for inbox_store, conflict_store in [
        (InMemoryObservationInboxStore(), InMemoryObservationConflictStore()),
        (
            MongoObservationInboxStore(FakeCollection()),
            MongoObservationConflictStore(FakeCollection()),
        ),
    ]:
        inbox = _inbox_record().model_copy(
            update={"room_id": "room-1", "room_epoch": 1}
        )
        conflict = A2AObservationConflictRecord(
            conflict_id="conflict-retention",
            room_id="room-1",
            room_epoch=1,
            source_identity="source-retention",
            accepted_observation_id=inbox.observation_id,
            accepted_payload_digest="accepted",
            conflicting_payload_digest="conflicting",
            binding_scope="endpoint",
            received_at=NOW,
        )
        await inbox_store.insert(inbox)
        await conflict_store.insert(conflict)
        assert await inbox_store.delete_by_epoch("room-1", 1) == 1
        assert await conflict_store.delete_by_epoch("room-1", 1) == 1
        assert await inbox_store.load(inbox.observation_id) is None
        assert await conflict_store.list_for_source("source-retention") == []


async def test_mongo_duplicate_key_races_classify_exact_winners_as_replay():
    conflict_collection = DuplicateAfterWriteCollection(fail_insert=True)
    conflict_store = MongoObservationConflictStore(conflict_collection)
    conflict = A2AObservationConflictRecord(
        conflict_id="conflict-1",
        room_id="room-1",
        room_epoch=1,
        source_identity="source-1",
        accepted_observation_id="observation-1",
        accepted_payload_digest="accepted",
        conflicting_payload_digest="conflicting",
        binding_scope="endpoint",
        received_at=NOW,
    )
    assert await conflict_store.insert(conflict) == "replayed"
    assert await conflict_store.insert(conflict) == "replayed"

    epoch_collection = DuplicateAfterWriteCollection(fail_upsert=True)
    epoch_store = MongoRoomEpochStore(epoch_collection)
    outcome, epoch = await epoch_store.activate(
        "room-1", "creation-1", activated_at=NOW
    )
    assert outcome == "replayed"
    assert epoch.active and epoch.creation_id == "creation-1"
    assert (await epoch_store.activate("room-1", "creation-1", activated_at=NOW))[
        0
    ] == ("replayed")


async def test_mongo_and_memory_room_epoch_recreation_rules_match():
    stores = [InMemoryRoomEpochStore(), MongoRoomEpochStore(FakeCollection())]
    for store in stores:
        outcome, active = await store.activate("room-1", "create-1", activated_at=NOW)
        assert outcome == "accepted"
        assert (
            await store.deactivate(
                "room-1", active.epoch, "delete-1", deactivated_at=NOW
            )
        )[0] == "accepted"
        assert (await store.activate("room-1", "create-1", activated_at=NOW))[0] == (
            "conflict"
        )
        outcome, recreated = await store.activate(
            "room-1", "create-2", activated_at=NOW
        )
        assert outcome == "accepted"
        assert recreated.epoch == active.epoch + 1


def _answer_record(
    text: str, *, route_fingerprint: str = "route-fingerprint"
) -> DurableHITLAnswerRecord:
    return DurableHITLAnswerRecord(
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route_fingerprint,
        authenticated_answerer_id="user-1",
        answer_digest=f"digest:{text}",
        answers=[
            HITLQuestionAnswer(question_id="q-1", answer=HITLTextAnswer(text=text))
        ],
        verified_auth_reference_digests=[],
        verified_auth_references=[],
        applied_at=NOW,
    )


async def test_mongo_and_memory_hitl_concurrent_differing_answers_match():
    interaction_doc = _published_interaction_doc(
        "room-1", "interaction-1", published=True
    )
    spec = A2AInteractionSpec.model_validate(interaction_doc["spec"])
    route = HITLRouteSnapshotV2.model_validate(interaction_doc["route"])
    route_fingerprint = route.fingerprint

    memory = InMemoryHITLApplicationStore()
    assert (
        await memory.ensure_interaction(
            interaction_id="interaction-1",
            spec=spec,
            route=route,
            fingerprint="fp-1",
        )
        == "accepted"
    )
    assert await memory.mark_eligible("interaction-1") == "accepted"
    assert (
        await memory.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Ada", route_fingerprint=route_fingerprint),
        )
        == "accepted"
    )
    assert (
        await memory.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Bob", route_fingerprint=route_fingerprint),
        )
        == "conflict"
    )

    collection = ConcurrentAnswerCollection()
    collection.values.append(interaction_doc)
    mongo = MongoHITLApplicationStore(collection)
    first = asyncio.create_task(
        mongo.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Ada", route_fingerprint=route_fingerprint),
        )
    )
    second = asyncio.create_task(
        mongo.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Bob", route_fingerprint=route_fingerprint),
        )
    )
    await collection._both_waiting.wait()
    collection._release.set()
    assert sorted(await asyncio.gather(first, second)) == ["accepted", "conflict"]


@pytest.mark.parametrize("backend", ["memory", "mongo"])
async def test_hitl_answer_winner_excludes_abandon_with_store_parity(backend):
    interaction_doc = _published_interaction_doc(
        "room-1", "interaction-1", published=True
    )
    spec = A2AInteractionSpec.model_validate(interaction_doc["spec"])
    route = HITLRouteSnapshotV2.model_validate(interaction_doc["route"])
    if backend == "memory":
        store = InMemoryHITLApplicationStore()
        assert (
            await store.ensure_interaction(
                interaction_id="interaction-1",
                spec=spec,
                route=route,
                fingerprint="fp-1",
            )
            == "accepted"
        )
        assert await store.mark_eligible("interaction-1") == "accepted"
    else:
        collection = FakeCollection()
        collection.values.append(interaction_doc)
        store = MongoHITLApplicationStore(collection)

    assert (
        await store.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Ada", route_fingerprint=route.fingerprint),
        )
        == "accepted"
    )
    assert (
        await store.abandon(
            "interaction-1", call_record_id="call-1", reason="user_canceled"
        )
        == "conflict"
    )


@pytest.mark.parametrize("backend", ["memory", "mongo"])
async def test_hitl_abandon_winner_excludes_answer_with_store_parity(backend):
    interaction_doc = _published_interaction_doc(
        "room-1", "interaction-1", published=True
    )
    spec = A2AInteractionSpec.model_validate(interaction_doc["spec"])
    route = HITLRouteSnapshotV2.model_validate(interaction_doc["route"])
    if backend == "memory":
        store = InMemoryHITLApplicationStore()
        assert (
            await store.ensure_interaction(
                interaction_id="interaction-1",
                spec=spec,
                route=route,
                fingerprint="fp-1",
            )
            == "accepted"
        )
        assert await store.mark_eligible("interaction-1") == "accepted"
    else:
        collection = FakeCollection()
        collection.values.append(interaction_doc)
        store = MongoHITLApplicationStore(collection)

    assert (
        await store.abandon(
            "interaction-1", call_record_id="call-1", reason="user_canceled"
        )
        == "accepted"
    )
    assert (
        await store.ensure_answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            record=_answer_record("Ada", route_fingerprint=route.fingerprint),
        )
        == "conflict"
    )


def _intent_doc(intent_id: str, *, status: str, next_attempt_at=None) -> dict:
    return {
        "intent_id": intent_id,
        "kind": "append_orchestrator_event",
        "target": "orchestrator_run_events",
        "dedupe_key": f"event-{intent_id}",
        "required": True,
        "event_id": f"event-{intent_id}",
        "event_sequence": 1,
        "causation_id": "complete",
        "payload": {},
        "status": status,
        "blocked_reason": None,
        "claim_owner": None,
        "claim_expires_at": None,
        "attempt_count": 0,
        "next_attempt_at": next_attempt_at,
    }


async def test_mongo_list_due_projection_intents_unwinds_sorts_and_filters():
    collection = FakeCollection()
    due_pending = {
        "run_id": "run-due",
        "projection_outbox": [
            _intent_doc("intent-1", status="pending", next_attempt_at=None)
        ],
    }
    future_pending = {
        "run_id": "run-future",
        "projection_outbox": [
            _intent_doc(
                "intent-2",
                status="pending",
                next_attempt_at=NOW + timedelta(minutes=10),
            )
        ],
    }
    expired_claimed = {
        "run_id": "run-expired",
        "projection_outbox": [
            _intent_doc(
                "intent-3",
                status="claimed",
                next_attempt_at=None,
            )
            | {
                "claim_owner": "worker-a",
                "claim_expires_at": NOW - timedelta(seconds=1),
            }
        ],
    }
    collection.values = [future_pending, expired_claimed, due_pending]
    store = MongoOrchestratorRunStore(collection)

    due = await store.list_due_projection_intents(due_at=NOW, limit=10)

    assert [(run_id, intent.intent_id) for run_id, intent in due] == [
        ("run-due", "intent-1"),
        ("run-expired", "intent-3"),
    ]


async def test_mongo_release_projection_intent_backs_off_durably():
    collection = FakeCollection()
    run = make_run().model_copy(
        update={
            "projection_outbox": [
                ProjectionIntent.model_validate(
                    {
                        **_intent_doc("intent-1", status="claimed"),
                        "claim_owner": "worker-a",
                        "claim_expires_at": NOW,
                    }
                )
            ]
        }
    )
    collection.values = [run.model_dump(mode="python")]
    store = MongoOrchestratorRunStore(collection)

    released = await store.release_projection_intent(
        run.run_id,
        "intent-1",
        expected_state_version=0,
        owner_id="worker-a",
        next_attempt_at=NOW + timedelta(seconds=5),
        now=NOW,
    )
    assert released.outcome in {"accepted", "replayed"}
    assert released.run is not None
    intent = next(
        item for item in released.run.projection_outbox if item.intent_id == "intent-1"
    )
    assert intent.status == "pending"
    assert intent.next_attempt_at == NOW + timedelta(seconds=5)


def _published_interaction_doc(
    room_id: str,
    interaction_id: str,
    *,
    published: bool | None,
    eligible: bool = True,
) -> dict:
    spec = A2AInteractionSpec(
        schema_version=1,
        interaction_id=interaction_id,
        questions=[
            HITLQuestionSpec(
                question_id="q-1",
                interaction_kind=HITLInteractionKind.QUESTIONNAIRE,
                prompt="What?",
                answer_kind="text",
                required=True,
            )
        ],
    )
    route = HITLRouteSnapshotV2(
        orchestration_run_id="run-1",
        call_record_id="call-1",
        invocation_id="inv-1",
        room_id=room_id,
        room_epoch=1,
        binding_id="binding-1",
        agent_id="agent-1",
        task_id="task-1",
        context_id="context-1",
        interaction_revision=1,
        interaction_fingerprint="fp-1",
    )
    doc = {
        "interaction_id": interaction_id,
        "spec": spec.model_dump(mode="python"),
        "route": route.model_dump(mode="python"),
        "fingerprint": "fp-1",
        "eligible": eligible,
        "abandoned": None,
        "answers": {},
    }
    if published is not None:
        doc["published"] = published
    return doc


async def test_mongo_hitl_published_flag_filter_and_transitions():
    collection = FakeCollection()
    answered = _published_interaction_doc(
        "room-1", "published-answered", published=True
    )
    answered["answers"] = {"1": {"durable": "answer"}}
    collection.values.extend(
        [
            _published_interaction_doc("room-1", "published-true", published=True),
            answered,
            _published_interaction_doc("room-1", "published-false", published=False),
            # Missing flag = legacy auto-published document; must still surface.
            _published_interaction_doc("room-1", "published-missing", published=None),
            _published_interaction_doc(
                "room-1", "not-eligible", published=True, eligible=False
            ),
            _published_interaction_doc("room-2", "other-room", published=True),
        ]
    )
    store = MongoHITLApplicationStore(collection)

    visible = [
        item.interaction_id for item in await store.get_published_interactions("room-1")
    ]
    # published=False is suppressed; missing (legacy) and True are visible;
    # non-eligible interactions and other rooms are excluded.
    assert visible == ["published-true", "published-missing"]

    assert await store.mark_published("published-false") == "accepted"
    assert await store.mark_published("published-false") == "replayed"
    # Legacy docs with no flag are already treated as published.
    assert await store.mark_published("published-missing") == "replayed"
    assert await store.mark_published("does-not-exist") == "error"

    visible_after = [
        item.interaction_id for item in await store.get_published_interactions("room-1")
    ]
    assert visible_after == ["published-true", "published-false", "published-missing"]
