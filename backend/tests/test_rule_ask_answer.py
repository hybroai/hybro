"""An answer to a check rule's question is one of two fixed strings.

``approval`` refuses any other text before anything is recorded, so the
Question stays pending and the owner can answer again (row 8).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api_gateway.routes.hitl_routes import respond_to_hitl_interaction
from approval.application import HITLApplicationCoordinator
from approval.exceptions import HITLConflictError
from models.hitl import HITLBatchResponseRequest
from tests.test_hitl_milestone2 import _interaction, _request_doc


def _rule_question(**updates):
    row = _request_doc(
        source="system",
        prompt="Allow this call to Echo?",
        prompt_type="approval",
        choices=["Approve", "Reject"],
        source_step_id="call-1",
    )
    row.update(updates)
    return row


_INVENTORY_FIELDS = (
    "request_id",
    "prompt",
    "prompt_type",
    "choices",
    "source_step_id",
)


def _interaction_for(row, **updates):
    """The interaction that created exactly this one question."""
    member = {field: row[field] for field in _INVENTORY_FIELDS if field in row}
    return _interaction(
        source=row["public_source"], creation_inventory=[member], **updates
    )


def _coordinator(row, interaction):
    persistence = MagicMock()
    persistence.get_hitl_request = AsyncMock(return_value=row)
    persistence.claim_hitl_request = AsyncMock(return_value=dict(row))
    lifecycle = MagicMock()
    lifecycle.get_interaction_strict = AsyncMock(return_value=interaction)
    lifecycle.record_interaction_answer = AsyncMock(return_value=None)
    coordinator = HITLApplicationCoordinator(lifecycle=lifecycle)
    return coordinator, SimpleNamespace(persistence=persistence), lifecycle


async def _answer(coordinator, service, text):
    return await coordinator.handle_batch_response(
        service,
        conversation_id="room-1",
        interaction_id="interaction-1",
        answers=[{"request_id": "request-1", "user_input": text}],
        user_id="user-1",
    )


@pytest.mark.parametrize("text", ["approve", "Yes", "Approve please", "Rejected"])
async def test_any_other_answer_is_refused_before_it_is_recorded(text):
    row = _rule_question()
    coordinator, service, lifecycle = _coordinator(row, _interaction_for(row))

    with pytest.raises(HITLConflictError):
        await _answer(coordinator, service, text)

    service.persistence.claim_hitl_request.assert_not_awaited()
    lifecycle.record_interaction_answer.assert_not_awaited()


@pytest.mark.parametrize("text", ["Approve", "Reject", "  Approve  "])
async def test_the_two_choices_are_recorded(text):
    row = _rule_question()
    coordinator, service, _lifecycle = _coordinator(row, _interaction_for(row))

    # The recording itself fails in this fake; it is reached, which is the point.
    with pytest.raises(Exception) as raised:
        await _answer(coordinator, service, text)

    assert not isinstance(raised.value, HITLConflictError)
    claim = service.persistence.claim_hitl_request.await_args
    assert claim.kwargs["user_input"] == text.strip()


async def test_a_supervisor_approval_takes_any_answer_as_before():
    row = {
        **_rule_question(),
        "public_source": "supervisor",
        "evidence_origin": "supervisor",
    }
    coordinator, service, _lifecycle = _coordinator(row, _interaction_for(row))

    with pytest.raises(Exception) as raised:
        await _answer(coordinator, service, "Sure, go ahead")

    assert not isinstance(raised.value, HITLConflictError)
    service.persistence.claim_hitl_request.assert_awaited_once()


async def test_the_answer_route_refuses_a_bad_answer_with_a_conflict(
    mock_user, sample_room
):
    row = _rule_question(room_id=sample_room.conversation_id)
    coordinator, service, lifecycle = _coordinator(
        row, _interaction_for(row, room_id=sample_room.conversation_id)
    )

    async def resolve(conversation_id, interaction_id, answers, user_id, _client):
        return await coordinator.handle_batch_response(
            service,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            answers=answers,
            user_id=user_id,
        )

    manager = MagicMock()
    manager.resolve_hitl_batch = AsyncMock(side_effect=resolve)
    ownership = MagicMock()
    ownership.get_room_owner = AsyncMock(return_value=mock_user.user_id)
    body = HITLBatchResponseRequest.model_validate(
        {
            "interaction_id": "interaction-1",
            "answers": [{"request_id": "request-1", "user_input": "maybe"}],
        }
    )

    with pytest.raises(HTTPException) as refused:
        await respond_to_hitl_interaction(
            sample_room.conversation_id,
            body,
            mock_user,
            manager=manager,
            room_ownership=ownership,
        )

    assert refused.value.status_code == 409
    assert "maybe" not in str(refused.value.detail)
    service.persistence.claim_hitl_request.assert_not_awaited()
    lifecycle.record_interaction_answer.assert_not_awaited()


async def test_an_answer_that_finds_the_run_busy_reads_as_accepted(monkeypatch):
    from approval.exceptions import HITLRunBusyError
    from tests.test_approval_logging import _aggregate, _rows

    aggregate = {**_aggregate(), "status": "answers_recorded"}
    rows = [
        {**row, "status": "answer_recorded", "user_input": "Approve"} for row in _rows()
    ]
    lifecycle = MagicMock()
    lifecycle.claim_interaction_application = AsyncMock(return_value=aggregate)
    lifecycle.mark_interaction_application_state = AsyncMock(return_value=aggregate)
    application = HITLApplicationCoordinator(lifecycle=lifecycle)
    application.repair_persisted_answer_refs = AsyncMock(return_value=aggregate)
    application._request_rows = AsyncMock(return_value=rows)
    application.finalize_applied = AsyncMock(return_value=True)
    application._apply_supervisor = AsyncMock(
        side_effect=HITLRunBusyError("the Run is executing")
    )
    monkeypatch.setattr(
        "approval.application.validate_route_classifications", lambda *_a: None
    )
    service = MagicMock()
    service.canonical_run_allows_resume = AsyncMock(return_value=True)
    service.emit_canonical_resumed_control = AsyncMock()

    result = await application.apply_interaction(service, aggregate)

    assert result["status"] == "accepted"
    # Left applying, so the reconciler applies it once the pass ends.
    marked = lifecycle.mark_interaction_application_state.await_args
    assert marked.kwargs["status"] == "applying"
    assert marked.kwargs["error"] == "run_busy"


def test_a_pending_rule_question_reaches_the_common_request():
    """The pending list carries a check rule's question as asked by the system.

    ``ExecutionFacade.get_pending_hitl`` translates every row; a ``system``
    source used to fail validation there.
    """
    from approval.service import _public_hitl_request_from_doc
    from approval.translators import model_hitl_request_to_common

    common = model_hitl_request_to_common(
        _public_hitl_request_from_doc(_rule_question())
    )

    assert common.source == "system"
    assert common.prompt_type == "approval"
