"""Consumer-visible errors and task states at the MCP/HTTP boundary."""

import asyncio
import json
import logging

import httpx
import pytest
import uvicorn
from mcp import Client

import server
from common.observability.logging import StructuredFormatter, get_log_context
from server import create_server

server.EVENTS.strict = True


def events(caplog, name):
    return [record for record in caplog.records if record.msg == name]


async def call_with_transport(handler, tool, arguments, listen="loopback"):
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        async with Client(create_server(http, listen=listen)) as client:
            return await client.call_tool(tool, arguments)


FAKE_TOKEN = "Bearer hybro_client_" + "f" * 43


async def call_over_http(handler, tool, arguments, headers=None, listen="loopback"):
    """Call one tool through the streamable HTTP app, as a real client does."""
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL, transport=httpx.MockTransport(handler)
    ) as backend:
        app = create_server(backend, listen=listen).streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=server.transport_security_for(listen),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    headers=_with_mcp_headers(headers),
                )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _with_mcp_headers(extra):
    """The streamable HTTP headers, plus a dict or a list of repeated lines."""
    pairs = list(extra.items()) if isinstance(extra, dict) else list(extra or [])
    return httpx.Headers(
        [
            ("accept", "application/json, text/event-stream"),
            ("mcp-protocol-version", "2025-06-18"),
            *pairs,
        ]
    )


def session_reply(status="created", state="working", **extra):
    """The backend's receipt for one message."""
    return {
        "session_id": "sess-1",
        "client_request_id": "turn-1",
        "agent_id": "story",
        "status": status,
        "state": state,
        "message": "Created. Use session_id to query this session.",
        "created_at": "2026-10-15T21:52:37Z",
        "updated_at": "2026-10-15T21:52:37Z",
        **extra,
    }


async def test_first_send_opens_a_session_with_minted_ids():
    sent = []

    def backend(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=session_reply())

    result = await call_with_transport(
        backend, "send_message", {"agent_id": "story", "text": "Plan a trip"}
    )

    assert not result.is_error
    path, payload = sent[0]
    assert path == "/api/v1/sessions/messages"
    assert payload["agent_id"] == "story"
    assert payload["text"] == "Plan a trip"
    assert payload["client_label"] == "MCP client"
    assert len(payload["client_request_id"]) == 32
    assert "session_id" not in payload
    summary = result.structured_content
    assert summary["session_id"] == "sess-1"
    assert summary["status"] == "created"
    assert summary["state"] == "working"
    assert summary["created_at"] == "2026-10-15T21:52:37Z"
    assert summary["updated_at"] == "2026-10-15T21:52:37Z"
    assert summary["message"].startswith("Created.")
    assert summary["agent_id"] == "story"
    assert "check_session" in summary["next_action"]
    assert "reply_text" not in summary
    assert "raw" not in summary
    assert {"conversation", "needs_input", "data", "files"}.isdisjoint(summary)
    assert json.loads(result.content[0].text) == summary


async def test_send_with_handle_continues_the_session():
    sent = []

    def backend(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=session_reply())

    result = await call_with_transport(
        backend,
        "send_message",
        {
            "agent_id": "travel",
            "text": "Now book it",
            "session_id": "sess-1",
            "subnet_id": "team",
            "client_request_id": "turn-2",
            "client_label": "Buyer agent",
        },
    )

    assert not result.is_error
    assert sent == [
        {
            "agent_id": "travel",
            "text": "Now book it",
            "session_id": "sess-1",
            "subnet_id": "team",
            "client_request_id": "turn-2",
            "client_label": "Buyer agent",
        }
    ]


async def test_send_with_handle_does_not_mint_a_label():
    sent = []

    def backend(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=session_reply())

    await call_with_transport(
        backend,
        "send_message",
        {"agent_id": "story", "text": "Go", "session_id": "sess-1"},
    )

    assert "client_label" not in sent[0]


@pytest.mark.parametrize(
    ("status", "state", "is_error", "hint"),
    [
        ("created", "working", False, "check_session"),
        ("accepted", "working", False, "check_session"),
        ("accepted", "failed", True, "check_session"),
    ],
)
async def test_receipts_point_the_client_to_check_session(
    status, state, is_error, hint
):
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=session_reply(status, state)),
        "send_message",
        {"agent_id": "story", "text": "Go"},
    )

    summary = result.structured_content
    assert result.is_error is is_error
    assert summary["status"] == status
    assert hint in summary["next_action"]
    assert "do not resend" in summary["next_action"]


async def test_busy_send_says_the_message_was_not_recorded():
    reply = session_reply("busy", "waiting_on_owner")

    result = await call_with_transport(
        lambda _: httpx.Response(200, json=reply),
        "send_message",
        {"agent_id": "story", "text": "Go", "session_id": "sess-1"},
    )

    assert not result.is_error
    summary = result.structured_content
    assert summary["status"] == "busy"
    assert "not recorded" in summary["next_action"]
    assert "check_session" in summary["next_action"]


async def test_include_raw_returns_the_backend_response():
    reply = session_reply()

    result = await call_with_transport(
        lambda _: httpx.Response(200, json=reply),
        "send_message",
        {"agent_id": "story", "text": "Go", "include_raw": True},
    )

    assert result.structured_content["raw"] == reply


async def test_send_rejects_text_over_the_session_limit():
    sent = []
    result = await call_with_transport(
        lambda request: sent.append(request) or httpx.Response(200, json={}),
        "send_message",
        {"agent_id": "story", "text": "x" * 10_001},
    )
    assert result.is_error
    assert sent == []


@pytest.mark.parametrize("removed", ["conversation", "data", "files"])
async def test_send_no_longer_accepts_a2a_arguments(removed):
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as http:
        async with Client(create_server(http)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert removed not in tools["send_message"].input_schema["properties"]


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        ("session_closed", "Omit session_id"),
        ("request_conflict", "new client_request_id"),
        ("agent_unavailable", "Choose another agent"),
    ],
)
async def test_send_conflicts_branch_on_the_backend_code(code, hint):
    error = {"detail": {"code": code, "message": "m", "client_request_id": "t"}}

    result = await call_with_transport(
        lambda _: httpx.Response(409, json=error),
        "send_message",
        {"agent_id": "story", "text": "Go"},
    )

    assert result.is_error
    assert result.structured_content["detail"] == error["detail"]
    assert hint in result.structured_content["next_action"]


async def test_unknown_session_hint_starts_a_new_session():
    error = {"detail": {"code": "session_not_found", "message": "m"}}

    result = await call_with_transport(
        lambda _: httpx.Response(404, json=error),
        "check_session",
        {"session_id": "missing"},
    )

    assert result.is_error
    assert "Omit session_id" in result.structured_content["next_action"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("find_agents", {"query": "story"}),
        ("send_message", {"agent_id": "story", "text": "Go"}),
    ],
)
async def test_no_owner_account_points_the_user_to_setup(tool, arguments):
    error = {"detail": {"code": "no_account", "message": "m"}}

    result = await call_with_transport(
        lambda _: httpx.Response(503, json=error), tool, arguments
    )

    assert result.is_error
    hint = result.structured_content["next_action"]
    assert "no owner account" in hint
    assert "hybro owner create" in hint


async def test_check_session_reads_state_and_history():
    calls = []
    status = {
        "session_id": "s/1",
        "state": "completed",
        "reason": None,
        "agents": [{"agent_id": "story", "name": "Story"}],
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:01:00Z",
        "history": [
            {
                "client_request_id": "turn-1",
                "text": "Write a story",
                "sent_at": "2026-10-14T00:00:00Z",
                "agent_id": "story",
                "agent_name": "Story",
                "state": "completed",
                "reason": None,
                "reply": "Once upon a time",
                "replied_at": "2026-10-14T00:01:00Z",
            }
        ],
    }

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json=status)

    result = await call_with_transport(backend, "check_session", {"session_id": "s/1"})

    assert not result.is_error
    assert calls[0].method == "GET"
    assert calls[0].url.raw_path == b"/api/v1/sessions/s%2F1"
    summary = result.structured_content
    assert summary["history"][0]["reply"] == "Once upon a time"
    assert summary["updated_at"] == "2026-10-14T00:01:00Z"
    assert "history" in summary["next_action"]


@pytest.mark.parametrize(
    ("state", "hint"),
    [
        ("working", "check_session later"),
        ("waiting_on_owner", "owner's decision"),
        ("rejected", "new session"),
        ("denied", "check rule"),
        ("closed", "new session"),
    ],
)
async def test_check_session_states_carry_next_action(state, hint):
    status = {
        "session_id": "s1",
        "state": state,
        "agents": [],
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
        "history": [],
    }
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=status),
        "check_session",
        {"session_id": "s1"},
    )
    assert hint in result.structured_content["next_action"]


async def test_close_session_posts_close():
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json={"session_id": "sess-1", "status": "closed"})

    result = await call_with_transport(
        backend, "close_session", {"session_id": "sess-1"}
    )

    assert not result.is_error
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/api/v1/sessions/sess-1/close"
    assert result.structured_content == {"session_id": "sess-1", "status": "closed"}


async def test_list_subnets_uses_subnet_vocabulary():
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "groups": [
                    {
                        "group_id": "g1",
                        "name": "Research",
                        "description": None,
                        "agent_count": 2,
                    }
                ]
            },
        )

    result = await call_with_transport(backend, "list_subnets", {})
    assert not result.is_error
    assert result.structured_content == {
        "subnets": [
            {
                "subnet_id": "g1",
                "name": "Research",
                "description": None,
                "agent_count": 2,
            }
        ]
    }
    assert calls[0].url.path == "/api/v1/agents/groups"


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        (
            "Preset team for the \u201cTravel Planner\u201d use case. "
            "[hybro-use-case:travel-planner]",
            "Preset team for the \u201cTravel Planner\u201d use case.",
        ),
        ("[hybro-use-case:travel-planner]", None),
        ("Research team", "Research team"),
    ],
)
async def test_list_subnets_hides_internal_preset_markers(stored, shown):
    def backend(request):
        group = {
            "group_id": "g1",
            "name": "Travel Planner",
            "description": stored,
            "agent_count": 3,
        }
        return httpx.Response(200, json={"groups": [group]})

    result = await call_with_transport(backend, "list_subnets", {})
    assert result.structured_content["subnets"][0]["description"] == shown


async def test_find_agents_scopes_search_and_renames_groups():
    calls = []
    match = {
        "agent_id": "weather",
        "name": "Weather Agent",
        "description": "Forecasts",
        "skills": [{"id": "f", "name": "Forecast", "description": None, "tags": []}],
        "input_modes": ["text"],
        "output_modes": ["text"],
        "groups": [{"group_id": "g1", "name": "Research"}],
        "score": 0.9,
    }

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json={"agents": [match]})

    result = await call_with_transport(
        backend,
        "find_agents",
        {"query": "weather forecast", "subnet_id": "g1", "limit": 3},
    )
    assert not result.is_error
    [agent] = result.structured_content["agents"]
    assert agent["subnets"] == [{"subnet_id": "g1", "name": "Research"}]
    assert "groups" not in agent
    assert calls[0].url.path == "/api/v1/agents/search"
    assert dict(calls[0].url.params) == {
        "query": "weather forecast",
        "limit": "3",
        "group_id": "g1",
    }


async def test_get_agent_escapes_the_agent_id_path_segment():
    calls = []
    profile = {
        "agent_id": "a/b",
        "status": "active",
        "name": "Odd",
        "streaming": False,
        "input_modes": ["text"],
        "output_modes": ["text"],
        "skills": [],
        "groups": [],
    }

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json=profile)

    result = await call_with_transport(backend, "get_agent", {"agent_id": "a/b"})
    assert not result.is_error
    assert result.structured_content["subnets"] == []
    assert calls[0].url.raw_path == b"/api/v1/agents/a%2Fb/profile"


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (404, "find_agents"),
        (409, "Choose another agent"),
        (429, "Retry after a short wait"),
        (504, "Do not resend"),
    ],
)
async def test_http_errors_carry_next_action(status, hint):
    error = {"detail": {"code": "X", "message": "m", "agent_id": "story"}}
    result = await call_with_transport(
        lambda _: httpx.Response(status, json=error),
        "send_message",
        {"agent_id": "story", "text": "Go"},
    )
    assert result.is_error
    assert result.structured_content["detail"] == error["detail"]
    assert hint in result.structured_content["next_action"]


async def test_tools_publish_output_schemas():
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as http:
        async with Client(create_server(http)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {
        "list_subnets",
        "find_agents",
        "get_agent",
        "discover_agents",
        "send_message",
        "check_session",
        "close_session",
        "answer_question",
    }
    for tool in tools.values():
        assert tool.output_schema is not None
    assert tools["send_message"].annotations.destructive_hint is True
    assert tools["find_agents"].annotations.read_only_hint is True
    assert tools["check_session"].annotations.read_only_hint is True
    assert tools["close_session"].annotations.destructive_hint is True
    assert tools["answer_question"].annotations.destructive_hint is True
    assert tools["answer_question"].annotations.idempotent_hint is False


async def test_http_validation_error_is_an_actionable_tool_error():
    error = {
        "detail": {
            "code": "VALIDATION",
            "message": "room_team requires a room",
        }
    }
    result = await call_with_transport(
        lambda _: httpx.Response(422, json=error),
        "discover_agents",
        {"subnet_id": "room_team"},
    )
    assert result.is_error
    assert result.structured_content["detail"] == error["detail"]
    assert "Fix the arguments" in result.structured_content["next_action"]


async def test_empty_team_does_not_expand_to_unscoped_inventory():
    def backend(request):
        agents = (
            []
            if request.url.params.get("group_id") == "empty"
            else [{"agent_id": "other"}]
        )
        return httpx.Response(200, json={"agents": agents})

    result = await call_with_transport(
        backend, "discover_agents", {"subnet_id": "empty"}
    )
    assert not result.is_error
    assert result.structured_content == {"agents": []}


async def test_ambiguous_send_failure_is_not_retried_and_keeps_correlation():
    delivered = []

    def accept_then_disconnect(request):
        delivered.append(json.loads(request.content))
        raise httpx.ReadError("secret transport details", request=request)

    result = await call_with_transport(
        accept_then_disconnect,
        "send_message",
        {"agent_id": "story", "text": "Go"},
    )
    assert len(delivered) == 1
    assert result.is_error
    detail = result.structured_content["detail"]
    assert detail["code"] == "BACKEND_UNAVAILABLE"
    assert detail["agent_id"] == "story"
    assert detail["client_request_id"] == delivered[0]["client_request_id"]
    assert "secret" not in result.content[0].text
    assert "Do not resend" in result.structured_content["next_action"]


async def test_timeout_stops_local_wait_without_repeating_send(monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_SECONDS", 0.01)
    deliveries = 0
    stopped = asyncio.Event()

    async def accept_then_hang(request):
        nonlocal deliveries
        deliveries += 1
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    result = await call_with_transport(
        accept_then_hang,
        "send_message",
        {"agent_id": "story", "client_request_id": "slow-turn", "text": "Go"},
    )
    assert result.is_error
    assert result.structured_content["detail"]["code"] == "BACKEND_TIMEOUT"
    assert result.structured_content["detail"]["client_request_id"] == "slow-turn"
    assert deliveries == 1
    assert stopped.is_set()


async def test_non_json_upstream_failure_does_not_expose_response_body():
    result = await call_with_transport(
        lambda _: httpx.Response(502, text="private debug information"),
        "discover_agents",
        {},
    )
    assert result.is_error
    assert result.structured_content["detail"]["code"] == "INVALID_BACKEND_RESPONSE"
    assert "private debug information" not in result.content[0].text


@pytest.mark.parametrize(
    ("code", "body", "status"),
    [
        (200, {"status": "ok", "private": "detail must not leak"}, "ready"),
        (503, {"status": "degraded"}, "unavailable"),
        (200, {"status": "degraded"}, "unavailable"),
        (401, {"detail": "private auth diagnostics"}, "unavailable"),
        (200, {"other": []}, "unavailable"),
        (200, [], "unavailable"),
    ],
)
async def test_health_checks_the_backend_health_route(code, body, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json=body)

    async with httpx.AsyncClient(
        base_url="http://backend:8000/custom/v2/",
        transport=httpx.MockTransport(handler),
    ) as backend:
        app = create_server(backend).streamable_http_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
        ) as client:
            response = await client.get(
                "/health", headers={"authorization": "Bearer hybro_client_fake"}
            )
    assert response.json() == {"service": "hybro-mcp", "status": status}
    assert response.headers["cache-control"] == "no-store"
    # The root health route needs no credential, so it says nothing about one.
    assert str(calls[0].url) == "http://backend:8000/health"
    assert calls[0].method == "GET"
    assert "authorization" not in calls[0].headers


async def test_health_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(server, "HEALTH_TIMEOUT_SECONDS", 0.01)

    async def hang(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL, transport=httpx.MockTransport(hang)
    ) as backend:
        app = create_server(backend).streamable_http_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
        ) as client:
            response = await client.get("/health")
    assert response.json()["status"] == "unavailable"


FOREIGN = [
    ({"host": "attacker.test:8001"}, 421),
    ({"origin": "https://attacker.test"}, 403),
]


@pytest.mark.parametrize(("headers", "code"), FOREIGN)
async def test_loopback_transport_retains_dns_rebinding_protection(headers, code):
    security = server.transport_security_for("loopback")
    assert security is server.TRANSPORT_SECURITY
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as backend:
        app = create_server(backend, listen="loopback").streamable_http_app(
            stateless_http=True,
            host="0.0.0.0",
            transport_security=security,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                assert (await client.get("/mcp", headers=headers)).status_code == code
                assert (
                    await client.get("/health", headers=headers)
                ).status_code == code


@pytest.mark.parametrize(("headers", "_"), FOREIGN)
async def test_public_transport_leaves_the_check_to_the_token(headers, _):
    """On a public listener any Host and Origin reach the routes; the client
    token, checked by the backend, is the control."""
    security = server.transport_security_for("public")
    assert security.enable_dns_rebinding_protection is False
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})
        ),
    ) as backend:
        app = create_server(backend, listen="public").streamable_http_app(
            stateless_http=True,
            json_response=True,
            host="0.0.0.0",
            transport_security=security,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                listed = await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": "2025-06-18",
                        **headers,
                    },
                )
                health = await client.get("/health", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["result"]["tools"]
    assert health.status_code == 200
    assert health.json()["status"] == "ready"


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"host": "hybro.example.com"}, 200),
        ({"host": "hybro.example.com:39443"}, 200),
        ({"host": "hybro.example.com", "origin": "https://hybro.example.com"}, 200),
        ({"host": "other.example.com"}, 421),
        ({"host": "attacker.test:8001"}, 421),
        ({"origin": "https://attacker.test"}, 403),
    ],
)
async def test_loopback_behind_a_proxy_also_admits_the_configured_name(headers, code):
    security = server.transport_security_for("loopback", "hybro.example.com")
    assert security.enable_dns_rebinding_protection is True
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})
        ),
    ) as backend:
        app = create_server(
            backend, listen="loopback", proxied_host="hybro.example.com"
        ).streamable_http_app(
            stateless_http=True,
            json_response=True,
            host="0.0.0.0",
            transport_security=security,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                health = await client.get("/health", headers=headers)
    assert health.status_code == code


@pytest.mark.parametrize(
    ("name", "host", "origin"),
    [
        # Every form the entrypoint's caddy_host returns, as browsers send it.
        ("my_nas.lan", "my_nas.lan:3000", "https://my_nas.lan"),
        (
            "xn--bcher-kva.example",
            "xn--bcher-kva.example",
            "https://xn--bcher-kva.example:3000",
        ),
        ("10.0.0.5", "10.0.0.5:3000", "http://10.0.0.5:3000"),
        ("fd00::5", "[fd00::5]:3000", "https://[fd00::5]"),
        ("fd00::5", "[fd00::5]", "https://[fd00::5]:3000"),
    ],
)
async def test_every_proxied_name_form_is_admitted_as_browsers_send_it(
    monkeypatch, name, host, origin
):
    monkeypatch.setenv(
        "HYBRO_MCP_CONFIG",
        json.dumps(
            {"api_prefix": "/api/v1", "listen": "loopback", "proxied_host": name}
        ),
    )
    config = server.connection_config(container=True)
    check = server.TransportSecurityMiddleware(
        server.transport_security_for(config.listen, config.proxied_host)
    )

    assert check._validate_host(host)
    assert check._validate_origin(origin)
    assert not check._validate_host("[fd00::6]:3000")
    assert not check._validate_origin("https://[fd00::6]")


def test_without_a_proxied_name_loopback_keeps_its_check():
    assert server.transport_security_for("loopback") is server.TRANSPORT_SECURITY
    assert server.transport_security_for("loopback", None) is server.TRANSPORT_SECURITY


def test_the_proxied_name_reaches_the_configuration(monkeypatch):
    monkeypatch.setenv(
        "HYBRO_MCP_CONFIG",
        '{"api_prefix":"/api/v1","listen":"loopback","proxied_host":"hybro.example.com"}',
    )
    assert server.connection_config(container=True).proxied_host == "hybro.example.com"
    monkeypatch.setenv("HYBRO_MCP_CONFIG", '{"api_prefix":"/api/v1"}')
    assert server.connection_config(container=True).proxied_host is None


def test_container_configuration_is_scoped_and_validated(monkeypatch):
    monkeypatch.setenv("HYBRO_MCP_CONFIG", '{"api_prefix":"/custom/v2"}')
    assert server.connection_config(container=True).api_prefix == "/custom/v2"
    # A projection without the listen mode keeps the host check.
    assert server.connection_config(container=True).listen == "loopback"
    assert server.connection_config(container=False).api_prefix == "/api/v1"
    assert server.connection_config(container=False).listen == "loopback"
    monkeypatch.setenv("HYBRO_MCP_CONFIG", '{"api_prefix":"/api/v1","listen":"public"}')
    assert server.connection_config(container=True).listen == "public"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "{bad",
        "[]",
        '{"token":"private"}',
        '{"api_prefix":"https://evil.test"}',
        '{"listen":"everywhere"}',
        '{"listen":null}',
        '{"proxied_host":"evil.test:80"}',
        '{"proxied_host":"Hy Bro"}',
    ],
)
def test_invalid_container_configuration_fails_safely(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("HYBRO_MCP_CONFIG", raising=False)
    else:
        monkeypatch.setenv("HYBRO_MCP_CONFIG", raw)
    with pytest.raises(ValueError, match="start with hybro") as error:
        server.connection_config(container=True)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1:3100", True),
        ("localhost:3000", True),
        ("attacker.test:3000", False),
        ("mcp:8001", False),
    ],
)
async def test_any_published_host_port_passes_the_host_guard(host, allowed):
    """Caddy forwards the published host port; the name stays pinned."""
    # /health runs behind the same guard and returns at once; /mcp would stream.
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    ) as backend:
        app = create_server(backend).streamable_http_app(
            stateless_http=True,
            host="0.0.0.0",
            transport_security=server.TRANSPORT_SECURITY,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                response = await client.get("/health", headers={"host": host})
    assert (response.status_code != 421) is allowed


# ---- Logging ----


@pytest.fixture
def mcp_logs(caplog):
    caplog.set_level(logging.INFO, logger="hybro.mcp")
    return caplog


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    level = root.level
    handlers = tuple(root.handlers)
    state = {handler: (handler.level, handler.formatter) for handler in handlers}
    yield root
    root.setLevel(level)
    for handler in tuple(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
        handler.setLevel(state[handler][0])
        handler.setFormatter(state[handler][1])


class ContextCapture(logging.Handler):
    """Record the bound log context when an mcp_tool_called record is handled."""

    def __init__(self, contexts):
        super().__init__()
        self.contexts = contexts

    def emit(self, record):
        if record.msg == "mcp_tool_called":
            self.contexts.append(get_log_context())


@pytest.fixture
def contexts():
    captured = []
    handler = ContextCapture(captured)
    logger = logging.getLogger("hybro.mcp")
    logger.addHandler(handler)
    yield captured
    logger.removeHandler(handler)


async def test_tool_call_logs_its_outcome_and_sends_its_request_id(mcp_logs, contexts):
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json=session_reply())

    result = await call_with_transport(
        backend,
        "send_message",
        {
            "agent_id": "story",
            "text": "Go",
            "session_id": "sess-1",
            "client_request_id": "turn-1",
        },
    )

    assert not result.is_error
    [record] = events(mcp_logs, "mcp_tool_called")
    assert contexts == [
        {"request_id": record.http_request_id, "trace_id": record.http_request_id}
    ]
    assert record.levelno == logging.INFO
    assert record.tool == "send_message"
    assert record.outcome == "success"
    assert record.duration_ms >= 0
    assert len(record.http_request_id) == 32
    assert record.session_id == "sess-1"
    assert record.client_request_id == "turn-1"
    assert sent[0].headers["x-request-id"] == record.http_request_id
    assert json.loads(sent[0].content)["client_request_id"] == "turn-1"
    assert events(mcp_logs, "mcp_backend_call_failed") == []


async def test_tool_call_logs_only_id_shaped_caller_ids(mcp_logs):
    def backend(request):
        return httpx.Response(200, json=session_reply())

    await call_with_transport(
        backend,
        "send_message",
        {
            "agent_id": "story",
            "text": "Go",
            "session_id": "sess-1",
            "client_request_id": "my email is someone@example.com",
        },
    )

    [record] = events(mcp_logs, "mcp_tool_called")
    assert record.session_id == "sess-1"
    assert record.client_request_id == "invalid"


def _raise(error):
    def handler(request):
        raise error("backend detail", request=request)

    return handler


@pytest.mark.parametrize(
    ("handler", "code", "status"),
    [
        (lambda _: httpx.Response(502, json={"detail": "x"}), "HTTP_ERROR", 502),
        (_raise(httpx.ConnectError), "BACKEND_UNAVAILABLE", None),
        (_raise(httpx.ReadTimeout), "BACKEND_TIMEOUT", None),
        (
            lambda _: httpx.Response(200, text="not json"),
            "INVALID_BACKEND_RESPONSE",
            200,
        ),
        (lambda _: httpx.Response(200, json=[]), "INVALID_BACKEND_RESPONSE", 200),
    ],
)
async def test_backend_failures_are_logged_with_the_tool_request_id(
    mcp_logs, handler, code, status
):
    result = await call_with_transport(handler, "discover_agents", {})

    assert result.is_error
    [failed] = events(mcp_logs, "mcp_backend_call_failed")
    [called] = events(mcp_logs, "mcp_tool_called")
    assert failed.levelno == logging.WARNING
    assert failed.code == code
    assert getattr(failed, "status", None) == status
    assert failed.http_request_id == called.http_request_id
    assert called.levelno == logging.ERROR
    assert called.outcome == "error"
    assert called.tool == "discover_agents"


async def test_raising_tool_logs_an_error_and_still_fails_the_call(
    mcp_logs, monkeypatch
):
    def broken(_body):
        raise RuntimeError("PRIVATE detail")

    monkeypatch.setattr(server, "tool_result", broken)

    result = await call_with_transport(
        lambda _: httpx.Response(200, json={"agents": []}), "discover_agents", {}
    )

    assert result.is_error
    [called] = events(mcp_logs, "mcp_tool_called")
    assert called.levelno == logging.ERROR
    assert called.outcome == "error"
    assert called.error_type == "RuntimeError"
    assert called.exc_info is None
    assert "PRIVATE" not in str(vars(called))


async def test_cancelled_tool_call_is_not_logged_as_an_error(mcp_logs):
    @server.logged_tool
    async def slow() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await slow()

    [called] = events(mcp_logs, "mcp_tool_called")
    assert called.levelno == logging.INFO
    assert called.outcome == "cancelled"
    assert not hasattr(called, "error_type")


async def test_health_sends_a_request_id_without_logging(mcp_logs):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"agents": []})

    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL, transport=httpx.MockTransport(handler)
    ) as backend:
        app = create_server(backend).streamable_http_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
        ) as client:
            await client.get("/health")
    assert len(calls[0].headers["x-request-id"]) == 32
    assert events(mcp_logs, "mcp_tool_called") == []
    assert events(mcp_logs, "mcp_backend_call_failed") == []


@pytest.mark.parametrize(
    ("image", "output_format"), [(True, "json"), (False, "logfmt")]
)
def test_configure_for_installs_the_backend_formatter(
    restore_root_logging, image, output_format
):
    server.configure_for(image=image)

    [formatter] = [
        handler.formatter
        for handler in restore_root_logging.handlers
        if isinstance(handler.formatter, StructuredFormatter)
    ]
    assert formatter.service == "hybro-mcp"
    assert formatter.output_format == output_format
    assert restore_root_logging.level == logging.INFO


async def test_serve_leaves_logging_to_the_structured_formatter(monkeypatch, mcp_logs):
    captured = {}

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        async def serve(self):
            captured["served"] = True

    def fake_config(app, **kwargs):
        captured.update(app=app, kwargs=kwargs)
        return "config"

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    app = object()

    await server.serve(app)

    assert captured == {
        "app": app,
        "kwargs": {
            "host": server.MCP_HOST,
            "port": server.MCP_PORT,
            "log_config": None,
            "access_log": False,
            # Longer than Caddy's 3-second grace period; see the stop budget
            # in backend/runtime_entrypoint.py.
            "timeout_graceful_shutdown": 5,
        },
        "config": "config",
        "served": True,
    }
    [starting] = events(mcp_logs, "mcp_server_starting")
    assert (starting.host, starting.port) == (server.MCP_HOST, server.MCP_PORT)


@pytest.mark.parametrize("listen", ["loopback", "public"])
async def test_main_configures_logging_before_building_the_server(monkeypatch, listen):
    calls = []
    app = object()

    class FakeServer:
        def streamable_http_app(self, **kwargs):
            calls.append(("app", kwargs))
            return app

    def fake_create_server(client, *, listen, proxied_host):
        assert proxied_host is None
        calls.append(("create_server", listen))
        return FakeServer()

    async def fake_serve(served):
        calls.append(("serve", served))

    monkeypatch.setattr(
        server, "configure_for", lambda image: calls.append(("log", image))
    )
    monkeypatch.setattr(server, "create_server", fake_create_server)
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setenv(
        "HYBRO_MCP_CONFIG", json.dumps({"api_prefix": "/api/v1", "listen": listen})
    )

    await server.main(image=True)

    security = server.transport_security_for(listen)
    assert calls == [
        ("log", True),
        ("create_server", listen),
        (
            "app",
            {
                "streamable_http_path": "/mcp",
                "stateless_http": True,
                "json_response": True,
                "transport_security": security,
                "host": server.MCP_HOST,
            },
        ),
        ("serve", app),
    ]


# ---- client tokens ----

TOOL_CALLS = [
    ("list_subnets", {}, {"groups": []}),
    ("find_agents", {"query": "weather"}, {"agents": []}),
    (
        "get_agent",
        {"agent_id": "story"},
        {
            "agent_id": "story",
            "status": "active",
            "streaming": False,
            "input_modes": [],
            "output_modes": [],
            "skills": [],
            "groups": [],
        },
    ),
    ("discover_agents", {}, {"agents": []}),
    ("send_message", {"agent_id": "story", "text": "Go"}, session_reply()),
    (
        "check_session",
        {"session_id": "sess-1"},
        {
            "session_id": "sess-1",
            "state": "working",
            "agents": [],
            "created_at": "2026-10-15T21:52:37Z",
            "updated_at": "2026-10-15T21:52:37Z",
            "history": [],
        },
    ),
    (
        "close_session",
        {"session_id": "sess-1"},
        {"session_id": "sess-1", "status": "closed"},
    ),
    (
        "answer_question",
        {"session_id": "sess-1", "question_id": "int-1", "answer": "approve"},
        {
            "session_id": "sess-1",
            "question_id": "int-1",
            "answer": "approve",
            "status": "accepted",
        },
    ),
]


@pytest.mark.parametrize(("tool", "arguments", "reply"), TOOL_CALLS)
async def test_every_tool_forwards_the_callers_authorization(tool, arguments, reply):
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json=reply)

    result = await call_over_http(
        backend, tool, arguments, headers={"Authorization": FAKE_TOKEN}
    )

    assert not result["isError"], result
    assert [request.headers["authorization"] for request in sent] == [FAKE_TOKEN]
    # The backend records that the client connected through MCP.
    assert [request.headers["x-hybro-via"] for request in sent] == ["mcp"]


@pytest.mark.parametrize(("tool", "arguments", "reply"), TOOL_CALLS)
async def test_a_call_without_authorization_sends_none(tool, arguments, reply):
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json=reply)

    result = await call_over_http(backend, tool, arguments)

    assert not result["isError"], result
    assert sent and all("authorization" not in r.headers for r in sent)


async def test_the_default_label_applies_only_without_a_token():
    payloads = []

    def backend(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=session_reply())

    arguments = {"agent_id": "story", "text": "Go"}
    await call_over_http(backend, "send_message", arguments)
    await call_over_http(
        backend, "send_message", arguments, headers={"Authorization": FAKE_TOKEN}
    )
    await call_over_http(
        backend,
        "send_message",
        {**arguments, "client_label": "Laptop"},
        headers={"Authorization": FAKE_TOKEN},
    )

    assert payloads[0]["client_label"] == "MCP client"
    assert "client_label" not in payloads[1]
    assert payloads[2]["client_label"] == "Laptop"


@pytest.mark.parametrize("code", ["unknown_client_token", "client_token_required"])
async def test_a_refused_token_names_the_fix(code):
    error = {"detail": {"code": code, "message": "m"}}
    result = await call_with_transport(
        lambda _: httpx.Response(401, json=error),
        "list_subnets",
        {},
    )
    assert result.is_error
    hint = result.structured_content["next_action"]
    assert "client token" in hint
    assert "mock auth" not in hint


async def test_instructions_describe_client_tokens():
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as http:
        instructions = create_server(http).instructions
    assert "client token" in instructions
    assert "mock-auth" not in instructions


# ---- the caller's address on a public listener ----


@pytest.mark.parametrize(("tool", "arguments", "reply"), TOOL_CALLS)
async def test_a_public_listener_passes_the_callers_address_on(tool, arguments, reply):
    """The backend's trusted-host and cross-site checks judge the caller."""
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json=reply)

    caller = {"X-Forwarded-Host": "nas.local:3000", "Origin": "http://nas.local:3000"}
    result = await call_over_http(
        backend, tool, arguments, headers=caller, listen="public"
    )

    assert not result["isError"], result
    assert [
        (r.headers["x-forwarded-host"], r.headers["origin"], r.headers["host"])
        for r in sent
    ] == [("nas.local:3000", "http://nas.local:3000", "127.0.0.1:8000")]


async def test_without_a_forwarded_host_the_callers_host_is_passed_on():
    # Reached without Caddy, the name the caller used is still judged.
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json={"groups": []})

    await call_over_http(
        backend, "list_subnets", {}, headers={"host": "evil.test:3000"}, listen="public"
    )

    assert sent[0].headers["x-forwarded-host"] == "evil.test:3000"
    # Most MCP clients send no Origin, and none is made up.
    assert "origin" not in sent[0].headers


async def test_every_forwarded_host_line_is_passed_on():
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json={"groups": []})

    await call_over_http(
        backend,
        "list_subnets",
        {},
        headers=[("x-forwarded-host", "hybro.test"), ("x-forwarded-host", "evil.test")],
        listen="public",
    )

    assert sent[0].headers["x-forwarded-host"] == "hybro.test, evil.test"


async def test_a_loopback_listener_passes_no_caller_address_on():
    # Its own host check already pinned the names; a browser-based client on
    # another local port keeps working without a token.
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json={"groups": []})

    await call_over_http(
        backend,
        "list_subnets",
        {},
        headers={
            "X-Forwarded-Host": "localhost:3100",
            "Origin": "http://localhost:6274",
        },
    )

    assert "x-forwarded-host" not in sent[0].headers
    assert "origin" not in sent[0].headers


async def test_an_unknown_address_names_the_setting_without_the_host():
    error = {"detail": {"code": "unknown_host", "message": "m"}}
    result = await call_over_http(
        lambda _: httpx.Response(421, json=error),
        "list_subnets",
        {},
        headers={"host": "evil.test:3000"},
        listen="public",
    )
    assert result["isError"]
    hint = result["structuredContent"]["next_action"]
    assert "frontend.server_url" in hint
    assert "evil.test" not in json.dumps(result)


async def test_a_web_page_without_a_token_is_told_to_send_one():
    error = {"detail": {"code": "cross_site_request", "message": "m"}}
    result = await call_with_transport(
        lambda _: httpx.Response(403, json=error), "list_subnets", {}
    )
    assert result.is_error
    assert "client token" in result.structured_content["next_action"]


async def test_a_public_listener_refuses_a_call_whose_address_it_cannot_tell(
    mcp_logs,
):
    # The in-memory client has no HTTP request. Without the caller's address the
    # backend would see only loopback, so nothing is sent.
    sent = []

    def backend(request):
        sent.append(request)
        return httpx.Response(200, json={"groups": []})

    result = await call_with_transport(backend, "list_subnets", {}, listen="public")

    assert result.is_error
    assert result.structured_content["detail"]["code"] == "CALLER_ADDRESS_UNKNOWN"
    assert sent == []
    [failed] = events(mcp_logs, "mcp_backend_call_failed")
    assert failed.code == "CALLER_ADDRESS_UNKNOWN"


async def test_check_session_denied_names_the_rule_reason():
    reason = "The owner's check rule rule_abc denied this agent call."
    status = {
        "session_id": "s1",
        "state": "denied",
        "reason": reason,
        "agents": [],
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
        "history": [],
    }
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=status),
        "check_session",
        {"session_id": "s1"},
    )
    assert result.structured_content["state"] == "denied"
    assert reason in result.structured_content["next_action"]


QUESTION = {
    "question_id": "int/1",
    "kind": "permission",
    "text": "Allow this call to Travel Planner?",
    "agent_id": "travel",
    "agent_name": "Travel Planner",
    "answers": ["approve", "refuse"],
}


def waiting_status(**extra):
    return {
        "session_id": "sess-1",
        "state": "waiting_on_owner",
        "agents": [{"agent_id": "travel", "name": "Travel Planner"}],
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
        "history": [],
        **extra,
    }


async def test_check_session_shows_a_question_the_client_may_answer():
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=waiting_status(question=QUESTION)),
        "check_session",
        {"session_id": "sess-1"},
    )

    assert not result.is_error
    summary = result.structured_content
    assert summary["question"] == QUESTION
    assert "answer_question" in summary["next_action"]
    assert "the user" in summary["next_action"]


async def test_check_session_without_a_question_waits_for_the_owner():
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=waiting_status()),
        "check_session",
        {"session_id": "sess-1"},
    )

    summary = result.structured_content
    assert summary.get("question") is None
    assert "owner's decision" in summary["next_action"]
    assert "answer_question" not in summary["next_action"]


@pytest.mark.parametrize("answer", ["approve", "refuse"])
async def test_answer_question_posts_the_answer(answer):
    calls = []
    reply = {
        "session_id": "s/1",
        "question_id": "int/1",
        "answer": answer,
        "status": "accepted",
    }

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json=reply)

    result = await call_with_transport(
        backend,
        "answer_question",
        {"session_id": "s/1", "question_id": "int/1", "answer": answer},
    )

    assert not result.is_error
    assert calls[0].method == "POST"
    assert calls[0].url.raw_path == b"/api/v1/sessions/s%2F1/questions/int%2F1/answer"
    assert json.loads(calls[0].content) == {"answer": answer}
    assert result.structured_content["status"] == "accepted"
    assert "check_session" in result.structured_content["next_action"]


@pytest.mark.parametrize("answer", ["Approve", "yes", "reject", ""])
async def test_answer_question_takes_only_approve_or_refuse(answer):
    calls = []

    result = await call_with_transport(
        lambda request: calls.append(request) or httpx.Response(500),
        "answer_question",
        {"session_id": "s1", "question_id": "int-1", "answer": answer},
    )

    assert result.is_error
    assert calls == []


@pytest.mark.parametrize(
    ("status", "code", "hint"),
    [
        (403, "answer_not_granted", "Connect page"),
        (403, "question_owner_only", "owner answers"),
        (409, "question_not_pending", "check_session"),
        (503, "answer_unconfirmed", "check_session"),
        (502, "answer_failed", "check_session"),
        (404, "session_not_found", "new session"),
    ],
)
async def test_answer_refusals_name_the_next_step(status, code, hint):
    error = {"detail": {"code": code, "message": "Fixed text."}}

    result = await call_with_transport(
        lambda _: httpx.Response(status, json=error),
        "answer_question",
        {"session_id": "s1", "question_id": "int-1", "answer": "approve"},
    )

    assert result.is_error
    assert result.structured_content["detail"] == error["detail"]
    assert hint in result.structured_content["next_action"]


async def test_instructions_describe_answering_permission_questions():
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as http:
        mcp_server = create_server(http)
    assert "answer_question" in mcp_server.instructions
