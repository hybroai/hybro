from __future__ import annotations

from agent.repository.documents import (
    _safe_parse_agent,
    _safe_parse_agent_group,
)
from common.observability import events as E
from common.observability import get_logger
from common.utils.mongo_indexes import create_index
from models.agent import Agent
from models.agent_group import AgentGroup

logger = get_logger(__name__)


class AgentRuntimeStore:
    def __init__(self, *, agent_groups, agent_repository) -> None:
        self._agent_groups = agent_groups
        self._agent_repository = agent_repository

    async def ensure_indexes(self) -> None:
        await create_index(
            self._agent_groups,
            "agent_groups",
            [("group_id", 1)],
            name="agent_group_id_unique",
            unique=True,
            critical=True,
        )

    async def add_agent_group(self, agent_group: AgentGroup) -> bool:
        try:
            await self._agent_groups.insert_one(agent_group.model_dump(mode="json"))
            return True
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="add_agent_group",
                group_id=agent_group.group_id,
            )
            return False

    async def get_agent_groups_by_owner(self, owner_id: str) -> list[AgentGroup]:
        try:
            docs = await self._agent_groups.find({"owner_id": owner_id})
            groups = [_safe_parse_agent_group(doc) for doc in docs if doc is not None]
            return [group for group in groups if group is not None]
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_groups_by_owner",
                owner_id=owner_id,
            )
            return []

    async def get_agent_group_by_id(self, group_id: str) -> AgentGroup | None:
        try:
            return _safe_parse_agent_group(
                await self._agent_groups.find_one({"group_id": group_id})
            )
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_group_by_id",
                group_id=group_id,
            )
            return None

    async def update_agent_group(self, group_id: str, updates: dict) -> bool:
        try:
            return await self._agent_groups.update_one(
                {"group_id": group_id},
                {"$set": dict(updates)},
            )
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_agent_group",
                group_id=group_id,
            )
            return False

    async def delete_agent_group(self, group_id: str) -> bool:
        try:
            return await self._agent_groups.delete_one({"group_id": group_id})
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="delete_agent_group",
                group_id=group_id,
            )
            return False

    async def get_all_active_agents(self, user_id: str | None = None) -> list[Agent]:
        try:
            docs = await self._agent_repository.list_visible(
                user_id=user_id,
                active_only=True,
                limit=0,
            )
            agents = [_safe_parse_agent(doc) for doc in docs if doc is not None]
            return [agent for agent in agents if agent is not None]
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_all_active_agents",
                user_id=user_id,
            )
            return []

    async def get_agent_name_by_agent_id(self, agent_id: str) -> str | None:
        try:
            doc = await self._agent_repository.get_by_id(agent_id)
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_name_by_agent_id",
                agent_id=agent_id,
            )
            return None
        card = (doc or {}).get("agent_card") or {}
        return (
            card.get("name") if isinstance(card, dict) else getattr(card, "name", None)
        )

    async def get_agent_by_agent_id(self, agent_id: str) -> Agent | None:
        try:
            return _safe_parse_agent(await self._agent_repository.get_by_id(agent_id))
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_by_agent_id",
                agent_id=agent_id,
            )
            return None

    async def increment_agent_call_count(self, agent_id: str, *, success: bool) -> None:
        try:
            await self._agent_repository.increment_agent_call_count(
                agent_id,
                success=success,
            )
        except Exception as exc:
            logger.emit(
                E.AGENT_RUNTIME_STORE_OPERATION_FAILED,
                exc=exc,
                operation="increment_agent_call_count",
                agent_id=agent_id,
            )
