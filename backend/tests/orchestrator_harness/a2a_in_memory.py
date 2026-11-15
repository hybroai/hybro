"""In-memory A2A runtime stores and HITL ports for orchestrator tests."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer
from execution.adapters.hitl import (
    StoredHITLInteraction,
    _clone_route,
    _clone_spec,
)
from execution.orchestrator.a2a_runtime.hitl import _digest_json
from execution.orchestrator.a2a_runtime.models import (
    ACTIVE_AGENT_CALL_STATES,
    A2AObservationConflictRecord,
    A2AObservationInboxRecord,
    AgentCallLedgerRecord,
    AgentToolBindingRecord,
    DurableHITLAnswerRecord,
    HITLRouteSnapshotV2,
    InlineDataArtifact,
    PreparedInvocationSnapshot,
    RoomEpoch,
    VerifiedAuthReferenceBinding,
)
from execution.orchestrator.a2a_runtime.ports import HITLAbandonOutcome, StoreOutcome
from execution.orchestrator.models import ToolInvocation


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


class InMemoryHITLApplicationPort:
    def __init__(self) -> None:
        self._interactions: dict[
            str, tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str]
        ] = {}
        self._answer_records: dict[tuple[str, int], DurableHITLAnswerRecord] = {}
        self._eligible_interactions: set[str] = set()
        self._published_interactions: set[str] = set()
        self._abandoned_interactions: dict[str, tuple[str, str]] = {}

    async def create_or_replay(
        self,
        *,
        call: AgentCallLedgerRecord,
        interaction: A2AInteractionSpec,
        interaction_fingerprint: str,
    ) -> str:
        route = HITLRouteSnapshotV2(
            orchestration_run_id=call.run_id,
            call_record_id=call.call_record_id,
            invocation_id=call.invocation_id,
            conversation_id=call.conversation_id,
            room_epoch=call.room_epoch,
            binding_id=call.binding_id,
            agent_id=call.agent_id,
            task_id=call.a2a_task_id,
            context_id=call.a2a_context_id,
            interaction_revision=1,
            interaction_fingerprint=interaction_fingerprint,
        )
        interaction_id = interaction.interaction_id
        desired = (
            A2AInteractionSpec.model_validate(interaction.model_dump(mode="python")),
            HITLRouteSnapshotV2.model_validate(route.model_dump(mode="python")),
            interaction_fingerprint,
        )
        existing = self._interactions.get(interaction_id)
        if existing is not None and existing != desired:
            raise ValueError("HITL interaction identity conflict")
        self._interactions.setdefault(interaction_id, desired)
        if (
            call.state in {"input_required", "auth_required"}
            and call.pending_interaction_id == interaction_id
            and call.interaction_fingerprint == interaction_fingerprint
        ):
            self._eligible_interactions.add(interaction_id)
        return interaction_id

    async def activate(
        self,
        interaction_id: str,
        *,
        call_record_id: str,
        interaction_fingerprint: str,
    ) -> StoreOutcome:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "error"
        if (
            stored[1].call_record_id != call_record_id
            or stored[2] != interaction_fingerprint
            or interaction_id in self._abandoned_interactions
        ):
            return "conflict"
        if interaction_id in self._eligible_interactions:
            return "replayed"
        self._eligible_interactions.add(interaction_id)
        return "accepted"

    async def abandon(
        self,
        interaction_id: str,
        *,
        call_record_id: str,
        reason: str,
    ) -> HITLAbandonOutcome:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "absent"
        _spec, route, _fingerprint = stored
        if route.call_record_id != call_record_id:
            return "conflict"
        desired = (call_record_id, reason)
        existing = self._abandoned_interactions.get(interaction_id)
        if existing is not None:
            return "replayed" if existing[0] == call_record_id else "conflict"
        lifecycle_close = reason.startswith("terminal_winner:") or (
            reason == "superseded_by_new_interaction"
        )
        if not lifecycle_close and interaction_id not in self._eligible_interactions:
            return "conflict"
        if (
            not lifecycle_close
            and (interaction_id, route.interaction_revision) in self._answer_records
        ):
            return "conflict"
        # No await occurs between the answer fence and mutation, so this has
        # the same single-winner semantics as the Mongo conditional update.
        self._eligible_interactions.discard(interaction_id)
        self._abandoned_interactions[interaction_id] = desired
        return "accepted"

    def is_abandoned_for_test(self, interaction_id: str) -> bool:
        return interaction_id in self._abandoned_interactions

    def read_interaction_for_test(
        self, interaction_id: str
    ) -> tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str] | None:
        stored = self._interactions.get(interaction_id)
        if (
            stored is None
            or interaction_id not in self._eligible_interactions
            or interaction_id in self._abandoned_interactions
        ):
            return None
        spec, route, fingerprint = stored
        return (
            A2AInteractionSpec.model_validate(spec.model_dump(mode="python")),
            HITLRouteSnapshotV2.model_validate(route.model_dump(mode="python")),
            fingerprint,
        )

    async def read_interaction(
        self, interaction_id: str
    ) -> tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str] | None:
        return self.read_interaction_for_test(interaction_id)

    async def get_eligible_interactions(
        self, conversation_id: str
    ) -> list[tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str]]:
        interactions = []
        for interaction_id, stored in self._interactions.items():
            if interaction_id not in self._eligible_interactions:
                continue
            if interaction_id in self._abandoned_interactions:
                continue
            spec, route, fingerprint = stored
            if route.conversation_id != conversation_id:
                continue
            interactions.append(
                (
                    A2AInteractionSpec.model_validate(spec.model_dump(mode="python")),
                    HITLRouteSnapshotV2.model_validate(route.model_dump(mode="python")),
                    fingerprint,
                )
            )
        return interactions

    async def get_published_interactions(
        self, conversation_id: str
    ) -> list[tuple[A2AInteractionSpec, HITLRouteSnapshotV2, str]]:
        interactions = []
        for interaction_id, stored in self._interactions.items():
            if interaction_id not in self._eligible_interactions:
                continue
            if interaction_id not in self._published_interactions:
                continue
            if interaction_id in self._abandoned_interactions:
                continue
            spec, route, fingerprint = stored
            if route.conversation_id != conversation_id:
                continue
            if (interaction_id, route.interaction_revision) in self._answer_records:
                continue
            interactions.append(
                (
                    A2AInteractionSpec.model_validate(spec.model_dump(mode="python")),
                    HITLRouteSnapshotV2.model_validate(route.model_dump(mode="python")),
                    fingerprint,
                )
            )
        return interactions

    async def publish(self, interaction_id: str, *, call_record_id: str) -> str:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "error"
        if stored[1].call_record_id != call_record_id:
            return "conflict"
        if interaction_id in self._abandoned_interactions:
            return "conflict"
        if interaction_id in self._published_interactions:
            return "replayed"
        self._published_interactions.add(interaction_id)
        return "accepted"

    async def read_answers(
        self, interaction_id: str, interaction_revision: int
    ) -> list[HITLQuestionAnswer] | None:
        if (
            interaction_id not in self._eligible_interactions
            or interaction_id in self._abandoned_interactions
        ):
            return None
        record = await self.read_answer_record(interaction_id, interaction_revision)
        return list(record.answers) if record is not None else None

    async def read_answer_record(
        self, interaction_id: str, interaction_revision: int
    ) -> DurableHITLAnswerRecord | None:
        record = self._answer_records.get((interaction_id, interaction_revision))
        return _clone_answer_record(record) if record is not None else None

    async def answer(
        self,
        *,
        interaction_id: str,
        interaction_revision: int,
        route_fingerprint: str,
        answers: list[HITLQuestionAnswer],
        authenticated_answerer_id: str,
        verified_auth_reference_digests: list[str],
        verified_auth_references: list[VerifiedAuthReferenceBinding],
    ) -> str:
        stored = self._interactions.get(interaction_id)
        if (
            stored is None
            or interaction_id not in self._eligible_interactions
            or interaction_id in self._abandoned_interactions
        ):
            raise KeyError(interaction_id)
        spec, route, _ = stored
        if (
            interaction_revision != route.interaction_revision
            or route.fingerprint != route_fingerprint
        ):
            raise ValueError("HITL route fingerprint or revision changed")
        spec.validate_answers(answers)
        answer_digest = _digest_json(
            [answer.model_dump(mode="json") for answer in answers]
        )
        desired = DurableHITLAnswerRecord(
            interaction_id=interaction_id,
            interaction_revision=interaction_revision,
            route_fingerprint=route_fingerprint,
            authenticated_answerer_id=authenticated_answerer_id,
            answer_digest=answer_digest,
            answers=answers,
            verified_auth_reference_digests=verified_auth_reference_digests,
            verified_auth_references=verified_auth_references,
            applied_at=datetime.now(UTC),
        )
        key = (interaction_id, interaction_revision)
        existing = self._answer_records.get(key)
        if existing is not None and _answer_identity(existing) != _answer_identity(
            desired
        ):
            raise ValueError("HITL answer identity conflict")
        if existing is None:
            self._answer_records[key] = _clone_answer_record(desired)
        return answer_digest


def _answer_identity(record: DurableHITLAnswerRecord) -> tuple[object, ...]:
    return (
        record.interaction_id,
        record.interaction_revision,
        record.route_fingerprint,
        record.authenticated_answerer_id,
        record.answer_digest,
        tuple(record.answers),
        tuple(record.verified_auth_reference_digests),
        tuple(record.verified_auth_references),
    )


def _clone_answer_record(record: DurableHITLAnswerRecord) -> DurableHITLAnswerRecord:
    return DurableHITLAnswerRecord.model_validate(record.model_dump(mode="python"))


class InMemoryHITLApplicationStore:
    def __init__(self) -> None:
        self._interactions: dict[str, StoredHITLInteraction] = {}
        self._answers: dict[tuple[str, int], DurableHITLAnswerRecord] = {}

    async def ensure_interaction(
        self,
        *,
        interaction_id: str,
        spec: A2AInteractionSpec,
        route: HITLRouteSnapshotV2,
        fingerprint: str,
    ) -> str:
        existing = self._interactions.get(interaction_id)
        if existing is not None:
            if (
                existing.spec == spec
                and existing.route == route
                and existing.fingerprint == fingerprint
            ):
                return "replayed"
            return "conflict"
        self._interactions[interaction_id] = StoredHITLInteraction(
            interaction_id=interaction_id,
            spec=_clone_spec(spec),
            route=_clone_route(route),
            fingerprint=fingerprint,
            eligible=False,
            published=False,
        )
        return "accepted"

    async def load_interaction(
        self, interaction_id: str
    ) -> StoredHITLInteraction | None:
        return self._interactions.get(interaction_id)

    async def get_eligible_interactions(
        self, conversation_id: str
    ) -> list[StoredHITLInteraction]:
        return [
            i
            for i in self._interactions.values()
            if i.route.conversation_id == conversation_id
            and i.eligible
            and i.abandoned is None
        ]

    async def get_published_interactions(
        self, conversation_id: str
    ) -> list[StoredHITLInteraction]:
        return [
            i
            for i in self._interactions.values()
            if i.route.conversation_id == conversation_id
            and i.eligible
            and i.abandoned is None
            and i.published
            and (i.interaction_id, i.route.interaction_revision) not in self._answers
        ]

    async def mark_eligible(self, interaction_id: str) -> str:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "error"
        if stored.eligible:
            return "replayed"
        self._interactions[interaction_id] = StoredHITLInteraction(
            interaction_id=stored.interaction_id,
            spec=stored.spec,
            route=stored.route,
            fingerprint=stored.fingerprint,
            eligible=True,
            abandoned=stored.abandoned,
            published=stored.published,
        )
        return "accepted"

    async def mark_published(self, interaction_id: str) -> str:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "error"
        if stored.published:
            return "replayed"
        self._interactions[interaction_id] = StoredHITLInteraction(
            interaction_id=stored.interaction_id,
            spec=stored.spec,
            route=stored.route,
            fingerprint=stored.fingerprint,
            eligible=stored.eligible,
            abandoned=stored.abandoned,
            published=True,
        )
        return "accepted"

    async def abandon(
        self, interaction_id: str, *, call_record_id: str, reason: str
    ) -> str:
        stored = self._interactions.get(interaction_id)
        if stored is None:
            return "absent"
        if stored.abandoned is not None:
            return "replayed" if stored.abandoned[0] == call_record_id else "conflict"
        lifecycle_close = reason.startswith("terminal_winner:") or (
            reason == "superseded_by_new_interaction"
        )
        if stored.route.call_record_id != call_record_id or (
            not lifecycle_close and not stored.eligible
        ):
            return "conflict"
        if (
            not lifecycle_close
            and (interaction_id, stored.route.interaction_revision) in self._answers
        ):
            return "conflict"
        self._interactions[interaction_id] = StoredHITLInteraction(
            interaction_id=stored.interaction_id,
            spec=stored.spec,
            route=stored.route,
            fingerprint=stored.fingerprint,
            eligible=False,
            abandoned=(call_record_id, reason),
            published=stored.published,
        )
        return "accepted"

    async def load_answer(
        self, interaction_id: str, interaction_revision: int
    ) -> DurableHITLAnswerRecord | None:
        record = self._answers.get((interaction_id, interaction_revision))
        return _clone_answer(record) if record is not None else None

    async def ensure_answer(
        self,
        *,
        interaction_id: str,
        interaction_revision: int,
        record: DurableHITLAnswerRecord,
    ) -> str:
        stored = self._interactions.get(interaction_id)
        if (
            stored is None
            or not stored.eligible
            or stored.abandoned is not None
            or stored.route.interaction_revision != interaction_revision
            or record.interaction_id != interaction_id
            or record.interaction_revision != interaction_revision
            or record.route_fingerprint != stored.route.fingerprint
        ):
            return "conflict"
        key = (interaction_id, interaction_revision)
        existing = self._answers.get(key)
        if existing is not None:
            return (
                "replayed"
                if _answer_identity(existing) == _answer_identity(record)
                else "conflict"
            )
        self._answers[key] = _clone_answer(record)
        return "accepted"


def _clone_answer(record: DurableHITLAnswerRecord) -> DurableHITLAnswerRecord:
    return DurableHITLAnswerRecord.model_validate(record.model_dump(mode="python"))


__all__ = [
    "InMemoryAgentCallLedgerStore",
    "InMemoryAgentToolBindingStore",
    "InMemoryHITLApplicationPort",
    "InMemoryHITLApplicationStore",
    "InMemoryObservationConflictStore",
    "InMemoryObservationInboxStore",
    "InMemoryPreparedInvocationSnapshotReader",
    "InMemoryRoomEpochStore",
]
