"""Deterministic in-memory repositories for conformance and failure tests."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel

from ..models import ToolInvocation, ToolResult
from ..persistence import TERMINAL_RUN_STATUSES
from ..ports import OrchestratorRunStore
from .models import (
    ACTIVE_AGENT_CALL_STATES,
    A2AObservationConflictRecord,
    A2AObservationInboxRecord,
    AgentCallLedgerRecord,
    AgentToolBindingRecord,
    InlineDataArtifact,
    PreparedInvocationSnapshot,
    RoomEpoch,
)


def _clone[ModelT: BaseModel](value: ModelT) -> ModelT:
    return type(value).model_validate(value.model_dump(mode="python"))


class InMemoryAgentToolBindingStore:
    def __init__(self) -> None:
        self._records: dict[str, AgentToolBindingRecord] = {}

    async def insert(self, record: AgentToolBindingRecord) -> str:
        existing = self._records.get(record.binding_id)
        if existing is not None:
            return "replayed" if existing == record else "conflict"
        if any(
            item.run_id == record.run_id and item.tool_name == record.tool_name
            for item in self._records.values()
        ):
            return "conflict"
        self._records[record.binding_id] = _clone(record)
        return "accepted"

    async def load(self, binding_id: str) -> AgentToolBindingRecord | None:
        record = self._records.get(binding_id)
        return _clone(record) if record is not None else None

    async def list_for_run(self, run_id: str) -> list[AgentToolBindingRecord]:
        return [
            _clone(record)
            for record in sorted(
                (
                    record
                    for record in self._records.values()
                    if record.run_id == run_id
                ),
                key=lambda record: record.tool_name,
            )
        ]

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        keys = [
            key
            for key, record in self._records.items()
            if record.conversation_id == conversation_id
            and record.room_epoch == room_epoch
        ]
        for key in keys:
            del self._records[key]
        return len(keys)


class InMemoryPreparedInvocationSnapshotReader:
    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], PreparedInvocationSnapshot] = {}

    def put(self, snapshot: PreparedInvocationSnapshot) -> str:
        key = (snapshot.run_id, snapshot.invocation_id)
        existing = self._snapshots.get(key)
        if existing is not None:
            return "replayed" if existing == snapshot else "conflict"
        self._snapshots[key] = _clone(snapshot)
        return "accepted"

    def read_snapshot_for_test(
        self, run_id: str, invocation_id: str
    ) -> PreparedInvocationSnapshot | None:
        snapshot = self._snapshots.get((run_id, invocation_id))
        return _clone(snapshot) if snapshot is not None else None

    async def read_prepared(
        self, invocation: ToolInvocation
    ) -> PreparedInvocationSnapshot | None:
        snapshot = self._snapshots.get((invocation.run_id, invocation.invocation_id))
        return _clone(snapshot) if snapshot is not None else None


class InMemoryAgentCallLedgerStore:
    def __init__(self) -> None:
        self._records: dict[str, AgentCallLedgerRecord] = {}
        self._by_invocation: dict[tuple[str, str], str] = {}
        self._by_acceptance: dict[str, str] = {}
        self._by_idempotency: dict[str, str] = {}
        self._by_source: dict[tuple[str, str, int], str] = {}

    def read_authority_for_test(
        self,
    ) -> tuple[
        dict[str, AgentCallLedgerRecord],
        dict[str, dict[object, str]],
    ]:
        records = {key: _clone(value) for key, value in self._records.items()}
        indexes: dict[str, dict[object, str]] = {
            "invocation": dict(self._by_invocation),
            "acceptance": dict(self._by_acceptance),
            "idempotency": dict(self._by_idempotency),
            "source": dict(self._by_source),
        }
        return records, indexes

    async def insert(self, record: AgentCallLedgerRecord) -> str:
        if self._aliases_conflict(record):
            return "conflict"
        keys = [
            self._records.get(record.call_record_id),
            self._records.get(
                self._by_invocation.get((record.run_id, record.invocation_id), "")
            ),
            self._records.get(self._by_acceptance.get(record.acceptance_id, "")),
            self._records.get(self._by_idempotency.get(record.idempotency_key, "")),
            self._records.get(
                self._by_source.get(
                    (record.run_id, record.assistant_message_id, record.source_index),
                    "",
                )
            ),
        ]
        existing = next((item for item in keys if item is not None), None)
        if existing is not None:
            return (
                "replayed"
                if _acceptance_identity(existing) == _acceptance_identity(record)
                else "conflict"
            )
        self._records[record.call_record_id] = _clone(record)
        self._by_invocation[(record.run_id, record.invocation_id)] = (
            record.call_record_id
        )
        self._by_acceptance[record.acceptance_id] = record.call_record_id
        self._by_idempotency[record.idempotency_key] = record.call_record_id
        self._by_source[
            (record.run_id, record.assistant_message_id, record.source_index)
        ] = record.call_record_id
        return "accepted"

    async def load(
        self, run_id: str, invocation_id: str
    ) -> AgentCallLedgerRecord | None:
        record_id = self._by_invocation.get((run_id, invocation_id))
        record = self._records.get(record_id or "")
        return _clone(record) if record is not None else None

    async def load_by_record_id(
        self, call_record_id: str
    ) -> AgentCallLedgerRecord | None:
        record = self._records.get(call_record_id)
        return _clone(record) if record is not None else None

    async def find_by_alias(
        self, binding_scope: str, *, task_id: str | None, context_id: str | None
    ) -> AgentCallLedgerRecord | None:
        matches = []
        for record in self._records.values():
            if any(
                alias.binding_scope == binding_scope
                and (
                    (alias.kind == "task" and alias.value == task_id)
                    or (alias.kind == "context" and alias.value == context_id)
                )
                for alias in record.ownership_aliases
            ):
                matches.append(record)
        return _clone(matches[0]) if len(matches) == 1 else None

    async def cas(
        self,
        record: AgentCallLedgerRecord,
        *,
        expected_state_version: int,
    ) -> str:
        current = self._records.get(record.call_record_id)
        if current is None:
            return "error"
        if current == record:
            return "replayed"
        if current.state_version != expected_state_version:
            return "conflict"
        if record.state_version != expected_state_version + 1:
            return "error"
        if self._aliases_conflict(record):
            return "conflict"
        self._records[record.call_record_id] = _clone(record)
        return "accepted"

    async def claim(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = self._records.get(call_record_id)
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
        self._records[call_record_id] = _clone(claimed)
        return _clone(claimed)

    async def renew(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        renewed_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = self._records.get(call_record_id)
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
        self._records[call_record_id] = _clone(renewed)
        return _clone(renewed)

    async def release(
        self,
        call_record_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime | None,
        released_at: datetime,
    ) -> AgentCallLedgerRecord | None:
        record = self._records.get(call_record_id)
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
        self._records[call_record_id] = _clone(released)
        return _clone(released)

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[AgentCallLedgerRecord]:
        return [
            _clone(record)
            for record in sorted(
                (
                    record
                    for record in self._records.values()
                    if record.state in ACTIVE_AGENT_CALL_STATES
                    and (
                        record.next_attempt_at is None
                        or record.next_attempt_at <= due_at
                    )
                    and (
                        record.claim_expires_at is None
                        or record.claim_expires_at <= due_at
                    )
                ),
                key=lambda record: (
                    record.next_attempt_at or record.updated_at,
                    record.call_record_id,
                ),
            )[:limit]
        ]

    async def list_for_run(self, run_id: str) -> list[AgentCallLedgerRecord]:
        return [
            _clone(record)
            for record in self._records.values()
            if record.run_id == run_id
        ]

    def _aliases_conflict(self, candidate: AgentCallLedgerRecord) -> bool:
        candidate_keys = set(candidate.ownership_alias_keys)
        return any(
            record.call_record_id != candidate.call_record_id
            and bool(candidate_keys.intersection(record.ownership_alias_keys))
            for record in self._records.values()
        )

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        ids = [
            record.call_record_id
            for record in self._records.values()
            if record.conversation_id == conversation_id
            and record.room_epoch == room_epoch
        ]
        for record_id in ids:
            record = self._records.pop(record_id)
            self._by_invocation.pop((record.run_id, record.invocation_id), None)
            self._by_acceptance.pop(record.acceptance_id, None)
            self._by_idempotency.pop(record.idempotency_key, None)
            self._by_source.pop(
                (record.run_id, record.assistant_message_id, record.source_index), None
            )
        return len(ids)


class InMemoryObservationInboxStore:
    def __init__(self) -> None:
        self._records: dict[str, A2AObservationInboxRecord] = {}
        self._by_source: dict[str, str] = {}

    def read_authority_for_test(
        self,
    ) -> tuple[dict[str, A2AObservationInboxRecord], dict[str, str]]:
        return (
            {key: _clone(value) for key, value in self._records.items()},
            dict(self._by_source),
        )

    async def insert(self, record: A2AObservationInboxRecord) -> str:
        existing_id = self._by_source.get(record.source_identity)
        existing = self._records.get(existing_id or "")
        if existing is not None:
            return (
                "replayed"
                if existing.payload_digest == record.payload_digest
                else "conflict"
            )
        if record.observation_id in self._records:
            return (
                "replayed"
                if self._records[record.observation_id] == record
                else "conflict"
            )
        self._records[record.observation_id] = _clone(record)
        self._by_source[record.source_identity] = record.observation_id
        return "accepted"

    async def load(self, observation_id: str) -> A2AObservationInboxRecord | None:
        record = self._records.get(observation_id)
        return _clone(record) if record is not None else None

    async def load_by_source_identity(
        self, source_identity: str
    ) -> A2AObservationInboxRecord | None:
        record = self._records.get(self._by_source.get(source_identity, ""))
        return _clone(record) if record is not None else None

    async def load_inline_artifact(
        self, ref_id: str
    ) -> tuple[A2AObservationInboxRecord, InlineDataArtifact] | None:
        matches = [
            (record, artifact)
            for record in self._records.values()
            for artifact in record.observation.inline_artifacts
            if artifact.ref_id == ref_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("inline artifact Ref is ambiguous")
        record, artifact = matches[0]
        return _clone(record), _clone(artifact)

    async def cas(
        self,
        record: A2AObservationInboxRecord,
        *,
        expected_state_version: int,
        owner_id: str | None = None,
        claim_token: str | None = None,
    ) -> str:
        current = self._records.get(record.observation_id)
        if current is None:
            return "error"
        if current == record:
            return "replayed"
        if current.state_version != expected_state_version:
            return "conflict"
        if owner_id is not None and (
            current.claim_owner != owner_id or current.claim_token != claim_token
        ):
            return "conflict"
        if record.state_version != expected_state_version + 1:
            return "error"
        if _inbox_evidence_identity(current) != _inbox_evidence_identity(record):
            return "conflict"
        self._records[record.observation_id] = _clone(record)
        return "accepted"

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
        record = self._records.get(observation_id)
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
        self._records[observation_id] = _clone(claimed)
        return _clone(claimed)

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
        record = self._records.get(observation_id)
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
        self._records[observation_id] = _clone(renewed)
        return _clone(renewed)

    async def list_due(
        self, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]:
        return [
            _clone(record)
            for record in self._records.values()
            if record.state not in {"completed", "quarantined"}
            and (record.next_attempt_at is None or record.next_attempt_at <= due_at)
            and (record.claim_expires_at is None or record.claim_expires_at <= due_at)
        ][:limit]

    async def list_due_for_call(
        self, call_record_id: str, *, due_at: datetime, limit: int
    ) -> list[A2AObservationInboxRecord]:
        due = await self.list_due(due_at=due_at, limit=len(self._records))
        return [record for record in due if record.call_record_id == call_record_id][
            :limit
        ]

    async def delete_by_binding_scope(self, binding_scope: str) -> int:
        ids = [
            record.observation_id
            for record in self._records.values()
            if record.binding_scope == binding_scope
        ]
        for observation_id in ids:
            record = self._records.pop(observation_id)
            self._by_source.pop(record.source_identity, None)
        return len(ids)

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        ids = [
            record.observation_id
            for record in self._records.values()
            if record.conversation_id == conversation_id
            and record.room_epoch == room_epoch
        ]
        for observation_id in ids:
            record = self._records.pop(observation_id)
            self._by_source.pop(record.source_identity, None)
        return len(ids)


class InMemoryObservationConflictStore:
    def __init__(self) -> None:
        self._records: dict[str, A2AObservationConflictRecord] = {}

    async def insert(self, record: A2AObservationConflictRecord) -> str:
        existing = self._records.get(record.conflict_id)
        if existing is not None:
            return (
                "replayed"
                if _conflict_identity(existing) == _conflict_identity(record)
                else "conflict"
            )
        self._records[record.conflict_id] = _clone(record)
        return "accepted"

    async def list_for_source(
        self, source_identity: str
    ) -> list[A2AObservationConflictRecord]:
        return [
            _clone(record)
            for record in self._records.values()
            if record.source_identity == source_identity
        ]

    async def delete_by_epoch(self, conversation_id: str, room_epoch: int) -> int:
        ids = [
            record.conflict_id
            for record in self._records.values()
            if record.conversation_id == conversation_id
            and record.room_epoch == room_epoch
        ]
        for conflict_id in ids:
            self._records.pop(conflict_id)
        return len(ids)


class InMemoryRoomEpochStore:
    def __init__(self) -> None:
        self._records: dict[str, RoomEpoch] = {}

    async def read(self, conversation_id: str) -> RoomEpoch | None:
        record = self._records.get(conversation_id)
        return _clone(record) if record is not None else None

    async def read_active(self, conversation_id: str) -> RoomEpoch | None:
        record = self._records.get(conversation_id)
        return _clone(record) if record is not None and record.active else None

    async def activate(
        self, conversation_id: str, creation_id: str, *, activated_at: datetime
    ) -> tuple[str, RoomEpoch | None]:
        current = self._records.get(conversation_id)
        if current is not None and current.active:
            return (
                ("replayed", _clone(current))
                if current.creation_id == creation_id
                else ("conflict", _clone(current))
            )
        if current is not None and current.creation_id == creation_id:
            return "conflict", _clone(current)
        epoch = 1 if current is None else current.high_water_mark + 1
        record = RoomEpoch(
            conversation_id=conversation_id,
            epoch=epoch,
            high_water_mark=epoch,
            active=True,
            creation_id=creation_id,
            updated_at=activated_at,
        )
        self._records[conversation_id] = _clone(record)
        return "accepted", _clone(record)

    async def deactivate(
        self,
        conversation_id: str,
        epoch: int,
        deletion_id: str,
        *,
        deactivated_at: datetime,
    ) -> tuple[str, RoomEpoch | None]:
        current = self._records.get(conversation_id)
        if current is None or current.epoch != epoch:
            return "conflict", _clone(current) if current is not None else None
        if not current.active:
            return (
                ("replayed", _clone(current))
                if current.deletion_id == deletion_id
                else ("conflict", _clone(current))
            )
        record = current.model_copy(
            update={
                "active": False,
                "deletion_id": deletion_id,
                "updated_at": deactivated_at,
            }
        )
        self._records[conversation_id] = _clone(record)
        return "accepted", _clone(record)

    async def verify_active(self, conversation_id: str, epoch: int) -> bool:
        record = self._records.get(conversation_id)
        return bool(record and record.active and record.epoch == epoch)

    async def verify_cleanup_epoch(
        self, conversation_id: str, epoch: int, deletion_id: str
    ) -> bool:
        record = self._records.get(conversation_id)
        return bool(
            record
            and not record.active
            and record.epoch == epoch
            and record.deletion_id == deletion_id
        )


class RunCheckpointReader:
    """Read-only generic Run proof used by A2A process managers."""

    def __init__(self, run_store: OrchestratorRunStore) -> None:
        self.run_store = run_store

    async def is_acceptance_checkpointed(
        self,
        run_id: str,
        invocation_id: str,
        acceptance_id: str,
        idempotency_key: str,
        binding_digest: str,
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and entry.acceptance is not None
            and entry.acceptance.acceptance_id == acceptance_id
            and entry.acceptance.idempotency_key == idempotency_key
            and entry.invocation.idempotency_key == idempotency_key
            and entry.invocation.tool.binding.binding_digest == binding_digest
            for batch in run.tool_batches
            for entry in batch.entries
        )

    async def is_suspension_checkpointed(
        self, run_id: str, invocation_id: str, status: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and entry.state == status
            for batch in run.tool_batches
            for entry in batch.entries
        )

    async def is_run_terminal(self, run_id: str) -> bool:
        run = await self.run_store.load(run_id)
        return run is not None and run.status in TERMINAL_RUN_STATUSES

    async def is_outcome_checkpointed(
        self, run_id: str, invocation_id: str, outcome_digest: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        for batch in run.tool_batches:
            for entry in batch.entries:
                result = entry.buffered_terminal_result
                if (
                    entry.invocation is not None
                    and entry.invocation.invocation_id == invocation_id
                    and isinstance(result, ToolResult)
                    and sha256(result.model_dump_json().encode()).hexdigest()
                    == outcome_digest
                ):
                    return True
        return False

    async def has_processed_observation(
        self, run_id: str, invocation_id: str, observation_id: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and observation_id in entry.processed_observation_ids
            for batch in run.tool_batches
            for entry in batch.entries
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
