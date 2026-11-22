"""Permission questions a client with the owner's grant sees and answers.

A permission question is a check rule's ask (asked by the system, prompt type
``approval``) or an Agent's own question of the stored kind
``policy_decision``. Whether a question is one is read from the stored
interaction, never from the request or from any text. A ``questionnaire``,
an ``auth_challenge``, an untyped Agent question and a supervisor's question
always stay with the owner: the client never sees their text.

The answer goes through the Panel's own ``resolve_hitl_batch``, as the owner,
with the client's id recorded. Approve and refuse are what the Panel's
Approve and Reject send. The store's pending claim decides a race with the
owner; the loser is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from approval import (
    HITLConflictError,
    HITLContinuationLostError,
    HITLDeliveryUncertainError,
    HITLExpiredError,
    HITLNotFoundError,
    HITLRoomMismatchError,
)
from client.protocols import ClientIdentity
from common.dto import HITLRequest
from common.errors import AppError
from common.observability import events as E
from common.observability import log_id
from common.protocols import HITLManager
from common.utils.logger import get_logger
from models.session import (
    SessionAnswer,
    SessionQuestion,
    SessionQuestionAnswered,
)
from session.models import SessionRecord
from session.protocols import AnswerGrants, SessionOwner
from session.waiter import SessionWaiter

logger = get_logger(__name__)

# What the Panel's Approve and Reject send for an approval question.
PANEL_ANSWER: dict[SessionAnswer, str] = {"approve": "Approve", "refuse": "Reject"}

# The approval aggregate reads ``open`` until an answer is recorded; an
# Agent's typed question reads ``pending`` until it is answered.
_OPEN_INTERACTION = frozenset({"open", "pending"})

NOT_FOUND = ("session_not_found", "Session not found")
NOT_GRANTED = (
    "answer_not_granted",
    "This client may not answer permission questions. The owner can allow it "
    "on the Connect page, Clients tab.",
)
OWNER_ONLY = ("question_owner_only", "Only the owner can answer this question.")
NOT_PENDING = (
    "question_not_pending",
    "The question is no longer waiting for an answer. Check the session for its state.",
)
CLIENTS_UNAVAILABLE = ("clients_unavailable", "Clients are unavailable.")
QUESTIONS_UNAVAILABLE = (
    "questions_unavailable",
    "The session's questions cannot be read right now; try again.",
)
UNCONFIRMED = (
    "answer_unconfirmed",
    "The answer was recorded, but its delivery to the agent is not confirmed "
    "yet. Check the session for its state.",
)
FAILED = ("answer_failed", "The answer could not be applied; try again.")

# A refusal from the answer path that means someone else answered first, the
# Question ended, or its Run did.
_LOST = (
    HITLConflictError,
    HITLExpiredError,
    HITLNotFoundError,
    HITLContinuationLostError,
)


def _error(pair: tuple[str, str]) -> AppError:
    code, message = pair
    return AppError(message, code=code)


def is_permission_question(request: HITLRequest) -> bool:
    """A check rule's ask, or an Agent's own question of kind policy_decision."""
    if request.prompt_type != "approval":
        return False
    if request.source == "system":
        return True
    return request.source == "agent" and request.interaction_kind == "policy_decision"


@dataclass(frozen=True, slots=True)
class PendingInteraction:
    """Every member of one interaction the session's latest request waits on."""

    interaction_id: str
    rows: tuple[HITLRequest, ...]

    @property
    def permission(self) -> bool:
        return all(is_permission_question(row) for row in self.rows)

    @property
    def open(self) -> bool:
        """Nothing is answered yet, and every member is listed."""
        count = self.rows[0].question_count
        return (
            len(self.rows) == count
            and sorted(row.question_index for row in self.rows) == list(range(count))
            and all(
                row.status == "pending" and row.interaction_status in _OPEN_INTERACTION
                for row in self.rows
            )
        )

    @property
    def text(self) -> str:
        ordered = sorted(self.rows, key=lambda row: row.question_index)
        return "\n\n".join(row.prompt for row in ordered)


class PermissionQuestions:
    def __init__(
        self,
        *,
        approvals: HITLManager,
        grants: AnswerGrants,
        waiter: SessionWaiter,
    ) -> None:
        self._approvals = approvals
        self._grants = grants
        self._waiter = waiter

    async def granted(self, client: ClientIdentity) -> bool:
        """The stored grant, read now; a failed read refuses the request."""
        try:
            return await self._grants.may_answer_permission_questions(client)
        except Exception as exc:
            logger.emit(
                E.SESSION_ANSWER_GRANT_READ_FAILED,
                exc=exc,
                client_id=log_id(client.client_id),
            )
            raise _error(CLIENTS_UNAVAILABLE) from exc

    async def shown_to(self, client: ClientIdentity) -> bool:
        """Whether a check shows the question; a failed read shows none."""
        try:
            return await self.granted(client)
        except AppError:
            # granted() logged the failure; the client sees its plain status.
            return False

    async def visible(self, record: SessionRecord) -> SessionQuestion | None:
        """The one open permission question; the caller checked the grant.

        Several interactions waiting at once show none, as it is not clear
        which one the client would be answering.
        """
        groups = await self._pending(record)
        if len(groups) != 1:
            return None
        (current,) = groups.values()
        if not current.permission or not current.open:
            return None
        return SessionQuestion(question_id=current.interaction_id, text=current.text)

    async def answer(
        self,
        record: SessionRecord,
        question_id: str,
        answer: SessionAnswer,
        *,
        owner: SessionOwner,
        client: ClientIdentity,
    ) -> SessionQuestionAnswered:
        """Apply the answer as the owner; the caller checked the grant."""
        assert record.conversation_id is not None
        current = (await self._pending(record)).get(question_id)
        if current is None:
            if await self._asked(record, question_id):
                raise _error(NOT_PENDING)
            raise _error(NOT_FOUND)
        if not current.permission:
            raise _error(OWNER_ONLY)
        if not current.open:
            raise _error(NOT_PENDING)
        session_ref = log_id(record.session_id)
        question_ref = log_id(question_id)
        client_ref = log_id(client.client_id)
        try:
            await self._approvals.resolve_hitl_batch(
                record.conversation_id,
                question_id,
                [
                    {"request_id": row.request_id, "user_input": PANEL_ANSWER[answer]}
                    for row in sorted(current.rows, key=lambda r: r.question_index)
                ],
                owner.user_id,
                client_request_id=current.rows[0].client_request_id,
                answered_by_client_id=client.client_id,
            )
        except _LOST as exc:
            raise _error(NOT_PENDING) from exc
        except HITLRoomMismatchError as exc:
            raise _error(NOT_FOUND) from exc
        except HITLDeliveryUncertainError as exc:
            logger.emit(
                E.SESSION_QUESTION_ANSWER_FAILED,
                exc=exc,
                session_id=session_ref,
                question_id=question_ref,
                client_id=client_ref,
            )
            raise _error(UNCONFIRMED) from exc
        except Exception as exc:
            logger.emit(
                E.SESSION_QUESTION_ANSWER_FAILED,
                exc=exc,
                session_id=session_ref,
                question_id=question_ref,
                client_id=client_ref,
            )
            raise _error(FAILED) from exc
        logger.emit(
            E.SESSION_QUESTION_ANSWERED,
            session_id=session_ref,
            question_id=question_ref,
            client_id=client_ref,
            answer=answer,
        )
        return SessionQuestionAnswered(
            session_id=record.session_id, question_id=question_id, answer=answer
        )

    async def _pending(self, record: SessionRecord) -> dict[str, PendingInteraction]:
        """The interactions the session's latest request waits on, by id."""
        conversation_id = record.conversation_id
        message_id = record.last_user_message_id
        if not conversation_id or not message_id:
            return {}
        try:
            pending = await self._approvals.get_pending_hitl(conversation_id)
        except Exception as exc:
            logger.emit(
                E.SESSION_QUESTIONS_READ_FAILED,
                exc=exc,
                session_id=log_id(record.session_id),
            )
            raise _error(QUESTIONS_UNAVAILABLE) from exc
        groups: dict[str, list[HITLRequest]] = {}
        for row in pending:
            if (
                row.conversation_id == conversation_id
                and row.user_message_id == message_id
                and row.interaction_id
            ):
                groups.setdefault(row.interaction_id, []).append(row)
        return {
            interaction_id: PendingInteraction(
                interaction_id=interaction_id, rows=tuple(rows)
            )
            for interaction_id, rows in groups.items()
        }

    async def _asked(self, record: SessionRecord, question_id: str) -> bool:
        assert record.conversation_id is not None
        try:
            return await self._waiter.asked(record.conversation_id, question_id)
        except Exception as exc:
            logger.emit(
                E.SESSION_QUESTIONS_READ_FAILED,
                exc=exc,
                session_id=log_id(record.session_id),
            )
            raise _error(QUESTIONS_UNAVAILABLE) from exc


__all__ = [
    "PANEL_ANSWER",
    "PermissionQuestions",
    "PendingInteraction",
    "is_permission_question",
]
