"""Injected Mongo repositories for the orchestrator A2A runtime."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, Protocol

from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    DuplicateKeyError,
    ExecutionTimeout,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
    WTimeoutError,
)

from execution.orchestrator.a2a_runtime.errors import RecoverableAdapterError
from execution.orchestrator.a2a_runtime.models import (
    A2AObservationConflictRecord,
    A2AObservationInboxRecord,
    AgentCallLedgerRecord,
    AgentToolBindingRecord,
    InlineDataArtifact,
    RoomEpoch,
)


class AsyncMongoCollection(Protocol):
    async def find_one(self, query: dict[str, object]) -> dict[str, Any] | None: ...

    async def insert_one(self, document: dict[str, object]) -> object: ...

    async def replace_one(
        self,
        query: dict[str, object],
        document: dict[str, object],
        *,
        upsert: bool = False,
    ) -> object: ...

    async def update_one(
        self,
        query: dict[str, object],
        update: dict[str, object],
        *,
        upsert: bool = False,
    ) -> object: ...

    async def delete_many(self, query: dict[str, object]) -> object: ...

    def find(self, query: dict[str, object]) -> Any: ...

    def aggregate(self, pipeline: list[dict[str, object]]) -> Any: ...


_TRANSIENT_MONGO_ERRORS = (
    AutoReconnect,
    ConnectionFailure,
    ExecutionTimeout,
    NetworkTimeout,
    ServerSelectionTimeoutError,
    WTimeoutError,
)


def _is_transient_mongo_error(exc: PyMongoError) -> bool:
    return isinstance(exc, _TRANSIENT_MONGO_ERRORS) or any(
        exc.has_error_label(label)
        for label in ("RetryableWriteError", "TransientTransactionError")
    )


async def _mongo_await(awaitable: Any) -> Any:
    try:
        return await awaitable
    except PyMongoError as exc:
        if _is_transient_mongo_error(exc):
            raise RecoverableAdapterError("transient Mongo operation failed") from exc
        raise


def _mongo_find(collection: AsyncMongoCollection, query: dict[str, object]) -> Any:
    try:
        return collection.find(query)
    except PyMongoError as exc:
        if _is_transient_mongo_error(exc):
            raise RecoverableAdapterError("transient Mongo query failed") from exc
        raise


def _mongo_aggregate(
    collection: AsyncMongoCollection, pipeline: list[dict[str, object]]
) -> Any:
    try:
        return collection.aggregate(pipeline)
    except PyMongoError as exc:
        if _is_transient_mongo_error(exc):
            raise RecoverableAdapterError("transient Mongo aggregate failed") from exc
        raise


class _MongoCollectionBoundary:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self._collection = collection

    async def find_one(self, query: dict[str, object]) -> dict[str, Any] | None:
        return await _mongo_await(self._collection.find_one(query))

    async def insert_one(self, document: dict[str, object]) -> object:
        return await _mongo_await(self._collection.insert_one(document))

    async def replace_one(
        self,
        query: dict[str, object],
        document: dict[str, object],
        *,
        upsert: bool = False,
    ) -> object:
        operation = (
            self._collection.replace_one(query, document, upsert=True)
            if upsert
            else self._collection.replace_one(query, document)
        )
        return await _mongo_await(operation)

    async def update_one(
        self,
        query: dict[str, object],
        update: dict[str, object],
        *,
        upsert: bool = False,
    ) -> object:
        operation = (
            self._collection.update_one(query, update, upsert=True)
            if upsert
            else self._collection.update_one(query, update)
        )
        return await _mongo_await(operation)

    async def delete_many(self, query: dict[str, object]) -> object:
        return await _mongo_await(self._collection.delete_many(query))

    def find(self, query: dict[str, object]) -> Any:
        return _mongo_find(self._collection, query)

    def aggregate(self, pipeline: list[dict[str, object]]) -> Any:
        return _mongo_aggregate(self._collection, pipeline)


def _bounded(collection: AsyncMongoCollection) -> AsyncMongoCollection:
    return _MongoCollectionBoundary(collection)


def _without_mongo_id(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "_id"}


def _restore_utc_datetimes(value: Any) -> Any:
    """Re-attach UTC to naive datetimes decoded from BSON.

    Motor returns BSON dates as offset-naive datetimes; mixing them with
    aware ``datetime.now(UTC)`` boundaries raises ``TypeError`` inside
    claim/renew/list_due comparisons.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: _restore_utc_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_utc_datetimes(item) for item in value]
    return value


def _from_document(model: Any, value: dict[str, Any]) -> Any:
    return model.model_validate(_restore_utc_datetimes(_without_mongo_id(value)))


async def _to_list(cursor: Any, *, length: int | None = None) -> list[dict[str, Any]]:
    """Materialize a find/aggregate result into documents.

    The protocol's ``find``/``aggregate`` return ``Any`` and production has
    shipped two shapes: Motor cursors (raw collections, ``to_list``) and
    already-awaited lists (async adapter methods returning materialized
    values). Accept all of them so a shape mismatch degrades gracefully
    instead of crashing the recovery/projection workers.
    """
    try:
        if inspect.isawaitable(cursor):
            values = await _mongo_await(cursor)
            if not isinstance(values, list):
                values = list(values)
        elif isinstance(cursor, (list, tuple)):
            values = list(cursor)
        elif hasattr(cursor, "to_list"):
            return await _mongo_await(cursor.to_list(length=length))
        else:
            values = []
            async for value in cursor:
                values.append(value)
                if length is not None and len(values) >= length:
                    break
            return values
        return values[:length] if length is not None else values
    except PyMongoError as exc:
        if _is_transient_mongo_error(exc):
            raise RecoverableAdapterError("transient Mongo cursor failed") from exc
        raise


class MongoAgentToolBindingStore:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self.collection = _bounded(collection)

    async def insert(self, record: AgentToolBindingRecord) -> str:
        existing = await self.load(record.binding_id)
        if existing is not None:
            return "replayed" if existing == record else "conflict"
        try:
            await self.collection.insert_one(record.model_dump(mode="python"))
        except DuplicateKeyError:
            existing = await self.load(record.binding_id)
            return "replayed" if existing == record else "conflict"
        except RecoverableAdapterError:
            existing = await self.load(record.binding_id)
            if existing == record:
                return "replayed"
            raise
        return "accepted"

    async def load(self, binding_id: str) -> AgentToolBindingRecord | None:
        value = await self.collection.find_one({"binding_id": binding_id})
        return _from_document(AgentToolBindingRecord, value) if value else None

    async def list_for_run(self, run_id: str) -> list[AgentToolBindingRecord]:
        values = await _to_list(self.collection.find({"run_id": run_id}))
        return sorted(
            (_from_document(AgentToolBindingRecord, value) for value in values),
            key=lambda item: item.tool_name,
        )

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        result = await self.collection.delete_many(
            {"room_id": conversation_id, "room_epoch": room_epoch}
        )
        return int(getattr(result, "deleted_count", 0))


class MongoAgentCallLedgerStore:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self.collection = _bounded(collection)

    async def insert(self, record: AgentCallLedgerRecord) -> str:
        existing = await self.load(record.run_id, record.invocation_id)
        if existing is not None:
            return (
                "replayed"
                if _acceptance_identity(existing) == _acceptance_identity(record)
                else "conflict"
            )
        try:
            await self.collection.insert_one(record.model_dump(mode="python"))
        except DuplicateKeyError:
            existing = await self.load(record.run_id, record.invocation_id)
            return (
                "replayed"
                if existing
                and _acceptance_identity(existing) == _acceptance_identity(record)
                else "conflict"
            )
        except RecoverableAdapterError:
            existing = await self.load(record.run_id, record.invocation_id)
            if existing and _acceptance_identity(existing) == _acceptance_identity(
                record
            ):
                return "replayed"
            raise
        return "accepted"

    async def load(
        self, run_id: str, invocation_id: str
    ) -> AgentCallLedgerRecord | None:
        value = await self.collection.find_one(
            {"run_id": run_id, "invocation_id": invocation_id}
        )
        return _from_document(AgentCallLedgerRecord, value) if value else None

    async def load_by_record_id(
        self, call_record_id: str
    ) -> AgentCallLedgerRecord | None:
        value = await self.collection.find_one({"call_record_id": call_record_id})
        return _from_document(AgentCallLedgerRecord, value) if value else None

    async def find_by_alias(
        self, binding_scope: str, *, task_id: str | None, context_id: str | None
    ) -> AgentCallLedgerRecord | None:
        candidates = []
        for kind, value in (("task", task_id), ("context", context_id)):
            if value:
                candidates.append(
                    {
                        "endpoint_scope_digest": binding_scope,
                        "ownership_aliases": {
                            "$elemMatch": {
                                "kind": kind,
                                "value": value,
                                "binding_scope": binding_scope,
                            }
                        },
                    }
                )
        if not candidates:
            return None
        values = await _to_list(self.collection.find({"$or": candidates}), length=2)
        return (
            _from_document(AgentCallLedgerRecord, values[0])
            if len(values) == 1
            else None
        )

    async def cas(
        self,
        record: AgentCallLedgerRecord,
        *,
        expected_state_version: int,
    ) -> str:
        current = await self.load_by_record_id(record.call_record_id)
        if current is None:
            return "error"
        if current == record:
            return "replayed"
        if (
            current.state_version != expected_state_version
            or record.state_version != expected_state_version + 1
        ):
            return "conflict"
        if record.ownership_alias_keys:
            conflicts = await _to_list(
                self.collection.find(
                    {
                        "ownership_alias_keys": {"$in": record.ownership_alias_keys},
                        "call_record_id": {"$ne": record.call_record_id},
                    }
                ),
                length=1,
            )
            if conflicts:
                return "conflict"
        try:
            result = await self.collection.replace_one(
                {
                    "call_record_id": record.call_record_id,
                    "state_version": expected_state_version,
                },
                record.model_dump(mode="python"),
            )
        except DuplicateKeyError:
            return "conflict"
        except RecoverableAdapterError:
            replay = await self.load_by_record_id(record.call_record_id)
            if replay == record:
                return "replayed"
            raise
        if int(getattr(result, "modified_count", 0)) == 1:
            return "accepted"
        replay = await self.load_by_record_id(record.call_record_id)
        return "replayed" if replay == record else "conflict"

    async def claim(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = await self.load_by_record_id(call_record_id)
        if (
            record is None
            or record.state_version != expected_state_version
            or lease_expires_at <= claimed_at
            or (
                record.claim_expires_at is not None
                and record.claim_expires_at > claimed_at
            )
            or (
                record.next_attempt_at is not None
                and record.next_attempt_at > claimed_at
            )
        ):
            return None
        claimed = record.model_copy(
            update={
                "claim_owner": owner_id,
                "claim_expires_at": lease_expires_at,
                "next_attempt_at": None,
                "state_version": record.state_version + 1,
                "updated_at": claimed_at,
            }
        )
        return (
            claimed
            if await self.cas(claimed, expected_state_version=record.state_version)
            in {"accepted", "replayed"}
            else None
        )

    async def renew(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        renewed_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = await self.load_by_record_id(call_record_id)
        if (
            record is None
            or record.state_version != expected_state_version
            or record.claim_owner != owner_id
            or record.claim_expires_at is None
            or lease_expires_at <= renewed_at
            or lease_expires_at <= record.claim_expires_at
        ):
            return None
        renewed = record.model_copy(
            update={
                "claim_expires_at": lease_expires_at,
                "state_version": record.state_version + 1,
                "updated_at": renewed_at,
            }
        )
        return (
            renewed
            if await self.cas(renewed, expected_state_version=record.state_version)
            in {"accepted", "replayed"}
            else None
        )

    async def release(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime | None,
        released_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = await self.load_by_record_id(call_record_id)
        if (
            record is None
            or record.state_version != expected_state_version
            or record.claim_owner != owner_id
        ):
            return None
        released = record.model_copy(
            update={
                "claim_owner": None,
                "claim_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "state_version": record.state_version + 1,
                "updated_at": released_at,
            }
        )
        return (
            released
            if await self.cas(released, expected_state_version=record.state_version)
            in {"accepted", "replayed"}
            else None
        )

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[AgentCallLedgerRecord]:
        query = {
            "state": {
                "$nin": ["completed", "failed", "canceled", "rejected", "expired"]
            },
            "$and": [
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": due_at}},
                    ]
                },
                {
                    "$or": [
                        {"claim_expires_at": None},
                        {"claim_expires_at": {"$lte": due_at}},
                    ]
                },
            ],
        }
        values = await _to_list(self.collection.find(query), length=limit)
        return [_from_document(AgentCallLedgerRecord, value) for value in values]

    async def list_for_run(self, run_id: str) -> list[AgentCallLedgerRecord]:
        values = await _to_list(self.collection.find({"run_id": run_id}))
        return [_from_document(AgentCallLedgerRecord, value) for value in values]

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        result = await self.collection.delete_many(
            {"room_id": conversation_id, "room_epoch": room_epoch}
        )
        return int(getattr(result, "deleted_count", 0))


class MongoObservationInboxStore:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self.collection = _bounded(collection)

    async def insert(self, record: A2AObservationInboxRecord) -> str:
        existing = await self.load_by_source_identity(record.source_identity)
        if existing is not None:
            return (
                "replayed"
                if existing.payload_digest == record.payload_digest
                else "conflict"
            )
        try:
            await self.collection.insert_one(record.model_dump(mode="python"))
        except DuplicateKeyError:
            existing = await self.load_by_source_identity(record.source_identity)
            return (
                "replayed"
                if existing and existing.payload_digest == record.payload_digest
                else "conflict"
            )
        except RecoverableAdapterError:
            existing = await self.load_by_source_identity(record.source_identity)
            if existing and existing == record:
                return "replayed"
            raise
        return "accepted"

    async def load(self, observation_id: str) -> A2AObservationInboxRecord | None:
        value = await self.collection.find_one({"observation_id": observation_id})
        return _from_document(A2AObservationInboxRecord, value) if value else None

    async def load_by_source_identity(
        self, source_identity: str
    ) -> A2AObservationInboxRecord | None:
        value = await self.collection.find_one({"source_identity": source_identity})
        return _from_document(A2AObservationInboxRecord, value) if value else None

    async def load_inline_artifact(
        self, ref_id: str
    ) -> tuple[A2AObservationInboxRecord, InlineDataArtifact] | None:
        values = await _to_list(
            self.collection.find({"observation.inline_artifacts.ref_id": ref_id}),
            length=2,
        )
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("inline artifact Ref is ambiguous")
        record = _from_document(A2AObservationInboxRecord, values[0])
        matches = [
            artifact
            for artifact in record.observation.inline_artifacts
            if artifact.ref_id == ref_id
        ]
        if len(matches) != 1:
            raise ValueError("inline artifact descriptor is ambiguous")
        return record, matches[0]

    async def cas(  # noqa: C901
        self,
        record: A2AObservationInboxRecord,
        *,
        expected_state_version: int,
        owner_id: str | None = None,
        claim_token: str | None = None,
    ) -> str:
        current = await self.load(record.observation_id)
        if current is None:
            return "error"
        if current == record:
            return "replayed"
        if (
            current.state_version != expected_state_version
            or record.state_version != expected_state_version + 1
        ):
            return "conflict"
        if owner_id is not None and (
            current.claim_owner != owner_id or current.claim_token != claim_token
        ):
            return "conflict"
        if _inbox_evidence_identity(current) != _inbox_evidence_identity(record):
            return "conflict"
        query: dict[str, object] = {
            "observation_id": record.observation_id,
            "state_version": expected_state_version,
        }
        if owner_id is not None:
            query.update(claim_owner=owner_id, claim_token=claim_token)
        try:
            result = await self.collection.replace_one(
                query, record.model_dump(mode="python")
            )
        except DuplicateKeyError:
            return "conflict"
        except RecoverableAdapterError:
            replay = await self.load(record.observation_id)
            if replay == record:
                return "replayed"
            raise
        if int(getattr(result, "modified_count", 0)) == 1:
            return "accepted"
        replay = await self.load(record.observation_id)
        return "replayed" if replay == record else "conflict"

    async def claim(
        self,
        observation_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        claim_token: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> A2AObservationInboxRecord | None:
        record = await self.load(observation_id)
        if (
            record is None
            or record.state_version != expected_state_version
            or record.state in {"completed", "quarantined"}
            or lease_expires_at <= claimed_at
            or (
                record.claim_expires_at is not None
                and record.claim_expires_at > claimed_at
            )
            or (
                record.next_attempt_at is not None
                and record.next_attempt_at > claimed_at
            )
        ):
            return None
        claimed = record.model_copy(
            update={
                "state": "claimed",
                "claim_owner": owner_id,
                "claim_token": claim_token,
                "claim_expires_at": lease_expires_at,
                "attempt_count": record.attempt_count + 1,
                "state_version": record.state_version + 1,
            }
        )
        return (
            claimed
            if await self.cas(claimed, expected_state_version=record.state_version)
            in {"accepted", "replayed"}
            else None
        )

    async def renew(
        self,
        observation_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        claim_token: str,
        lease_expires_at: datetime,
        renewed_at: datetime,
    ) -> A2AObservationInboxRecord | None:
        record = await self.load(observation_id)
        if (
            record is None
            or record.state_version != expected_state_version
            or record.state != "claimed"
            or record.claim_owner != owner_id
            or record.claim_token != claim_token
            or record.claim_expires_at is None
            or record.claim_expires_at <= renewed_at
            or lease_expires_at <= record.claim_expires_at
        ):
            return None
        renewed = record.model_copy(
            update={
                "claim_expires_at": lease_expires_at,
                "state_version": record.state_version + 1,
            }
        )
        return (
            renewed
            if await self.cas(
                renewed,
                expected_state_version=record.state_version,
                owner_id=owner_id,
                claim_token=claim_token,
            )
            in {"accepted", "replayed"}
            else None
        )

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]:
        query = {
            "state": {"$nin": ["completed", "quarantined"]},
            "$and": [
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": due_at}},
                    ]
                },
                {
                    "$or": [
                        {"claim_expires_at": None},
                        {"claim_expires_at": {"$lte": due_at}},
                    ]
                },
            ],
        }
        values = await _to_list(self.collection.find(query), length=limit)
        return [_from_document(A2AObservationInboxRecord, value) for value in values]

    async def list_due_for_call(
        self, call_record_id: str, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]:
        query = {
            "call_record_id": call_record_id,
            "state": {"$nin": ["completed", "quarantined"]},
            "$and": [
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": due_at}},
                    ]
                },
                {
                    "$or": [
                        {"claim_expires_at": None},
                        {"claim_expires_at": {"$lte": due_at}},
                    ]
                },
            ],
        }
        values = await _to_list(self.collection.find(query), length=limit)
        return [_from_document(A2AObservationInboxRecord, value) for value in values]

    async def delete_by_binding_scope(self, binding_scope: str) -> int:
        result = await self.collection.delete_many({"binding_scope": binding_scope})
        return int(getattr(result, "deleted_count", 0))

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        result = await self.collection.delete_many(
            {"room_id": conversation_id, "room_epoch": room_epoch}
        )
        return int(getattr(result, "deleted_count", 0))


class MongoObservationConflictStore:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self.collection = _bounded(collection)

    async def insert(self, record: A2AObservationConflictRecord) -> str:
        existing = await self.collection.find_one({"conflict_id": record.conflict_id})
        if existing:
            return (
                "replayed"
                if _conflict_identity(
                    A2AObservationConflictRecord.model_validate(
                        _without_mongo_id(existing)
                    )
                )
                == _conflict_identity(record)
                else "conflict"
            )
        try:
            await self.collection.insert_one(record.model_dump(mode="python"))
        except (DuplicateKeyError, RecoverableAdapterError) as exc:
            winner = await self.collection.find_one({"conflict_id": record.conflict_id})
            if winner is None:
                if isinstance(exc, RecoverableAdapterError):
                    raise
                return "conflict"
            persisted = A2AObservationConflictRecord.model_validate(
                _without_mongo_id(winner)
            )
            if _conflict_identity(persisted) == _conflict_identity(record):
                return "replayed"
            if isinstance(exc, RecoverableAdapterError):
                raise
            return "conflict"
        return "accepted"

    async def list_for_source(
        self, source_identity: str
    ) -> list[A2AObservationConflictRecord]:
        values = await _to_list(
            self.collection.find({"source_identity": source_identity})
        )
        return [_from_document(A2AObservationConflictRecord, value) for value in values]

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        result = await self.collection.delete_many(
            {"room_id": conversation_id, "room_epoch": room_epoch}
        )
        return int(getattr(result, "deleted_count", 0))


class MongoRoomEpochStore:
    def __init__(self, collection: AsyncMongoCollection) -> None:
        self.collection = _bounded(collection)

    async def read(self, conversation_id: str) -> RoomEpoch | None:
        value = await self.collection.find_one({"room_id": conversation_id})
        return _from_document(RoomEpoch, value) if value else None

    async def read_active(self, conversation_id: str) -> RoomEpoch | None:
        value = await self.collection.find_one(
            {"room_id": conversation_id, "active": True}
        )
        return _from_document(RoomEpoch, value) if value else None

    async def activate(
        self, conversation_id: str, creation_id: str, *, activated_at: datetime
    ) -> tuple[str, RoomEpoch | None]:
        value = await self.collection.find_one({"room_id": conversation_id})
        current = _from_document(RoomEpoch, value) if value else None
        if current and current.active:
            return (
                ("replayed", current)
                if current.creation_id == creation_id
                else ("conflict", current)
            )
        if current is not None and current.creation_id == creation_id:
            return "conflict", current
        epoch = 1 if current is None else current.high_water_mark + 1
        record = RoomEpoch(
            conversation_id=conversation_id,
            epoch=epoch,
            high_water_mark=epoch,
            active=True,
            creation_id=creation_id,
            updated_at=activated_at,
        )
        operation_error: RecoverableAdapterError | None = None
        try:
            result = await self.collection.replace_one(
                {
                    "room_id": conversation_id,
                    "high_water_mark": current.high_water_mark
                    if current
                    else {"$exists": False},
                },
                record.model_dump(mode="python"),
                upsert=current is None,
            )
        except DuplicateKeyError:
            result = None
        except RecoverableAdapterError as exc:
            operation_error = exc
            result = None
        accepted = bool(
            result
            and (
                getattr(result, "modified_count", 0)
                or getattr(result, "upserted_id", None)
            )
        )
        if accepted:
            return "accepted", record
        winner_value = await self.collection.find_one({"room_id": conversation_id})
        winner = _from_document(RoomEpoch, winner_value) if winner_value else None
        if winner is not None and _same_room_activation(winner, record):
            return "replayed", winner
        if operation_error is not None:
            raise operation_error
        return "conflict", winner or current

    async def deactivate(
        self,
        conversation_id: str,
        epoch: int,
        deletion_id: str,
        *,
        deactivated_at: datetime,
    ) -> tuple[str, RoomEpoch | None]:
        current = await self.read_active(conversation_id)
        if current is None or current.epoch != epoch:
            value = await self.collection.find_one({"room_id": conversation_id})
            existing = _from_document(RoomEpoch, value) if value else None
            if (
                existing
                and not existing.active
                and existing.epoch == epoch
                and existing.deletion_id == deletion_id
            ):
                return "replayed", existing
            return "conflict", existing
        record = current.model_copy(
            update={
                "active": False,
                "deletion_id": deletion_id,
                "updated_at": deactivated_at,
            }
        )
        operation_error: RecoverableAdapterError | None = None
        try:
            result = await self.collection.replace_one(
                {"room_id": conversation_id, "epoch": epoch, "active": True},
                record.model_dump(mode="python"),
            )
        except RecoverableAdapterError as exc:
            operation_error = exc
            result = None
        if result is not None and int(getattr(result, "modified_count", 0)) == 1:
            return "accepted", record
        winner_value = await self.collection.find_one({"room_id": conversation_id})
        winner = _from_document(RoomEpoch, winner_value) if winner_value else None
        if winner is not None and _same_room_deactivation(winner, record):
            return "replayed", winner
        if operation_error is not None:
            raise operation_error
        return "conflict", winner or current

    async def verify_active(self, conversation_id: str, epoch: int) -> bool:
        value = await self.collection.find_one(
            {"room_id": conversation_id, "epoch": epoch, "active": True}
        )
        return value is not None

    async def verify_cleanup_epoch(
        self, conversation_id: str, epoch: int, deletion_id: str
    ) -> bool:
        value = await self.collection.find_one(
            {
                "room_id": conversation_id,
                "epoch": epoch,
                "active": False,
                "deletion_id": deletion_id,
            }
        )
        return value is not None


def _same_room_activation(winner: RoomEpoch, intended: RoomEpoch) -> bool:
    return (
        winner.conversation_id == intended.conversation_id
        and winner.epoch == intended.epoch
        and winner.high_water_mark == intended.high_water_mark
        and winner.active
        and winner.creation_id == intended.creation_id
        and winner.deletion_id is None
    )


def _same_room_deactivation(winner: RoomEpoch, intended: RoomEpoch) -> bool:
    return (
        winner.conversation_id == intended.conversation_id
        and winner.epoch == intended.epoch
        and winner.high_water_mark == intended.high_water_mark
        and not winner.active
        and winner.creation_id == intended.creation_id
        and winner.deletion_id == intended.deletion_id
    )


def _inbox_evidence_identity(
    record: A2AObservationInboxRecord,
) -> tuple[object, ...]:
    return (
        record.observation_id,
        record.source_kind,
        record.source_identity,
        record.payload_digest,
        record.received_at,
        record.binding_scope,
        record.conversation_id,
        record.room_epoch,
        record.call_record_id,
        record.task_id,
        record.context_id,
        record.agent_id,
        record.event_kind,
        record.observation,
    )


def _conflict_identity(record: A2AObservationConflictRecord) -> tuple[str, ...]:
    return (
        record.conflict_id,
        record.conversation_id,
        str(record.room_epoch),
        record.source_identity,
        record.accepted_observation_id,
        record.accepted_payload_digest,
        record.conflicting_payload_digest,
        record.binding_scope,
    )


def _acceptance_identity(record: AgentCallLedgerRecord) -> tuple[object, ...]:
    return (
        record.call_record_id,
        record.run_id,
        record.invocation_id,
        record.acceptance_id,
        record.idempotency_key,
        record.binding_digest,
        record.arguments_digest,
        record.resource_manifest.content_digest,
        record.room_epoch,
    )
