from __future__ import annotations

from typing import Any

from common.dto import AgentInfo
from common.observability import events as E
from common.utils.logger import get_logger
from conversation.dto import SavedAgentGroupSnapshot
from conversation.unbound import UNBOUND_RUNTIME_STORE

logger = get_logger(__name__)


class RepositoryRoomMembershipSeedSource:
    def __init__(
        self,
        *,
        membership_store=None,
        agent_registry=None,
    ) -> None:
        self._store = membership_store or UNBOUND_RUNTIME_STORE
        self._agent_registry = agent_registry

    def bind_store(self, membership_store) -> None:
        self._store = membership_store

    async def get_saved_group(self, group_id: str) -> SavedAgentGroupSnapshot | None:
        group = await self._store.get_agent_group_by_id(group_id)
        if group is None:
            return None
        return SavedAgentGroupSnapshot(
            group_id=group.group_id,
            name=group.name,
            owner_id=group.owner_id,
            type=group.type,
            agent_ids=list(group.agents or []),
        )

    async def list_current_agents(self, user_id: str | None) -> list[AgentInfo]:
        if self._agent_registry is not None:
            try:
                return await self._agent_registry.list_visible_agents(
                    user_id=user_id, active_only=True
                )
            except Exception as exc:
                logger.emit(
                    E.MEMBERSHIP_AGENTS_FALLBACK_SELECTED, exc=exc, user_id=user_id
                )

        agents = await self._store.get_all_active_agents(user_id=user_id)
        return [self.agent_info_from_record(agent) for agent in agents or []]

    @staticmethod
    def agent_info_from_record(agent: Any) -> AgentInfo:
        return _agent_info_from_record(agent)


def _agent_info_from_record(agent: Any) -> AgentInfo:
    card = getattr(agent, "agent_card", None)
    raw_status = getattr(agent, "agent_status", None)
    status = getattr(raw_status, "value", None)
    agent_id = getattr(agent, "agent_id", None)
    name = getattr(card, "name", None)
    resolved_status = status or str(raw_status or "active")
    missing_fields = [
        field
        for field, missing in (
            ("agent_id", not agent_id),
            ("name", name is None),
            ("status", raw_status is None or not resolved_status),
        )
        if missing
    ]
    if missing_fields:
        logger.emit(
            E.MEMBERSHIP_AGENT_FIELDS_NOT_FOUND,
            agent_id=agent_id,
            missing_agent_fields=missing_fields,
        )
    return AgentInfo(
        agent_id=agent_id,
        name=name,
        description=getattr(card, "description", None),
        url=getattr(card, "url", None),
        provider_id=getattr(agent, "provider_id", None),
        status=resolved_status,
        capabilities=[],
        source=getattr(agent, "source", "cloud"),
        is_public=agent.is_public,
        public_url=getattr(agent, "public_url", None),
        raw_card=card.model_dump(mode="json") if hasattr(card, "model_dump") else {},
    )


__all__ = ["RepositoryRoomMembershipSeedSource"]
