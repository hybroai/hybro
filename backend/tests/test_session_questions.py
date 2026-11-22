"""A granted client sees and answers the permission question of its session.

A permission question is a check rule's ask (source ``system``, prompt type
``approval``) or an Agent's own question of kind ``policy_decision``. The
client sees it only while it holds the owner's grant, and answers it through
the same ``resolve_hitl_batch`` the Panel uses, as the owner, with its own id
recorded. Every other question stays with the owner.

The client registry is the real ``ClientService`` over an in-memory store, so
the grant is read from the stored record on each call.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from approval import HITLConflictError, HITLDeliveryUncertainError
from approval.exceptions import (
    HITLContinuationLostError,
    HITLExpiredError,
    HITLNotFoundError,
    HITLRoutingFailedError,
)
from client.protocols import NO_TOKEN, ClientIdentity
from client.service import ClientService
from common.dto import HITLRequest, HITLResponse
from common.errors import AppError
from session.facade import SessionFacade
from tests.fakes.client import MemoryClientStore
from tests.test_session_facade import (
    OWNER,
    STARTED,
    WAITING,
    Harness,
    make_harness,
)


class RecordingApprovals:
    """The Panel's answer port, recording each call."""

    def __init__(self, harness: Harness) -> None:
        self._base = harness.approvals
        self.resolved: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.on_resolve = None

    async def resolve_hitl_batch(
        self,
        conversation_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        responder_id: str,
        client_request_id: str | None = None,
        answered_by_client_id: str | None = None,
    ) -> HITLResponse:
        if self.on_resolve is not None:
            await self.on_resolve()
        if self.error is not None:
            raise self.error
        self.resolved.append(
            {
                "conversation_id": conversation_id,
                "interaction_id": interaction_id,
                "answers": answers,
                "responder_id": responder_id,
                "client_request_id": client_request_id,
                "answered_by_client_id": answered_by_client_id,
            }
        )
        return HITLResponse(
            request_id=interaction_id,
            status="accepted",
            responder_id=responder_id,
            client_request_id=client_request_id,
        )


class Setup:
    def __init__(self) -> None:
        self.clients = ClientService(MemoryClientStore(), auth_mode="mock")
        self.h = make_harness(on_schedule=[STARTED, WAITING], grants=self.clients)
        self.answers = RecordingApprovals(self.h)
        # The facade holds the same approvals object; answers go to the recorder.
        self.h.approvals.resolve_hitl_batch = self.answers.resolve_hitl_batch
        self.client: ClientIdentity | None = None
        self.tokens: list[str] = []

    async def granted_client(self, *, granted: bool = True) -> ClientIdentity:
        created = await self.clients.create("CI bot")
        self.tokens.append(created.token)
        if granted:
            await self.clients.set_answer_grant(created.client_id, True)
        self.client = ClientIdentity(
            client_id=created.client_id, name=created.name, has_token=True
        )
        return self.client

    async def open_session(self, client: ClientIdentity) -> str:
        receipt = await self.h.send(client=client)
        return receipt.session_id

    def pending(self, *rows: HITLRequest) -> None:
        self.h.approvals.pending = list(rows)

    async def check(self, session_id: str, client: ClientIdentity):
        return await self.h.facade.check(session_id, owner=OWNER, client=client)

    async def answer(
        self, session_id: str, question_id: str, answer: str, client: ClientIdentity
    ):
        from models.session import SessionQuestionAnswer

        return await self.h.facade.answer(
            session_id,
            question_id,
            SessionQuestionAnswer(answer=answer),
            owner=OWNER,
            client=client,
        )


def rule_ask(**updates: Any) -> HITLRequest:
    data: dict[str, Any] = {
        "request_id": "req-1",
        "conversation_id": "conv-1",
        "user_message_id": "msg-1",
        "source": "system",
        "prompt": "Allow this call to Travel Planner?\n\nBook the Lyon hotel",
        "prompt_type": "approval",
        "choices": ["Approve", "Reject"],
        "interaction_id": "int-1",
        "interaction_status": "open",
        "interaction_version": 1,
        "client_request_id": "cr-1",
        "orchestration_run_id": "run-1",
    }
    data.update(updates)
    return HITLRequest(**data)


def agent_decision(**updates: Any) -> HITLRequest:
    data: dict[str, Any] = {
        "source": "agent",
        "prompt": "May I pay the deposit of 120 EUR?",
        "interaction_kind": "policy_decision",
        "agent_name": "Travel Planner",
        "interaction_status": "pending",
    }
    data.update(updates)
    return rule_ask(**data)


def questionnaire(**updates: Any) -> HITLRequest:
    data: dict[str, Any] = {
        "source": "agent",
        "prompt": "Which hotel may I book?",
        "prompt_type": "text",
        "choices": None,
        "interaction_kind": "questionnaire",
        "interaction_status": "pending",
    }
    data.update(updates)
    return rule_ask(**data)


async def setup_with(
    *rows: HITLRequest, granted: bool = True
) -> tuple[Setup, ClientIdentity, str]:
    s = Setup()
    client = await s.granted_client(granted=granted)
    session_id = await s.open_session(client)
    s.pending(*rows)
    return s, client, session_id


async def expect_error(awaitable, code: str) -> AppError:
    with pytest.raises(AppError) as raised:
        await awaitable
    assert raised.value.code == code
    return raised.value


# -- What a client sees -------------------------------------------------------


async def test_a_granted_client_sees_a_rule_ask():
    s, client, session_id = await setup_with(rule_ask())

    status = await s.check(session_id, client)

    assert status.state == "waiting_on_owner"
    question = status.question
    assert question is not None
    assert question.question_id == "int-1"
    assert question.kind == "permission"
    assert question.text == "Allow this call to Travel Planner?\n\nBook the Lyon hotel"
    assert question.agent_id == "agent-travel"
    assert question.agent_name == "Travel Planner"
    assert question.answers == ["approve", "refuse"]


async def test_a_granted_client_sees_an_agents_own_approval_question():
    s, client, session_id = await setup_with(agent_decision())

    question = (await s.check(session_id, client)).question

    assert question is not None
    assert question.question_id == "int-1"
    assert question.text == "May I pay the deposit of 120 EUR?"


async def test_several_permission_questions_of_one_interaction_are_shown_together():
    s, client, session_id = await setup_with(
        agent_decision(question_count=2),
        agent_decision(
            request_id="req-2",
            prompt="May I book the train?",
            question_count=2,
            question_index=1,
        ),
    )

    question = (await s.check(session_id, client)).question

    assert question is not None
    assert question.text == (
        "May I pay the deposit of 120 EUR?\n\nMay I book the train?"
    )


async def test_a_client_without_the_grant_sees_what_it_saw_before():
    s, client, session_id = await setup_with(rule_ask(), granted=False)

    status = await s.check(session_id, client)
    body = status.model_dump(mode="json")

    assert status.state == "waiting_on_owner"
    assert "question" not in body
    assert "Lyon hotel" not in status.model_dump_json()


async def test_the_no_token_client_never_sees_a_question():
    s = Setup()
    session_id = await s.open_session(NO_TOKEN)
    s.pending(rule_ask())

    status = await s.check(session_id, NO_TOKEN)

    assert "question" not in status.model_dump(mode="json")


@pytest.mark.parametrize(
    "row",
    [
        questionnaire(),
        questionnaire(interaction_kind=None),
        questionnaire(interaction_kind="auth_challenge", prompt_type="authentication"),
        # An Agent question without a stored kind is never a permission one,
        # whatever its prompt type reads.
        agent_decision(interaction_kind=None),
        # A supervisor's approval question is not a check rule's ask.
        rule_ask(source="supervisor"),
        # A system question that is not an approval.
        rule_ask(prompt_type="text", choices=None),
    ],
)
async def test_other_questions_are_never_shown(row):
    s, client, session_id = await setup_with(row)

    status = await s.check(session_id, client)

    assert status.question is None
    assert row.prompt not in status.model_dump_json()


async def test_a_mixed_interaction_is_never_shown():
    s, client, session_id = await setup_with(
        rule_ask(question_count=2),
        questionnaire(request_id="req-2", question_count=2, question_index=1),
    )

    status = await s.check(session_id, client)

    assert status.question is None
    assert "Which hotel" not in status.model_dump_json()
    assert "Lyon hotel" not in status.model_dump_json()


async def test_a_question_already_answered_is_not_shown():
    s, client, session_id = await setup_with(
        rule_ask(status="answer_recorded", interaction_status="applying")
    )

    assert (await s.check(session_id, client)).question is None


async def test_a_question_of_an_earlier_request_is_not_shown():
    s, client, session_id = await setup_with(rule_ask(user_message_id="msg-0"))

    assert (await s.check(session_id, client)).question is None


async def test_another_sessions_question_is_not_shown():
    s, client, session_id = await setup_with(rule_ask(conversation_id="conv-9"))

    assert (await s.check(session_id, client)).question is None


async def test_a_failed_grant_read_refuses_the_check():
    s, client, session_id = await setup_with(rule_ask())

    async def broken(_client):
        raise RuntimeError("store down")

    s.clients.may_answer_permission_questions = broken

    await expect_error(s.check(session_id, client), "clients_unavailable")


# -- Answering ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "answer", "text"),
    [
        (rule_ask(), "approve", "Approve"),
        (rule_ask(), "refuse", "Reject"),
        (agent_decision(), "approve", "Approve"),
        (agent_decision(), "refuse", "Reject"),
    ],
)
async def test_an_answer_goes_through_the_panels_path_as_the_owner(row, answer, text):
    s, client, session_id = await setup_with(row)

    result = await s.answer(session_id, "int-1", answer, client)

    assert result.session_id == session_id
    assert result.question_id == "int-1"
    assert result.answer == answer
    assert s.answers.resolved == [
        {
            "conversation_id": "conv-1",
            "interaction_id": "int-1",
            "answers": [{"request_id": "req-1", "user_input": text}],
            "responder_id": OWNER.user_id,
            "client_request_id": "cr-1",
            "answered_by_client_id": client.client_id,
        }
    ]


async def test_every_question_of_the_interaction_takes_the_answer():
    s, client, session_id = await setup_with(
        agent_decision(question_count=2),
        agent_decision(request_id="req-2", question_count=2, question_index=1),
    )

    await s.answer(session_id, "int-1", "refuse", client)

    assert s.answers.resolved[0]["answers"] == [
        {"request_id": "req-1", "user_input": "Reject"},
        {"request_id": "req-2", "user_input": "Reject"},
    ]


async def test_a_client_without_the_grant_is_refused():
    s, client, session_id = await setup_with(rule_ask(), granted=False)

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "answer_not_granted"
    )
    assert s.answers.resolved == []


async def test_a_grant_cleared_after_the_check_is_refused():
    s, client, session_id = await setup_with(rule_ask())
    assert (await s.check(session_id, client)).question is not None

    await s.clients.set_answer_grant(client.client_id, False)

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "answer_not_granted"
    )
    assert s.answers.resolved == []


async def test_a_client_revoked_after_the_check_is_refused():
    s, client, session_id = await setup_with(rule_ask())
    assert (await s.check(session_id, client)).question is not None

    await s.clients.revoke(client.client_id)

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "answer_not_granted"
    )
    assert s.answers.resolved == []


async def test_the_no_token_client_can_never_answer():
    s = Setup()
    session_id = await s.open_session(NO_TOKEN)
    s.pending(rule_ask())

    await expect_error(
        s.answer(session_id, "int-1", "approve", NO_TOKEN), "answer_not_granted"
    )
    assert s.answers.resolved == []


async def test_a_failed_grant_read_refuses_the_answer():
    s, client, session_id = await setup_with(rule_ask())

    async def broken(_client):
        raise RuntimeError("store down")

    s.clients.may_answer_permission_questions = broken

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "clients_unavailable"
    )
    assert s.answers.resolved == []


async def test_unknown_foreign_and_missing_all_read_session_not_found():
    s, client, session_id = await setup_with(rule_ask())
    other = await s.granted_client()
    other_session = await s.open_session(other)
    s.pending(rule_ask())

    foreign = await expect_error(
        s.answer(session_id, "int-1", "approve", other), "session_not_found"
    )
    unknown_session = await expect_error(
        s.answer("sess-404", "int-1", "approve", client), "session_not_found"
    )
    unknown_question = await expect_error(
        s.answer(session_id, "int-404", "approve", client), "session_not_found"
    )
    # The other session has a container of its own; its question is not this one.
    assert other_session != session_id

    assert foreign.message == unknown_session.message == unknown_question.message
    assert s.answers.resolved == []


async def test_a_question_of_another_session_reads_session_not_found():
    s, client, session_id = await setup_with(rule_ask(conversation_id="conv-9"))

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "session_not_found"
    )
    assert s.answers.resolved == []


async def test_a_question_of_an_earlier_request_is_refused_as_not_pending():
    s, client, session_id = await setup_with()
    # The earlier request asked int-0; it was answered and is gone.
    await s.h.events.append(
        conversation_id="conv-1",
        kind="hitl_request",
        payload_public={"request_id": "req-0", "interaction_id": "int-0"},
    )
    s.pending(rule_ask())

    await expect_error(
        s.answer(session_id, "int-0", "approve", client), "question_not_pending"
    )
    assert s.answers.resolved == []


async def test_a_pending_question_of_an_earlier_request_is_refused():
    s, client, session_id = await setup_with(rule_ask(user_message_id="msg-0"))
    await s.h.events.append(
        conversation_id="conv-1",
        kind="hitl_request",
        payload_public={"request_id": "req-1", "interaction_id": "int-1"},
    )

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_not_pending"
    )
    assert s.answers.resolved == []


@pytest.mark.parametrize(
    "row",
    [
        questionnaire(),
        questionnaire(interaction_kind="auth_challenge", prompt_type="authentication"),
        agent_decision(interaction_kind=None),
        rule_ask(source="supervisor"),
    ],
)
async def test_a_question_for_the_owner_only_is_refused(row):
    s, client, session_id = await setup_with(row)

    error = await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_owner_only"
    )

    assert row.prompt not in error.message
    assert s.answers.resolved == []


async def test_a_mixed_interaction_is_refused():
    s, client, session_id = await setup_with(
        rule_ask(question_count=2),
        questionnaire(request_id="req-2", question_count=2, question_index=1),
    )

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_owner_only"
    )
    assert s.answers.resolved == []


async def test_an_interaction_with_a_hidden_member_is_refused():
    """Every member must be listed and pending; a partial list is not enough."""
    s, client, session_id = await setup_with(rule_ask(question_count=2))

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_not_pending"
    )
    assert s.answers.resolved == []


@pytest.mark.parametrize(
    "row",
    [
        rule_ask(status="answer_recorded", interaction_status="applying"),
        rule_ask(interaction_status="partially_answered"),
        rule_ask(interaction_status="delivery_uncertain"),
    ],
)
async def test_a_question_already_answered_is_refused(row):
    s, client, session_id = await setup_with(row)

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_not_pending"
    )
    assert s.answers.resolved == []


@pytest.mark.parametrize(
    "error",
    [
        HITLConflictError("Request req-1 already has a different answer"),
        HITLExpiredError("HITL interaction has expired"),
        HITLNotFoundError("HITL interaction not found"),
        HITLContinuationLostError("Run became non-resumable"),
    ],
)
async def test_an_answer_that_loses_to_another_is_refused_as_not_pending(error):
    s, client, session_id = await setup_with(rule_ask())
    s.answers.error = error

    refused = await expect_error(
        s.answer(session_id, "int-1", "approve", client), "question_not_pending"
    )

    assert str(error) not in refused.message


async def test_an_unconfirmed_delivery_has_its_own_code():
    s, client, session_id = await setup_with(agent_decision())
    s.answers.error = HITLDeliveryUncertainError("internal transport detail")

    refused = await expect_error(
        s.answer(session_id, "int-1", "approve", client), "answer_unconfirmed"
    )

    assert "internal transport detail" not in refused.message


async def test_a_failed_application_has_fixed_text():
    s, client, session_id = await setup_with(rule_ask())
    s.answers.error = HITLRoutingFailedError("mongo said: secret path")

    refused = await expect_error(
        s.answer(session_id, "int-1", "approve", client), "answer_failed"
    )

    assert "secret" not in refused.message


async def test_a_closed_session_refuses_an_answer():
    s, client, session_id = await setup_with(rule_ask())
    await s.h.facade.close(session_id, owner=OWNER, client=client)
    s.pending(rule_ask())

    await expect_error(
        s.answer(session_id, "int-1", "approve", client), "session_closed"
    )
    assert s.answers.resolved == []


async def test_the_grant_is_read_again_for_every_answer():
    s, client, session_id = await setup_with(rule_ask())
    calls: list[str] = []
    original = s.clients.may_answer_permission_questions

    async def counting(identity):
        calls.append(identity.client_id)
        return await original(identity)

    s.clients.may_answer_permission_questions = counting

    # The fake keeps the question pending, so both answers reach the grant read.
    await s.answer(session_id, "int-1", "approve", client)
    await s.answer(session_id, "int-1", "refuse", client)

    assert calls == [client.client_id, client.client_id]


async def test_the_answer_is_logged_by_id_without_the_token(caplog):
    s, client, session_id = await setup_with(rule_ask())
    caplog.set_level(logging.INFO)

    await s.answer(session_id, "int-1", "approve", client)

    records = [
        r for r in caplog.records if "session_question_answered" in r.getMessage()
    ]
    assert records, "the answer is logged"
    text = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert s.tokens and all(token not in text for token in s.tokens)
    assert "hybro_client_" not in text


def test_the_facade_takes_the_grant_port():
    import inspect

    assert "grants" in inspect.signature(SessionFacade.__init__).parameters
