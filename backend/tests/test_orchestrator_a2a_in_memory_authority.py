from __future__ import annotations

import pytest

from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer
from execution.orchestrator.a2a_runtime.models import (
    A2AObservationInboxRecord,
    NormalizedA2AObservation,
)
from tests.orchestrator_harness.a2a_in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryHITLApplicationPort,
    InMemoryObservationInboxStore,
    InMemoryPreparedInvocationSnapshotReader,
)

from ._orchestrator_a2a_helpers import ledger_record, prepared
from ._orchestrator_helpers import NOW


async def test_ledger_primary_records_and_secondary_indexes_are_private_copies():
    store = InMemoryAgentCallLedgerStore()
    record = ledger_record()
    await store.insert(record)
    records, indexes = store.read_authority_for_test()
    records[record.call_record_id].resource_manifest.refs.append(
        record.resource_manifest.refs[0] if record.resource_manifest.refs else None
    )
    indexes["invocation"].clear()
    assert not hasattr(store, "records")
    assert not hasattr(store, "by_invocation")
    loaded = await store.load(record.run_id, record.invocation_id)
    assert loaded == record
    _, fresh_indexes = store.read_authority_for_test()
    assert fresh_indexes["invocation"] == {
        (record.run_id, record.invocation_id): record.call_record_id
    }


async def test_inbox_primary_and_source_index_test_views_are_defensive():
    ledger = ledger_record()
    observation = NormalizedA2AObservation(
        observation_id="observation-1",
        call_record_id=ledger.call_record_id,
        source_kind="direct",
        source_identity="direct:observation-1",
        binding_scope="endpoint",
        event_kind="working",
        observed_at=NOW,
    )
    record = A2AObservationInboxRecord(
        observation_id=observation.observation_id,
        source_kind=observation.source_kind,
        source_identity=observation.source_identity,
        payload_digest="payload",
        received_at=NOW,
        binding_scope=observation.binding_scope,
        conversation_id="room-1",
        room_epoch=1,
        call_record_id=ledger.call_record_id,
        event_kind=observation.event_kind,
        observation=observation,
    )
    store = InMemoryObservationInboxStore()
    await store.insert(record)
    records, source_index = store.read_authority_for_test()
    records.clear()
    source_index.clear()
    assert not hasattr(store, "records")
    assert not hasattr(store, "by_source")
    assert await store.load(record.observation_id) == record
    assert await store.load_by_source_identity(record.source_identity) == record


def test_prepared_snapshot_authority_is_private_and_defensive():
    reader = InMemoryPreparedInvocationSnapshotReader()
    snapshot = prepared()
    reader.put(snapshot)
    returned = reader.read_snapshot_for_test(snapshot.run_id, snapshot.invocation_id)
    returned.binding.input_modes.append("mutated")
    changed = snapshot.model_copy(
        update={
            "requesting_subject_id": "attacker",
        }
    )
    assert reader.put(changed) == "conflict"
    assert not hasattr(reader, "snapshots")
    fresh = reader.read_snapshot_for_test(snapshot.run_id, snapshot.invocation_id)
    assert fresh.binding.input_modes == snapshot.binding.input_modes


async def test_hitl_interactions_and_answers_are_private_and_defensive():
    owner = InMemoryHITLApplicationPort()
    call = ledger_record()
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "interaction-1",
            "questions": [
                {
                    "question_id": "q1",
                    "interaction_kind": "questionnaire",
                    "prompt": "Choose",
                    "answer_kind": "single_choice",
                    "choices": ["a", "b"],
                }
            ],
        }
    )
    await owner.create_or_replay(
        call=call, interaction=spec, interaction_fingerprint="fingerprint"
    )
    assert (
        await owner.activate(
            spec.interaction_id,
            call_record_id=call.call_record_id,
            interaction_fingerprint="fingerprint",
        )
        == "accepted"
    )
    _, route, _ = owner.read_interaction_for_test(spec.interaction_id)
    interaction_copy = owner.read_interaction_for_test(spec.interaction_id)
    with pytest.raises(TypeError, match="immutable"):
        interaction_copy[0].questions.clear()
    assert not hasattr(owner, "interactions")
    assert owner.read_interaction_for_test(spec.interaction_id)[0].questions

    answers = [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "q1",
                "answer": {"kind": "single_choice", "choice": "a"},
            }
        )
    ]
    await owner.answer(
        interaction_id=spec.interaction_id,
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=answers,
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    answer_copy = await owner.read_answer_record(spec.interaction_id, 1)
    answer_copy.answers.clear()
    assert not hasattr(owner, "answer_records")
    assert (await owner.read_answer_record(spec.interaction_id, 1)).answers == answers
