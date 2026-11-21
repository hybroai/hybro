"""Sign-in is enforced: the owner's cookie is the only user credential.

The app is the real one with its middleware and routes. Only the services the
checks read are bound, so each test shows what a route refuses before any
work.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import main
from account.protocols import OwnerCreate
from api_gateway.owner_session import SESSION_COOKIE
from tests.fakes.account import MemoryAccountStore, build_service
from tests.fakes.client import store_with_requirement

API = "/api/v1"
PASSWORD = "correct horse battery"
OWNER = {"name": "Ada", "email": "ada@example.com", "password": PASSWORD}
HOST = "hybro.test"
ORIGIN = {"Origin": f"http://{HOST}"}


class RecordingCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def get_visible(self, *, agent_id, user_id):
        from common.dto import AgentRouteResponse

        self.calls.append(("get", user_id))
        return AgentRouteResponse(success=True)

    async def list_visible(self, *, user_id, active_only=False):
        from common.dto import AgentRouteResponse

        self.calls.append(("list", user_id))
        return AgentRouteResponse(success=True, agents=[])


async def make_app(*, with_owner: bool = True, **account_kwargs):
    from client import ClientService

    app = main.create_app()
    store = account_kwargs.pop("store", None) or MemoryAccountStore()
    account = build_service(store=store, **account_kwargs)
    if with_owner:
        await account.create_owner(OwnerCreate(**OWNER))
    app.state.owner_account = account
    app.state.client_registry = ClientService(
        store_with_requirement(required=False), auth_mode="mock"
    )
    catalog = RecordingCatalog()
    app.state.api_gateway_deps = SimpleNamespace(agent_catalog=catalog)
    return app, account, catalog


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=f"http://{HOST}",
        headers=ORIGIN,
    )


async def sign_in(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/sign-in",
        json={"email": OWNER["email"], "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert SESSION_COOKIE in client.cookies


# -- No production path skips authentication ----------------------------------


def test_the_production_app_installs_no_user_override():
    # The session's ``app`` fixture adds the test user to ``main.app``; a
    # fresh app and the module itself carry none.
    assert main.create_app().dependency_overrides == {}
    source = Path(main.__file__).read_text()
    assert "dependency_overrides" not in source


def test_the_clerk_sign_in_and_query_token_are_gone():
    from common import auth

    for name in (
        "get_current_user",
        "get_optional_user",
        "get_current_user_with_query_token",
        "verify_clerk_token_from_request",
        "bind_auth_config",
        "resolve_provider_name",
    ):
        assert not hasattr(auth, name), name


# -- Owner routes ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/agent/getAllAgents",
        "/agent/getAgent/agent-1",
        "/rooms",
        "/sessions",
        "/clients",
        "/setup/status",
        "/settings",
    ],
)
async def test_owner_routes_refuse_a_request_without_the_cookie(path):
    app, _, catalog = await make_app()
    async with client_for(app) as client:
        response = await client.get(f"{API}{path}")

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "not_signed_in"
    assert catalog.calls == []


async def test_a_bearer_token_opens_no_owner_route():
    app, _, catalog = await make_app()
    async with client_for(app) as client:
        response = await client.get(
            f"{API}/agent/getAllAgents",
            headers={"Authorization": "Bearer clerk-session-jwt"},
        )

    assert response.status_code == 401
    assert catalog.calls == []


async def test_get_agent_and_get_all_agents_act_for_the_signed_in_owner():
    app, _, catalog = await make_app()
    async with client_for(app) as client:
        await sign_in(client)
        listing = await client.get(f"{API}/agent/getAllAgents")
        single = await client.get(f"{API}/agent/getAgent/agent-1")

    assert listing.status_code == single.status_code == 200
    assert catalog.calls == [
        ("list", "user_local_developer"),
        ("get", "user_local_developer"),
    ]


async def test_owner_routes_refuse_before_the_account_exists():
    app, _, catalog = await make_app(with_owner=False)
    async with client_for(app) as client:
        response = await client.get(f"{API}/agent/getAllAgents")

    assert response.status_code == 401
    assert catalog.calls == []


async def test_the_event_stream_ignores_a_query_token():
    app, _, _ = await make_app()
    async with client_for(app) as client:
        response = await client.get(
            f"{API}/sse/room/room-1/events", params={"token": "clerk-session-jwt"}
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "not_signed_in"


async def test_the_owner_keeps_an_identity_taken_over_in_clerk_mode():
    """An account created in Clerk mode keeps working after a switch to mock."""
    store = MemoryAccountStore()
    clerk = build_service(
        store=store, auth_mode="clerk", admin_user_ids=frozenset({"user_admin"})
    )
    await clerk.create_owner(OwnerCreate(**OWNER))
    app, _, catalog = await make_app(with_owner=False, store=store)
    async with client_for(app) as client:
        await sign_in(client)
        response = await client.get(f"{API}/agent/getAllAgents")

    assert response.status_code == 200
    assert catalog.calls == [("list", "user_admin")]


# -- The Client API ----------------------------------------------------------------


async def test_the_client_api_refuses_until_the_account_exists():
    app, _, _ = await make_app(with_owner=False)
    async with client_for(app) as client:
        response = await client.get(f"{API}/agents/discovery")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "no_account"


async def test_the_client_api_acts_for_the_owner_record():
    from account.protocols import NoAccount

    store = MemoryAccountStore()
    account = build_service(
        store=store, auth_mode="clerk", admin_user_ids=frozenset({"user_admin"})
    )
    with pytest.raises(NoAccount):
        await account.acting_owner()
    await account.create_owner(OwnerCreate(**OWNER))
    # The record decides, whatever the mode says now.
    mock = build_service(store=store)

    owner = await mock.acting_owner()

    assert (owner.user_id, owner.name) == ("user_admin", "Ada")


# -- DNS rebinding -------------------------------------------------------------


def rebound_client(app) -> httpx.AsyncClient:
    # A page on a name the attacker points at this machine: the browser sends
    # that name as Host and as Origin, so the two match.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://rebound.example",
        headers={"Origin": "http://rebound.example"},
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_rebound_page_cannot_reach_the_no_token_client_api(method):
    app, _, catalog = await make_app()
    async with rebound_client(app) as client:
        response = await client.request(method, f"{API}/agents/discovery")

    assert response.status_code == 421
    assert response.json()["detail"]["code"] == "unknown_host"
    assert catalog.calls == []


async def test_a_rebound_page_cannot_sign_in():
    app, _, _ = await make_app()
    async with rebound_client(app) as client:
        response = await client.post(
            f"{API}/auth/sign-in",
            json={"email": OWNER["email"], "password": PASSWORD},
        )

    assert response.status_code == 421
    assert SESSION_COOKIE not in client.cookies
