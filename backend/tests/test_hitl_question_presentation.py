"""Structured agent questions: headers, described choices, Other, optional.

An agent may ask several short questions in one interaction, each with a
header, suggested choices with descriptions, and a free-text "Other" answer.
These fields travel from the A2A interaction spec to the pending API, the SSE
``hitl_request`` frame, and back into typed answers for the agent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from a2a_adapter.orchestrator_direct_client import _continuation_text
from api_gateway.routes.hitl_routes import _pending_hitl_public_payload
from approval.exceptions import HITLConflictError
from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer, HITLQuestionSpec
from delivery.translator import to_sse_frame
from execution.orchestrator.a2a_runtime.interaction_outcome import (
    emit_hitl_request_events,
)
from execution.orchestrator_routing import DualRuntimeRouter, _map_interaction_answers
from tests.orchestrator_harness.a2a_in_memory import InMemoryHITLApplicationPort

from ._orchestrator_a2a_helpers import ledger_record


def structured_spec() -> A2AInteractionSpec:
    return A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "trip-1",
            "questions": [
                {
                    "question_id": "destination",
                    "interaction_kind": "questionnaire",
                    "header": "Destination",
                    "prompt": "Where would you like to go?",
                    "answer_kind": "single_choice",
                    "choices": [
                        {"value": "Kyoto", "description": "Temples and food"},
                        {"value": "Osaka", "label": "Osaka city"},
                    ],
                    "allow_other": True,
                },
                {
                    "question_id": "interests",
                    "interaction_kind": "questionnaire",
                    "header": "Interests",
                    "prompt": "What do you enjoy?",
                    "answer_kind": "multi_choice",
                    "choices": ["Food", "Temples"],
                    "allow_other": True,
                    "required": False,
                },
                {
                    "question_id": "budget",
                    "interaction_kind": "questionnaire",
                    "prompt": "Any budget?",
                    "answer_kind": "text",
                    "required": False,
                },
            ],
        }
    )


def answer(question_id: str, value: dict) -> HITLQuestionAnswer:
    return HITLQuestionAnswer.model_validate(
        {"question_id": question_id, "answer": value}
    )


# ── Protocol ────────────────────────────────────────────────────────────


def test_described_choices_keep_values_and_presentation():
    destination = structured_spec().questions[0]

    assert destination.header == "Destination"
    assert destination.choices == ["Kyoto", "Osaka"]
    assert [option.model_dump() for option in destination.choice_options] == [
        {"value": "Kyoto", "label": None, "description": "Temples and food"},
        {"value": "Osaka", "label": "Osaka city", "description": None},
    ]
    assert destination.allow_other is True


def test_plain_string_choices_carry_no_presentation():
    interests = structured_spec().questions[1]
    assert interests.choices == ["Food", "Temples"]
    assert interests.choice_options is None


def test_questions_without_presentation_keep_their_earlier_dump():
    # Persisted interaction fingerprints hash this dump.
    question = HITLQuestionSpec(
        question_id="q1",
        interaction_kind="questionnaire",
        prompt="Which island?",
        answer_kind="text",
    )
    assert question.model_dump(mode="json") == {
        "question_id": "q1",
        "interaction_kind": "questionnaire",
        "prompt": "Which island?",
        "answer_kind": "text",
        "required": True,
        "choices": None,
    }


def test_structured_spec_round_trips_through_its_dump():
    spec = structured_spec()
    assert A2AInteractionSpec.model_validate(spec.model_dump(mode="json")) == spec


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"header": "x" * 41}, "header"),
        ({"header": "   "}, "header"),
        ({"answer_kind": "text", "choices": None, "allow_other": True}, "Other"),
        (
            {"choices": [{"value": "Kyoto"}, {"value": "Kyoto"}]},
            "unique",
        ),
        ({"choices": [{"value": "Kyoto", "description": "x" * 501}, "Osaka"]}, "500"),
    ],
)
def test_invalid_presentation_is_rejected(overrides, message):
    question = {
        "question_id": "destination",
        "interaction_kind": "questionnaire",
        "prompt": "Where?",
        "answer_kind": "single_choice",
        "choices": ["Kyoto", "Osaka"],
        **overrides,
    }
    with pytest.raises(ValidationError, match=message):
        HITLQuestionSpec.model_validate(question)


def test_other_answers_are_accepted_only_when_allowed():
    spec = structured_spec()
    destination, interests, _ = spec.questions

    destination.validate_answer(
        answer("destination", {"kind": "single_choice", "other": "Lisbon"})
    )
    interests.validate_answer(
        answer(
            "interests",
            {"kind": "multi_choice", "choices": ["Food"], "other": "Hiking"},
        )
    )
    interests.validate_answer(
        answer("interests", {"kind": "multi_choice", "choices": [], "other": "Hiking"})
    )

    closed = destination.model_copy(update={"allow_other": False})
    with pytest.raises(ValueError, match="Other"):
        closed.validate_answer(
            answer("destination", {"kind": "single_choice", "other": "Lisbon"})
        )


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "single_choice"},
        {"kind": "single_choice", "choice": "Kyoto", "other": "Lisbon"},
        {"kind": "multi_choice", "choices": []},
    ],
)
def test_choice_answers_need_exactly_one_kind_of_selection(value):
    with pytest.raises(ValidationError):
        answer("destination", value)


def test_optional_questions_may_be_skipped_but_required_ones_may_not():
    spec = structured_spec()
    destination = answer("destination", {"kind": "single_choice", "choice": "Kyoto"})

    spec.validate_answers([destination])

    with pytest.raises(ValueError, match="required"):
        spec.validate_answers([answer("budget", {"kind": "text", "text": "1500 USD"})])
    with pytest.raises(ValueError, match="inventory"):
        spec.validate_answers(
            [destination, answer("unknown", {"kind": "text", "text": "x"})]
        )
    with pytest.raises(ValueError, match="once"):
        spec.validate_answers([destination, destination])


@pytest.mark.asyncio
async def test_application_port_accepts_skipped_optional_questions():
    owner = InMemoryHITLApplicationPort()
    call = ledger_record().model_copy(
        update={"a2a_task_id": "task-1", "a2a_context_id": "context-1"}
    )
    interaction_id = await owner.create_or_replay(
        call=call, interaction=structured_spec(), interaction_fingerprint="fp"
    )
    await owner.activate(
        interaction_id, call_record_id=call.call_record_id, interaction_fingerprint="fp"
    )
    _, route, _ = owner.read_interaction_for_test(interaction_id)
    await owner.publish(interaction_id, call_record_id=call.call_record_id)

    result = await owner.answer(
        interaction_id=interaction_id,
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=[answer("destination", {"kind": "single_choice", "other": "Lisbon"})],
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )

    assert result


# ── Answer mapping from the UI's text answers ───────────────────────────


def test_ui_text_maps_to_choices_or_other():
    mapped = _map_interaction_answers(
        structured_spec(),
        [
            {"request_id": "destination", "user_input": "Lisbon"},
            {"request_id": "interests", "user_input": "Food, Hiking, Temples"},
        ],
    )

    assert [item.model_dump(mode="json") for item in mapped] == [
        {
            "question_id": "destination",
            "answer": {"kind": "single_choice", "choice": None, "other": "Lisbon"},
        },
        {
            "question_id": "interests",
            "answer": {
                "kind": "multi_choice",
                "choices": ["Food", "Temples"],
                "other": "Hiking",
            },
        },
    ]


def test_ui_text_matching_a_choice_selects_it():
    [mapped] = _map_interaction_answers(
        structured_spec(), [{"request_id": "destination", "user_input": "Kyoto"}]
    )
    assert mapped.answer.choice == "Kyoto"
    assert mapped.answer.other is None


def test_ui_must_still_answer_required_questions():
    with pytest.raises(HITLConflictError, match="changed before submission"):
        _map_interaction_answers(
            structured_spec(), [{"request_id": "budget", "user_input": "1500"}]
        )


def test_agent_receives_other_answers_as_text():
    text = _continuation_text(
        [
            answer("destination", {"kind": "single_choice", "other": "Lisbon"}),
            answer(
                "interests",
                {"kind": "multi_choice", "choices": ["Food"], "other": "Hiking"},
            ),
        ]
    )
    assert text == "destination: Lisbon\ninterests: Food, Hiking"


# ── Public delivery ─────────────────────────────────────────────────────


def canonical_run() -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(user_message_id="user-1"),
        client_request_id="cr-1",
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(call_id="call-1", opaque_public_call_id="inv_1")
                ]
            )
        ],
        tool_catalog=SimpleNamespace(
            entries=[
                SimpleNamespace(
                    definition=SimpleNamespace(name="agent_abc", label="Travel"),
                    agent_display_name="Travel Planner Agent",
                )
            ]
        ),
    )


EXPECTED_DESTINATION = {
    "header": "Destination",
    "required": True,
    "allow_other": True,
    "choices": ["Kyoto", "Osaka"],
    "choice_options": [
        {"value": "Kyoto", "description": "Temples and food"},
        {"value": "Osaka", "label": "Osaka city"},
    ],
}


@pytest.mark.asyncio
async def test_hitl_request_frames_carry_the_presentation():
    emitted: list = []
    delivery = SimpleNamespace(
        emit=AsyncMock(side_effect=lambda event: emitted.append(event) or True)
    )

    await emit_hitl_request_events(
        record=ledger_record(run_id="run-1", call_id="call-1"),
        interaction=structured_spec(),
        interaction_id="trip-1",
        hitl_delivery=delivery,
        run_store=SimpleNamespace(load=AsyncMock(return_value=canonical_run())),
        canonical_control=AsyncMock(),
    )

    frames = [
        to_sse_frame(event, timestamp=datetime(2026, 10, 15, tzinfo=UTC))["data"]
        for event in emitted
    ]
    assert {key: frames[0].get(key) for key in EXPECTED_DESTINATION} == (
        EXPECTED_DESTINATION
    )
    assert frames[1]["required"] is False
    assert "choice_options" not in frames[1]
    assert "header" not in frames[2]
    assert frames[2]["allow_other"] is False


@pytest.mark.asyncio
async def test_pending_questions_carry_the_presentation():
    record = ledger_record(run_id="run-1", call_id="call-1").model_copy(
        update={"state": "input_required", "pending_interaction_id": "trip-1"}
    )
    router = DualRuntimeRouter.__new__(DualRuntimeRouter)
    router._runtime = SimpleNamespace(
        hitl_port=SimpleNamespace(
            get_published_interactions=AsyncMock(
                return_value=[
                    (
                        structured_spec(),
                        SimpleNamespace(
                            orchestration_run_id="run-1",
                            call_record_id=record.call_record_id,
                            interaction_revision=1,
                        ),
                        "fp",
                    )
                ]
            )
        ),
        run_store=SimpleNamespace(load=AsyncMock(return_value=canonical_run())),
        call_ledger=SimpleNamespace(load_by_record_id=AsyncMock(return_value=record)),
        public_secret_values=(),
    )

    pending = await router.get_pending_hitl("room-1")
    payloads = [_pending_hitl_public_payload(request) for request in pending]

    assert {key: payloads[0].get(key) for key in EXPECTED_DESTINATION} == (
        EXPECTED_DESTINATION
    )
    assert payloads[1]["required"] is False
    assert payloads[2]["allow_other"] is False
    assert "header" not in payloads[2]


# ── Approve or Reject ───────────────────────────────────────────────────


def approval_spec(**question: object) -> A2AInteractionSpec:
    return A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "share-1",
            "questions": [
                {
                    "question_id": "share",
                    "interaction_kind": "policy_decision",
                    "prompt": "Share floor-plan.pdf with the client?",
                    "answer_kind": "policy_decision",
                    **question,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("ui_text", "decision"), [("Approve", "approve"), ("Reject", "deny")]
)
def test_ui_approve_and_reject_answer_a_policy_decision(ui_text, decision):
    [mapped] = _map_interaction_answers(
        approval_spec(), [{"request_id": "share", "user_input": ui_text}]
    )
    assert mapped.answer.kind == "policy_decision"
    assert mapped.answer.decision.value == decision
    approval_spec().validate_answers([mapped])


def test_unrecognized_policy_decision_text_is_a_conflict():
    with pytest.raises(HITLConflictError, match="Approve or Reject"):
        _map_interaction_answers(
            approval_spec(), [{"request_id": "share", "user_input": "maybe"}]
        )


@pytest.mark.parametrize(
    ("ui_text", "confirmed"), [("Confirm", True), ("Decline", False)]
)
def test_ui_confirm_and_decline_answer_a_confirmation(ui_text, confirmed):
    spec = A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": "confirm-1",
            "questions": [
                {
                    "question_id": "go",
                    "interaction_kind": "questionnaire",
                    "prompt": "Book it?",
                    "answer_kind": "confirmation",
                }
            ],
        }
    )
    [mapped] = _map_interaction_answers(
        spec, [{"request_id": "go", "user_input": ui_text}]
    )
    assert mapped.answer.confirmed is confirmed


# ── The owner's answers on the record ───────────────────────────────────


@pytest.mark.asyncio
async def test_resolved_frames_carry_a_readable_answer_per_question():
    from execution.orchestrator.a2a_runtime.interaction_outcome import (
        emit_hitl_resolved_events,
    )

    emitted: list = []
    delivery = SimpleNamespace(
        emit=AsyncMock(side_effect=lambda event: emitted.append(event) or True)
    )
    run = canonical_run()
    run.status = "awaiting_user"

    await emit_hitl_resolved_events(
        record=ledger_record(run_id="run-1", call_id="call-1"),
        interaction=structured_spec(),
        interaction_id="trip-1",
        status="responded",
        hitl_delivery=delivery,
        run_store=SimpleNamespace(load=AsyncMock(return_value=run)),
        canonical_control=AsyncMock(),
        answers=[
            answer("destination", {"kind": "single_choice", "choice": "Osaka"}),
            answer(
                "interests",
                {"kind": "multi_choice", "choices": ["Food"], "other": "Hiking"},
            ),
        ],
    )

    frames = {
        frame["request_id"]: frame
        for frame in (
            to_sse_frame(event, timestamp=datetime(2026, 10, 15, tzinfo=UTC))["data"]
            for event in emitted
        )
    }
    # Choice labels are shown instead of raw values.
    assert frames["destination"]["answer_text"] == "Osaka city"
    assert frames["interests"]["answer_text"] == "Food, Hiking"
    # A skipped optional question has no answer.
    assert "answer_text" not in frames["budget"]
    # The owner answered; no client is named.
    assert {frame["answered_by"] for frame in frames.values()} == {"owner"}
    assert all("answered_by_client_id" not in frame for frame in frames.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "by", "named"),
    [("responded", "client", "client-1"), ("canceled", None, None)],
)
async def test_resolved_frames_name_the_client_that_answered(status, by, named):
    from execution.orchestrator.a2a_runtime.interaction_outcome import (
        emit_hitl_resolved_events,
    )

    emitted: list = []
    delivery = SimpleNamespace(
        emit=AsyncMock(side_effect=lambda event: emitted.append(event) or True)
    )
    run = canonical_run()
    run.status = "awaiting_user"

    await emit_hitl_resolved_events(
        record=ledger_record(run_id="run-1", call_id="call-1"),
        interaction=structured_spec(),
        interaction_id="trip-1",
        status=status,
        hitl_delivery=delivery,
        run_store=SimpleNamespace(load=AsyncMock(return_value=run)),
        canonical_control=AsyncMock(),
        answered_by_client_id="client-1",
    )

    frames = [
        to_sse_frame(event, timestamp=datetime(2026, 10, 15, tzinfo=UTC))["data"]
        for event in emitted
    ]
    assert frames
    assert {frame.get("answered_by") for frame in frames} == {by}
    assert {frame.get("answered_by_client_id") for frame in frames} == {named}


@pytest.mark.parametrize(
    ("question", "value", "text"),
    [
        (
            {"interaction_kind": "policy_decision", "answer_kind": "policy_decision"},
            {"kind": "policy_decision", "decision": "approve"},
            "Approved",
        ),
        (
            {"interaction_kind": "policy_decision", "answer_kind": "policy_decision"},
            {"kind": "policy_decision", "decision": "deny"},
            "Rejected",
        ),
        (
            {"interaction_kind": "questionnaire", "answer_kind": "confirmation"},
            {"kind": "confirmation", "confirmed": True},
            "Yes",
        ),
        (
            {
                "interaction_kind": "auth_challenge",
                "answer_kind": "authorization_result",
            },
            {"kind": "authorization_result", "authorization_reference": "authref:a1"},
            "Signed in",
        ),
    ],
)
def test_readable_answers_never_expose_references(question, value, text):
    from execution.orchestrator.a2a_runtime.hitl_prompt import public_answer_text

    spec = HITLQuestionSpec.model_validate(
        {"question_id": "q", "prompt": "?", **question}
    )
    assert public_answer_text(spec, answer("q", value).answer, secret_values=()) == text


def test_model_answer_text_keeps_choice_values_and_hides_references():
    from execution.orchestrator.a2a_runtime.hitl_prompt import model_answer_text

    assert (
        model_answer_text(
            answer("q", {"kind": "multi_choice", "choices": ["a"], "other": "b"}).answer
        )
        == "a, b"
    )
    assert (
        model_answer_text(
            answer(
                "q",
                {
                    "kind": "authorization_result",
                    "authorization_reference": "authref:a1",
                },
            ).answer
        )
        == "Signed in"
    )
