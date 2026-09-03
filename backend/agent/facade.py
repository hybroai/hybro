from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agent.constants import AGENT_CARD_NO_OVERWRITE
from agent.matching import (
    accepts_input_modes,
    is_searchable_query,
    rank_agent_docs,
    select_top_matches,
)
from agent.public_url import PublicUrlGenerator
from agent.translators import (
    _status_value,
    agent_card_from_doc,
    agent_info_from_doc,
    registration_doc_from_card,
)
from agent.url_utils import normalize_agent_url
from common.dto import LocalAgentUpsertResult
from common.dto.agent import (
    AgentCardSnapshot,
    AgentInfo,
    AgentMatchResult,
)
from common.observability import NoopTracingProvider
from common.protocols import (
    AgentCardResolver,
    AgentExclusionReader,
    AgentRepository,
)

logger = logging.getLogger(__name__)

_ALLOWED_UPDATE_KEYS = frozenset(
    {
        "agent_status",
        "is_public",
        "rate_limit_per_user_per_hour",
        "rate_limit_system_per_hour",
        "agent_card",
    }
)


class AgentFacade:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        card_resolver: AgentCardResolver,
        exclusion_reader: AgentExclusionReader | None = None,
        public_url_base_domain: str = "hybro.ai",
        public_url_protocol: str = "https",
        id_factory: Callable[[], str],
        now: Callable[[], datetime],
        tracer: Any | None = None,
    ) -> None:
        self._repository = repository
        self._card_resolver = card_resolver
        self._exclusion_reader = exclusion_reader
        self._public_url_base_domain = public_url_base_domain
        self._public_url_protocol = public_url_protocol
        self._id_factory = id_factory
        self._now = now
        self._tracer = tracer or NoopTracingProvider()

    def bind_exclusion_reader(
        self,
        exclusion_reader: AgentExclusionReader | None,
    ) -> None:
        self._exclusion_reader = exclusion_reader

    async def get_agent(self, agent_id: str) -> AgentInfo | None:
        doc = await self._repository.get_by_id(agent_id)
        if doc is None:
            return None
        return agent_info_from_doc(doc)

    async def get_agent_card(self, agent_id: str) -> AgentCardSnapshot | None:
        doc = await self._repository.get_by_id(agent_id)
        if doc is None:
            return None
        return agent_card_from_doc(doc)

    async def get_agents_by_ids(self, agent_ids: list[str]) -> list[AgentInfo]:
        docs = await self._repository.get_by_ids(agent_ids)
        docs_by_id = {doc.get("agent_id"): doc for doc in docs}
        ordered_docs: list[dict] = []
        for agent_id in agent_ids:
            if agent_id in docs_by_id:
                ordered_docs.append(docs_by_id[agent_id])
        enriched = list(ordered_docs)
        return [agent_info_from_doc(doc) for doc in enriched]

    async def is_agent_healthy(self, agent_id: str) -> bool:
        doc = await self._repository.get_by_id(agent_id)
        return (
            doc is not None
            and _status_value(doc.get("agent_status"), default=None) == "active"
        )

    async def is_directly_callable(self, agent_id: str) -> bool:
        doc = await self._repository.get_by_id(agent_id)
        return (
            doc is not None
            and _status_value(doc.get("agent_status"), default=None) == "active"
        )

    async def match_agents(
        self,
        query: str,
        limit: int = 5,
        filter_ids: list[str] | None = None,
        respect_visibility: bool = True,
        requesting_user_id: str | None = None,
    ) -> list[AgentMatchResult]:
        selected = await self._match_agent_records(
            query,
            limit=limit,
            filter_ids=filter_ids,
            respect_visibility=respect_visibility,
            requesting_user_id=requesting_user_id,
        )
        return [
            AgentMatchResult(
                agent_id=match["agent_id"],
                score=match["lexical_score"],
                reason=f"Lexical match score: {match['lexical_score']:.2f}",
                agent=match["agent"],
            )
            for match in selected
        ]

    async def match_for_message(
        self,
        query: str,
        *,
        limit: int = 5,
        filter_ids: list[str] | None = None,
        requesting_user_id: str | None = None,
        required_input_modes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._match_agent_records(
            query,
            limit=limit,
            filter_ids=filter_ids,
            respect_visibility=True,
            requesting_user_id=requesting_user_id,
            required_input_modes=required_input_modes,
        )

    async def register_agent(
        self,
        url: str,
        provider_id: str,
        **kwargs: Any,
    ) -> AgentInfo:
        if not url:
            raise ValueError("url is required")
        if not provider_id:
            raise ValueError("provider_id is required")

        requested_normalized = normalize_agent_url(url)
        card = kwargs.get("resolved_card")
        if card is None:
            card = await self._card_resolver.resolve_card(url)
        if card is None:
            raise ValueError("agent card could not resolve")

        normalized_url = normalize_agent_url(card.url or url) or requested_normalized
        existing = await self._repository.find_by_normalized_url(
            normalized_url,
            provider_id=None,
        )
        if existing is not None:
            raise ValueError("Agent with this URL is already registered")

        agent_id = self._id_factory()
        public_url = await self._public_url_generator().generate_public_url(
            agent_name=card.name,
            agent_id=agent_id,
            preferred_subdomain=kwargs.get("preferred_subdomain"),
        )
        doc = registration_doc_from_card(
            agent_id=agent_id,
            provider_id=provider_id,
            card=card,
            normalized_url=normalized_url,
            public_url=public_url,
            now=self._now(),
            is_public=kwargs.get("is_public", True),
            rate_limit_per_user_per_hour=kwargs.get("rate_limit_per_user_per_hour"),
            rate_limit_system_per_hour=kwargs.get("rate_limit_system_per_hour"),
        )

        self._validate_rate_limits(doc)
        await self._repository.upsert(agent_id, doc)
        return agent_info_from_doc(doc)

    async def upsert_local_agent(
        self,
        discovery_url: str,
        card: AgentCardSnapshot,
    ) -> LocalAgentUpsertResult:
        normalized_url = normalize_agent_url(card.url or discovery_url)
        if not normalized_url:
            raise ValueError("local agent URL could not normalize")

        existing = await self._repository.find_by_normalized_url(normalized_url)
        if existing is not None and existing.get("source", "cloud") != "local":
            return LocalAgentUpsertResult(
                agent_id=existing["agent_id"],
                managed=False,
            )

        now = self._now()
        raw_card = dict(card.raw_card)
        if not raw_card.get("name"):
            raw_card["name"] = card.name
        raw_card.setdefault("description", card.description)
        if not raw_card.get("url"):
            raw_card["url"] = card.url or discovery_url

        if existing is not None:
            stored_card = dict(existing.get("agent_card") or {})
            merged_card = {**stored_card, **raw_card}
            for key in AGENT_CARD_NO_OVERWRITE:
                if stored_card.get(key):
                    merged_card[key] = stored_card[key]
            was_inactive = (
                _status_value(existing.get("agent_status"), default=None) != "active"
            )
            await self._repository.update(
                existing["agent_id"],
                {
                    "agent_card": merged_card,
                    "capabilities": list(card.capabilities),
                    "agent_status": "active",
                    "updated_at": now,
                },
            )
            return LocalAgentUpsertResult(
                agent_id=existing["agent_id"],
                managed=True,
                reactivated=was_inactive,
            )

        agent_id = hashlib.sha256(
            f"hybro-local-agent:{normalized_url}".encode()
        ).hexdigest()[:32]
        await self._repository.upsert(
            agent_id,
            {
                "agent_id": agent_id,
                "provider_id": None,
                "agent_card": raw_card,
                "normalized_url": normalized_url,
                "public_url": None,
                "agent_status": "active",
                "is_public": True,
                "source": "local",
                "capabilities": list(card.capabilities),
                "rate_limit_per_user_per_hour": None,
                "rate_limit_system_per_hour": None,
                "call_count": 0,
                "call_success_count": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        return LocalAgentUpsertResult(
            agent_id=agent_id,
            managed=True,
            added=True,
        )

    async def list_local_agent_ids(self) -> list[str]:
        docs = await self._repository.get_by_source("local")
        return [doc["agent_id"] for doc in docs]

    async def mark_local_agents_inactive(self, agent_ids: list[str]) -> int:
        return await self._repository.mark_agents_inactive(
            agent_ids,
            source="local",
        )

    async def delete_agent(self, agent_id: str, provider_id: str) -> bool:
        doc = await self._repository.get_by_id(agent_id)
        if doc is None or doc.get("provider_id") != provider_id:
            return False
        return bool(await self._repository.delete(agent_id))

    async def update_agent(self, agent_id: str, updates: dict) -> AgentInfo | None:
        unknown = set(updates) - _ALLOWED_UPDATE_KEYS
        if unknown:
            raise ValueError(f"Unknown agent update keys: {sorted(unknown)}")

        current = await self._repository.get_by_id(agent_id)
        if current is None:
            return None

        update_doc = self._build_update_doc(current, updates)
        self._validate_rate_limits(update_doc)
        updated = await self._repository.update(agent_id, update_doc)
        if updated is None:
            return None

        return agent_info_from_doc(updated)

    async def list_agents(self, provider_id: str) -> list[AgentInfo]:
        docs = await self._repository.get_by_provider(provider_id)
        return [agent_info_from_doc(doc) for doc in docs]

    async def list_public_agents(self, limit: int = 50) -> list[AgentInfo]:
        docs = await self._repository.get_public(limit=limit)
        return [agent_info_from_doc(doc) for doc in docs]

    async def increment_agent_call_count(self, agent_id: str, *, success: bool) -> None:
        await self._repository.increment_agent_call_count(agent_id, success=success)

    async def resolve_agent_card_from_url(self, url: str) -> AgentCardSnapshot | None:
        return await self._card_resolver.resolve_card(url)

    async def list_visible_agents(
        self,
        *,
        user_id: str | None = None,
        active_only: bool = False,
        query: dict[str, Any] | None = None,
        limit: int = 0,
    ) -> list[AgentInfo]:
        docs = await self._repository.list_visible(
            user_id=user_id,
            active_only=active_only,
            query=query,
            limit=limit,
        )
        return [agent_info_from_doc(doc) for doc in docs]

    async def get_agent_by_url(self, url: str) -> AgentInfo | None:
        doc = await self._repository.find_by_normalized_url(
            normalize_agent_url(url),
            provider_id=None,
        )
        return agent_info_from_doc(doc) if doc else None

    async def update_health(self, agent_id: str, healthy: bool) -> None:
        await self._repository.update_health(agent_id, healthy)

    async def _get_excluded_agent_ids(self) -> frozenset[str]:
        if self._exclusion_reader is None:
            return frozenset()
        return frozenset(await self._exclusion_reader.get_excluded_agent_ids())

    def _public_url_generator(self) -> PublicUrlGenerator:
        return PublicUrlGenerator(
            exists=self._repository.public_url_exists,
            base_domain=self._public_url_base_domain,
            protocol=self._public_url_protocol,
            id_factory=self._id_factory,
        )

    async def _match_agent_records(
        self,
        query: str,
        *,
        limit: int,
        filter_ids: list[str] | None = None,
        respect_visibility: bool = True,
        requesting_user_id: str | None = None,
        required_input_modes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not is_searchable_query(query) or filter_ids == []:
            return []

        # respect_visibility=False widens matching to public agents across users;
        # private agents remain hidden unless scoped to the requesting owner.
        user_id = requesting_user_id if respect_visibility else None
        candidates = await self._repository.list_visible(
            user_id=user_id,
            active_only=True,
            agent_ids=filter_ids,
            limit=0,
        )
        if not candidates:
            return []

        excluded_agent_ids = await self._get_excluded_agent_ids()
        if excluded_agent_ids:
            candidates = [
                doc
                for doc in candidates
                if doc.get("agent_id") not in excluded_agent_ids
            ]
            if not candidates:
                return []

        if required_input_modes is not None:
            candidates = [
                doc
                for doc in candidates
                if accepts_input_modes(doc, required_input_modes)
            ]
            if not candidates:
                return []

        candidate_ids = [str(doc["agent_id"]) for doc in candidates]
        mongo_scores: dict[str, float] = {}
        mongo_matched_ids: set[str] = set()
        try:
            results = await self._repository.text_search(
                candidate_ids,
                query,
                limit=len(candidate_ids),
            )
            for result in results:
                agent_id = str(result.get("agent_id") or "")
                if agent_id not in candidate_ids:
                    continue
                mongo_matched_ids.add(agent_id)
                mongo_scores[agent_id] = float(result.get("score", 0.0) or 0.0)
        except Exception:
            logger.warning(
                "Agent text search unavailable; using application lexical fallback",
                exc_info=True,
            )
        ranked = rank_agent_docs(
            candidates,
            mongo_scores,
            mongo_matched_ids=mongo_matched_ids,
            query=query,
        )
        selected = select_top_matches(ranked, limit=limit)
        for match in selected:
            match["agent"] = agent_info_from_doc(match["agent"])
        return selected

    def _build_update_doc(self, current: dict, updates: dict) -> dict:
        update_doc: dict[str, Any] = {}
        for key in (
            "agent_status",
            "is_public",
            "rate_limit_per_user_per_hour",
            "rate_limit_system_per_hour",
        ):
            if key in updates:
                update_doc[key] = updates[key]

        if "agent_card" in updates:
            current_card = dict(current.get("agent_card") or {})
            for key, value in dict(updates["agent_card"]).items():
                if key in AGENT_CARD_NO_OVERWRITE:
                    continue
                current_card[key] = value
            update_doc["agent_card"] = current_card
        return update_doc

    @staticmethod
    def _validate_rate_limits(doc: dict) -> None:
        for key in (
            "rate_limit_per_user_per_hour",
            "rate_limit_system_per_hour",
        ):
            value = doc.get(key)
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"{key} must be a positive integer or None")
