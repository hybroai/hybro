"""``PolicyService`` over an in-memory store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from policy import PolicyService
from policy.models import MAX_RULES, RuleChange
from policy.protocols import (
    RuleLimitReached,
    RuleNotFound,
    RuleOrderMismatch,
    RuleRegistry,
)
from tests.fakes.policy import MemoryRuleStore

NOW = datetime(2026, 11, 20, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


def _service() -> tuple[PolicyService, MemoryRuleStore]:
    store = MemoryRuleStore()
    return PolicyService(store, now=Clock()), store


def test_the_service_is_a_rule_registry():
    service, _ = _service()
    assert isinstance(service, RuleRegistry)


async def test_create_appends_and_list_is_in_order():
    service, _ = _service()
    first = await service.create(RuleChange(agent_id="coder", answer="deny"))
    second = await service.create(RuleChange(answer="allow"))

    listing = await service.list_rules()

    assert [r.rule_id for r in listing.rules] == [first.rule_id, second.rule_id]
    assert first.rule_id.startswith("rule_")
    assert first.kind == "check"
    assert first.enabled is True
    assert second.agent_id is None


async def test_update_replaces_condition_answer_and_state():
    service, _ = _service()
    rule = await service.create(RuleChange(agent_id="coder", answer="deny"))

    updated = await service.update(
        rule.rule_id, RuleChange(agent_id=None, answer="allow", enabled=False)
    )

    assert (updated.agent_id, updated.answer, updated.enabled) == (
        None,
        "allow",
        False,
    )
    assert updated.updated_at > rule.updated_at
    assert updated.created_at == rule.created_at


async def test_update_and_delete_of_an_unknown_rule_are_refused():
    service, _ = _service()
    with pytest.raises(RuleNotFound):
        await service.update("rule_nope", RuleChange(answer="deny"))
    with pytest.raises(RuleNotFound):
        await service.delete("rule_nope")


async def test_delete_removes_the_rule():
    service, _ = _service()
    rule = await service.create(RuleChange(answer="deny"))

    await service.delete(rule.rule_id)

    assert (await service.list_rules()).rules == []


async def test_reorder_sets_the_order_and_create_appends_after_it():
    service, _ = _service()
    a = await service.create(RuleChange(agent_id="a", answer="deny"))
    b = await service.create(RuleChange(agent_id="b", answer="deny"))
    c = await service.create(RuleChange(agent_id="c", answer="deny"))
    await service.delete(b.rule_id)

    reordered = await service.reorder([c.rule_id, a.rule_id])
    d = await service.create(RuleChange(agent_id="d", answer="allow"))

    assert [r.rule_id for r in reordered.rules] == [c.rule_id, a.rule_id]
    listing = await service.list_rules()
    assert [r.rule_id for r in listing.rules] == [c.rule_id, a.rule_id, d.rule_id]


@pytest.mark.parametrize(
    "order",
    [
        lambda a, b: [a],
        lambda a, b: [a, b, "rule_other"],
        lambda a, b: [a, a],
        lambda a, b: [a, "rule_other"],
    ],
)
async def test_reorder_must_name_every_rule_once(order):
    service, _ = _service()
    a = await service.create(RuleChange(agent_id="a", answer="deny"))
    b = await service.create(RuleChange(agent_id="b", answer="deny"))

    with pytest.raises(RuleOrderMismatch):
        await service.reorder(order(a.rule_id, b.rule_id))

    listing = await service.list_rules()
    assert [r.rule_id for r in listing.rules] == [a.rule_id, b.rule_id]


async def test_create_is_refused_at_the_limit():
    service, store = _service()
    for _ in range(MAX_RULES):
        await service.create(RuleChange(answer="allow"))

    with pytest.raises(RuleLimitReached):
        await service.create(RuleChange(answer="allow"))
    assert len(store.records) == MAX_RULES


@pytest.mark.parametrize("answer", ["ask", "ALLOW", "", None])
def test_only_allow_and_deny_are_answers(answer):
    with pytest.raises(ValidationError) as caught:
        RuleChange(answer=answer)
    assert caught.value.errors()[0]["loc"] == ("answer",)


@pytest.mark.parametrize("agent_id", ["", "   ", "x" * 257])
def test_an_agent_id_is_not_blank_or_too_long(agent_id):
    with pytest.raises(ValidationError):
        RuleChange(agent_id=agent_id, answer="deny")


def test_a_change_takes_no_other_field():
    with pytest.raises(ValidationError):
        RuleChange(answer="deny", position=3)
    with pytest.raises(ValidationError):
        RuleChange(answer="deny", enabled="yes")
