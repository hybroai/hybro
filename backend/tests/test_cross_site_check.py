"""The cross-site check: who may send a changing request with the owner's cookie."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_gateway.cross_site import CrossSiteCheckMiddleware, same_address
from api_gateway.owner_session import SESSION_COOKIE

COOKIE = {"Cookie": f"{SESSION_COOKIE}=value"}


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CrossSiteCheckMiddleware, api_prefix="/api/v1")

    @app.api_route("/api/v1/thing", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.post("/api/v1/auth/sign-in")
    @app.post("/api/v1/auth/setup")
    @app.post("/api/v1/auth/sign-out")
    async def handler() -> dict:
        return {"ok": True}

    return TestClient(app)


def _post(client: TestClient, path: str = "/api/v1/thing", **headers: str):
    return client.post(path, headers=headers)


# ---- Scope ----


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_changing_request_without_a_cookie_and_without_an_origin_passes(method):
    response = _client().request(method, "/api/v1/thing")

    assert response.status_code == 200


def test_a_changing_request_without_a_cookie_passes_with_a_foreign_origin():
    # Not this check's business: no cookie means no owner authority to forge.
    response = _post(_client(), Origin="https://evil.example")

    assert response.status_code == 200


def test_another_cookie_does_not_bring_a_request_into_scope():
    response = _post(_client(), Cookie="theme=dark", Origin="https://evil.example")

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_read_with_the_cookie_is_not_checked(method):
    response = _client().request(
        method, "/api/v1/thing", headers={**COOKIE, "Origin": "https://evil.example"}
    )

    assert response.status_code != 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_cookie_request_with_a_foreign_origin_is_refused(method):
    response = _client().request(
        method, "/api/v1/thing", headers={**COOKIE, "Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "cross_site_request",
            "message": "This request did not come from this Hybro address.",
        }
    }


def test_a_cookie_request_without_an_origin_is_refused():
    assert _post(_client(), **COOKIE).status_code == 403


@pytest.mark.parametrize("origin", ["null", "", "testserver", "http://", "not a url"])
def test_a_cookie_request_with_an_unusable_origin_is_refused(origin):
    assert _post(_client(), **COOKIE, Origin=origin).status_code == 403


def test_a_cookie_request_from_hybros_own_address_passes():
    assert _post(_client(), **COOKIE, Origin="http://testserver").status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/auth/sign-in", "/api/v1/auth/setup"])
def test_sign_in_and_setup_are_checked_without_a_cookie(path):
    client = _client()

    assert _post(client, path).status_code == 403
    assert _post(client, path, Origin="https://evil.example").status_code == 403
    assert _post(client, path, Origin="http://testserver").status_code == 200


def test_sign_out_without_a_cookie_is_not_checked():
    assert _post(_client(), "/api/v1/auth/sign-out").status_code == 200


# ---- Hybro's own address ----


def test_through_caddy_the_host_is_kept_and_the_forwarded_host_repeats_it():
    response = _client().post(
        "/api/v1/thing",
        headers={
            **COOKIE,
            "Host": "hybro.example.com",
            "X-Forwarded-Host": "hybro.example.com",
            "Origin": "https://hybro.example.com",
        },
    )

    assert response.status_code == 200


def test_through_the_next_dev_proxy_the_forwarded_host_is_the_browsers():
    # The rewrite proxy sets Host to the backend's address and puts the
    # browser's in X-Forwarded-Host.
    headers = {
        **COOKIE,
        "Host": "127.0.0.1:8000",
        "X-Forwarded-Host": "localhost:3000",
    }
    client = _client()

    own = client.post(
        "/api/v1/thing", headers={**headers, "Origin": "http://localhost:3000"}
    )
    backend = client.post(
        "/api/v1/thing", headers={**headers, "Origin": "http://127.0.0.1:8000"}
    )

    assert own.status_code == 200
    # The forwarded host wins over Host when both are present.
    assert backend.status_code == 403


def test_a_direct_call_falls_back_to_the_host_header():
    response = _client().post(
        "/api/v1/thing",
        headers={
            **COOKIE,
            "Host": "127.0.0.1:8000",
            "Origin": "http://127.0.0.1:8000",
        },
    )

    assert response.status_code == 200


def test_the_port_is_compared():
    response = _client().post(
        "/api/v1/thing",
        headers={**COOKIE, "Host": "localhost:3100", "Origin": "http://localhost:3000"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("http://hybro.test", "hybro.test", True),
        ("https://hybro.test", "hybro.test", True),
        # An Origin on a default port carries none; the Host may still name it.
        ("https://hybro.test", "hybro.test:443", True),
        ("http://hybro.test", "hybro.test:80", True),
        ("http://hybro.test:80", "hybro.test", True),
        # The scheme is not compared.
        ("http://hybro.test", "hybro.test:443", True),
        ("http://HYBRO.test", "hybro.TEST", True),
        ("http://hybro.test:3100", "hybro.test:3100", True),
        ("http://hybro.test:3100", "hybro.test", False),
        ("http://hybro.test", "hybro.test:3100", False),
        ("http://evil.test", "hybro.test", False),
        ("http://hybro.test.evil.test", "hybro.test", False),
        ("http://[::1]:3100", "[::1]:3100", True),
        ("http://[::1]", "[::1]:80", True),
        ("http://[::1]:3100", "[::2]:3100", False),
        ("http://hybro.test", "", False),
        ("http://user@hybro.test", "hybro.test", False),
        ("http://hybro.test/path", "hybro.test", False),
        ("http://hybro.test:bad", "hybro.test", False),
        ("http://hybro.test:", "hybro.test", False),
        ("http://hybro.test\t", "hybro.test", False),
        ("http://hybro.test?", "hybro.test", False),
        ("http://hybro.test#", "hybro.test", False),
        ("http://hybro.test ", "hybro.test", False),
        ("http://hybro.test", "hybro.test:", False),
        ("ftp://hybro.test", "hybro.test", False),
    ],
)
def test_same_address(origin, host, expected):
    assert same_address(origin, host) is expected


def test_the_first_forwarded_host_is_used_when_several_are_listed():
    response = _client().post(
        "/api/v1/thing",
        headers={
            **COOKIE,
            "X-Forwarded-Host": "hybro.example.com, inner.proxy",
            "Origin": "https://hybro.example.com",
        },
    )

    assert response.status_code == 200


def test_the_check_is_installed_on_the_application():
    from main import app, settings

    installed = [
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CrossSiteCheckMiddleware
    ]

    assert len(installed) == 1
    assert installed[0].kwargs == {"api_prefix": settings.api_prefix}


def test_a_refusal_carries_the_cors_headers_of_an_allowed_origin():
    from main import app, settings

    origin = settings.frontend_origins[0]

    # No lifespan: the refusal comes from the middleware, before any route.
    response = TestClient(app).post(
        f"{settings.api_prefix}/auth/sign-in",
        json={"email": "a@b.co", "password": "x"},
        headers={"Origin": origin, "Host": "10.0.0.5:8000"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cross_site_request"
    assert response.headers["access-control-allow-origin"] == origin
