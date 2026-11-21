"""The owner's check rules: an ordered list, kept in its own collection."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime

from common.utils.time import utcnow
from policy.evaluate import RuleDecision, evaluate
from policy.models import (
    MAX_RULES,
    CheckRule,
    RuleCreate,
    RuleList,
    RuleReplace,
    RuleView,
)
from policy.protocols import (
    RuleLimitReached,
    RuleNotFound,
    RuleOrderMismatch,
    RuleStore,
)


def _view(record: CheckRule) -> RuleView:
    return RuleView(
        rule_id=record.rule_id,
        kind=record.kind,
        agent_id=record.agent_id,
        answer=record.answer,
        enabled=record.enabled,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _list(records: list[CheckRule]) -> RuleList:
    return RuleList(rules=[_view(r) for r in records])


class PolicyService:
    def __init__(
        self, store: RuleStore, *, now: Callable[[], datetime] = utcnow
    ) -> None:
        self._store = store
        self._now = now

    async def list_rules(self) -> RuleList:
        return _list(await self._store.list_all())

    async def check_agent_call(self, agent_id: str) -> RuleDecision:
        """The answer the rules give for a new call to this agent. With no
        rules, or none that matches, the answer is allow."""
        return evaluate(await self._store.list_all(), agent_id)

    async def create(self, change: RuleCreate) -> RuleView:
        """Add a rule at the end of the list."""
        rules = await self._store.list_all()
        # Not atomic: concurrent creates may pass the cap by a few (see
        # MAX_RULES) or share a position, which creation time then orders.
        if len(rules) >= MAX_RULES:
            raise RuleLimitReached
        now = self._now()
        record = CheckRule(
            rule_id="rule_" + secrets.token_hex(12),
            agent_id=change.agent_id,
            answer=change.answer,
            enabled=change.enabled,
            position=rules[-1].position + 1 if rules else 0,
            created_at=now,
            updated_at=now,
        )
        await self._store.insert(record)
        return _view(record)

    async def update(self, rule_id: str, change: RuleReplace) -> RuleView:
        updated = await self._store.update(
            rule_id,
            {
                "agent_id": change.agent_id,
                "answer": change.answer,
                "enabled": change.enabled,
                "updated_at": self._now(),
            },
        )
        if updated is None:
            raise RuleNotFound
        return _view(updated)

    async def delete(self, rule_id: str) -> None:
        if not await self._store.delete(rule_id):
            raise RuleNotFound

    async def reorder(self, rule_ids: list[str]) -> RuleList:
        """Put the rules in this order. It must name every rule exactly once,
        so a rule added or removed since the owner read the list refuses it."""
        current = await self._store.list_all()
        if len(rule_ids) != len(current) or set(rule_ids) != {
            r.rule_id for r in current
        }:
            raise RuleOrderMismatch
        await self._store.set_positions(rule_ids, self._now())
        return _list(await self._store.list_all())


__all__ = ["PolicyService"]
