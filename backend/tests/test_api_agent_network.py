"""Exercise authenticated HTTP access through Execution and the real A2A adapter."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from a2a_adapter import client_facade
from agent.facade import AgentFacade
from api_gateway.dependencies import get_api_gateway_deps
from common import auth
from common.auth import get_current_user
from models.agent_group import AgentGroup
from tests.fakes.a2a_v1 import make_agent_card
from tests.fakes.a2a_v1_agent import (
    A2A10AgentStub,
    completed_task_payload,
    input_required_task_payload,
)
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
    # The send fetches the live card from the registered URL.
    doc["normalized_url"] = "http://agent.test"
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
    app.dependency_overrides[get_api_gateway_deps] = lambda: SimpleNamespace(
        agent_service=registry, agent_matcher=registry, agent_group_store=groups
    )
    remote = A2A10AgentStub(
        send_result={"task": input_required_task_payload(question="Which runtime?")}
    )

    @asynccontextmanager
    async def bounded_client(*, timeout, **_policy):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=remote.app), timeout=timeout
        ) as client:
            yield client

    monkeypatch.setattr(client_facade, "bounded_client", bounded_client)
    monkeypatch.setattr(client_facade, "card_client", bounded_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hybro.test"
    ) as client:
        yield client, remote, collection, groups


def message_body(**overrides):
    return {
        "agent_id": "reviewer",
        "group_id": "team",
        "client_request_id": "pi-turn-1",
        "message": {"parts": [{"kind": "text", "text": "Review this code"}]},
        **overrides,
    }


async def test_mock_auth_discovery_send_and_continue_preserve_a2a_results(network_api):
    client, remote, _, _ = network_api
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

    response = await client.post("/api/v1/agents/messages", json=message_body())
    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"] == "reviewer"
    assert payload["client_request_id"] == "pi-turn-1"
    task = payload["result"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["status"]["message"]["parts"][0]["text"] == "Which runtime?"

    remote.send_result = {
        "task": completed_task_payload(text="Python 3.12 is supported")
    }
    continuation = message_body(
        client_request_id="pi-turn-2",
        message={
            "contextId": task["contextId"],
            "taskId": task["id"],
            "parts": [{"kind": "text", "text": "Python 3.12"}],
        },
    )
    response = await client.post("/api/v1/agents/messages", json=continuation)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"]["state"] == "TASK_STATE_COMPLETED"
    assert result["artifacts"][0]["parts"][0]["text"] == "Python 3.12 is supported"
    assert remote.last_message()["taskId"] == task["id"]
    assert remote.last_message()["contextId"] == task["contextId"]
    after = await client.get("/api/v1/agents/discovery", params={"group_id": "team"})
    assert after.json()["agents"] == discovered


async def test_send_rechecks_state_after_discovery_and_preserves_error_correlation(
    network_api,
):
    client, remote, collection, _ = network_api
    assert (await client.get("/api/v1/agents/discovery")).status_code == 200
    collection.docs[0]["agent_status"] = "inactive"
    response = await client.post("/api/v1/agents/messages", json=message_body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "CONFLICT"
    assert detail["client_request_id"] == "pi-turn-1"
    assert detail["agent_id"] == "reviewer"
    assert remote.requests == []


@pytest.mark.parametrize(
    "override",
    [
        {"agent_id": " "},
        {"agent_url": "http://unregistered.example/"},
        {"message": {"role": "agent", "parts": [{"kind": "text", "text": "x"}]}},
        {"message": {"parts": []}},
        {"message": {"taskId": " ", "parts": [{"kind": "text", "text": "x"}]}},
        {
            "message": {
                "parts": [{"kind": "file", "metadata": {"file_id": "room-secret"}}]
            }
        },
    ],
)
async def test_invalid_or_proxy_override_requests_never_reach_agent(
    network_api, override
):
    client, remote, _, _ = network_api
    response = await client.post(
        "/api/v1/agents/messages", json=message_body(**override)
    )
    assert response.status_code == 422
    assert remote.requests == []


async def test_group_errors_do_not_expand_discovery_scope(network_api):
    client, remote, _, groups = network_api
    groups.get_agent_group_by_id.return_value = None
    missing = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "missing"}
    )
    assert missing.status_code == 404
    room = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "room_team"}
    )
    assert room.status_code == 422
    assert remote.requests == []


async def test_groups_search_and_profile_help_choose_an_agent(network_api):
    client, remote, _, _ = network_api

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
    assert remote.requests == []


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
    client, remote, _, _ = network_api
    response = await client.get(path, params=params)
    assert response.status_code == status
    assert remote.requests == []


def clerk_session(monkeypatch, app, *, user_id: str | None):
    """Exercise real auth dependencies, replacing only Clerk's remote boundary."""
    monkeypatch.delitem(app.dependency_overrides, get_current_user)
    monkeypatch.setattr(
        auth,
        "authenticate_request",
        lambda request, options: SimpleNamespace(
            is_signed_in=user_id is not None,
            payload={"sub": user_id, "sid": "test-session"} if user_id else None,
        ),
    )


async def test_clerk_anonymous_requests_fail_before_store_or_remote_access(
    network_api, monkeypatch, app
):
    client, remote, collection, groups = network_api
    clerk_session(monkeypatch, app, user_id=None)

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "team"}
    )
    send = await client.post("/api/v1/agents/messages", json=message_body())

    assert discovery.status_code == send.status_code == 401
    assert discovery.headers["www-authenticate"] == "Bearer"
    assert send.headers["www-authenticate"] == "Bearer"
    assert collection.find_calls == collection.find_one_calls == []
    groups.get_agent_group_by_id.assert_not_awaited()
    assert remote.requests == []


@pytest.mark.parametrize("status", ["active", "inactive"])
async def test_clerk_cannot_discover_or_invoke_foreign_private_agent(
    network_api, monkeypatch, app, status
):
    client, remote, collection, _ = network_api
    clerk_session(monkeypatch, app, user_id="another-user")
    collection.docs[0]["agent_status"] = status

    discovery = await client.get("/api/v1/agents/discovery")
    assert discovery.status_code == 200
    assert discovery.json() == {"agents": []}
    denied = await client.post(
        "/api/v1/agents/messages", json=message_body(group_id="all_agents")
    )
    missing = await client.post(
        "/api/v1/agents/messages",
        json=message_body(agent_id="missing", group_id="all_agents"),
    )
    assert denied.status_code == missing.status_code == 404
    detail = denied.json()["detail"]
    assert detail["code"] == missing.json()["detail"]["code"] == "NOT_FOUND"
    assert detail["agent_id"] == "reviewer"
    assert detail["client_request_id"] == "pi-turn-1"
    assert "status" not in detail
    assert "agent_card" not in detail
    assert remote.requests == []


async def test_clerk_cannot_use_guessed_foreign_group_even_for_public_agent(
    network_api, monkeypatch, app
):
    client, remote, collection, groups = network_api
    clerk_session(monkeypatch, app, user_id="another-user")
    collection.docs[0]["is_public"] = True
    group = groups.get_agent_group_by_id.return_value
    group.group_id = uuid5(
        NAMESPACE_URL, f"hybro-agent-group:{group.owner_id}:review"
    ).hex

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": group.group_id}
    )
    send = await client.post(
        "/api/v1/agents/messages", json=message_body(group_id=group.group_id)
    )

    assert discovery.status_code == send.status_code == 403
    assert discovery.json()["detail"]["code"] == "FORBIDDEN"
    detail = send.json()["detail"]
    assert detail["code"] == "FORBIDDEN"
    assert detail["client_request_id"] == "pi-turn-1"
    assert detail["agent_id"] == "reviewer"
    assert collection.find_calls == collection.find_one_calls == []
    assert remote.requests == []


@pytest.mark.parametrize("public", [True, False])
async def test_clerk_can_discover_and_invoke_public_or_owned_agent(
    network_api, monkeypatch, app, public
):
    client, remote, collection, groups = network_api
    user_id = "another-user" if public else "user_local_developer"
    clerk_session(monkeypatch, app, user_id=user_id)
    collection.docs[0]["is_public"] = public
    groups.get_agent_group_by_id.return_value.owner_id = user_id

    discovery = await client.get(
        "/api/v1/agents/discovery", params={"group_id": "team"}
    )
    assert discovery.status_code == 200
    assert [agent["agent_id"] for agent in discovery.json()["agents"]] == ["reviewer"]
    response = await client.post("/api/v1/agents/messages", json=message_body())
    assert response.status_code == 200
    assert response.json()["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert remote.last_message()["parts"][0]["text"] == "Review this code"


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
