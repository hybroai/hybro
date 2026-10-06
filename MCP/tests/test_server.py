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


@pytest.mark.parametrize(
    ("state", "is_error"),
    [("TASK_STATE_INPUT_REQUIRED", False), ("TASK_STATE_FAILED", True)],
)
async def test_task_state_and_continuation_survive_mcp_result(state, is_error):
    payload = {
        "agent_id": "story",
        "client_request_id": "turn-1",
        "result": {
            "kind": "task",
            "id": "task-1",
            "contextId": "context-1",
            "status": {
                "state": state,
                "message": {"parts": [{"text": "A question or failure reason"}]},
            },
        },
    }
    result = await call_with_transport(
        lambda _: httpx.Response(200, json=payload),
        "send_agent_message",
        {"agent_id": "story", "message": {"parts": [{"kind": "text", "text": "Go"}]}},
    )
    assert result.is_error is is_error
    assert result.structured_content == payload
    assert json.loads(result.content[0].text) == payload


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
        {"group_id": "room_team"},
    )
    assert result.is_error
    assert result.structured_content == error


async def test_empty_team_does_not_expand_to_unscoped_inventory():
    def backend(request):
        agents = (
            []
            if request.url.params.get("group_id") == "empty"
            else [{"agent_id": "other"}]
        )
        return httpx.Response(200, json={"agents": agents})

    result = await call_with_transport(
        backend, "discover_agents", {"group_id": "empty"}
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
        "send_agent_message",
        {"agent_id": "story", "message": {"parts": [{"kind": "text", "text": "Go"}]}},
    )
    assert len(delivered) == 1
    assert result.is_error
    detail = result.structured_content["detail"]
    assert detail["code"] == "BACKEND_UNAVAILABLE"
    assert detail["agent_id"] == "story"
    assert detail["client_request_id"] == delivered[0]["client_request_id"]
    assert "secret" not in result.content[0].text


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
        "send_agent_message",
        {
            "agent_id": "story",
            "client_request_id": "slow-turn",
            "message": {"parts": [{"kind": "text", "text": "Go"}]},
        },
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
            transport=httpx.ASGITransport(app), base_url="http://mcp:8001"
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
