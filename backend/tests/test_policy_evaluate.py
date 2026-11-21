"""``policy.evaluate``: the ordered check rules, first match decides."""

from __future__ import annotations

from datetime import UTC, datetime

from policy import RuleDecision, evaluate
from policy.models import CheckRule

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _rule(rule_id: str, agent_id: str | None, answer: str, *, enabled=True):
    return CheckRule(
        rule_id=rule_id,
        agent_id=agent_id,
        answer=answer,
        enabled=enabled,
        position=0,
        created_at=NOW,
        updated_at=NOW,
    )


def test_no_rules_allow_with_no_deciding_rule():
    assert evaluate([], "coder") == RuleDecision(answer="allow", rule_id=None)


def test_no_match_allows():
    rules = [_rule("r1", "reviewer", "deny")]
    assert evaluate(rules, "coder") == RuleDecision(answer="allow", rule_id=None)


def test_a_rule_for_the_agent_decides():
    rules = [_rule("r1", "coder", "deny")]
    assert evaluate(rules, "coder") == RuleDecision(answer="deny", rule_id="r1")


def test_the_first_match_decides_in_list_order():
    rules = [_rule("r1", "coder", "allow"), _rule("r2", "coder", "deny")]
    assert evaluate(rules, "coder") == RuleDecision(answer="allow", rule_id="r1")
    assert evaluate(list(reversed(rules)), "coder") == RuleDecision(
        answer="deny", rule_id="r2"
    )


def test_any_agent_matches_every_agent():
    rules = [_rule("r1", "coder", "allow"), _rule("r2", None, "deny")]
    assert evaluate(rules, "coder").rule_id == "r1"
    assert evaluate(rules, "reviewer") == RuleDecision(answer="deny", rule_id="r2")


def test_a_rule_that_is_off_never_matches():
    rules = [_rule("r1", "coder", "deny", enabled=False), _rule("r2", None, "allow")]
    assert evaluate(rules, "coder") == RuleDecision(answer="allow", rule_id="r2")
    assert evaluate(rules[:1], "coder") == RuleDecision(answer="allow", rule_id=None)


def test_the_agent_id_matches_exactly():
    rules = [_rule("r1", "coder", "deny")]
    assert evaluate(rules, "Coder").rule_id is None
    assert evaluate(rules, "coder-2").rule_id is None
