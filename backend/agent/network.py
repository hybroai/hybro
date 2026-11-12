from __future__ import annotations

from pydantic import JsonValue

from agent.protocols import AgentGroupStore
from common.dto import AgentInfo
from common.errors import AppError, ConflictError, NotFoundError, ValidationError
from common.protocols import AgentMatcher, AgentRegistry
from models.agent_group import (
    BUILTIN_GROUP_ALL_AGENTS,
    BUILTIN_GROUP_ROOM_TEAM,
    AgentGroup,
)
from models.agent_network import (
    AgentDiscoveryResponse,
    AgentGroupListResponse,
    AgentGroupRef,
    AgentGroupSummary,
    AgentProfile,
    AgentSearchMatch,
    AgentSearchResponse,
    AgentSkillProfile,
    AgentSkillSummary,
    DiscoveredAgent,
)


class AgentNetworkService:
    def __init__(
        self,
        registry: AgentRegistry,
        groups: AgentGroupStore,
        matcher: AgentMatcher,
        *,
        requesting_user_id: str,
    ) -> None:
        self._registry = registry
        self._groups = groups
        self._matcher = matcher
        self._requesting_user_id = requesting_user_id

    async def discover(self, group_id: str | None = None) -> AgentDiscoveryResponse:
        agent_ids = await self._group_agent_ids(group_id)
        agents = await self._registry.list_visible_agents(
            user_id=self._requesting_user_id,
            active_only=True,
            query={"agent_id": {"$in": agent_ids}} if agent_ids is not None else None,
        )
        return AgentDiscoveryResponse(
            agents=[
                DiscoveredAgent(
                    agent_id=agent.agent_id,
                    agent_card=_discovery_card(agent.raw_card),
                )
                for agent in agents
            ]
        )

    async def list_groups(self) -> AgentGroupListResponse:
        groups = await self._owned_groups()
        member_ids = sorted({agent_id for group in groups for agent_id in group.agents})
        active_ids: set[str] = set()
        if member_ids:
            agents = await self._registry.list_visible_agents(
                user_id=self._requesting_user_id,
                active_only=True,
                query={"agent_id": {"$in": member_ids}},
            )
            active_ids = {agent.agent_id for agent in agents}
        return AgentGroupListResponse(
            groups=[
                AgentGroupSummary(
                    group_id=group.group_id,
                    name=group.name,
                    description=group.description,
                    agent_count=len(set(group.agents) & active_ids),
                )
                for group in groups
            ]
        )

    async def search(
        self, query: str, group_id: str | None = None, limit: int = 5
    ) -> AgentSearchResponse:
        agent_ids = await self._group_agent_ids(group_id)
        matches = await self._matcher.match_agents(
            query,
            limit=limit,
            filter_ids=agent_ids,
            respect_visibility=True,
            requesting_user_id=self._requesting_user_id,
        )
        memberships = _memberships(await self._owned_groups())
        results: list[AgentSearchMatch] = []
        for match in matches:
            if match.agent is None:
                continue
            card = match.agent.raw_card
            results.append(
                AgentSearchMatch(
                    agent_id=match.agent_id,
                    name=_text(card.get("name")) or match.agent.name,
                    description=_text(card.get("description")),
                    skills=[
                        AgentSkillSummary(**_skill_summary(skill))
                        for skill in _skills(card)
                    ],
                    input_modes=_modes(card, "Input"),
                    output_modes=_modes(card, "Output"),
                    groups=memberships.get(match.agent_id, []),
                    score=round(match.score, 4),
                )
            )
        return AgentSearchResponse(agents=results)

    async def get_profile(self, agent_id: str) -> AgentProfile:
        agents = await self._registry.list_visible_agents(
            user_id=self._requesting_user_id,
            query={"agent_id": agent_id},
        )
        if not agents:
            raise NotFoundError("Agent", agent_id)
        agent = agents[0]
        card = agent.raw_card
        provider = card.get("provider")
        capabilities = card.get("capabilities")
        return AgentProfile(
            agent_id=agent.agent_id,
            status=agent.status,
            name=_text(card.get("name")) or agent.name,
            description=_text(card.get("description")),
            version=_text(card.get("version")),
            provider=(
                _text(provider.get("organization"))
                if isinstance(provider, dict)
                else None
            ),
            streaming=(
                isinstance(capabilities, dict) and capabilities.get("streaming") is True
            ),
            input_modes=_modes(card, "Input"),
            output_modes=_modes(card, "Output"),
            skills=[
                AgentSkillProfile(
                    **_skill_summary(skill),
                    examples=_strings(skill.get("examples")),
                    input_modes=_strings(
                        skill.get("inputModes") or skill.get("input_modes")
                    ),
                    output_modes=_strings(
                        skill.get("outputModes") or skill.get("output_modes")
                    ),
                )
                for skill in _skills(card)
            ],
            groups=_memberships(await self._owned_groups()).get(agent.agent_id, []),
        )

    async def resolve_target(
        self, agent_id: str, group_id: str | None = None
    ) -> AgentInfo:
        agent_ids = await self._group_agent_ids(group_id)
        agents = await self._registry.list_visible_agents(
            user_id=self._requesting_user_id,
            query={"agent_id": agent_id},
        )
        if not agents:
            raise NotFoundError("Agent", agent_id)
        agent = agents[0]
        if agent.status != "active":
            raise ConflictError(
                "Agent is not active",
                details={"agent_id": agent_id, "status": agent.status},
            )
        if agent_ids is not None and agent_id not in agent_ids:
            raise ConflictError(
                "Agent is not a member of the selected group",
                details={"agent_id": agent_id, "group_id": group_id},
            )
        return agent

    async def _owned_groups(self) -> list[AgentGroup]:
        groups = await self._groups.get_agent_groups_by_owner(self._requesting_user_id)
        return [group for group in groups if group.type != "builtin"]

    async def _group_agent_ids(self, group_id: str | None) -> list[str] | None:
        if group_id is None or group_id == BUILTIN_GROUP_ALL_AGENTS:
            return None
        if group_id == BUILTIN_GROUP_ROOM_TEAM:
            raise ValidationError(
                "room_team requires a room and cannot scope the agent network",
                details={"group_id": group_id},
            )
        group = await self._groups.get_agent_group_by_id(group_id)
        if group is None:
            raise NotFoundError("AgentGroup", group_id)
        if group.owner_id != self._requesting_user_id:
            raise AppError(
                "Cannot access another owner's agent group", code="FORBIDDEN"
            )
        return group.agents


# Card fields a client needs to choose an agent. Transport (endpoint URLs,
# interfaces) and security (schemes, requirements, signatures, credentials) stay
# inside Hybro: clients reach agents only through Hybro by agent_id.
_DISCOVERY_CARD_FIELDS = (
    "name",
    "description",
    "version",
    "protocolVersion",
    "provider",
    "documentationUrl",
    "iconUrl",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
)


def _discovery_card(raw_card: dict[str, JsonValue]) -> dict[str, JsonValue]:
    card = {key: raw_card[key] for key in _DISCOVERY_CARD_FIELDS if key in raw_card}
    skills = card.get("skills")
    if isinstance(skills, list):
        card["skills"] = [
            {key: value for key, value in skill.items() if key != "security"}
            if isinstance(skill, dict)
            else skill
            for skill in skills
        ]
    return card


def _memberships(groups: list[AgentGroup]) -> dict[str, list[AgentGroupRef]]:
    memberships: dict[str, list[AgentGroupRef]] = {}
    for group in groups:
        ref = AgentGroupRef(group_id=group.group_id, name=group.name)
        for agent_id in dict.fromkeys(group.agents):
            memberships.setdefault(agent_id, []).append(ref)
    return memberships


# Stored Cards come from remote agents, so every field is read defensively.
def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _modes(card: dict[str, JsonValue], direction: str) -> list[str]:
    modes = card.get(f"default{direction}Modes") or card.get(
        f"default_{direction.lower()}_modes"
    )
    return _strings(modes) or ["text"]


def _skills(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    skills = card.get("skills")
    if not isinstance(skills, list):
        return []
    return [skill for skill in skills if isinstance(skill, dict)]


def _skill_summary(skill: dict[str, JsonValue]) -> dict[str, str | list[str] | None]:
    return {
        "id": _text(skill.get("id")),
        "name": _text(skill.get("name")),
        "description": _text(skill.get("description")),
        "tags": _strings(skill.get("tags")),
    }
