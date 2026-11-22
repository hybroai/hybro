"""Public payloads for client sessions with the owner's agents.

A session is a conversation that a client of the instance owner opens through
the API or MCP. The client addresses one agent per message; the owner approves
or rejects agent interactions in the Hybro app. The client never sees the
conversation container id or the approval question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from common.idempotency import MAX_CLIENT_REQUEST_ID_LENGTH
from common.utils.time import ensure_utc
from models.room import MAX_MESSAGE_LENGTH

SessionState = Literal[
    "completed",
    "working",
    "waiting_on_owner",
    "rejected",
    "denied",
    "failed",
    "closed",
]

SessionIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]
ClientRequestId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_CLIENT_REQUEST_ID_LENGTH
    ),
]
ClientLabel = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
SessionText = Annotated[str, StringConstraints(max_length=MAX_MESSAGE_LENGTH)]


class SessionMessageRequest(BaseModel):
    """One client message to one agent, opening a session when no handle is given."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionIdentifier | None = None
    agent_id: SessionIdentifier
    text: SessionText
    subnet_id: SessionIdentifier | None = None
    client_request_id: ClientRequestId
    client_label: ClientLabel | None = Field(
        default=None,
        description=(
            "The name the client gives itself. Required to open a session "
            "without a client token; deprecated with a token, where the "
            "client's name is used."
        ),
    )

    @model_validator(mode="after")
    def _require_text(self) -> Self:
        # client_label is checked by the session: it is required to open a
        # session only without a client token.
        if not self.text.strip():
            raise ValueError("text must not be blank")
        return self


class SessionClient(BaseModel):
    """The client that opened a session. "No token" has no token."""

    client_id: str
    name: str
    has_token: bool


class SessionAgent(BaseModel):
    agent_id: str
    name: str | None = None


def _utc(value: datetime | None) -> datetime | None:
    # Stored times come back naive; they are UTC.
    return ensure_utc(value) if value is not None else None


SessionReceiptStatus = Literal["created", "accepted", "busy"]

RECEIPT_TIP = (
    "Use session_id to query this session (MCP check_session, or "
    "GET /sessions/{session_id}) for the agents' responses."
)


class SessionMessageResponse(BaseModel):
    """The receipt for one client message, returned as soon as Hybro has it.

    ``created`` opened the session, ``accepted`` recorded a message in an
    existing session, and ``busy`` means the message was not recorded because
    an earlier one is still active. Agents' responses come from the query.
    """

    session_id: str
    client_request_id: str
    agent_id: str
    status: SessionReceiptStatus
    state: SessionState
    message: str
    created_at: datetime
    updated_at: datetime

    _utc_times = field_validator("created_at", "updated_at")(_utc)


class SessionHistoryEntry(BaseModel):
    """One client message and what came of it.

    It never carries the owner's decisions: a request waiting for the owner
    only shows ``waiting_on_owner``.
    """

    client_request_id: str
    text: str
    sent_at: datetime | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    state: SessionState
    reason: str | None = None
    reply: str | None = None
    replied_at: datetime | None = None

    _utc_times = field_validator("sent_at", "replied_at")(_utc)


SessionAnswer = Literal["approve", "refuse"]


def _answers() -> list[SessionAnswer]:
    return ["approve", "refuse"]


class SessionQuestion(BaseModel):
    """A permission question the client may answer, shown only with the grant.

    A permission question is a check rule's ask or an Agent's own approval
    question. Every other question stays with the owner and is never shown.
    """

    question_id: str = Field(description="Answer it with this id.")
    kind: Literal["permission"] = "permission"
    text: str = Field(
        description="The question; several questions of one ask are separated "
        "by a blank line, and one answer applies to all of them."
    )
    agent_id: str | None = Field(default=None, description="The agent it concerns.")
    agent_name: str | None = None
    answers: list[SessionAnswer] = Field(default_factory=_answers)


class SessionStatus(BaseModel):
    """The client's view of a session. It never carries the container id."""

    session_id: str
    state: SessionState
    reason: str | None = None
    agents: list[SessionAgent] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    history: list[SessionHistoryEntry] = Field(default_factory=list)
    question: SessionQuestion | None = Field(
        default=None,
        description="The permission question waiting for an answer. Present "
        "only for a client the owner allowed to answer permission questions; "
        "absent otherwise.",
        exclude_if=lambda value: value is None,
    )

    _utc_times = field_validator("created_at", "updated_at")(_utc)


class SessionQuestionAnswer(BaseModel):
    """A client's answer to a permission question of its session."""

    model_config = ConfigDict(extra="forbid", strict=True)

    answer: SessionAnswer


class SessionQuestionAnswered(BaseModel):
    """The answer was accepted; the session's state shows what follows."""

    session_id: str
    question_id: str
    answer: SessionAnswer
    status: Literal["accepted"] = "accepted"


class SessionClosed(BaseModel):
    session_id: str
    status: Literal["open", "closed"]


class SessionSummary(BaseModel):
    """One row of the owner's session list."""

    session_id: str
    client: SessionClient
    client_label: str | None = None
    title: str | None = None
    agents: list[SessionAgent] = Field(default_factory=list)
    state: SessionState
    paused_agent_id: str | None = None
    conversation_id: str | None = None
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary] = Field(default_factory=list)


class SessionOwnerRecord(BaseModel):
    """The owner's view of one session, including the conversation container."""

    session_id: str
    client: SessionClient
    client_label: str | None = None
    title: str | None = None
    state: SessionState
    reason: str | None = None
    paused_agent_id: str | None = None
    agents: list[SessionAgent] = Field(default_factory=list)
    status: Literal["open", "closed"]
    conversation_id: str | None = None
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None


class SessionErrorDetail(BaseModel):
    code: str
    message: str
    session_id: str | None = None
    client_request_id: str | None = None
    question_id: str | None = None


class SessionErrorResponse(BaseModel):
    detail: SessionErrorDetail


class SessionInvalidRequestDetail(BaseModel):
    """A 422 of the answer route: fixed text and the fields at fault."""

    code: Literal["invalid_request"]
    message: str
    fields: list[str]


class SessionInvalidRequest(BaseModel):
    detail: SessionInvalidRequestDetail


__all__ = [
    "RECEIPT_TIP",
    "SessionAgent",
    "SessionClosed",
    "SessionErrorDetail",
    "SessionErrorResponse",
    "SessionInvalidRequest",
    "SessionInvalidRequestDetail",
    "SessionListResponse",
    "SessionMessageRequest",
    "SessionMessageResponse",
    "SessionOwnerRecord",
    "SessionHistoryEntry",
    "SessionAnswer",
    "SessionQuestion",
    "SessionQuestionAnswer",
    "SessionQuestionAnswered",
    "SessionReceiptStatus",
    "SessionClient",
    "SessionState",
    "SessionStatus",
    "SessionSummary",
]
