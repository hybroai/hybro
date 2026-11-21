"""Check rules: the stored record and the payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
)

from common.utils.time import ensure_utc

# What a check rule answers. "ask" arrives with the Questions it holds.
RuleAnswer = Literal["allow", "deny"]
# The kind of rule. Route rules are a later phase.
RuleKind = Literal["check"]

# The owner writes each rule by hand; create refuses a rule past this, so the
# list stays one small read. Two creates at once can each pass the check, so
# the list may hold a few more; it is never cut short (a dropped deny rule
# would allow a call), and an order may name up to twice as many.
MAX_RULES = 100
MAX_ORDER = 2 * MAX_RULES

AgentId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]
RuleId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


def _utc(value: datetime) -> datetime:
    # Mongo returns naive datetimes; they are stored as UTC.
    return ensure_utc(value)


class CheckRule(BaseModel):
    """One check rule (collection ``rules``).

    ``agent_id`` is the one agent it matches, or null for any agent. A rule
    whose ``enabled`` is false (off) never matches. ``position`` orders the
    list; it can have gaps after a delete.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    kind: RuleKind = "check"
    agent_id: str | None
    answer: RuleAnswer
    enabled: bool
    position: int
    created_at: datetime
    updated_at: datetime

    _utc_times = field_validator("created_at", "updated_at")(_utc)


class RuleCreate(BaseModel):
    """A new rule: its condition, answer and state. ``agent_id`` null or left
    out means any agent; a new rule is on unless ``enabled`` says otherwise."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId | None = None
    answer: RuleAnswer
    enabled: StrictBool = True


class RuleReplace(BaseModel):
    """The whole of an existing rule. Every field is required, so a field left
    out never falls back to a default: ``agent_id`` is null for any agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId | None
    answer: RuleAnswer
    enabled: StrictBool


class RuleOrder(BaseModel):
    """Every rule's id, once each, in the new order."""

    model_config = ConfigDict(extra="forbid")

    rule_ids: list[RuleId] = Field(max_length=MAX_ORDER)


class RuleView(BaseModel):
    """A rule as the owner sees it. Its place is its place in the list."""

    rule_id: str
    kind: RuleKind
    agent_id: str | None
    answer: RuleAnswer
    enabled: bool
    created_at: datetime
    updated_at: datetime

    _utc_times = field_validator("created_at", "updated_at")(_utc)


class RuleList(BaseModel):
    """The check rules in order; the first that matches decides."""

    rules: list[RuleView] = Field(default_factory=list)


__all__ = [
    "MAX_ORDER",
    "MAX_RULES",
    "AgentId",
    "CheckRule",
    "RuleAnswer",
    "RuleCreate",
    "RuleReplace",
    "RuleKind",
    "RuleList",
    "RuleOrder",
    "RuleView",
]
