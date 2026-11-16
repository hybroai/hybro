from __future__ import annotations

import pytest

from common.dto.hitl import (
    A2AInteractionSpec,
    HITLInteractionKind,
    HITLQuestionAnswer,
    HITLQuestionSpec,
    HITLTextAnswer,
)
from execution.adapters.hitl import DurableHITLApplicationPort
from execution.orchestrator.a2a_runtime.models import DurableHITLAnswerRecord
from tests.orchestrator_harness.a2a_in_memory import InMemoryHITLApplicationStore

from ._orchestrator_a2a_helpers import ledger_record


def _interaction(interaction_id="interaction-1"):
    return A2AInteractionSpec(
        schema_version=1,
        interaction_id=interaction_id,
        questions=[
            HITLQuestionSpec(
                question_id="q-1",
                interaction_kind=HITLInteractionKind.QUESTIONNAIRE,
                prompt="What is your name?",
                answer_kind="text",
                required=True,
            )
        ],
    )


def _answer():
    return HITLQuestionAnswer(question_id="q-1", answer=HITLTextAnswer(text="Ada"))


def _waiting_call():
    return ledger_record(state="input_required").model_copy(
        update={
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fingerprint-1",
        }
    )


async def test_create_or_replay_persists_interaction():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    interaction_id = await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    assert interaction_id == "interaction-1"
    # Replay with the same identity is idempotent.
    assert (
        await port.create_or_replay(
            call=_waiting_call(),
            interaction=_interaction(),
            interaction_fingerprint="fingerprint-1",
        )
        == "interaction-1"
    )


async def test_create_or_replay_conflict_raises():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    with pytest.raises(ValueError, match="identity conflict"):
        await port.create_or_replay(
            call=_waiting_call(),
            interaction=_interaction(),
            interaction_fingerprint="fingerprint-2",
        )


async def test_activate_and_read_interaction():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=ledger_record(state="working"),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    assert (
        await port.activate(
            "interaction-1",
            call_record_id=ledger_record().call_record_id,
            interaction_fingerprint="fingerprint-1",
        )
        == "accepted"
    )
    assert (
        await port.activate(
            "interaction-1",
            call_record_id=ledger_record().call_record_id,
            interaction_fingerprint="fingerprint-1",
        )
        == "replayed"
    )
    spec, route, fingerprint = await port.read_interaction("interaction-1")
    assert spec.interaction_id == "interaction-1"
    assert fingerprint == "fingerprint-1"


async def test_abandon_blocks_read_and_marks_state():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    call_record_id = _waiting_call().call_record_id
    assert (
        await port.abandon(
            "interaction-1", call_record_id=call_record_id, reason="room deleted"
        )
        == "accepted"
    )
    assert await port.read_interaction("interaction-1") is None
    assert (
        await port.abandon(
            "interaction-1", call_record_id=call_record_id, reason="room deleted"
        )
        == "replayed"
    )


async def test_answer_validates_and_persists_idempotently():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    route_fingerprint = (await port.read_interaction("interaction-1"))[1].fingerprint
    digest = await port.answer(
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route_fingerprint,
        answers=[_answer()],
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    assert digest
    record = await port.read_answer_record("interaction-1", 1)
    assert isinstance(record, DurableHITLAnswerRecord)
    assert record.answer_digest == digest
    # Replay the identical answer returns the same digest.
    assert (
        await port.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route_fingerprint,
            answers=[_answer()],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )
        == digest
    )
    # A changed answer conflicts.
    with pytest.raises(ValueError, match="identity conflict"):
        await port.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route_fingerprint,
            answers=[
                HITLQuestionAnswer(question_id="q-1", answer=HITLTextAnswer(text="Bob"))
            ],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )


async def test_answer_inventory_mismatch_raises():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    await port.create_or_replay(
        call=_waiting_call(),
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )
    route_fingerprint = (await port.read_interaction("interaction-1"))[1].fingerprint
    with pytest.raises(ValueError, match="inventory"):
        await port.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route_fingerprint,
            answers=[],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )


async def test_publish_marks_interaction_user_visible():
    port = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    call = _waiting_call()
    await port.create_or_replay(
        call=call,
        interaction=_interaction(),
        interaction_fingerprint="fingerprint-1",
    )

    # Parked (eligible but unpublished) is NOT user-visible.
    assert await port.get_published_interactions(call.conversation_id) == []
    # The unanswered interaction is still answerable/eligible.
    assert len(await port.get_eligible_interactions(call.conversation_id)) == 1

    # Publish requires exact call ownership.
    assert (
        await port.publish("interaction-1", call_record_id="wrong-call") == "conflict"
    )
    assert (
        await port.publish("interaction-1", call_record_id=call.call_record_id)
        == "accepted"
    )

    published = await port.get_published_interactions(call.conversation_id)
    assert [spec.interaction_id for spec, _route, _fp in published] == ["interaction-1"]

    # A durable answer removes user actionability immediately while retaining
    # eligibility for continuation recovery.
    route_fingerprint = published[0][1].fingerprint
    await port.answer(
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route_fingerprint,
        answers=[_answer()],
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    assert await port.get_published_interactions(call.conversation_id) == []
    assert len(await port.get_eligible_interactions(call.conversation_id)) == 1

    # Publishing again is idempotent.
    assert (
        await port.publish("interaction-1", call_record_id=call.call_record_id)
        == "replayed"
    )
    assert await port.publish("missing", call_record_id=call.call_record_id) == "error"
