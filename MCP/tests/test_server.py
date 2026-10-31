"""Consumer-visible errors and task states at the MCP/HTTP boundary."""

import asyncio
import json

import httpx
import pytest
from mcp import Client

import server
from server import create_server


async def call_with_transport(handler, tool, arguments):
    async with httpx.AsyncClient(
        base_url=server.BACKEND_API_URL,
        transport=httpx.MockTransport(handler),
    ) as http:
        async with Client(create_server(http)) as client:
            return await client.call_tool(tool, arguments)


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
    }
    for tool in tools.values():
        assert tool.output_schema is not None
    assert tools["send_message"].annotations.destructive_hint is True
    assert tools["find_agents"].annotations.read_only_hint is True
    assert tools["check_session"].annotations.read_only_hint is True
    assert tools["close_session"].annotations.destructive_hint is True


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
        (200, {"agents": [{"private": "card must not leak"}]}, "ready"),
        (401, {"detail": "private auth diagnostics"}, "unsupported_auth"),
        (403, {}, "unsupported_auth"),
        (503, {}, "unavailable"),
        (200, {"other": []}, "unavailable"),
        (200, [], "unavailable"),
    ],
)
async def test_health_checks_discovery_without_exposing_cards(code, body, status):
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
            response = await client.get("/health")
    assert response.json() == {"service": "hybro-mcp", "status": status}
    assert response.headers["cache-control"] == "no-store"
    assert str(calls[0].url) == "http://backend:8000/custom/v2/agents/discovery"
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


@pytest.mark.parametrize(
    ("headers", "code"),
    [({"host": "attacker.test:8001"}, 421), ({"origin": "https://attacker.test"}, 403)],
)
async def test_container_transport_retains_dns_rebinding_protection(headers, code):
    async with httpx.AsyncClient(base_url=server.BACKEND_API_URL) as backend:
        app = create_server(backend).streamable_http_app(
            stateless_http=True,
            host="0.0.0.0",
            transport_security=server.TRANSPORT_SECURITY,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as client:
                assert (await client.get("/mcp", headers=headers)).status_code == code
                assert (
                    await client.get("/health", headers=headers)
                ).status_code == code


def test_container_configuration_is_scoped_and_validated(monkeypatch):
    monkeypatch.setenv("HYBRO_MCP_CONFIG", '{"api_prefix":"/custom/v2"}')
    assert server.connection_config(container=True).api_prefix == "/custom/v2"
    assert server.connection_config(container=False).api_prefix == "/api/v1"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "{bad",
        "[]",
        '{"token":"private"}',
        '{"api_prefix":"https://evil.test"}',
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
