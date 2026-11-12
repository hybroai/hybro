from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from agent.facade import AgentFacade
from agent.network import AgentNetworkService
from agent.protocols import AgentGroupStore
from common.errors import AppError, ConflictError, NotFoundError, ValidationError
from models.agent_group import AgentGroup
from tests.test_agent_facade import Resolver, _doc
from tests.test_agent_repository import _repo


def _network(
    docs: list[dict],
    groups: list[AgentGroup] | None = None,
    *,
    requesting_user_id: str = "owner",
):
    repository, _ = _repo(docs)
    registry = AgentFacade(
        repository=repository,
        card_resolver=Resolver(),
        id_factory=lambda: "unused",
        now=lambda: datetime.now(UTC),
    )
    groups_by_id = {group.group_id: group for group in groups or []}
    group_store = AsyncMock(spec=AgentGroupStore)
    group_store.get_agent_group_by_id.side_effect = groups_by_id.get
    group_store.get_agent_groups_by_owner.side_effect = lambda owner_id: [
        group for group in groups_by_id.values() if group.owner_id == owner_id
    ]
    return AgentNetworkService(
        registry, group_store, registry, requesting_user_id=requesting_user_id
    )


@pytest.mark.asyncio
async def test_discovery_includes_all_visible_active_agents_without_truncation():
    docs = [_doc(f"private-{index}", "Private", public=False) for index in range(1001)]
    docs.extend(
        [
            _doc("public", "Public"),
            _doc("inactive", "Inactive", active=False),
            _doc("foreign", "Foreign private", public=False, provider_id="other"),
        ]
    )
    network = _network(docs)
    expected_ids = {f"private-{index}" for index in range(1001)} | {"public"}

    discovered = await network.discover()
    all_agents = await network.discover("all_agents")

    assert {agent.agent_id for agent in discovered.agents} == expected_ids
    assert {agent.agent_id for agent in all_agents.agents} == expected_ids


@pytest.mark.asyncio
async def test_saved_group_scopes_active_members_and_empty_group_never_expands():
    network = _network(
        [
            _doc("member", "Private member", public=False),
            _doc("inactive", "Inactive member", active=False),
            _doc("outside", "Outside"),
            _doc("foreign", "Foreign private", public=False, provider_id="other"),
        ],
        [
            AgentGroup(
                group_id="saved",
                name="Saved",
                type="user",
                owner_id="owner",
                agents=["member", "inactive", "removed", "foreign"],
            ),
            AgentGroup(group_id="empty", name="Empty", type="user", owner_id="owner"),
        ],
    )

    discovered = await network.discover("saved")
    empty = await network.discover("empty")

    assert [agent.agent_id for agent in discovered.agents] == ["member"]
    assert empty.agents == []
    assert (await network.resolve_target("member", "saved")).agent_id == "member"
    with pytest.raises(ConflictError):
        await network.resolve_target("outside", "saved")
    with pytest.raises(ConflictError):
        await network.resolve_target("member", "empty")


@pytest.mark.asyncio
async def test_target_resolution_distinguishes_missing_and_inactive_agents():
    network = _network(
        [
            _doc("private", "Private", public=False),
            _doc("inactive", "Inactive", active=False),
        ]
    )

    assert (await network.resolve_target("private")).agent_id == "private"
    assert (await network.resolve_target("private", "all_agents")).agent_id == "private"
    with pytest.raises(NotFoundError) as missing:
        await network.resolve_target("missing")
    assert missing.value.details["entity_id"] == "missing"
    with pytest.raises(ConflictError):
        await network.resolve_target("inactive")


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_foreign_private_target_is_indistinguishable_from_missing(active):
    network = _network(
        [_doc("foreign", "Secret", provider_id="other", public=False, active=active)]
    )

    assert (await network.discover()).agents == []
    with pytest.raises(NotFoundError) as denied:
        await network.resolve_target("foreign")
    assert denied.value.details == {"entity_type": "Agent", "entity_id": "foreign"}


@pytest.mark.asyncio
async def test_foreign_group_is_forbidden_even_with_a_public_member():
    network = _network(
        [_doc("public", "Public")],
        [
            AgentGroup(
                group_id="foreign",
                name="Foreign team",
                type="user",
                owner_id="other",
                agents=["public"],
            )
        ],
    )

    with pytest.raises(AppError) as discovery:
        await network.discover("foreign")
    assert discovery.value.code == "FORBIDDEN"
    with pytest.raises(AppError) as send:
        await network.resolve_target("public", "foreign")
    assert send.value.code == "FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_id", "error"),
    [("missing", NotFoundError), ("room_team", ValidationError)],
)
async def test_network_rejects_missing_and_room_scoped_groups(group_id, error):
    network = _network([_doc("agent", "Agent")])

    with pytest.raises(error):
        await network.discover(group_id)
    with pytest.raises(error):
        await network.resolve_target("agent", group_id)


@pytest.mark.asyncio
async def test_discovery_omits_transport_and_security_but_routing_keeps_them():
    doc = _doc("agent", "Agent")
    public_card = dict(doc["agent_card"])
    doc["agent_card"].update(
        {
            "authentication": {"schemes": ["Bearer"], "credentials": "secret"},
            "security": [{"bearer": []}],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "supportedInterfaces": [
                {"url": "https://agent.example/a2a", "protocolBinding": "JSONRPC"}
            ],
            "extensions": {"custom": {"supported": True}},
        }
    )
    network = _network([doc])

    discovered = await network.discover()

    assert discovered.agents[0].agent_card == {
        key: value for key, value in public_card.items() if key != "url"
    }
    target = await network.resolve_target("agent")
    assert target.raw_card["authentication"]["credentials"] == "secret"
    assert (
        target.raw_card["supportedInterfaces"][0]["url"] == "https://agent.example/a2a"
    )


def _skilled_doc(agent_id: str, name: str, skill: str, **kwargs) -> dict:
    doc = _doc(agent_id, name, **kwargs)
    doc["agent_card"]["skills"] = [
        {
            "id": f"{agent_id}-skill",
            "name": skill,
            "description": f"{skill} on request",
            "tags": [skill.split()[0].lower()],
            "examples": [f"Please do {skill}"],
            "inputModes": None,
        }
    ]
    return doc


def _research_group(**overrides) -> AgentGroup:
    values = {
        "group_id": "research",
        "name": "Research",
        "type": "user",
        "owner_id": "owner",
        "agents": ["weather", "inactive", "removed"],
    }
    values.update(overrides)
    return AgentGroup(**values)


@pytest.mark.asyncio
async def test_list_groups_counts_only_active_visible_members():
    network = _network(
        [
            _doc("weather", "Weather"),
            _doc("inactive", "Inactive", active=False),
            _doc("foreign", "Foreign", public=False, provider_id="other"),
        ],
        [
            _research_group(agents=["weather", "inactive", "removed", "foreign"]),
            AgentGroup(group_id="empty", name="Empty", type="user", owner_id="owner"),
            AgentGroup(group_id="theirs", name="Theirs", type="user", owner_id="other"),
        ],
    )

    listed = await network.list_groups()

    assert [(g.group_id, g.agent_count) for g in listed.groups] == [
        ("research", 1),
        ("empty", 0),
    ]
    assert listed.groups[0].name == "Research"


@pytest.mark.asyncio
async def test_search_ranks_compact_matches_with_group_membership():
    network = _network(
        [
            _skilled_doc("weather", "Weather Agent", "Weather forecast"),
            _skilled_doc("travel", "Travel Agent", "Trip planning"),
        ],
        [_research_group()],
    )

    found = await network.search("weather forecast")

    match = found.agents[0]
    assert match.agent_id == "weather"
    assert match.name == "Weather Agent"
    assert match.input_modes == ["text"]
    assert match.output_modes == ["text"]
    assert [g.group_id for g in match.groups] == ["research"]
    assert match.skills[0].model_dump() == {
        "id": "weather-skill",
        "name": "Weather forecast",
        "description": "Weather forecast on request",
        "tags": ["weather"],
    }
    assert match.score > 0


@pytest.mark.asyncio
async def test_search_scope_never_expands_beyond_the_group():
    network = _network(
        [
            _skilled_doc("weather", "Weather Agent", "Weather forecast"),
            _skilled_doc("other-weather", "Other Weather", "Weather forecast"),
        ],
        [
            _research_group(),
            AgentGroup(group_id="empty", name="Empty", type="user", owner_id="owner"),
            AgentGroup(
                group_id="theirs",
                name="Theirs",
                type="user",
                owner_id="other",
                agents=["weather"],
            ),
        ],
    )

    scoped = await network.search("weather", "research", limit=5)
    empty = await network.search("weather", "empty")

    assert [agent.agent_id for agent in scoped.agents] == ["weather"]
    assert empty.agents == []
    with pytest.raises(AppError) as forbidden:
        await network.search("weather", "theirs")
    assert forbidden.value.code == "FORBIDDEN"
    with pytest.raises(ValidationError):
        await network.search("weather", "room_team")


@pytest.mark.asyncio
async def test_profile_omits_transport_and_security_details():
    doc = _skilled_doc("weather", "Weather Agent", "Weather forecast")
    doc["agent_card"].update(
        {
            "version": "1.2.0",
            "provider": {"organization": "Hybro", "url": "https://hybro.ai"},
            "capabilities": {"streaming": True},
            "defaultOutputModes": ["text", "application/json"],
            "authentication": {"credentials": "secret"},
            "securitySchemes": {"bearer": {"type": "http"}},
            "supportedInterfaces": [{"url": "http://weather-agent:7001"}],
        }
    )
    network = _network([doc], [_research_group()])

    profile = await network.get_profile("weather")

    dumped = profile.model_dump()
    assert dumped == {
        "agent_id": "weather",
        "status": "active",
        "name": "Weather Agent",
        "description": None,
        "version": "1.2.0",
        "provider": "Hybro",
        "streaming": True,
        "input_modes": ["text"],
        "output_modes": ["text", "application/json"],
        "skills": [
            {
                "id": "weather-skill",
                "name": "Weather forecast",
                "description": "Weather forecast on request",
                "tags": ["weather"],
                "examples": ["Please do Weather forecast"],
                "input_modes": [],
                "output_modes": [],
            }
        ],
        "groups": [{"group_id": "research", "name": "Research"}],
    }
    assert "secret" not in str(dumped)
    assert "weather-agent:7001" not in str(dumped)


@pytest.mark.asyncio
async def test_profile_hides_missing_and_foreign_private_agents():
    network = _network(
        [
            _doc("foreign", "Secret", provider_id="other", public=False),
            _doc("inactive", "Inactive", active=False),
        ]
    )

    assert (await network.get_profile("inactive")).status == "inactive"
    for agent_id in ("missing", "foreign"):
        with pytest.raises(NotFoundError):
            await network.get_profile(agent_id)


@pytest.mark.asyncio
async def test_profile_tolerates_malformed_remote_card_fields():
    doc = _doc("odd", "Odd")
    doc["agent_card"].update(
        {
            "skills": ["not-a-skill", {"name": "Real", "tags": "x", "examples": [1]}],
            "provider": "not-an-object",
            "defaultInputModes": "text",
        }
    )
    network = _network([doc])

    profile = await network.get_profile("odd")

    assert profile.provider is None
    assert profile.input_modes == ["text"]
    assert [skill.name for skill in profile.skills] == ["Real"]
    assert profile.skills[0].tags == []
    assert profile.skills[0].examples == []


@pytest.mark.asyncio
async def test_search_ranks_beyond_a_thousand_agents():
    docs = [_doc(f"filler-{index:04d}", "Filler") for index in range(1000)]
    docs.append(_skilled_doc("weather", "Weather Agent", "Weather forecast"))
    network = _network(docs)

    found = await network.search("weather forecast")

    assert found.agents[0].agent_id == "weather"
