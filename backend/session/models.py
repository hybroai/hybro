"""Durable session record."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from common.utils.time import ensure_utc


def new_session_id() -> str:
    """An opaque, unguessable session handle."""
    return secrets.token_urlsafe(24)


class SessionRequest(BaseModel):
    """One client message recorded in the session, in send order."""

    model_config = ConfigDict(frozen=True)

    client_request_id: str
    user_message_id: str
    # The client that sent this step; null on a step from before clients.
    client_id: str | None = None
    sent_at: datetime

    @field_validator("sent_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SessionRecord(BaseModel):
    """One client session with the owner's agents.

    ``conversation_id`` is null until the conversation container exists; a
    send reserves the record first and creates the container second, so a
    retry with the same ``creation_request_id`` completes a crashed creation.
    ``last_send_event_seq`` is the room event sequence read before the last
    accepted send; state resolution pages events after it. ``requests`` lists
    every recorded client message, so retries of earlier requests and the
    client's history do not depend on a run having started. ``client_id`` is
    the client that opened the session, which alone may reach it; a session
    from before clients has none and counts as "No token". ``client_label`` is
    the name the client gave, which a request with a token may leave out.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    owner_user_id: str
    client_id: str | None = None
    client_label: str | None = None
    title: str | None = None
    conversation_id: str | None = None
    creation_request_id: str | None = None
    last_client_request_id: str | None = None
    last_user_message_id: str | None = None
    last_accepted_agent_id: str | None = None
    last_send_event_seq: int = 0
    requests: tuple[SessionRequest, ...] = ()
    status: Literal["open", "closed"] = "open"
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None

    def request(self, client_request_id: str) -> SessionRequest | None:
        return next(
            (r for r in self.requests if r.client_request_id == client_request_id),
            None,
        )

    @field_validator("opened_at", "last_activity_at", "closed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        # Mongo returns naive datetimes; they are stored as UTC.
        return ensure_utc(value) if value is not None else None


__all__ = ["SessionRecord", "SessionRequest", "new_session_id"]
