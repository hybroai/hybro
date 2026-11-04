"""
Agent selection for explicit target scopes and agent suggestions.

Wraps AgentMatcher with optional LLM reranking and returns ranked agents as
AgentSelectionResult, plus the route-facing AgentSuggestionResult.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from agent.matcher import AgentMatcher
from agent.protocols import AgentSuggestion, AgentSuggestionResult
from common.dto import AgentInfo, AgentRoutingCandidate
from common.observability import events as E
from common.observability import get_logger

logger = get_logger(__name__)


class RoutingStrategy(StrEnum):
    """Strategy for routing messages to agents"""

    SINGLE = "single"  # Route to 1 best agent (simple questions)
    PARALLEL = "parallel"  # Route to 2-3 agents simultaneously (multi-perspective)
    SEQUENTIAL = "sequential"  # Route to agents with dependency chain (complex tasks)


@dataclass
class AgentSelection:
    """Represents a selected agent with reason for selection"""

    agent_id: str
    agent_name: str
    reason: str
    score: float = 1.0


@dataclass
class AgentSelectionResult:
    """Result of agent selection process"""

    strategy: RoutingStrategy
    agents: list[AgentSelection]
    reasoning: str
    needs_debate: bool = False


class AgentSelectionService:
    """
    Rank agents for a message through AgentMatcher.

    Delegates to AgentMatcher.match(), optionally reranks the top candidates,
    and returns an AgentSelectionResult.
    """

    def __init__(self, matcher=None, llm_reranker=None):
        self._matcher = matcher or AgentMatcher()
        self._llm_reranker = llm_reranker

    def bind_facade(self, facade) -> None:
        self._matcher.bind_facade(facade)

    async def select_agents_for_message(
        self,
        message_text: str,
        top_k: int = 10,
        user_id: str | None = None,
        required_input_modes: list[str] | None = None,
        use_llm_rerank: bool = True,
    ) -> AgentSelectionResult:
        """
        Select agents for a message using deterministic matching.

        Args:
            message_text: The user's message to route
            top_k: Maximum number of agents to return (caps matcher output)
            user_id: Optional sender ID for private agent visibility
            required_input_modes: If present (non-None), message has attachments

        Returns:
            AgentSelectionResult with strategy, selected agents, and reasoning
        """
        logger.emit(
            E.AGENT_SELECTION_STARTED,
            user_id=user_id,
            message_char_count=len(message_text),
        )

        # Delegate to AgentMatcher — let exceptions propagate so callers
        # (e.g. TargetScopeResolver.resolve_explicit_target_scope) can surface a proper 500.
        match_result = await self._matcher.match(
            message_text=message_text,
            user_id=user_id,
            required_input_modes=required_input_modes,
        )

        # Convert MatchResult to AgentSelectionResult.
        if not match_result.agents:
            logger.emit(
                E.AGENT_SELECTION_COMPLETED,
                level=logging.WARNING,
                user_id=user_id,
                strategy=RoutingStrategy.SINGLE.value,
                selected_agent_count=0,
                candidate_count=match_result.total_candidates,
            )
            return AgentSelectionResult(
                strategy=RoutingStrategy.SINGLE,
                agents=[],
                reasoning="No matching agents found in the network",
                needs_debate=False,
            )

        ranked_agents = match_result.agents
        if use_llm_rerank and len(ranked_agents) > 1:
            rerank_head = ranked_agents[:5]
            ranked_agents = [
                *(await self._rerank(message_text, rerank_head)),
                *ranked_agents[5:],
            ]

        # Map MatchedAgent to AgentSelection, respecting top_k cap
        agent_selections = [
            AgentSelection(
                agent_id=matched.agent.agent_id,
                agent_name=matched.agent.name or "",
                reason=f"Lexical match score: {matched.lexical_score:.2f}",
                score=matched.final_score,
            )
            for matched in ranked_agents[:top_k]
        ]

        # Reported strategy: SINGLE for one agent, PARALLEL for more.
        strategy = (
            RoutingStrategy.SINGLE
            if len(agent_selections) == 1
            else RoutingStrategy.PARALLEL
        )

        reasoning = f"Matched {len(agent_selections)} agent(s) from {match_result.total_candidates} candidates"

        logger.emit(
            E.AGENT_SELECTION_COMPLETED,
            user_id=user_id,
            strategy=strategy.value,
            selected_agent_count=len(agent_selections),
            candidate_count=match_result.total_candidates,
        )

        return AgentSelectionResult(
            strategy=strategy,
            agents=agent_selections,
            reasoning=reasoning,
            needs_debate=False,  # Selection never requests a debate.
        )

    async def suggest_agents(
        self,
        message_text: str,
        top_k: int = 3,
        user_id: str | None = None,
    ) -> AgentSuggestionResult:
        """
        Public API method to suggest agents for a message.
        Returns a route-facing protocol DTO.

        Args:
            message_text: The user's message
            top_k: Maximum number of agents to suggest
            user_id: Optional sender ID for private agent visibility

        Returns:
            AgentSuggestionResult with routing metadata and suggested agents
        """
        result = await self.select_agents_for_message(
            message_text=message_text,
            top_k=top_k,
            user_id=user_id,
            use_llm_rerank=False,
        )

        return AgentSuggestionResult(
            analysis=result.reasoning,
            metadata={
                "routing_strategy": result.strategy.value,
                "needs_debate": result.needs_debate,
                "reasoning": result.reasoning,
            },
            suggested_agents=[
                AgentSuggestion(
                    agent_id=agent.agent_id,
                    name=agent.agent_name,
                    reason=agent.reason,
                    score=agent.score,
                )
                for agent in result.agents
            ],
        )

    async def _rerank(self, query: str, candidates):
        if self._llm_reranker is None:
            return candidates
        try:
            routing_candidates = [_routing_candidate(item.agent) for item in candidates]
            if hasattr(self._llm_reranker, "rank_agents_for_task"):
                ranked_ids = await self._llm_reranker.rank_agents_for_task(
                    query,
                    routing_candidates,
                )
            else:
                best_id = await self._llm_reranker.select_best_agent_for_task(
                    query,
                    routing_candidates,
                )
                ranked_ids = [best_id]
        except Exception as exc:
            # The lexical order stands.
            logger.emit(
                E.AGENT_SELECTION_RERANK_FAILED,
                exc=exc,
                candidate_count=len(candidates),
            )
            return candidates
        by_id = {item.agent.agent_id: item for item in candidates}
        ranked = []
        seen: set[str] = set()
        for candidate_id in ranked_ids if isinstance(ranked_ids, list) else []:
            candidate_id = str(candidate_id)
            if candidate_id in by_id and candidate_id not in seen:
                ranked.append(by_id[candidate_id])
                seen.add(candidate_id)
        return [
            *ranked,
            *(item for item in candidates if item.agent.agent_id not in seen),
        ]


def _routing_candidate(agent: AgentInfo) -> AgentRoutingCandidate:
    card = agent.raw_card
    skills = [
        str(
            (skill.get("name") or skill.get("id") or "Unknown")
            if isinstance(skill, dict)
            else skill
        )
        for skill in (card.get("skills") or [])
    ]
    capabilities = card.get("capabilities")
    return AgentRoutingCandidate(
        agent_id=str(agent.agent_id),
        name=str(agent.name or ""),
        description=str(agent.description or ""),
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        skills=skills,
    )
