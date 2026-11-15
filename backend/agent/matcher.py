from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent.matching import (
    accepts_input_modes as _accepts_input_modes,
)
from common.dto import AgentInfo


@runtime_checkable
class AgentMessageMatcher(Protocol):
    async def match_for_message(
        self,
        query: str,
        *,
        limit: int = 5,
        filter_ids: list[str] | None = None,
        requesting_user_id: str | None = None,
        required_input_modes: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...


def _agent_to_dict(agent: AgentInfo) -> dict:
    return {"agent_id": agent.agent_id, "agent_card": dict(agent.raw_card)}


def accepts_input_modes(
    agent: AgentInfo,
    required_input_modes: list[str] | None = None,
) -> bool:
    return _accepts_input_modes(_agent_to_dict(agent), required_input_modes)


@dataclass
class MatchedAgent:
    agent: AgentInfo
    lexical_score: float
    final_score: float


@dataclass
class MatchResult:
    agents: list[MatchedAgent]
    total_candidates: int
    filtered_count: int


class AgentMatcher:
    def __init__(self, facade=None) -> None:
        self._facade = facade
        self._bound = facade is not None

    def bind_facade(self, facade) -> None:
        self._facade = facade
        self._bound = True

    def _require_facade(self):
        if not self._bound or self._facade is None:
            raise RuntimeError(
                "AgentMatcher.bind_facade() not called - startup incomplete"
            )
        return self._facade

    async def match(
        self,
        message_text: str,
        user_id: str | None = None,
        required_input_modes: list[str] | None = None,
    ) -> MatchResult:
        facade = self._require_facade()
        if isinstance(facade, AgentMessageMatcher):
            matches = await facade.match_for_message(
                message_text,
                requesting_user_id=user_id,
                required_input_modes=required_input_modes,
            )
        else:
            matches = await facade.match_agents(
                message_text,
                requesting_user_id=user_id,
            )
        converted: list[MatchedAgent] = []
        for match in matches:
            matched_agent = _to_matched_agent(match)
            if matched_agent is not None:
                converted.append(matched_agent)
        return MatchResult(
            agents=converted,
            total_candidates=len(converted),
            filtered_count=len(converted),
        )


def _to_matched_agent(match) -> MatchedAgent | None:
    if isinstance(match, dict):
        agent = match.get("agent")
        if agent is None:
            return None
        return MatchedAgent(
            agent=agent,
            lexical_score=match.get("lexical_score", match.get("score", 0.0)),
            final_score=match.get(
                "final_score",
                match.get("lexical_score", match.get("score", 0.0)),
            ),
        )

    if getattr(match, "agent", None) is None:
        return None
    return MatchedAgent(
        agent=match.agent,
        lexical_score=getattr(match, "score", 0.0),
        final_score=getattr(match, "score", 0.0),
    )
