import inspect
from datetime import UTC, datetime

import pytest

from agent.facade import AgentFacade


def _doc(
    agent_id: str,
    name: str,
    *,
    description: str = "",
    provider_id: str = "owner",
    public: bool = True,
    active: bool = True,
    input_modes: list[str] | None = None,
) -> dict:
    return {
        "agent_id": agent_id,
        "provider_id": provider_id,
        "agent_status": "active" if active else "inactive",
        "is_public": public,
        "agent_card": {
            "name": name,
            "description": description,
            "url": f"https://example.com/{agent_id}",
            "skills": [],
            "defaultInputModes": input_modes or ["text"],
        },
    }


class Repository:
    def __init__(self, docs: list[dict], text_rows: list[dict] | None = None):
        self.docs = {doc["agent_id"]: doc for doc in docs}
        self.text_rows = text_rows or []
        self.text_search_calls: list[list[str]] = []

    async def list_visible(
        self,
        *,
        user_id=None,
        active_only=False,
        agent_ids=None,
        query=None,
        limit=0,
    ):
        del query, limit
        docs = list(self.docs.values())
        if active_only:
            docs = [doc for doc in docs if doc["agent_status"] == "active"]
        if agent_ids is not None:
            docs = [doc for doc in docs if doc["agent_id"] in agent_ids]
        return [
            doc
            for doc in docs
            if doc.get("is_public", True) or doc.get("provider_id") == user_id
        ]

    async def text_search(self, agent_ids, query, limit):
        del query, limit
        self.text_search_calls.append(list(agent_ids))
        return [row for row in self.text_rows if row["agent_id"] in agent_ids]

    async def get_by_id(self, agent_id):
        return self.docs.get(agent_id)

    async def delete(self, agent_id):
        return self.docs.pop(agent_id, None) is not None


class Resolver:
    async def resolve_card(self, _url):
        return None


def _facade(repo: Repository, exclusion_reader=None) -> AgentFacade:
    return AgentFacade(
        repository=repo,
        card_resolver=Resolver(),
        exclusion_reader=exclusion_reader,
        id_factory=lambda: "new",
        now=lambda: datetime.now(UTC),
    )


def test_gateway_public_url_configuration_is_not_exposed():
    assert "gateway_base_url" not in inspect.signature(AgentFacade).parameters
    assert not hasattr(AgentFacade, "_gateway_public_url")


@pytest.mark.asyncio
async def test_safe_candidates_are_filtered_before_mongo_text_search():
    repo = Repository(
        [
            _doc("public", "Writer", description="reports"),
            _doc("private", "Secret", description="reports", public=False),
            _doc("inactive", "Old", description="reports", active=False),
        ],
        text_rows=[
            {"agent_id": "private", "score": 99},
            {"agent_id": "inactive", "score": 98},
            {"agent_id": "public", "score": 1},
        ],
    )
    matches = await _facade(repo).match_agents("reports", requesting_user_id="other")
    assert [match.agent_id for match in matches] == ["public"]
    assert repo.text_search_calls == [["public"]]


@pytest.mark.asyncio
async def test_mongo_failure_uses_latin_and_cjk_application_fallback():
    class BrokenRepository(Repository):
        async def text_search(self, agent_ids, query, limit):
            raise RuntimeError("text index unavailable")

    repo = BrokenRepository(
        [
            _doc("travel", "旅行规划"),
            _doc("writer", "Writer"),
        ]
    )
    matches = await _facade(repo).match_agents("旅行")
    assert [match.agent_id for match in matches] == ["travel"]
    assert matches[0].reason == "Lexical match score: 1.00"


@pytest.mark.asyncio
async def test_required_input_modes_are_filtered_before_scoring():
    repo = Repository(
        [
            _doc("image", "Vision", input_modes=["image/png"]),
            _doc("text", "Writer", input_modes=["text"]),
        ],
        text_rows=[
            {"agent_id": "text", "score": 10},
            {"agent_id": "image", "score": 1},
        ],
    )
    matches = await _facade(repo).match_for_message(
        "vision",
        required_input_modes=["image/png"],
    )
    assert [match["agent_id"] for match in matches] == ["image"]
    assert repo.text_search_calls == [["image"]]


@pytest.mark.asyncio
async def test_delete_agent_only_mutates_mongo_repository():
    repo = Repository([_doc("a1", "Writer")])
    assert await _facade(repo).delete_agent("a1", "owner") is True
    assert repo.docs == {}


@pytest.mark.asyncio
async def test_delete_agent_rejects_discovered_local_agent():
    local_agent = _doc("local-1", "Local Writer")
    local_agent["source"] = "local"
    repo = Repository([local_agent])

    assert await _facade(repo).delete_agent("local-1", "other-owner") is False
    assert "local-1" in repo.docs
