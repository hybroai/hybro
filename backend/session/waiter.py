"""What the durable event log says about a session's requests.

The durable room event log is the only source. Lifecycle events are records of
kind ``run_event`` whose ``payload_public`` carries the run's ``type``,
``run_id``, ``correlation_id`` (the request's ``client_request_id``) and
``payload``. Owner decisions arrive as ``hitl_response`` records carrying
``status`` and ``run_id``. The waiter matches on the correlation id, so a
retried request finds the earlier outcome.

Questions are ``hitl_request`` records, and how each ended is the
``hitl_response`` record of the same request. Both carry the request's
``client_request_id``, so a Question belongs to the Step that asked it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.utils.time import ensure_utc
from session.protocols import ConversationEventsPort

_RUN_EVENT = "run_event"
_HITL_RESPONSE = "hitl_response"
_HITL_REQUEST = "hitl_request"
_SETTLED = "run_settled"
_WAITING_INPUT = "run_waiting_input"
_RESUMED = "run_resumed"
_STARTED = "run_started"
_TOOL_START = "tool_execution_start"
# The passthrough kernel's internal tool that forwards questions to the owner.
_SURFACE_TOOL = "surface_agent_questions"


@dataclass(slots=True)
class AskedQuestion:
    """One Question (an interaction) as the event log recorded it.

    ``items`` and ``answers`` are keyed by the member's request id; ``status``
    is the first response's status, ``None`` while nothing ended it.
    """

    question_id: str
    source: str | None = None
    prompt_type: str | None = None
    asked_at: datetime | None = None
    # request id -> (question index, header, prompt)
    items: dict[str, tuple[int, str | None, str]] = field(default_factory=dict)
    status: str | None = None
    resolved_at: datetime | None = None
    answers: dict[str, str] = field(default_factory=dict)
    # The client that answered as the owner; None when the owner answered.
    answered_by_client_id: str | None = None

    def note_request(self, payload: dict[str, Any], at: datetime | None) -> None:
        if self.asked_at is None:
            self.asked_at = at
        self.source = self.source or _text(payload.get("source"))
        self.prompt_type = self.prompt_type or _text(payload.get("prompt_type"))
        request_id = _text(payload.get("request_id"))
        prompt = payload.get("prompt")
        if request_id and isinstance(prompt, str):
            index = payload.get("question_index")
            self.items[request_id] = (
                index if isinstance(index, int) else len(self.items),
                _text(payload.get("header")),
                prompt,
            )

    def note_response(self, payload: dict[str, Any], at: datetime | None) -> None:
        status = _text(payload.get("status"))
        if self.status is None and status:
            self.status = status
            self.resolved_at = at
            if status == "responded":
                self.answered_by_client_id = _text(payload.get("answered_by_client_id"))
        request_id = _text(payload.get("request_id"))
        answer = _text(payload.get("answer_text"))
        if request_id and answer and status == "responded":
            self.answers[request_id] = answer


@dataclass(slots=True)
class CorrelatedOutcome:
    """What the event log says about one request's run."""

    seen: bool = False
    run_id: str | None = None
    last_type: str | None = None
    # Paused from run_waiting_input until run_resumed or settlement; the kernel
    # emits further tool events after the pause, so the last type is not enough.
    paused: bool = False
    settlement: dict[str, Any] | None = None
    rejected: bool = False
    last_seq: int = 0
    # When the request's run last changed (answers, resumes, settlement).
    last_event_at: datetime | None = None
    started_at: datetime | None = None
    user_message_id: str | None = None
    # The called agent's tool label, "Agent name - skill".
    agent_label: str | None = None
    not_started: bool = False
    # The request's Questions by interaction id, in the order they were asked.
    questions: dict[str, AskedQuestion] = field(default_factory=dict)
    _canceled_hitl_runs: set[str] = field(default_factory=set)

    @property
    def waiting_input(self) -> bool:
        return self.settlement is None and self.paused

    def absorb(self, record: dict[str, Any], correlation_id: str) -> None:
        self.last_seq = max(self.last_seq, int(record.get("room_seq") or 0))
        payload = record.get("payload_public")
        if not isinstance(payload, dict):
            return
        kind = record.get("kind")
        if kind in (_HITL_REQUEST, _HITL_RESPONSE):
            self._absorb_hitl(kind, payload, record, correlation_id)
            return
        if kind != _RUN_EVENT or payload.get("correlation_id") != correlation_id:
            return
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return
        run_id = payload.get("run_id")
        at = _event_time(record.get("ts"))
        if at is not None and (self.last_event_at is None or at > self.last_event_at):
            self.last_event_at = at
        self.seen = True
        self.run_id = run_id if isinstance(run_id, str) else self.run_id
        self.last_type = event_type
        inner = payload.get("payload")
        self._note_details(event_type, inner if isinstance(inner, dict) else {}, at)
        if event_type == _WAITING_INPUT:
            self.paused = True
        elif event_type == _RESUMED:
            self.paused = False
        if event_type == _SETTLED:
            self.paused = False
            settlement = payload.get("payload")
            self.settlement = settlement if isinstance(settlement, dict) else {}
            # A reject is an owner HITL cancel on this run before settlement.
            self.rejected = (
                self.settlement.get("status") == "canceled"
                and self.run_id in self._canceled_hitl_runs
            )

    def _absorb_hitl(
        self,
        kind: str,
        payload: dict[str, Any],
        record: dict[str, Any],
        correlation_id: str,
    ) -> None:
        self._note_question(kind, payload, record, correlation_id)
        if kind == _HITL_RESPONSE:
            run_id = payload.get("run_id")
            if payload.get("status") == "canceled" and isinstance(run_id, str):
                self._canceled_hitl_runs.add(run_id)

    def _note_question(
        self,
        kind: str,
        payload: dict[str, Any],
        record: dict[str, Any],
        correlation_id: str,
    ) -> None:
        if payload.get("client_request_id") != correlation_id:
            return
        question_id = _text(payload.get("interaction_id")) or _text(
            payload.get("request_id")
        )
        if question_id is None:
            return
        at = _event_time(record.get("ts"))
        if kind == _HITL_REQUEST:
            question = self.questions.setdefault(
                question_id, AskedQuestion(question_id=question_id)
            )
            question.note_request(payload, at)
            return
        question = self.questions.get(question_id)
        if question is not None:
            question.note_response(payload, at)

    def _note_details(
        self, event_type: str, inner: dict[str, Any], at: datetime | None
    ) -> None:
        """The request's message, start time, and called agent."""
        if event_type == _STARTED:
            message_id = inner.get("user_message_id")
            if isinstance(message_id, str) and message_id:
                self.user_message_id = message_id
            self.started_at = _event_time(inner.get("started_at")) or at
        elif event_type == _TOOL_START and self.agent_label is None:
            tool = inner.get("tool_name")
            if isinstance(tool, str) and tool and tool != _SURFACE_TOOL:
                self.agent_label = tool


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_time(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return ensure_utc(value) if isinstance(value, datetime) else None


def _absorb_into(
    outcomes: dict[str, CorrelatedOutcome], record: dict[str, Any]
) -> None:
    payload = record.get("payload_public")
    if not isinstance(payload, dict):
        return
    if record.get("kind") == _HITL_RESPONSE:
        # The reject check matches owner frames on the run id.
        for correlation_id, outcome in outcomes.items():
            outcome.absorb(record, correlation_id)
        return
    if record.get("kind") == _HITL_REQUEST:
        # A Question follows its request's run_started, so the outcome exists.
        correlation_id = payload.get("client_request_id")
        known = (
            outcomes.get(correlation_id) if isinstance(correlation_id, str) else None
        )
        if known is not None:
            known.absorb(record, correlation_id)
        return
    correlation_id = payload.get("correlation_id")
    if record.get("kind") == _RUN_EVENT and isinstance(correlation_id, str):
        outcomes.setdefault(correlation_id, CorrelatedOutcome()).absorb(
            record, correlation_id
        )


class SessionWaiter:
    """Reads a conversation's event log; it never waits for a run."""

    def __init__(
        self,
        events: ConversationEventsPort,
        *,
        start_grace_seconds: float,
        page_size: int = 500,
    ) -> None:
        self._events = events
        self.start_grace_seconds = start_grace_seconds
        self._page_size = page_size

    async def latest_seq(self, conversation_id: str) -> int:
        return await self._events.latest_seq(conversation_id)

    async def scan(
        self, conversation_id: str, correlation_id: str, *, after: int
    ) -> CorrelatedOutcome:
        """Read every event after ``after`` once, without waiting."""
        outcome = CorrelatedOutcome(last_seq=after)
        await self._read_new(conversation_id, correlation_id, outcome)
        return outcome

    async def history(self, conversation_id: str) -> dict[str, CorrelatedOutcome]:
        """Every request's outcome, keyed by correlation id, in one pass."""
        outcomes: dict[str, CorrelatedOutcome] = {}
        cursor = 0
        while True:
            page = await self._events.read_range(
                conversation_id, after=cursor, limit=self._page_size
            )
            for record in page:
                _absorb_into(outcomes, record)
            last = max((int(r.get("room_seq") or 0) for r in page), default=cursor)
            if len(page) < self._page_size or last <= cursor:
                return outcomes
            cursor = last

    async def asked(self, conversation_id: str, interaction_id: str) -> bool:
        """Whether the conversation ever showed a question of this interaction."""
        cursor = 0
        while True:
            page = await self._events.read_range(
                conversation_id, after=cursor, limit=self._page_size
            )
            for record in page:
                payload = record.get("payload_public")
                if (
                    record.get("kind") == _HITL_REQUEST
                    and isinstance(payload, dict)
                    and payload.get("interaction_id") == interaction_id
                ):
                    return True
            last = max((int(r.get("room_seq") or 0) for r in page), default=cursor)
            if len(page) < self._page_size or last <= cursor:
                return False
            cursor = last

    async def _read_new(
        self, conversation_id: str, correlation_id: str, outcome: CorrelatedOutcome
    ) -> None:
        while True:
            cursor = outcome.last_seq
            records = await self._events.read_range(
                conversation_id, after=cursor, limit=self._page_size
            )
            for record in records:
                outcome.absorb(record, correlation_id)
            if len(records) < self._page_size or outcome.last_seq <= cursor:
                return


__all__ = ["AskedQuestion", "CorrelatedOutcome", "SessionWaiter"]
