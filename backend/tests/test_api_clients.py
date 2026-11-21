"""Client tokens on the Client API, the owner's /clients routes, and the
owner's own session routes.

The app is the real one with its middleware; the session facade is a fake that
records who each call acted for, so the tests show which caller a route
resolves and what it refuses before any session work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from account.protocols import OWNER_USER_ID, OwnerCreate
from api_gateway.client_token import ClientCaller
from api_gateway.dependencies import get_agent_network
from api_gateway.owner_session import get_current_user
from client import ClientService
from client.protocols import NO_TOKEN, ClientIdentity
from common.auth import ClerkUser
from main import create_app
from models.session import (
    SessionClosed,
    SessionListResponse,
    SessionMessageResponse,
    SessionStatus,
)
from session.protocols import SessionOwner
from tests.fakes.account import build_service, sign_out, store_with_owner
from tests.fakes.client import MemoryClientStore, store_with_requirement

NOW = datetime(2026, 11, 20, tzinfo=UTC)
API = "/api/v1"
SEND = f"{API}/sessions/messages"
BODY = {
    "agent_id": "agent-1",
    "text": "Plan a trip",
    "client_request_id": "cr-1",
    "client_label": "Buyer agent",
}
SOURCE_ROUTES = (
    ("POST", SEND, BODY),
    ("GET", f"{API}/sessions/sess-1", None),
    ("POST", f"{API}/sessions/sess-1/close", None),
    ("GET", f"{API}/agents/discovery", None),
    ("GET", f"{API}/agents/search?query=trip", None),
    ("GET", f"{API}/agents/groups", None),
    ("GET", f"{API}/agents/agent-1/profile", None),
)


class RecordingFacade:
    """Records the owner and client of each call; answers with fixed payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, SessionOwner, ClientIdentity | None]] = []

    async def send(self, request, *, owner, client, resolver):
        self.calls.append(("send", owner, client))
        return SessionMessageResponse(
            session_id="sess-1",
            client_request_id=request.client_request_id,
            agent_id=request.agent_id,
            status="created",
            state="working",
            message="Created.",
            created_at=NOW,
            updated_at=NOW,
        )

    async def check(self, session_id, *, owner, client):
        self.calls.append(("check", owner, client))
        return SessionStatus(
            session_id=session_id, state="working", created_at=NOW, updated_at=NOW
        )

    async def close(self, session_id, *, owner, client):
        self.calls.append(("close", owner, client))
        return SessionClosed(session_id=session_id, status="closed")

    async def close_for_owner(self, session_id, *, owner):
        self.calls.append(("close_for_owner", owner, None))
        return SessionClosed(session_id=session_id, status="closed")

    async def list_for_owner(self, *, owner):
        self.calls.append(("list", owner, None))
        return SessionListResponse()

    async def record_for_owner(self, session_id, *, owner):
        raise AssertionError("not used")


def make_app(
    *,
    auth_mode: str = "mock",
    owner_id: str | None = OWNER_USER_ID,
    required: bool | None = None,
):
    """The app, with the owner account under ``owner_id`` (none for ``None``).

    The switch is seeded as at startup: ``required``, or on in Clerk mode and
    off otherwise, as for an install that already has data.
    """
    app = create_app()
    facade = RecordingFacade()
    if required is None:
        required = auth_mode == "clerk"
    clients = ClientService(
        store_with_requirement(required=required), auth_mode=auth_mode
    )
    app.state.client_registry = clients
    store = store_with_owner(owner_id) if owner_id else None
    app.state.owner_account = build_service(store=store, auth_mode=auth_mode)
    app.state.api_gateway_deps = SimpleNamespace(
        session_facade=facade,
        agent_service=object(),
        agent_group_store=object(),
        agent_matcher=object(),
    )
    app.dependency_overrides[get_current_user] = lambda: ClerkUser(
        user_id=owner_id or OWNER_USER_ID, session_id="s", claims={"name": "Ada"}
    )
    return app, facade, clients


def client_for(app, host: str = "hybro.test"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=f"http://{host}",
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- Who a Client API request is --------------------------------------------


async def test_no_token_is_no_token_and_acts_for_the_owner():
    app, facade, _ = make_app()
    async with client_for(app) as client:
        response = await client.post(SEND, json=BODY)

    assert response.status_code == 200
    _, owner, identity = facade.calls[0]
    assert identity == NO_TOKEN
    assert owner.user_id == OWNER_USER_ID


async def test_a_token_names_its_client():
    app, facade, clients = make_app()
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        response = await client.post(SEND, json=BODY, headers=bearer(created.token))

    assert response.status_code == 200
    _, owner, identity = facade.calls[0]
    assert (identity.client_id, identity.name, identity.has_token) == (
        created.client_id,
        "CI bot",
        True,
    )
    assert owner.user_id == OWNER_USER_ID


async def test_the_mcp_mark_is_recorded_on_the_clients_last_request():
    app, _, clients = make_app()
    api = await clients.create("CI bot")
    mcp = await clients.create("Claude Code")
    async with client_for(app) as client:
        await client.post(SEND, json=BODY, headers=bearer(api.token))
        await client.post(
            SEND, json=BODY, headers={**bearer(mcp.token), "X-Hybro-Via": "mcp"}
        )
        listed = await client.get(f"{API}/clients")

    assert listed.status_code == 200
    via = {s["name"]: s["last_used_via"] for s in listed.json()["clients"]}
    assert via == {"CI bot": "api", "Claude Code": "mcp"}


@pytest.mark.parametrize("value", ["MCP", "stdio", ""])
async def test_any_other_mark_counts_as_the_api(value):
    app, _, clients = make_app()
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        await client.post(
            SEND, json=BODY, headers={**bearer(created.token), "X-Hybro-Via": value}
        )
    listed = await clients.list_clients()
    assert listed.clients[0].last_used_via == "api"


async def test_client_label_is_optional_with_a_token():
    app, facade, clients = make_app()
    created = await clients.create("CI bot")
    body = {key: value for key, value in BODY.items() if key != "client_label"}
    async with client_for(app) as client:
        response = await client.post(SEND, json=body, headers=bearer(created.token))

    assert response.status_code == 200
    assert facade.calls[0][0] == "send"


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES)
@pytest.mark.parametrize(
    "header",
    [
        "Bearer hybro_client_not-a-real-token",
        # The Panel's leftover bearer, which mock mode used to ignore.
        "Bearer mock-jwt-token",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "hybro_client_without_scheme",
    ],
)
async def test_an_unknown_or_malformed_token_is_refused_everywhere(
    method, path, body, header
):
    app, facade, _ = make_app()
    async with client_for(app) as client:
        response = await client.request(
            method, path, json=body, headers={"Authorization": header}
        )

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_client_token"
    assert "Authorization" in detail["message"]
    assert "client token" in detail["message"]
    assert response.headers["www-authenticate"] == "Bearer"
    assert facade.calls == []


@pytest.mark.parametrize("auth_mode", ["mock", "clerk"])
async def test_a_revoked_or_replaced_token_is_refused(auth_mode):
    app, facade, clients = make_app(auth_mode=auth_mode)
    revoked = await clients.create("old")
    await clients.revoke(revoked.client_id)
    rotated = await clients.create("rotated")
    await clients.rotate(rotated.client_id)
    async with client_for(app) as client:
        responses = [
            await client.post(SEND, json=BODY, headers=bearer(token))
            for token in (revoked.token, rotated.token)
        ]

    assert [r.status_code for r in responses] == [401, 401]
    assert facade.calls == []


async def test_clerk_mode_requires_a_token():
    app, facade, clients = make_app(auth_mode="clerk")
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        refused = await client.post(SEND, json=BODY)
        accepted = await client.post(SEND, json=BODY, headers=bearer(created.token))

    assert refused.status_code == 401
    assert refused.json()["detail"]["code"] == "client_token_required"
    assert accepted.status_code == 200
    assert len(facade.calls) == 1


@pytest.mark.parametrize("required", [True, False])
async def test_a_revoked_token_is_refused_with_the_switch_on_and_off(required):
    app, facade, clients = make_app(required=required)
    revoked = await clients.create("old")
    await clients.revoke(revoked.client_id)
    async with client_for(app) as client:
        response = await client.post(SEND, json=BODY, headers=bearer(revoked.token))

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unknown_client_token"
    assert facade.calls == []


async def test_with_the_switch_on_no_token_is_refused_and_a_token_passes():
    app, facade, clients = make_app(required=True)
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        refused = [
            await client.request(method, path, json=body)
            for method, path, body in SOURCE_ROUTES
        ]
        accepted = await client.post(SEND, json=BODY, headers=bearer(created.token))

    assert {r.status_code for r in refused} == {401}
    assert {r.json()["detail"]["code"] for r in refused} == {"client_token_required"}
    assert refused[0].headers["www-authenticate"] == "Bearer"
    assert accepted.status_code == 200
    assert facade.calls[0][2].client_id == created.client_id


async def test_the_switch_applies_at_once_without_a_restart():
    app, facade, _ = make_app(required=False)
    async with client_for(app) as client:
        before = await client.post(SEND, json=BODY)
        turned_on = await client.put(
            f"{API}/clients/token-requirement", json={"required": True}
        )
        after = await client.post(SEND, json=BODY)

    assert before.status_code == 200
    assert turned_on.status_code == 200
    assert after.status_code == 401
    assert len(facade.calls) == 1


async def test_the_client_api_acts_for_the_owner_record():
    # An account created in Clerk mode took a listed administrator's id.
    app, facade, clients = make_app(auth_mode="clerk", owner_id="user_abc")
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        await client.post(SEND, json=BODY, headers=bearer(created.token))

    assert (facade.calls[0][1].user_id, facade.calls[0][1].name) == (
        "user_abc",
        "Ada",
    )


@pytest.mark.parametrize("token", [False, True])
async def test_the_client_api_refuses_until_the_account_exists(token):
    app, facade, clients = make_app(owner_id=None)
    created = await clients.create("CI bot")
    headers = bearer(created.token) if token else {}
    async with client_for(app) as client:
        refused = await client.post(SEND, json=BODY, headers=headers)
        await app.state.owner_account.create_owner(
            OwnerCreate(
                name="Ada", email="ada@example.com", password="correct horse battery"
            )
        )
        accepted = await client.post(SEND, json=BODY, headers=headers)

    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "no_account"
    assert "hybro" in refused.json()["detail"]["message"]
    assert accepted.status_code == 200
    assert len(facade.calls) == 1


# -- Requests that another website makes a browser send ------------------------


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES)
async def test_no_token_with_a_foreign_origin_is_refused(method, path, body):
    app, facade, _ = make_app()
    async with client_for(app) as client:
        response = await client.request(
            method, path, json=body, headers={"Origin": "https://evil.example"}
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cross_site_request"
    assert facade.calls == []


# As the MCP server passes a public listener's caller on: it calls the backend
# on loopback and forwards the caller's address as X-Forwarded-Host, and its
# Origin, so these checks judge the caller, not the MCP server.
MCP_CALL = {"X-Hybro-Via": "mcp"}


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES)
async def test_a_rebound_name_through_mcp_is_refused_without_a_token(
    method, path, body
):
    app, facade, _ = make_app(required=False)
    async with client_for(app, host="127.0.0.1:8000") as client:
        response = await client.request(
            method,
            path,
            json=body,
            headers={
                **MCP_CALL,
                "X-Forwarded-Host": "evil.test:3000",
                "Origin": "http://evil.test:3000",
            },
        )

    assert response.status_code == 421
    assert response.json()["detail"]["code"] == "unknown_host"
    assert "evil.test" not in response.text
    assert facade.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        # Reached by IP, by the configured name, and by loopback, with or
        # without an Origin (most MCP clients send none).
        {"X-Forwarded-Host": "192.168.1.10:3000"},
        {"X-Forwarded-Host": "hybro.test", "Origin": "http://hybro.test"},
        {"X-Forwarded-Host": "localhost:3000", "Origin": "http://localhost:3000"},
    ],
)
@pytest.mark.parametrize("token", [False, True])
async def test_a_remote_mcp_client_by_a_trusted_address_passes(headers, token):
    app, facade, clients = make_app(required=False)
    created = await clients.create("Laptop")
    auth = bearer(created.token) if token else {}
    async with client_for(app, host="127.0.0.1:8000") as client:
        response = await client.post(
            SEND, json=BODY, headers={**MCP_CALL, **headers, **auth}
        )

    assert response.status_code == 200, response.text
    assert facade.calls[0][2].client_id == (
        created.client_id if token else NO_TOKEN.client_id
    )


async def test_a_web_page_by_ip_through_mcp_needs_a_token():
    # An IP cannot be rebound, but a page there is still another website.
    app, facade, clients = make_app(required=False)
    created = await clients.create("Laptop")
    page = {
        **MCP_CALL,
        "X-Forwarded-Host": "192.168.1.10:3000",
        "Origin": "http://192.168.1.99:8080",
    }
    async with client_for(app, host="127.0.0.1:8000") as client:
        refused = await client.post(SEND, json=BODY, headers=page)
        accepted = await client.post(
            SEND, json=BODY, headers={**page, **bearer(created.token)}
        )

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "cross_site_request"
    assert accepted.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://hybro.test"},
        # Through Caddy or the Next dev proxy, which pass the browser's host.
        {"Origin": "http://localhost:3000", "X-Forwarded-Host": "localhost:3000"},
    ],
)
async def test_no_token_from_no_origin_or_our_own_address_passes(headers):
    app, facade, _ = make_app()
    async with client_for(app) as client:
        response = await client.post(SEND, json=BODY, headers=headers)

    assert response.status_code == 200
    assert facade.calls[0][2] == NO_TOKEN


async def test_a_token_with_a_foreign_origin_is_accepted():
    app, facade, clients = make_app()
    created = await clients.create("Web tool")
    async with client_for(app) as client:
        response = await client.post(
            SEND,
            json=BODY,
            headers={**bearer(created.token), "Origin": "https://tool.example"},
        )

    assert response.status_code == 200
    assert facade.calls[0][2].client_id == created.client_id


# -- The owner's session routes ---------------------------------------------


async def test_the_owner_routes_act_for_the_owner_without_a_client():
    app, facade, _ = make_app()
    async with client_for(app) as client:
        listing = await client.get(f"{API}/sessions")
        closed = await client.post(f"{API}/sessions/sess-1/record/close")
        # A leftover bearer is not read by the owner's routes in this release.
        with_bearer = await client.get(
            f"{API}/sessions", headers=bearer("mock-jwt-token")
        )

    assert listing.status_code == closed.status_code == with_bearer.status_code == 200
    assert closed.json() == {"session_id": "sess-1", "status": "closed"}
    assert [call[0] for call in facade.calls] == ["list", "close_for_owner", "list"]
    assert all(call[1].user_id == OWNER_USER_ID for call in facade.calls)


OWNER_SESSION_ROUTES = (
    ("GET", f"{API}/sessions"),
    ("GET", f"{API}/sessions/sess-1/record"),
    ("POST", f"{API}/sessions/sess-1/record/close"),
)


async def test_the_owner_sees_the_sessions_clients_opened_in_clerk_mode():
    app, facade, clients = make_app(auth_mode="clerk", owner_id="user_abc")
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        await client.post(SEND, json=BODY, headers=bearer(created.token))
        listing = await client.get(f"{API}/sessions")

    assert listing.status_code == 200
    sent_for, listed_for = facade.calls[0][1], facade.calls[1][1]
    assert sent_for.user_id == listed_for.user_id == "user_abc"


async def test_the_client_close_route_needs_the_client():
    app, facade, clients = make_app()
    created = await clients.create("CI bot")
    async with client_for(app) as client:
        response = await client.post(
            f"{API}/sessions/sess-1/close", headers=bearer(created.token)
        )

    assert response.status_code == 200
    name, _, identity = facade.calls[0]
    assert name == "close" and identity.client_id == created.client_id


def test_get_agent_network_acts_for_the_caller_owner():
    deps = SimpleNamespace(
        agent_service=object(), agent_group_store=object(), agent_matcher=object()
    )
    caller = ClientCaller(
        client=NO_TOKEN, owner=SessionOwner(user_id="user_abc", name="user_abc")
    )

    network = get_agent_network(caller, deps)

    assert network._requesting_user_id == "user_abc"


# -- The owner's /clients routes ------------------------------------------------


async def test_the_owner_creates_lists_renames_rotates_and_revokes_a_client():
    app, _, _ = make_app()
    async with client_for(app) as client:
        created = await client.post(f"{API}/clients", json={"name": "CI bot"})
        token = created.json()["token"]
        client_id = created.json()["client_id"]
        listing = await client.get(f"{API}/clients")
        renamed = await client.patch(
            f"{API}/clients/{client_id}", json={"name": "Nightly"}
        )
        rotated = await client.post(f"{API}/clients/{client_id}/rotate")
        old = await client.post(SEND, json=BODY, headers=bearer(token))
        new = await client.post(
            SEND, json=BODY, headers=bearer(rotated.json()["token"])
        )
        revoked = await client.post(f"{API}/clients/{client_id}/revoke")
        after = await client.post(
            SEND, json=BODY, headers=bearer(rotated.json()["token"])
        )
        rotate_revoked = await client.post(f"{API}/clients/{client_id}/rotate")
        missing = await client.patch(f"{API}/clients/nope", json={"name": "x"})

    assert created.status_code == 201
    assert token.startswith("hybro_client_")
    assert listing.json()["clients"][0]["client_id"] == client_id
    assert "token" not in listing.json()["clients"][0]
    assert renamed.json()["name"] == "Nightly"
    assert rotated.status_code == 200 and rotated.json()["token"] != token
    assert (old.status_code, new.status_code) == (401, 200)
    assert revoked.json()["has_token"] is False
    assert after.status_code == 401
    assert rotate_revoked.status_code == 409
    assert rotate_revoked.json()["detail"]["code"] == "client_revoked"
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "client_not_found"


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
async def test_a_client_needs_a_name(name):
    app, _, _ = make_app()
    async with client_for(app) as client:
        response = await client.post(f"{API}/clients", json={"name": name})
    assert response.status_code == 422


async def test_a_client_token_does_not_open_the_clients_routes(monkeypatch):
    app, _, clients = make_app()
    created = await clients.create("CI bot")
    # The owner's credential is the sign-in cookie; a client token is not one.
    sign_out(app, monkeypatch)
    async with client_for(app) as client:
        response = await client.get(f"{API}/clients", headers=bearer(created.token))
    # The owner routes take the owner's credential, never a client token.
    assert response.status_code == 401


# -- The owner's switch -----------------------------------------------------------


async def test_the_owner_reads_and_changes_the_switch():
    app, _, _ = make_app(required=False)
    async with client_for(app) as client:
        read = await client.get(f"{API}/clients/token-requirement")
        changed = await client.put(
            f"{API}/clients/token-requirement", json={"required": True}
        )
        again = await client.get(f"{API}/clients/token-requirement")

    assert read.status_code == 200
    assert read.json()["required"] is False
    assert read.json()["started_off"] is True
    assert changed.json()["required"] is True
    # How it started never changes; the one-time notice relies on it.
    assert changed.json()["started_off"] is True
    assert again.json() == changed.json()


async def test_clerk_mode_shows_the_switch_on_and_refuses_to_turn_it_off():
    app, _, clients = make_app(auth_mode="clerk", owner_id="user_abc")
    clients._store.requirement = clients._store.requirement.model_copy(
        update={"required": False}
    )
    async with client_for(app) as client:
        read = await client.get(f"{API}/clients/token-requirement")
        refused = await client.put(
            f"{API}/clients/token-requirement", json={"required": False}
        )
        discovery = await client.get(f"{API}/agents/discovery")

    assert read.json()["required"] is True
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "token_required_by_auth_mode"
    assert discovery.status_code == 401


@pytest.mark.parametrize("body", [{}, {"required": "yes"}, {"required": True, "x": 1}])
async def test_the_switch_needs_a_boolean(body):
    app, _, _ = make_app()
    async with client_for(app) as client:
        response = await client.put(f"{API}/clients/token-requirement", json=body)
    assert response.status_code == 422


async def test_the_switch_is_the_owners_only(monkeypatch):
    app, _, clients = make_app(required=True)
    created = await clients.create("CI bot")
    sign_out(app, monkeypatch)
    async with client_for(app) as client:
        read = await client.get(
            f"{API}/clients/token-requirement", headers=bearer(created.token)
        )
        change = await client.put(
            f"{API}/clients/token-requirement",
            json={"required": False},
            headers=bearer(created.token),
        )

    assert (read.status_code, change.status_code) == (401, 401)
    assert await clients.token_required() is True


async def test_an_unseeded_switch_is_unavailable_and_requires_a_token():
    app, facade, _ = make_app()
    app.state.client_registry = ClientService(MemoryClientStore(), auth_mode="mock")
    async with client_for(app) as client:
        read = await client.get(f"{API}/clients/token-requirement")
        change = await client.put(
            f"{API}/clients/token-requirement", json={"required": False}
        )
        send = await client.post(SEND, json=BODY)

    assert (read.status_code, change.status_code) == (503, 503)
    assert read.json()["detail"]["code"] == "token_requirement_unavailable"
    assert send.status_code == 401
    assert facade.calls == []


async def test_clients_are_unavailable_without_the_registry():
    app, facade, _ = make_app()
    del app.state.client_registry
    async with client_for(app) as client:
        response = await client.post(SEND, json=BODY)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "clients_unavailable"
    assert facade.calls == []
