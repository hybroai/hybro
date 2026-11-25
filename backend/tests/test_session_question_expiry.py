"""A check rule Question's end reaches the room log, and the Step shows it.

The approval reconciler expires a rule's Question and closes each member with
``INPUT_EXPIRED`` (``reconcile_terminal_interaction``, covered in
``test_hitl_aggregate_validation``). This follows one member from that event
through the real publisher into the room event log, and back out through the
session waiter to the Step view.
"""

from __future__ import annotations

import pytest

from approval.service import HITLService
from models.hitl import HITLEventType, HITLRequest
from session.steps import step_questions
from session.waiter import SessionWaiter
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.test_delivery_event_publisher import make_publisher


def _rule_question(**changes: object) -> HITLRequest:
    return HITLRequest(
        schema_version=3,
        request_id="request-1",
        interaction_id="interaction-1",
        question_index=0,
        question_count=1,
        room_id="room-1",
        user_message_id="message-1",
        orchestration_run_id="run-1",
        application_route="supervisor_run",
        public_source="system",
        evidence_origin="system",
        prompt="Allow Travel to book the hotel?",
        prompt_type="approval",
        client_request_id="cr-1",
        status="expired",
    ).model_copy(update=changes)


async def _asked(store: InMemoryRoomEventStore) -> None:
    """The run started and asked the rule's Question."""
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public={
            "run_id": "run-1",
            "type": "run_started",
            "payload": {"user_message_id": "message-1"},
            "correlation_id": "cr-1",
        },
    )
    await store.append(
        conversation_id="room-1",
        kind="hitl_request",
        payload_public={
            "request_id": "request-1",
            "interaction_id": "interaction-1",
            "prompt": _rule_question().prompt,
            "prompt_type": "approval",
            "source": "system",
            "question_index": 0,
            "client_request_id": "cr-1",
        },
    )


@pytest.mark.asyncio
async def test_an_expired_rule_question_is_an_expired_response_in_the_room_log():
    store = InMemoryRoomEventStore()
    service = HITLService(canonical_run_state_reader=lambda _run_id: "failed")
    service._delivery = make_publisher(room_events=store)

    await service._emit_hitl_event(
        conversation_id="room-1",
        event_type=HITLEventType.INPUT_EXPIRED,
        request=_rule_question(),
    )

    [record] = await store.read_range("room-1", after=0, limit=10)
    assert record["kind"] == "hitl_response"
    payload = record["payload_public"]
    assert payload["status"] == "expired"
    assert payload["run_id"] == "run-1"
    assert payload["client_request_id"] == "cr-1"
    assert payload["interaction_id"] == "interaction-1"
    assert "answered_by_client_id" not in payload
    assert "answered_by" not in payload
    assert "answer_text" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "answered_by", "answer"),
    [("client-1", "client", "Reject"), (None, "owner", "Approve")],
)
async def test_an_answered_rule_question_carries_the_answer_and_who_gave_it(
    client_id, answered_by, answer
):
    store = InMemoryRoomEventStore()
    service = HITLService(canonical_run_state_reader=lambda _run_id: "awaiting_user")
    service._delivery = make_publisher(room_events=store)

    await service._emit_hitl_event(
        conversation_id="room-1",
        event_type=HITLEventType.INPUT_RECEIVED,
        request=_rule_question(
            status="responded", user_input=answer, answered_by_client_id=client_id
        ),
    )

    [record] = await store.read_range("room-1", after=0, limit=10)
    payload = record["payload_public"]
    assert payload["status"] == "responded"
    assert payload["answer_text"] == answer
    assert payload["answered_by"] == answered_by
    assert payload.get("answered_by_client_id") == client_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_type", "answer"),
    [
        # An orchestrator ask_user reply goes through the same service.
        ("text", "Book the cheaper hotel"),
        ("single_choice", "Approve"),
        # A rule's question answered outside its two choices.
        ("approval", "Maybe"),
    ],
)
async def test_only_a_rule_choice_is_public_answer_text(prompt_type, answer):
    store = InMemoryRoomEventStore()
    service = HITLService(canonical_run_state_reader=lambda _run_id: "awaiting_user")
    service._delivery = make_publisher(room_events=store)

    await service._emit_hitl_event(
        conversation_id="room-1",
        event_type=HITLEventType.INPUT_RECEIVED,
        request=_rule_question(
            status="responded", prompt_type=prompt_type, user_input=answer
        ),
    )

    [record] = await store.read_range("room-1", after=0, limit=10)
    payload = record["payload_public"]
    assert payload["status"] == "responded"
    assert payload["answered_by"] == "owner"
    assert "answer_text" not in payload


@pytest.mark.asyncio
async def test_the_step_shows_a_rule_answer_and_who_gave_it():
    store = InMemoryRoomEventStore()
    service = HITLService(canonical_run_state_reader=lambda _run_id: "awaiting_user")
    service._delivery = make_publisher(room_events=store)
    await _asked(store)

    await service._emit_hitl_event(
        conversation_id="room-1",
        event_type=HITLEventType.INPUT_RECEIVED,
        request=_rule_question(
            status="responded", user_input="Reject", answered_by_client_id="client-1"
        ),
    )
    outcomes = await SessionWaiter(store, start_grace_seconds=0).history("room-1")

    [shown] = step_questions(outcomes["cr-1"], ended=True)
    assert shown.outcome == "answered"
    assert [answer.text for answer in shown.answers] == ["Reject"]
    assert (shown.answered_by, shown.answered_by_client_id) == ("client", "client-1")


@pytest.mark.asyncio
async def test_the_step_shows_the_expired_rule_question():
    store = InMemoryRoomEventStore()
    service = HITLService(canonical_run_state_reader=lambda _run_id: "failed")
    service._delivery = make_publisher(room_events=store)
    question = _rule_question()
    # The run asked the question before it expired.
    await _asked(store)

    await service._emit_hitl_event(
        conversation_id="room-1",
        event_type=HITLEventType.INPUT_EXPIRED,
        request=question,
    )
    outcomes = await SessionWaiter(store, start_grace_seconds=0).history("room-1")

    [shown] = step_questions(outcomes["cr-1"], ended=True)
    assert (shown.asked_by, shown.outcome) == ("system", "expired")
    assert shown.answers == []
