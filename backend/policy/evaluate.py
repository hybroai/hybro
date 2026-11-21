"""The answer the check rules give for one agent call. Pure: no I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from policy.models import CheckRule, RuleAnswer


@dataclass(frozen=True, slots=True)
class RuleDecision:
    """The answer, and the rule that gave it; ``None`` when no rule matched."""

    answer: RuleAnswer
    rule_id: str | None


ALLOW_BY_DEFAULT = RuleDecision(answer="allow", rule_id=None)


def evaluate(rules: Iterable[CheckRule], agent_id: str) -> RuleDecision:
    """Walk the rules in the order given; the first that matches decides.

    A rule matches when it is on and names this agent or any agent. No match
    means allow.
    """
    for rule in rules:
        if rule.enabled and rule.agent_id in (None, agent_id):
            return RuleDecision(answer=rule.answer, rule_id=rule.rule_id)
    return ALLOW_BY_DEFAULT


__all__ = ["ALLOW_BY_DEFAULT", "RuleDecision", "evaluate"]
