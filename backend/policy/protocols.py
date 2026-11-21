"""Policy ports.

``RuleRegistry`` is the surface ``api_gateway`` consumes: the owner's
``/rules`` routes. ``RuleStore`` is the durable record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from policy.models import (
    CheckRule,
    RuleCreate,
    RuleList,
    RuleOrder,
    RuleReplace,
    RuleView,
)


class RuleNotFound(Exception):
    """No rule has this id."""


class RuleOrderMismatch(Exception):
    """The new order does not name every rule exactly once."""


class RuleLimitReached(Exception):
    """The list already holds the most rules it can."""


@runtime_checkable
class RuleRegistry(Protocol):
    async def list_rules(self) -> RuleList: ...
    async def create(self, change: RuleCreate) -> RuleView: ...
    async def update(self, rule_id: str, change: RuleReplace) -> RuleView: ...
    async def delete(self, rule_id: str) -> None: ...
    async def reorder(self, rule_ids: list[str]) -> RuleList: ...


@runtime_checkable
class RuleStore(Protocol):
    async def ensure_indexes(self) -> None: ...
    async def insert(self, record: CheckRule) -> None: ...
    async def list_all(self) -> list[CheckRule]: ...
    async def update(
        self, rule_id: str, fields: dict[str, Any]
    ) -> CheckRule | None: ...
    async def delete(self, rule_id: str) -> bool: ...
    async def set_positions(self, rule_ids: list[str], at: datetime) -> None: ...


__all__ = [
    "RuleCreate",
    "RuleLimitReached",
    "RuleList",
    "RuleNotFound",
    "RuleOrder",
    "RuleOrderMismatch",
    "RuleReplace",
    "RuleRegistry",
    "RuleStore",
    "RuleView",
]
