"""The trusted-host check: a request must name an address this Hybro answers to.

A DNS rebinding page points its own name at this machine. The browser then
sends that name as ``Host`` and, on a changing request, as ``Origin``, so the
two match and the cross-site check passes. The trusted-host check refuses the
name itself: only loopback names, IP literals (which cannot be rebound) and
the hosts of ``frontend_origins`` (in the image, ``frontend.server_url``) are
this Hybro's addresses.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime_entrypoint as entry
from api_gateway.trusted_host import (
    TrustedHostMiddleware,
    trusted_host,
    trusted_names,
)
from common.config.loader import Settings

PUBLIC = "https://hybro.example.com"


def _client(origins: list[str]) -> TestClient:
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, origins=origins)

    @app.api_route("/api/v1/thing", methods=["GET", "POST"])
    async def thing() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return TestClient(app)


def _get(client: TestClient, path: str = "/api/v1/thing", **headers: str):
    return client.get(path, headers=headers)


# ---- The rule ----


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:3100",
        "LOCALHOST:3100",
        "127.0.0.1:8000",
        "127.0.0.1",
        "[::1]:3100",
    ],
)
def test_loopback_names_are_trusted(host):
    assert trusted_host(host, frozenset())


@pytest.mark.parametrize(
    "host", ["192.168.1.20:3100", "203.0.113.5", "10.0.0.2:443", "[2001:db8::1]:8443"]
)
def test_an_ip_literal_is_trusted_because_it_cannot_be_rebound(host):
    assert trusted_host(host, frozenset())


def test_the_configured_public_host_is_trusted_on_any_port():
    names = frozenset({"hybro.example.com"})
    assert trusted_host("hybro.example.com", names)
    assert trusted_host("HYBRO.example.com:8443", names)


@pytest.mark.parametrize(
    "host",
    [
        "rebound.example",
        "rebound.example:3100",
        "localhost.rebound.example",
        # As in Caddy's loopback matcher: not every resolver pins *.localhost.
        "app.localhost:3000",
        "hybro.example.com.rebound.example",
        "",
        "user@localhost",
        "localhost/path",
        "999.1.1.1",
    ],
)
def test_any_other_name_is_not_trusted(host):
    assert not trusted_host(host, frozenset({"hybro.example.com"}))


def test_a_unicode_server_url_matches_the_ascii_host_a_browser_sends():
    names = trusted_names(["https://bücher.example"])
    assert trusted_host("xn--bcher-kva.example", names)


def test_a_name_idna_2003_would_map_to_another_name_is_not_trusted():
    # Python's codec turns "straße" into "strasse", another registrable name;
    # a browser sends "xn--strae-oqa". Neither is trusted from the Unicode
    # form; the punycode form can be configured instead.
    names = trusted_names(["https://straße.example"])
    assert not trusted_host("strasse.example", names)
    assert not trusted_host("xn--strae-oqa.example", names)
    assert trusted_host(
        "xn--strae-oqa.example", trusted_names(["https://xn--strae-oqa.example"])
    )


# ---- The refusal ----


def test_a_foreign_host_is_refused_with_421_and_the_field_that_fixes_it():
    response = _get(_client([PUBLIC]), Host="rebound.example")

    assert response.status_code == 421
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_host"
    assert "frontend.server_url" in detail["message"]
    assert "backend.frontend_origins" in detail["message"]
    # The refusal does not repeat the name the request sent.
    assert "rebound" not in response.text


def test_a_foreign_forwarded_host_is_refused_behind_a_trusted_host():
    # A rebinding page cannot change Host, but it can add a header; either
    # name being foreign refuses the request.
    response = _get(
        _client([PUBLIC]),
        Host="127.0.0.1:8000",
        **{"X-Forwarded-Host": "rebound.example"},
    )
    assert response.status_code == 421


def test_a_foreign_host_is_refused_even_with_a_trusted_forwarded_host():
    response = _get(
        _client([PUBLIC]), Host="rebound.example", **{"X-Forwarded-Host": "localhost"}
    )
    assert response.status_code == 421


def test_health_answers_any_host():
    # No data, and probes and load balancers call it by any name.
    assert _get(_client([]), "/health", Host="rebound.example").status_code == 200


def test_every_method_is_covered():
    response = _client([]).post("/api/v1/thing", headers={"Host": "rebound.example"})
    assert response.status_code == 421


# ---- Each way Hybro is reached ----


def _image_origins(listener: entry.Listener, frontend: dict) -> list[str]:
    """The backend's frontend_origins as the image's entrypoint derives them."""
    programs = entry.projections(
        listener=listener,
        api_prefix="/api/v1",
        frontend=frontend,
        image_size="1024x1024",
        secrets={"default_agent_llm_token": "fixture-" * 8},
        agents=(),
        embedded_mongo=True,
    )
    return programs["backend"].env["HYBRO_FRONTEND_ORIGINS"].split(",")


def test_the_image_on_loopback_through_caddy():
    origins = _image_origins(entry.Listener("loopback", 3100, False), {})
    client = _client(origins)
    for host in ("localhost:3100", "127.0.0.1:3100"):
        response = _get(client, Host=host, **{"X-Forwarded-Host": host})
        assert response.status_code == 200, host


def test_the_image_on_a_public_listener_reached_by_ip():
    origins = _image_origins(entry.Listener("public", 3100, True), {})
    response = _get(
        _client(origins),
        Host="192.168.1.20:3100",
        **{"X-Forwarded-Host": "192.168.1.20:3100"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("forwarded", [True, False])
def test_a_reverse_proxy_with_a_domain_and_server_url(forwarded):
    origins = _image_origins(
        entry.Listener("public", 3100, True), {"server_url": PUBLIC}
    )
    headers = {"Host": "hybro.example.com"}
    if forwarded:
        headers["X-Forwarded-Host"] = "hybro.example.com"
    assert _get(_client(origins), **headers).status_code == 200


def test_a_domain_install_without_server_url_is_refused():
    # The one setup that changes: it sets frontend.server_url to its address.
    origins = _image_origins(entry.Listener("public", 3100, True), {})
    response = _get(
        _client(origins),
        Host="hybro.example.com",
        **{"X-Forwarded-Host": "hybro.example.com"},
    )
    assert response.status_code == 421


def test_the_next_dev_proxy_in_a_checkout():
    # The browser opens localhost:3000; Next forwards to the backend on 8000.
    client = _client(["http://localhost:3000"])
    for host in ("localhost:8000", "127.0.0.1:8000"):
        response = _get(client, Host=host, **{"X-Forwarded-Host": "localhost:3000"})
        assert response.status_code == 200, host


def test_the_backend_called_directly_in_a_checkout():
    assert _get(_client([]), Host="localhost:8000").status_code == 200


def test_mcp_and_the_bundled_agents_inside_the_container():
    # Both call http://127.0.0.1:8000, so they send that Host.
    import ast
    from pathlib import Path

    mcp = Path(__file__).resolve().parents[2] / "MCP" / "server.py"
    tree = ast.parse(mcp.read_text())
    urls = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "BACKEND_API_URL" for t in node.targets)
    ]
    assert urls == ["http://127.0.0.1:8000/api/v1/"]
    assert entry.BACKEND_ORIGIN == "http://127.0.0.1:8000"
    assert _get(_client([]), Host="127.0.0.1:8000").status_code == 200


def test_the_image_smoke_and_ci_reach_hybro_by_loopback_names():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    smoke = (root / "scripts" / "image_smoke.py").read_text()
    assert "http://localhost:<port>" in smoke
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    # host.docker.internal is the provider stub the backend calls, not an
    # address Hybro is reached by.
    hosts = {
        host
        for host in re.findall(r"https?://([A-Za-z0-9.:\[\]-]+)", ci)
        if not host.startswith("host.docker.internal")
    }
    assert hosts
    assert all(trusted_host(host, frozenset()) for host in hosts), hosts


@pytest.mark.parametrize(
    "callback", ["https://agents.example.com", "http://host.docker.internal:8000"]
)
def test_remote_agents_reach_the_push_callback_address(callback):
    # The address remote Agents call back on is one this Hybro answers to.
    from api_gateway.trusted_host import trusted_origins

    settings = Settings(a2a_push_callback_base_url=callback)
    host = callback.split("://", 1)[1]
    response = _get(_client(trusted_origins(settings)), "/api/v1/thing", Host=host)
    assert response.status_code == 200


def test_the_app_trusts_the_configured_origins_and_nothing_else():
    from api_gateway.trusted_host import trusted_origins

    settings = Settings(frontend_origins=["http://localhost:3000", PUBLIC])
    assert trusted_origins(settings) == ["http://localhost:3000", PUBLIC]


@pytest.mark.parametrize(
    "forwarded", ["localhost, rebound.example", "rebound.example, localhost"]
)
def test_every_forwarded_host_entry_must_be_trusted(forwarded):
    response = _get(
        _client([PUBLIC]), Host="127.0.0.1:8000", **{"X-Forwarded-Host": forwarded}
    )
    assert response.status_code == 421


def test_a_chain_of_trusted_forwarded_hosts_passes():
    response = _get(
        _client([PUBLIC]),
        Host="127.0.0.1:8000",
        **{"X-Forwarded-Host": "hybro.example.com, localhost:3100"},
    )
    assert response.status_code == 200


def test_a_second_forwarded_host_line_is_checked_too():
    response = _client([PUBLIC]).get(
        "/api/v1/thing",
        headers=[
            ("Host", "127.0.0.1:8000"),
            ("X-Forwarded-Host", "localhost"),
            ("X-Forwarded-Host", "rebound.example"),
        ],
    )
    assert response.status_code == 421


async def _status_for_raw_headers(headers: list[tuple[bytes, bytes]]) -> int:
    # A hand-built scope: an HTTP client always adds exactly one Host.
    reached = False

    async def app(scope, receive, send):
        nonlocal reached
        reached = True

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/thing",
        "raw_path": b"/api/v1/thing",
        "query_string": b"",
        "headers": headers,
    }
    await TrustedHostMiddleware(app, origins=[])(scope, receive, send)
    return 200 if reached else sent[0]["status"]


async def test_a_request_without_host_is_refused():
    assert await _status_for_raw_headers([]) == 421


async def test_a_second_host_line_is_checked_too():
    headers = [(b"host", b"localhost"), (b"host", b"rebound.example")]
    assert await _status_for_raw_headers(headers) == 421


async def test_a_single_loopback_host_line_passes():
    assert await _status_for_raw_headers([(b"host", b"localhost:8000")]) == 200
