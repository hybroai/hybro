"""What the backend believes behind Caddy, with and without ``backend.proxy_hops``.

In the image Caddy is the backend's only peer (127.0.0.1), which uvicorn's
proxy-header middleware trusts: it takes the scheme from ``X-Forwarded-Proto``
and the client address from ``X-Forwarded-For``. So every trust decision is
Caddy's, and these tests send the headers Caddy forwards in each case
(``test_runtime_entrypoint.py`` covers the generated Caddyfile; what Caddy
does with it is not run here):

- ``proxy_hops`` 0: Caddy replaces any forwarded header with its own view:
  its peer, ``http``, and the ``Host`` it received.
- ``proxy_hops`` 1: Caddy passes the outer proxy's ``-Proto`` and ``-Host``
  on, and ``X-Forwarded-For`` is the address that proxy saw.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_gateway.cross_site import CrossSiteCheckMiddleware
from api_gateway.owner_session import SESSION_COOKIE
from api_gateway.routes import auth_routes
from api_gateway.trusted_host import TrustedHostMiddleware
from tests.fakes.account import build_service

PASSWORD = "correct horse battery"
OWNER = {"name": "Ada", "email": "ada@example.com", "password": PASSWORD}
DOMAIN = "hybro.example.com"
PROXY = "172.17.0.1"  # The outer proxy, as Caddy sees it.
BROWSER = "203.0.113.7"


class AddressSpy:
    """The account service, noting the address each sign-in is limited by."""

    def __init__(self, service) -> None:
        self._service = service
        self.addresses: list[str] = []

    async def sign_in(self, request, *, address: str, user_agent: str):
        self.addresses.append(address)
        return await self._service.sign_in(
            request, address=address, user_agent=user_agent
        )

    def __getattr__(self, name: str):
        return getattr(self._service, name)


def _client(spy: AddressSpy) -> TestClient:
    app = FastAPI()
    app.add_middleware(CrossSiteCheckMiddleware, api_prefix="/api/v1")
    app.add_middleware(TrustedHostMiddleware, origins=[f"https://{DOMAIN}"])
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.state.owner_account = spy
    # As the image runs it: uvicorn trusts only 127.0.0.1, which is Caddy.
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    return TestClient(wrapped, client=("127.0.0.1", 40000))


def _spy() -> AddressSpy:
    from account.protocols import OwnerCreate

    service = build_service()
    asyncio.run(service.create_owner(OwnerCreate(**OWNER)))
    return AddressSpy(service)


def _sign_in(spy: AddressSpy, forwarded: dict[str, str]):
    return _client(spy).post(
        "/api/v1/auth/sign-in",
        json={"email": OWNER["email"], "password": PASSWORD},
        headers={"Origin": f"https://{DOMAIN}", **forwarded},
    )


def _attributes(response) -> set[str]:
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    return {part.strip().lower() for part in cookie.split(";")[1:]}


# The browser's request through an outer TLS proxy that keeps Host.
OFF = {
    "Host": DOMAIN,
    "X-Forwarded-Proto": "http",
    "X-Forwarded-For": PROXY,
    "X-Forwarded-Host": DOMAIN,
}
ON = {
    "Host": DOMAIN,
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": BROWSER,
    "X-Forwarded-Host": DOMAIN,
}


def test_without_the_setting_the_cookie_is_not_secure_and_the_proxy_is_the_address():
    spy = _spy()

    response = _sign_in(spy, OFF)

    assert response.status_code == 200, response.text
    assert "secure" not in _attributes(response)
    assert spy.addresses == [PROXY]


def test_with_the_setting_the_cookie_is_secure_and_the_browser_is_the_address():
    spy = _spy()

    response = _sign_in(spy, ON)

    assert response.status_code == 200, response.text
    assert "secure" in _attributes(response)
    assert spy.addresses == [BROWSER]


@pytest.mark.parametrize(
    ("forwarded_host", "status"),
    [
        # Off: nginx's default Host is its upstream, and Caddy copies it.
        ("127.0.0.1:8080", 403),
        # On: the outer proxy's X-Forwarded-Host reaches the backend.
        (DOMAIN, 200),
    ],
)
def test_a_proxy_that_rewrites_host_needs_the_setting(forwarded_host, status):
    response = _sign_in(
        _spy(),
        {
            "Host": "127.0.0.1:8080",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": BROWSER,
            "X-Forwarded-Host": forwarded_host,
        },
    )

    assert response.status_code == status, response.text
    if status == 403:
        assert response.json()["detail"]["code"] == "cross_site_request"


def test_with_the_setting_a_forwarded_host_this_hybro_does_not_answer_to_is_refused():
    response = _sign_in(_spy(), {**ON, "X-Forwarded-Host": "evil.example"})

    assert response.status_code == 421
    assert response.json()["detail"]["code"] == "unknown_host"


def test_several_forwarded_protocols_are_not_believed():
    # uvicorn takes only one value; a chain that appends keeps plain HTTP.
    response = _sign_in(_spy(), {**ON, "X-Forwarded-Proto": "https, http"})

    assert response.status_code == 200, response.text
    assert "secure" not in _attributes(response)
