"""The owner's Step view: one Step per client request, with its Questions.

A Step is one prompt from a client for one agent, with its result. Every
fact comes from the record that owns it: the prompt and the agent from the
client's message, the state from ``resolve_state``, the timing from the
run's lifecycle events, and the Questions from the ``hitl_request`` and
``hitl_response`` records in the room event log. Nothing is read from
execution's own records.
"""

from __future__ import annotations

from datetime import datetime

from common.observability import events as E
from common.observability import log_id
from common.utils.logger import get_logger
from common.utils.time import ensure_utc
from models.session import (
    QuestionOutcome,
    SessionState,
    SessionStepItem,
    SessionStepQuestion,
)
from session.waiter import AskedQuestion, CorrelatedOutcome

# A response status as the event log writes it, and the outcome it shows.
_RECORDED: dict[str, QuestionOutcome] = {
    "responded": "answered",
    "canceled": "canceled",
    "expired": "expired",
    "error": "error",
}
_ACTIVE_STATES = frozenset({"working", "waiting_on_owner"})

logger = get_logger(__name__)


def step_ended(state: SessionState) -> bool:
    return state not in _ACTIVE_STATES


def question_outcome(question: AskedQuestion, *, ended: bool) -> QuestionOutcome:
    """The recorded outcome; ``unanswered`` when the Step ended without one."""
    if question.status is not None:
        recorded = _RECORDED.get(question.status)
        if recorded is None:
            # A status the log may gain later is shown as an error, and noted.
            logger.emit(
                E.SESSION_QUESTION_STATUS_UNKNOWN,
                question_id=log_id(question.question_id),
                status=log_id(question.status),
            )
            return "error"
        return recorded
    return "unanswered" if ended else "pending"


def step_questions(
    outcome: CorrelatedOutcome | None, *, ended: bool
) -> list[SessionStepQuestion]:
    if outcome is None:
        return []
    return [_question(question, ended=ended) for question in outcome.questions.values()]


def _question(question: AskedQuestion, *, ended: bool) -> SessionStepQuestion:
    ordered = sorted(question.items.items(), key=lambda item: item[1][0])
    labels = {request_id: header for request_id, (_i, header, _p) in ordered}
    return SessionStepQuestion(
        question_id=question.question_id,
        asked_by=question.source,
        prompt_type=question.prompt_type,
        asked_at=question.asked_at,
        items=[
            SessionStepItem(label=header, text=prompt)
            for _request_id, (_index, header, prompt) in ordered
        ],
        outcome=question_outcome(question, ended=ended),
        resolved_at=question.resolved_at,
        answered_by=(
            "client"
            if question.answered_by == "client"
            else "owner"
            if question.answered_by == "owner"
            else None
        ),
        answered_by_client_id=question.answered_by_client_id,
        answers=[
            SessionStepItem(label=labels.get(request_id), text=answer)
            for request_id, answer in sorted(
                question.answers.items(),
                key=lambda item: question.items.get(item[0], (0, None, ""))[0],
            )
        ],
    )


def finished_at(outcome: CorrelatedOutcome | None) -> datetime | None:
    settlement = outcome.settlement if outcome is not None else None
    value = settlement.get("settled_at") if settlement else None
    if isinstance(value, str):
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return ensure_utc(value) if isinstance(value, datetime) else None


def duration_ms(outcome: CorrelatedOutcome | None) -> int | None:
    settlement = outcome.settlement if outcome is not None else None
    value = settlement.get("duration_ms") if settlement else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def label_skill(label: str | None, agent_name: str | None) -> str | None:
    """The skill from the agent's tool label, ``Agent - skill``.

    The agent's name may itself contain `` - ``, so the known name is
    stripped as a prefix; a label that does not start with it has no skill.
    """
    prefix = f"{agent_name} - " if agent_name else None
    if not label or prefix is None or not label.startswith(prefix):
        return None
    return label[len(prefix) :] or None


__all__ = [
    "duration_ms",
    "finished_at",
    "question_outcome",
    "label_skill",
    "step_ended",
    "step_questions",
]
