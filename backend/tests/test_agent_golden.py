import logging
from datetime import UTC, datetime

import pytest

from agent.facade import AgentFacade, AgentRegistrationConflict
from common.dto.agent import AgentCardSnapshot
from tests.test_agent_facade import Repository, _doc


class CardResolver:
    def __init__(self, card: AgentCardSnapshot):
        self.card = card

    async def resolve_card(self, _url):
        return self.card


class GoldenRepository(Repository):
    async def find_by_normalized_url(self, normalized_url, provider_id=None):
        del provider_id
        return next(
            (
                doc
                for doc in self.docs.values()
                if doc.get("normalized_url") == normalized_url
            ),
            None,
        )

    async def public_url_exists(self, _subdomain, _base_domain):
        return False

    async def upsert(self, agent_id, doc):
        self.docs[agent_id] = doc

    async def get_public(self, limit=50):
        return [doc for doc in self.docs.values() if doc["is_public"]][:limit]

    async def get_by_provider(self, provider_id):
        return [
            doc for doc in self.docs.values() if doc.get("provider_id") == provider_id
        ]


def _registered(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "agent_registered"]


def _golden_facade(repo, card):
    return AgentFacade(
        repository=repo,
        card_resolver=CardResolver(card),
        id_factory=lambda: "new-agent",
        now=lambda: datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_agent_golden_register_list_match_and_direct_callability():
    card = AgentCardSnapshot(
        agent_id="snapshot",
        name="Writer",
        description="Writes stories",
        url="https://writer.example",
        raw_card={
            "name": "Writer",
            "description": "Writes stories",
            "url": "https://writer.example",
            "defaultInputModes": ["text"],
        },
    )
    repo = GoldenRepository([])
    facade = _golden_facade(repo, card)

    registered = await facade.register_agent("https://writer.example", "u1")
    public = await facade.list_public_agents()
    owned = await facade.list_agents("u1")
    matched = await facade.match_agents("write stories", requesting_user_id=None)

    assert registered.agent_id == "new-agent"
    assert repo.docs["new-agent"]["normalized_url"] == "https://writer.example"
    assert public[0].agent_id == "new-agent"
    assert owned[0].agent_id == "new-agent"
    assert matched[0].agent_id == "new-agent"
    assert matched[0].agent.url == "https://writer.example"
    assert matched[0].score > 0
    assert await facade.is_agent_healthy("new-agent") is True
    assert await facade.is_directly_callable("new-agent") is True


@pytest.mark.asyncio
async def test_agent_golden_private_and_inactive_agents_do_not_leak_to_discovery():
    repo = GoldenRepository(
        [
            _doc("public", "Public", description="query"),
            _doc(
                "private",
                "Private",
                description="query",
                provider_id="u1",
                public=False,
            ),
            _doc("inactive", "Inactive", description="query", active=False),
        ],
        text_rows=[
            {"agent_id": "private", "score": 99},
            {"agent_id": "inactive", "score": 98},
            {"agent_id": "public", "score": 1},
        ],
    )
    facade = _golden_facade(
        repo,
        AgentCardSnapshot(
            agent_id="unused",
            name="unused",
            url="https://unused.example",
            raw_card={},
        ),
    )

    unauthenticated = await facade.match_agents("query", requesting_user_id=None)
    filtered_empty = await facade.match_agents("query", filter_ids=[])

    assert [match.agent_id for match in unauthenticated] == ["public"]
    assert repo.text_search_calls == [["public"]]
    assert filtered_empty == []


def _writer_card() -> AgentCardSnapshot:
    return AgentCardSnapshot(
        agent_id="snapshot",
        name="Writer",
        description="Writes stories",
        url="https://writer.example",
        raw_card={"name": "Writer", "url": "https://writer.example"},
    )


def _existing(**overrides) -> dict:
    doc = {
        "agent_id": "local-1",
        "provider_id": None,
        "source": "local",
        "normalized_url": "https://writer.example",
        "agent_status": "inactive",
        "inactive_since": datetime(2026, 1, 1, tzinfo=UTC),
        "is_public": True,
        "agent_card": {"name": "Writer", "url": "https://writer.example"},
    }
    doc.update(overrides)
    return doc


@pytest.mark.asyncio
async def test_register_takes_over_inactive_ownerless_discovery_record():
    repo = GoldenRepository([])
    repo.docs["local-1"] = _existing()
    facade = _golden_facade(repo, _writer_card())

    registered = await facade.register_agent("https://writer.example", "u1")

    assert registered.agent_id == "local-1"
    assert list(repo.docs) == ["local-1"]
    doc = repo.docs["local-1"]
    assert (doc["provider_id"], doc["source"], doc["agent_status"]) == (
        "u1",
        "cloud",
        "active",
    )
    assert doc["inactive_since"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"provider_id": "u1", "source": "cloud"}, "already_registered"),
        ({"provider_id": "u2", "source": "cloud"}, "owned_by_another_provider"),
        (
            {"provider_id": "u2", "source": "cloud", "agent_status": "inactive"},
            "owned_by_another_provider",
        ),
        ({"agent_status": "active"}, "active_local_discovery"),
        ({"source": "cloud"}, "owned_by_another_provider"),
    ],
)
async def test_register_refuses_records_it_cannot_take_over(caplog, overrides, code):
    caplog.set_level(logging.DEBUG)
    repo = GoldenRepository([])
    repo.docs["local-1"] = _existing(**overrides)
    before = dict(repo.docs["local-1"])
    facade = _golden_facade(repo, _writer_card())

    with pytest.raises(AgentRegistrationConflict) as exc_info:
        await facade.register_agent("https://writer.example", "u1")

    assert exc_info.value.code == code
    assert repo.docs["local-1"] == before
    assert _registered(caplog) == []


@pytest.mark.asyncio
async def test_register_refuses_takeover_when_discovery_changed_the_record(caplog):
    caplog.set_level(logging.DEBUG)
    repo = GoldenRepository([])
    repo.docs["local-1"] = _existing(agent_status="active")
    before = dict(repo.docs["local-1"])
    stale = _existing()
    reads = iter([stale])
    find = repo.find_by_normalized_url

    async def stale_first_read(normalized_url, provider_id=None):
        return next(reads, None) or await find(normalized_url, provider_id)

    repo.find_by_normalized_url = stale_first_read
    facade = _golden_facade(repo, _writer_card())

    with pytest.raises(AgentRegistrationConflict) as exc_info:
        await facade.register_agent("https://writer.example", "u1")

    assert exc_info.value.code == "active_local_discovery"
    assert repo.docs["local-1"] == before
    assert _registered(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("takeover", [False, True])
async def test_register_emits_agent_registered(caplog, takeover):
    caplog.set_level(logging.DEBUG)
    repo = GoldenRepository([])
    if takeover:
        repo.docs["local-1"] = _existing()
    facade = _golden_facade(repo, _writer_card())

    registered = await facade.register_agent("https://writer.example", "u1")

    [record] = _registered(caplog)
    assert record.levelno == logging.INFO
    assert record.agent_id == registered.agent_id
    assert record.source == "manual"
    assert record._hybro_system is True
