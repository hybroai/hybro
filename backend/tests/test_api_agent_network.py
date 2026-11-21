"""Exercise HTTP access to the agent network's read routes on the Source API.

The caller is a source; the routes read the agents of the owner it acts for.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from agent.facade import AgentFacade
from api_gateway.dependencies import get_api_gateway_deps
from models.agent_group import AgentGroup
from source import SourceService
from tests.fakes.a2a_v1 import make_agent_card
from tests.fakes.account import build_service, store_with_owner
from tests.fakes.source import store_with_requirement
from tests.test_agent_facade import Resolver, _doc
from tests.test_agent_repository import _repo


@pytest.fixture
async def network_api(monkeypatch, app):
    card = MessageToDict(
        make_agent_card(
            url="http://agent.test/",
            skills=[
                {
                    "id": "review",
                    "name": "Code review",
                    "description": "Review code",
                    "tags": ["review"],
                }
            ],
        )
    )
    doc = _doc("reviewer", "Reviewer", public=False, provider_id="user_local_developer")
    doc["agent_card"] = card
    repository, collection = _repo([doc])
    registry = AgentFacade(
        repository=repository,
        card_resolver=Resolver(),
        id_factory=lambda: "unused",
        now=lambda: datetime.now(UTC),
    )
    team = AgentGroup(
        group_id="team",
        name="Review team",
        type="user",
        owner_id="user_local_developer",
        agents=["reviewer"],
    )
    groups = SimpleNamespace(
        get_agent_group_by_id=AsyncMock(return_value=team),
        get_agent_groups_by_owner=AsyncMock(
            side_effect=lambda owner_id: [team] if owner_id == team.owner_id else []
        ),
    )
    monkeypatch.setattr(app, "dependency_overrides", app.dependency_overrides.copy())
    monkeypatch.setattr(
        app.state,
        "source_registry",
        SourceService(store_with_requirement(required=False), auth_mode="mock"),
        raising=False,
    )
    monkeypatch.setattr(
        app.state,
        "owner_account",
        build_service(store=store_with_owner()),
        raising=False,
    )
    app.dependency_overrides[get_api_gateway_deps] = lambda: SimpleNamespace(
        agent_service=registry, agent_matcher=registry, agent_group_store=groups
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hybro.test"
    ) as client:
        yield client, collection, groups


async def test_mock_auth_discovery_hides_transport_details(network_api):
    client, _, _ = network_api
    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "team"}
    )
    assert discovery.status_code == 200
    discovered = discovery.json()["agents"]
    assert [agent["agent_id"] for agent in discovered] == ["reviewer"]
    # Clients reach agents through Hybro by agent_id, never directly.
    card = discovered[0]["agent_card"]
    assert "supportedInterfaces" not in card
    assert "url" not in card
    assert "http://agent.test" not in str(card)


async def test_direct_send_route_is_gone(network_api):
    client, _, _ = network_api
    response = await client.post(
        "/api/v1/agents/messages",
        json={
            "agent_id": "reviewer",
            "message": {"parts": [{"kind": "text", "text": "Review this code"}]},
        },
    )
    assert response.status_code == 404


async def test_group_errors_do_not_expand_discovery_scope(network_api):
    client, _, groups = network_api
    groups.get_agent_group_by_id.return_value = None
    missing = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "missing"}
    )
    assert missing.status_code == 404
    room = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "room_team"}
    )
    assert room.status_code == 422


async def test_groups_search_and_profile_help_choose_an_agent(network_api):
    client, _, _ = network_api

    groups = await client.get("/api/v1/agents/groups")
    assert groups.status_code == 200
    assert groups.json() == {
        "groups": [
            {
                "group_id": "team",
                "name": "Review team",
                "description": None,
                "agent_count": 1,
            }
        ]
    }

    search = await client.get(
        "/api/v1/agents/search",
        params={"query": "code review", "group_id": "team", "limit": 3},
    )
    assert search.status_code == 200
    [match] = search.json()["agents"]
    assert match["agent_id"] == "reviewer"
    assert match["skills"][0]["name"] == "Code review"
    assert match["groups"] == [{"group_id": "team", "name": "Review team"}]
    assert "supportedInterfaces" not in match

    profile = await client.get("/api/v1/agents/reviewer/profile")
    assert profile.status_code == 200
    body = profile.json()
    assert body["agent_id"] == "reviewer"
    assert body["skills"][0]["id"] == "review"
    assert "http://agent.test/" not in profile.text


@pytest.mark.parametrize(
    ("path", "params", "status"),
    [
        ("/api/v1/agents/search", {}, 422),
        ("/api/v1/agents/search", {"query": "review", "limit": 21}, 422),
        ("/api/v1/agents/search", {"query": "review", "group_id": "room_team"}, 422),
        ("/api/v1/agents/missing/profile", {}, 404),
    ],
)
async def test_search_and_profile_reject_invalid_requests(
    network_api, path, params, status
):
    client, _, _ = network_api
    response = await client.get(path, params=params)
    assert response.status_code == status


async def clerk_source(monkeypatch, app, client, *, owner_id: str | None):
    """A Clerk-mode configuration, which requires a token.

    ``owner_id`` None sends no token; otherwise the owner account took that
    administrator's id, and the request carries a source token and acts for it.
    """
    sources = SourceService(store_with_requirement(required=True), auth_mode="clerk")
    monkeypatch.setattr(app.state, "source_registry", sources)
    store = store_with_owner(owner_id or "user_local_developer")
    monkeypatch.setattr(
        app.state, "owner_account", build_service(store=store, auth_mode="clerk")
    )
    if owner_id is not None:
        token = (await sources.create("Tests")).token
        client.headers["Authorization"] = f"Bearer {token}"


async def test_clerk_mode_without_a_token_fails_before_store_access(
    network_api, monkeypatch, app
):
    client, collection, groups = network_api
    await clerk_source(monkeypatch, app, client, owner_id=None)

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "team"}
    )

    assert discovery.status_code == 401
    assert discovery.headers["www-authenticate"] == "Bearer"
    assert collection.find_calls == collection.find_one_calls == []
    groups.get_agent_group_by_id.assert_not_awaited()


@pytest.mark.parametrize("status", ["active", "inactive"])
async def test_clerk_cannot_discover_foreign_private_agent(
    network_api, monkeypatch, app, status
):
    client, collection, _ = network_api
    await clerk_source(monkeypatch, app, client, owner_id="another-user")
    collection.docs[0]["agent_status"] = status

    discovery = await client.get("/api/v1/agents/discovery")
    assert discovery.status_code == 200
    assert discovery.json() == {"agents": []}
    profile = await client.get("/api/v1/agents/reviewer/profile")
    assert profile.status_code == 404
    assert profile.json()["detail"]["code"] == "NOT_FOUND"


async def test_clerk_cannot_use_guessed_foreign_group_even_for_public_agent(
    network_api, monkeypatch, app
):
    client, collection, groups = network_api
    await clerk_source(monkeypatch, app, client, owner_id="another-user")
    collection.docs[0]["is_public"] = True
    group = groups.get_agent_group_by_id.return_value
    group.group_id = uuid5(
        NAMESPACE_URL, f"hybro-agent-group:{group.owner_id}:review"
    ).hex

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": group.group_id}
    )

    assert discovery.status_code == 403
    assert discovery.json()["detail"]["code"] == "FORBIDDEN"
    assert collection.find_calls == collection.find_one_calls == []


@pytest.mark.parametrize("public", [True, False])
async def test_clerk_can_discover_public_or_owned_agent(
    network_api, monkeypatch, app, public
):
    client, collection, groups = network_api
    user_id = "another-user" if public else "user_local_developer"
    await clerk_source(monkeypatch, app, client, owner_id=user_id)
    collection.docs[0]["is_public"] = public
    groups.get_agent_group_by_id.return_value.owner_id = user_id

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "team"}
    )
    assert discovery.status_code == 200
    assert [agent["agent_id"] for agent in discovery.json()["agents"]] == ["reviewer"]


def test_discovery_card_keeps_choice_fields_and_drops_transport_and_security():
    from agent.network import _discovery_card

    raw = {
        "name": "Reviewer",
        "description": "Reviews code",
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "provider": {"organization": "Acme", "url": "https://acme.test"},
        "documentationUrl": "https://docs.acme.test",
        "iconUrl": "https://acme.test/icon.png",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {"id": "review", "name": "Review", "security": [{"oauth": ["read"]}]}
        ],
        "url": "http://reviewer:7001",
        "preferredTransport": "JSONRPC",
        "supportedInterfaces": [
            {"url": "http://reviewer:7001", "protocolBinding": "JSONRPC"}
        ],
        "additionalInterfaces": [{"url": "http://reviewer:7002", "transport": "GRPC"}],
        "security": [{"oauth": ["read"]}],
        "securitySchemes": {"oauth": {"type": "oauth2"}},
        "signatures": [{"protected": "x", "signature": "y"}],
        "supportsAuthenticatedExtendedCard": True,
        "authentication": {"schemes": ["bearer"], "credentials": "secret"},
    }

    card = _discovery_card(raw)

    assert set(card) == {
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
    }
    assert card["skills"] == [{"id": "review", "name": "Review"}]
    assert "reviewer:700" not in str(card)
    assert raw["url"] == "http://reviewer:7001"
