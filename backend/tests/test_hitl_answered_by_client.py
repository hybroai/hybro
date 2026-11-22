"""``approval`` records which client answered, and the answer applies once.

A client with the grant answers as the owner; its id is stored next to the
owner the answer was applied as. The id is part of the answer's identity, so
when the owner in the Panel and a client answer at the same moment with the
same text, the one that loses the pending claim is refused instead of being
taken for a retry of the winner.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from approval.exceptions import HITLConflictError
from models.hitl import HITLInteractionStatus, HITLRequest
from tests.test_rule_ask_answer import _coordinator, _interaction_for, _rule_question


async def _answer(coordinator, service, text, *, client_id=None):
    return await coordinator.handle_batch_response(
        service,
        conversation_id="room-1",
        interaction_id="interaction-1",
        answers=[{"request_id": "request-1", "user_input": text}],
        user_id="user-1",
        answered_by_client_id=client_id,
    )


def _answered(row, *, client_id):
    return {
        **row,
        "status": "answer_recorded",
        "user_input": "Approve",
        "answer_digest": None,
        "responded_by_user_id": "user-1",
        "answered_by_client_id": client_id,
    }


async def test_the_claim_records_the_answering_client():
    row = _rule_question()
    coordinator, service, _lifecycle = _coordinator(row, _interaction_for(row))

    with pytest.raises(Exception) as raised:
        await _answer(coordinator, service, "Approve", client_id="client_ci")

    assert not isinstance(raised.value, HITLConflictError)
    claim = service.persistence.claim_hitl_request.await_args
    assert claim.kwargs["responded_by_user_id"] == "user-1"
    assert claim.kwargs["answered_by_client_id"] == "client_ci"


async def test_the_owners_claim_records_no_client():
    row = _rule_question()
    coordinator, service, _lifecycle = _coordinator(row, _interaction_for(row))

    with pytest.raises(Exception) as raised:
        await _answer(coordinator, service, "Approve")

    assert not isinstance(raised.value, HITLConflictError)
    claim = service.persistence.claim_hitl_request.await_args
    assert claim.kwargs["answered_by_client_id"] is None


@pytest.mark.parametrize(
    ("winner", "loser"),
    [(None, "client_ci"), ("client_ci", None), ("client_ci", "client_other")],
)
async def test_the_same_answer_from_another_answerer_loses(winner, loser):
    row = _rule_question()
    coordinator, service, lifecycle = _coordinator(row, _interaction_for(row))
    answered = _answered(row, client_id=winner)
    answered["answer_digest"] = _digest("Approve")
    service.persistence.claim_hitl_request = AsyncMock(return_value=None)
    service.persistence.get_hitl_request = AsyncMock(side_effect=[row, answered])

    with pytest.raises(HITLConflictError):
        await _answer(coordinator, service, "Approve", client_id=loser)

    lifecycle.record_interaction_answer.assert_not_awaited()


@pytest.mark.parametrize("answerer", [None, "client_ci"])
async def test_a_retry_by_the_same_answerer_still_replays(answerer):
    row = _rule_question()
    coordinator, service, _lifecycle = _coordinator(row, _interaction_for(row))
    answered = _answered(row, client_id=answerer)
    answered["answer_digest"] = _digest("Approve")
    service.persistence.claim_hitl_request = AsyncMock(return_value=None)
    service.persistence.get_hitl_request = AsyncMock(side_effect=[row, answered])

    # Past the identity check, the fake's lifecycle refuses the record.
    with pytest.raises(Exception) as raised:
        await _answer(coordinator, service, "Approve", client_id=answerer)

    assert not isinstance(raised.value, HITLConflictError)


def test_the_stored_row_keeps_the_answering_client():
    row = _answered(_rule_question(), client_id="client_ci")
    row.pop("answer_digest")

    assert HITLRequest(**row).answered_by_client_id == "client_ci"


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _apply_reads(latest_status, *, client_id):
    """Record the last answer; the application reads ``canceled``."""
    row = _rule_question()
    interaction = _interaction_for(row)
    coordinator, service, lifecycle = _coordinator(row, interaction)
    recorded = {
        **interaction,
        "answer_request_ids": ["request-1"],
        "status": HITLInteractionStatus.ANSWERS_RECORDED.value,
    }
    latest = {**recorded, "status": latest_status}
    lifecycle.record_interaction_answer = AsyncMock(return_value=recorded)
    lifecycle.get_interaction_strict = AsyncMock(
        side_effect=[interaction, recorded, latest]
    )
    coordinator.apply_interaction = AsyncMock(
        return_value={"request_id": "request-1", "status": "canceled"}
    )
    return await _answer(coordinator, service, "Approve", client_id=client_id)


async def test_a_client_answer_applied_meanwhile_reads_accepted():
    """Another worker applied it before the Run ended: it counted."""
    result = await _apply_reads(HITLInteractionStatus.APPLIED.value, client_id="c")

    assert result["status"] == "accepted"


async def test_a_client_answer_closed_with_its_run_reads_canceled():
    result = await _apply_reads(HITLInteractionStatus.FAILED.value, client_id="c")

    assert result["status"] == "canceled"


async def test_the_panels_canceled_answer_reads_as_before():
    result = await _apply_reads(HITLInteractionStatus.APPLIED.value, client_id=None)

    assert result["status"] == "canceled"
