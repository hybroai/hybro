"""The machine-only routes the ``hybro`` command uses inside the container."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_gateway.cross_site import CrossSiteCheckMiddleware
from api_gateway.routes import machine_routes
from api_gateway.routes.machine_routes import TOKEN_HEADER
from tests.fakes.account import build_service

TOKEN = "a" * 64
PASSWORD = "correct horse battery"
OWNER = {"name": "Ada", "email": "ada@example.com", "password": PASSWORD}
AUTH = {TOKEN_HEADER: TOKEN}
ROUTES = (
    ("/api/v1/machine/setup-code", None),
    ("/api/v1/machine/owner", OWNER),
    ("/api/v1/machine/owner/password", {"password": "reset to this one"}),
)


def _client(service=None, *, token: str = TOKEN) -> TestClient:
    app = FastAPI()
    app.add_middleware(CrossSiteCheckMiddleware, api_prefix="/api/v1")
    app.include_router(machine_routes.router, prefix="/api/v1")
    app.state.owner_account = service if service is not None else build_service()
    app.state.machine_token = token
    return TestClient(app)


def test_the_header_is_not_authorization():
    assert TOKEN_HEADER == "X-Hybro-Machine-Token"


@pytest.mark.parametrize(("path", "body"), ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {TOKEN_HEADER: "b" * 64},
        {TOKEN_HEADER: ""},
        {TOKEN_HEADER: "été".encode("latin-1")},
        {"Authorization": f"Bearer {TOKEN}"},
        {"X-Hybro-Diagnostics-Token": TOKEN},
    ],
)
def test_a_request_without_the_token_is_refused(path, body, headers):
    response = _client().post(path, json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "machine_token_required"


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_no_token_is_accepted_when_the_backend_has_none(path, body):
    # A checkout has no handoff file; an empty header must not match it.
    client = _client(token="")

    for headers in ({}, {TOKEN_HEADER: ""}):
        assert client.post(path, json=body, headers=headers).status_code == 401


def test_the_owner_cookie_does_not_open_the_machine_routes():
    from account.protocols import OwnerCreate, SignInRequest
    from api_gateway.owner_session import SESSION_COOKIE

    service = build_service()
    asyncio.run(service.create_owner(OwnerCreate(**OWNER)))
    new = asyncio.run(
        service.sign_in(
            SignInRequest(email=OWNER["email"], password=PASSWORD),
            address="a",
            user_agent="",
        )
    )
    client = _client(service)
    client.cookies.set(SESSION_COOKIE, new.token)

    response = client.post(
        "/api/v1/machine/owner/password",
        json={"password": "reset to this one"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 401


def test_issue_a_setup_code_that_the_setup_route_accepts():
    service = build_service()

    response = _client(service).post("/api/v1/machine/setup-code", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"code", "expires_at"}
    assert len(body["code"]) >= 43


def test_create_the_owner():
    service = build_service()

    response = _client(service).post("/api/v1/machine/owner", json=OWNER, headers=AUTH)

    assert response.status_code == 201
    assert response.json() == {
        "user_id": "user_local_developer",
        "name": "Ada",
        "email": "ada@example.com",
    }
    assert "set-cookie" not in response.headers
    assert asyncio.run(service.exists()) is True


def test_the_first_two_work_only_while_no_account_exists():
    client = _client()
    assert client.post("/api/v1/machine/owner", json=OWNER, headers=AUTH).is_success

    again = client.post("/api/v1/machine/owner", json=OWNER, headers=AUTH)
    code = client.post("/api/v1/machine/setup-code", headers=AUTH)

    for response in (again, code):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "account_exists"


def test_create_the_owner_asks_which_admin_when_several_are_listed():
    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_a", "user_b"})
    )
    client = _client(service)

    asked = client.post("/api/v1/machine/owner", json=OWNER, headers=AUTH)
    chosen = client.post(
        "/api/v1/machine/owner", json={**OWNER, "owner_id": "user_b"}, headers=AUTH
    )

    assert asked.status_code == 409
    assert asked.json()["detail"]["code"] == "owner_id_required"
    assert asked.json()["detail"]["choices"] == ["user_a", "user_b"]
    assert chosen.json()["user_id"] == "user_b"


def test_reset_the_password_needs_an_account():
    response = _client().post(
        "/api/v1/machine/owner/password",
        json={"password": "reset to this one"},
        headers=AUTH,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_account"


def test_reset_the_password():
    client = _client()
    client.post("/api/v1/machine/owner", json=OWNER, headers=AUTH)

    response = client.post(
        "/api/v1/machine/owner/password",
        json={"password": "reset to this one"},
        headers=AUTH,
    )

    assert response.status_code == 204


def test_an_invalid_body_is_refused_without_echoing_the_password():
    response = _client().post(
        "/api/v1/machine/owner", json={**OWNER, "email": "nope"}, headers=AUTH
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert PASSWORD not in response.text


def test_the_routes_are_unavailable_before_the_account_service_is_bound():
    app = FastAPI()
    app.include_router(machine_routes.router, prefix="/api/v1")
    app.state.owner_account = None
    app.state.machine_token = TOKEN

    response = TestClient(app).post("/api/v1/machine/setup-code", headers=AUTH)

    assert response.status_code == 503
