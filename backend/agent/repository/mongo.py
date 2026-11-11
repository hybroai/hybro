from __future__ import annotations

from datetime import datetime

from common.protocols import MongoDAL
from common.utils.mongo_indexes import ensure_text_index
from common.utils.time import utcnow


class AgentMongoRepository:
    def __init__(self, mongo: MongoDAL, collection_name: str = "agents") -> None:
        self._agents = mongo.collection(collection_name)

    async def ensure_indexes(self) -> bool:
        """Create the URL uniqueness index; return lexical search readiness."""
        existing = await self._agents.index_information()
        index = existing.get("unique_normalized_url")
        needs_recreate = index is None or index.get("partialFilterExpression") != {
            "normalized_url": {"$type": "string"}
        }
        if needs_recreate:
            try:
                await self._agents.drop_index("unique_normalized_url")
            except Exception:
                pass
            await self._agents.create_index(
                [("normalized_url", 1)],
                unique=True,
                name="unique_normalized_url",
                partialFilterExpression={"normalized_url": {"$type": "string"}},
            )
        return await ensure_text_index(
            self._agents,
            name="agent_lexical_text",
            weights={
                "agent_card.name": 10,
                "agent_card.skills.name": 8,
                "agent_card.skills.tags": 6,
                "agent_card.description": 3,
                "agent_card.skills.description": 3,
            },
        )

    async def get_by_id(self, agent_id: str) -> dict | None:
        return await self._agents.find_one({"agent_id": agent_id})

    async def get_by_ids(self, agent_ids: list[str]) -> list[dict]:
        return await self._agents.find({"agent_id": {"$in": agent_ids}})

    async def get_by_provider(self, provider_id: str) -> list[dict]:
        return await self._agents.find({"provider_id": provider_id})

    async def get_by_source(self, source: str) -> list[dict]:
        return await self._agents.find({"source": source})

    async def get_public(self, limit: int = 50) -> list[dict]:
        return await self._agents.find(
            {"is_public": True},
            limit=limit,
        )

    async def list_active_agents(
        self, agent_ids: list[str] | None = None
    ) -> list[dict]:
        query: dict = {"agent_status": "active"}
        if agent_ids is not None:
            query["agent_id"] = {"$in": agent_ids}
        return await self._agents.find(query)

    async def find_by_normalized_url(
        self, normalized_url: str, provider_id: str | None = None
    ) -> dict | None:
        exact_query: dict = {"normalized_url": normalized_url}
        if provider_id is not None:
            exact_query["provider_id"] = provider_id
        return await self._agents.find_one(exact_query)

    async def list_visible(
        self,
        *,
        user_id: str | None = None,
        active_only: bool = False,
        agent_ids: list[str] | None = None,
        query: dict | None = None,
        limit: int = 0,
    ) -> list[dict]:
        visibility: list[dict] = [{"is_public": True}]
        if user_id is not None:
            visibility.append({"provider_id": user_id})

        conditions: list[dict] = []
        if query:
            conditions.append(dict(query))
        conditions.append({"$or": visibility})
        if active_only:
            conditions.append({"agent_status": "active"})
        if agent_ids is not None:
            conditions.append({"agent_id": {"$in": agent_ids}})

        final_query = conditions[0] if len(conditions) == 1 else {"$and": conditions}

        return await self._agents.find(final_query, limit=limit or None)

    async def text_search(
        self, agent_ids: list[str], query: str, limit: int
    ) -> list[dict]:
        if not agent_ids:
            return []
        return await self._agents.find(
            {
                "agent_id": {"$in": agent_ids},
                "$text": {"$search": query},
            },
            projection={
                "agent_id": 1,
                "score": {"$meta": "textScore"},
            },
            sort=[("score", {"$meta": "textScore"})],
            limit=limit,
        )

    async def upsert(self, agent_id: str, data: dict) -> None:
        await self._agents.update_one(
            {"agent_id": agent_id},
            {"$set": {**data, "agent_id": agent_id}},
            upsert=True,
        )

    async def delete(self, agent_id: str) -> bool:
        return await self._agents.delete_one({"agent_id": agent_id})

    async def update_where(
        self, agent_id: str, conditions: dict, updates: dict
    ) -> bool:
        """Set ``updates`` only while the record still matches ``conditions``."""
        return await self._agents.update_one(
            {**conditions, "agent_id": agent_id},
            {"$set": {**updates, "agent_id": agent_id}},
        )

    async def update(self, agent_id: str, updates: dict) -> dict | None:
        await self._agents.update_one(
            {"agent_id": agent_id},
            {"$set": updates},
        )
        return await self.get_by_id(agent_id)

    async def public_url_exists(self, subdomain: str, base_domain: str) -> bool:
        escaped = base_domain.replace(".", "\\.")
        count = await self._agents.count(
            {"public_url": {"$regex": f"://{subdomain}\\.{escaped}"}}
        )
        return count > 0

    async def update_health(self, agent_id: str, healthy: bool) -> None:
        status = "active" if healthy else "inactive"
        await self._agents.update_one(
            {"agent_id": agent_id},
            {"$set": {"agent_status": status}},
        )

    async def mark_agents_inactive(
        self,
        agent_ids: list[str],
        *,
        source: str,
    ) -> int:
        if not agent_ids:
            return 0
        return await self._agents.update_many(
            {
                "agent_id": {"$in": agent_ids},
                "source": source,
                "agent_status": {"$ne": "inactive"},
            },
            {"$set": {"agent_status": "inactive", "inactive_since": utcnow()}},
        )

    async def delete_inactive_ownerless(
        self,
        *,
        source: str,
        inactive_before: datetime,
    ) -> int:
        # Records marked inactive before ``inactive_since`` existed fall back
        # to ``updated_at``.
        return await self._agents.delete_many(
            {
                "source": source,
                "provider_id": None,
                "agent_status": "inactive",
                "$or": [
                    {"inactive_since": {"$lt": inactive_before}},
                    {
                        "inactive_since": None,
                        "updated_at": {"$lt": inactive_before},
                    },
                ],
            }
        )

    async def increment_agent_call_count(self, agent_id: str, *, success: bool) -> None:
        update = {"$inc": {"call_count": 1}}
        if success:
            update["$inc"]["call_success_count"] = 1
        await self._agents.update_one({"agent_id": agent_id}, update)

    async def activate_agents(self, agent_ids: list[str]) -> int:
        if not agent_ids:
            return 0
        return await self._agents.update_many(
            {"agent_id": {"$in": agent_ids}},
            {"$set": {"agent_status": "active"}},
        )
