"""The ``/auth`` routes: the owner's setup, sign-in and account pages."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_gateway.cross_site import CrossSiteCheckMiddleware
from api_gateway.owner_session import SESSION_COOKIE
from api_gateway.routes import auth_routes
from tests.fakes.account import MemoryAccountStore, build_service

PASSWORD = "correct horse battery"
OWNER = {"name": "Ada", "email": "ada@example.com", "password": PASSWORD}
ORIGIN = {"Origin": "http://testserver"}


def _app(service=None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CrossSiteCheckMiddleware, api_prefix="/api/v1")
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.state.owner_account = service
    return app


def _client(service=None, **kwargs) -> TestClient:
    service = service if service is not None else build_service()
    return TestClient(_app(service), headers=ORIGIN, **kwargs)


async def _with_owner(store: MemoryAccountStore | None = None):
    from account.protocols import OwnerCreate

    service = build_service(store=store)
    await service.create_owner(OwnerCreate(**OWNER))
    return service


def _signed_in_client(service, **kwargs) -> TestClient:
    client = _client(service, **kwargs)
    response = client.post(
        "/api/v1/auth/sign-in", json={"email": OWNER["email"], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def _owner_service():
    import asyncio

    return asyncio.run(_with_owner())


# ---- Status ----


def test_status_reports_no_account_and_no_owner():
    response = _client().get("/api/v1/auth/status")

    assert response.status_code == 200
    assert response.json() == {"account_exists": False, "owner": None}


def test_status_reports_the_account_without_naming_the_owner_when_signed_out():
    response = _client(_owner_service()).get("/api/v1/auth/status")

    assert response.json() == {"account_exists": True, "owner": None}


def test_status_names_the_owner_when_signed_in():
    client = _signed_in_client(_owner_service())

    assert client.get("/api/v1/auth/status").json() == {
        "account_exists": True,
        "owner": {
            "user_id": "user_local_developer",
            "name": "Ada",
            "email": "ada@example.com",
        },
    }


def test_the_routes_are_unavailable_before_the_account_service_is_bound():
    client = TestClient(_app(None), headers=ORIGIN)

    response = client.get("/api/v1/auth/status")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "account_unavailable"


# ---- Setup ----


def test_setup_creates_the_owner_and_sets_the_session_cookie():
    import asyncio

    service = build_service()
    code = asyncio.run(service.issue_setup_code()).code
    client = _client(service)

    response = client.post("/api/v1/auth/setup", json={"code": code, **OWNER})

    assert response.status_code == 200
    assert response.json()["user_id"] == "user_local_developer"
    assert SESSION_COOKIE in client.cookies
    assert client.get("/api/v1/auth/status").json()["owner"]["name"] == "Ada"


def test_setup_with_a_wrong_code_is_refused():
    response = _client().post("/api/v1/auth/setup", json={"code": "x" * 43, **OWNER})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "invalid_setup_code"
    assert "set-cookie" not in response.headers


def test_setup_fails_once_an_account_exists():
    response = _client(_owner_service()).post(
        "/api/v1/auth/setup", json={"code": "x" * 43, **OWNER}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "account_exists"


def test_setup_asks_which_admin_when_several_are_listed():
    import asyncio

    service = build_service(
        auth_mode="clerk", admin_user_ids=frozenset({"user_a", "user_b"})
    )
    code = asyncio.run(service.issue_setup_code()).code

    response = _client(service).post("/api/v1/auth/setup", json={"code": code, **OWNER})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "owner_id_required",
        "message": "Choose which administrator the owner account takes over.",
        "choices": ["user_a", "user_b"],
    }


def test_an_invalid_body_is_refused_without_echoing_the_password():
    response = _client().post(
        "/api/v1/auth/setup",
        json={"code": "c" * 43, "name": "Ada", "email": "nope", "password": PASSWORD},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert PASSWORD not in response.text


# ---- Sign-in and the cookie ----


def test_sign_in_sets_an_http_only_lax_cookie_for_the_whole_site():
    client = _client(_owner_service())

    response = client.post(
        "/api/v1/auth/sign-in", json={"email": OWNER["email"], "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_local_developer",
        "name": "Ada",
        "email": "ada@example.com",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "samesite=lax", "path=/", "max-age=2592000"} <= attributes
    assert "secure" not in attributes
    assert not any(part.startswith("domain=") for part in attributes)


def test_the_cookie_is_secure_when_the_request_arrived_over_https():
    client = _client(_owner_service(), base_url="https://testserver")

    response = client.post(
        "/api/v1/auth/sign-in",
        json={"email": OWNER["email"], "password": PASSWORD},
        headers={"Origin": "https://testserver"},
    )

    attributes = {
        part.strip().lower() for part in response.headers["set-cookie"].split(";")[1:]
    }
    assert "secure" in attributes


def test_a_wrong_password_is_401_without_a_cookie():
    response = _client(_owner_service()).post(
        "/api/v1/auth/sign-in",
        json={"email": OWNER["email"], "password": "wrong password!"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"
    assert "set-cookie" not in response.headers


def test_sign_in_records_the_browser_user_agent():
    client = _client(_owner_service())
    client.post(
        "/api/v1/auth/sign-in",
        json={"email": OWNER["email"], "password": PASSWORD},
        headers={"User-Agent": "Firefox/140"},
    )

    browsers = client.get("/api/v1/auth/browsers").json()["items"]

    assert [(b["user_agent"], b["current"]) for b in browsers] == [
        ("Firefox/140", True)
    ]
    assert set(browsers[0]) == {
        "id",
        "created_at",
        "last_used_at",
        "user_agent",
        "current",
    }


def test_sign_out_ends_the_session_and_clears_the_cookie():
    client = _signed_in_client(_owner_service())

    response = client.post("/api/v1/auth/sign-out")

    assert response.status_code == 204
    assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/auth/status").json()["owner"] is None


def test_sign_out_without_a_session_still_succeeds():
    assert _client(_owner_service()).post("/api/v1/auth/sign-out").status_code == 204


# ---- The owner's own routes need the cookie ----


def test_owner_routes_refuse_a_request_without_a_valid_cookie():
    client = _client(_owner_service())
    client.cookies.set(SESSION_COOKIE, "not-a-session")
    calls = [
        client.get("/api/v1/auth/browsers"),
        client.delete("/api/v1/auth/browsers/abc"),
        client.patch("/api/v1/auth/profile", json={"name": "A", "email": "a@b.co"}),
        client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "a brand new one"},
        ),
    ]

    for response in calls:
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "not_signed_in"


def test_a_bearer_header_does_not_open_the_owner_routes():
    client = _client(_owner_service())

    response = client.get(
        "/api/v1/auth/browsers", headers={"Authorization": "Bearer mock-jwt-token"}
    )

    assert response.status_code == 401


def test_profile_update_returns_the_new_profile():
    client = _signed_in_client(_owner_service())

    response = client.patch(
        "/api/v1/auth/profile", json={"name": "Ada L.", "email": "ada@lovelace.dev"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_local_developer",
        "name": "Ada L.",
        "email": "ada@lovelace.dev",
    }


def test_password_change_signs_out_the_other_browsers():
    service = _owner_service()
    first = _signed_in_client(service)
    second = _signed_in_client(service)

    response = first.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "a brand new one"},
    )

    assert response.status_code == 204
    assert first.get("/api/v1/auth/browsers").status_code == 200
    assert second.get("/api/v1/auth/browsers").status_code == 401


def test_password_change_with_a_wrong_current_password_is_401():
    client = _signed_in_client(_owner_service())

    response = client.post(
        "/api/v1/auth/password",
        json={"current_password": "wrong password!", "new_password": "a brand new 1"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


def test_revoking_another_browser_signs_it_out():
    service = _owner_service()
    first = _signed_in_client(service)
    second = _signed_in_client(service)
    other = next(
        b
        for b in first.get("/api/v1/auth/browsers").json()["items"]
        if not b["current"]
    )

    assert first.delete(f"/api/v1/auth/browsers/{other['id']}").status_code == 204
    assert second.get("/api/v1/auth/browsers").status_code == 401
    assert first.delete(f"/api/v1/auth/browsers/{other['id']}").status_code == 404


# ---- Requests from other websites, through the real routes ----


def test_sign_in_from_another_site_is_refused_before_the_password_is_checked():
    client = TestClient(_app(_owner_service()))
    body = {"email": OWNER["email"], "password": PASSWORD}

    foreign = client.post(
        "/api/v1/auth/sign-in", json=body, headers={"Origin": "https://evil.example"}
    )
    missing = client.post("/api/v1/auth/sign-in", json=body)

    for response in (foreign, missing):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "cross_site_request"
        assert "set-cookie" not in response.headers


def test_a_cookie_request_from_another_site_changes_nothing():
    service = _owner_service()
    client = _signed_in_client(service)

    response = client.post(
        "/api/v1/auth/sign-out", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert client.get("/api/v1/auth/browsers").status_code == 200


def test_too_many_attempts_at_once_is_429():
    from account.protocols import TooManyAttempts

    service = _owner_service()

    async def refuse(*args, **kwargs):
        raise TooManyAttempts

    service.sign_in = refuse

    response = _client(service).post(
        "/api/v1/auth/sign-in", json={"email": OWNER["email"], "password": PASSWORD}
    )

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "too_many_attempts"
