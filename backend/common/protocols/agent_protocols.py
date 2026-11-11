from __future__ import annotations

from typing import Protocol, runtime_checkable

from common.dto import (
    AgentCardSnapshot,
    AgentInfo,
    AgentMatchResult,
)
from common.protocols.json_types import JsonValue


@runtime_checkable
class AgentRegistry(Protocol):
    async def get_agent(self, agent_id: str) -> AgentInfo | None: ...
    async def get_agent_card(self, agent_id: str) -> AgentCardSnapshot | None: ...
    async def get_agents_by_ids(self, agent_ids: list[str]) -> list[AgentInfo]: ...
    async def list_active_agents(
        self, agent_ids: list[str] | None = None
    ) -> list[AgentInfo]: ...
    async def list_visible_agents(
        self,
        *,
        user_id: str | None = None,
        active_only: bool = False,
        query: dict[str, JsonValue] | None = None,
        limit: int = 0,
    ) -> list[AgentInfo]: ...
    async def get_agent_by_url(self, url: str) -> AgentInfo | None: ...
    async def is_agent_healthy(self, agent_id: str) -> bool: ...
    async def is_directly_callable(self, agent_id: str) -> bool: ...


@runtime_checkable
class AgentNetworkTargetResolver(Protocol):
    async def resolve_target(
        self, agent_id: str, group_id: str | None = None
    ) -> AgentInfo: ...


@runtime_checkable
class AgentMatcher(Protocol):
    async def match_agents(
        self,
        query: str,
        limit: int = 5,
        filter_ids: list[str] | None = None,
        respect_visibility: bool = True,
        requesting_user_id: str | None = None,
    ) -> list[AgentMatchResult]: ...


@runtime_checkable
class AgentExclusionReader(Protocol):
    async def get_excluded_agent_ids(self) -> frozenset[str]: ...


__all__ = [
    "AgentExclusionReader",
    "AgentMatcher",
    "AgentNetworkTargetResolver",
    "AgentRegistry",
]
