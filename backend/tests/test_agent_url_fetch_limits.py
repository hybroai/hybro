"""Auth, request validation, and size limits on owner-supplied agent URL fetches."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from a2a_adapter import bounded_http, card_resolver, client_facade, inspection
from a2a_adapter.agent_card_health import probe_agent_card_for_health
from a2a_adapter.card_resolver import AgentCardResolverImpl
from api_gateway.dependencies import get_agent_catalog, get_inspection_center
from common import auth
from common.auth import get_current_user
from common.dto import AgentRouteResponse
from tests.fakes.a2a_v1 import make_agent_card

_SECRET_DETAIL = "internal detail 10.1.2.3:8080 token=abc"
_URL_ROUTES = (
    "/api/v1/agent/getAgentCardFromUrl",
    "/api/v1/inspectionCenter/inspectAgentCard",
    "/api/v1/inspectionCenter/inspectA2AConnection",
)


@pytest.fixture
def url_route_api(monkeypatch, app):
    monkeypatch.setattr(app, "dependency_overrides", app.dependency_overrides.copy())
    catalog = MagicMock()
    catalog.resolve_card = AsyncMock(
        return_value=AgentRouteResponse(success=True, status_code=200)
    )
    center = MagicMock()
    center.inspect_agent_card = AsyncMock(return_value={"status_code": 200})
    center.inspect_a2a_connection = AsyncMock(return_value={"status_code": 200})
    app.dependency_overrides[get_agent_catalog] = lambda: catalog
    app.dependency_overrides[get_inspection_center] = lambda: center
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hybro.test"
    )
    return SimpleNamespace(client=client, catalog=catalog, center=center)


def _sign_out(monkeypatch, app):
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(
        auth,
        "authenticate_request",
        lambda request, options: SimpleNamespace(is_signed_in=False, payload=None),
    )


@pytest.mark.parametrize("path", _URL_ROUTES)
async def test_anonymous_agent_url_fetch_is_refused(
    url_route_api, monkeypatch, app, path
):
    _sign_out(monkeypatch, app)

    async with url_route_api.client as client:
        response = await client.post(path, json={"agent_url": "http://10.0.0.5"})

    assert response.status_code == 401
    url_route_api.catalog.resolve_card.assert_not_awaited()
    url_route_api.center.inspect_agent_card.assert_not_awaited()
    url_route_api.center.inspect_a2a_connection.assert_not_awaited()


@pytest.mark.parametrize("path", _URL_ROUTES)
@pytest.mark.parametrize(
    "body",
    [{}, {"agent_url": ""}, {"agent_url": 7}, {"agent_url": "h" * 5000}],
)
async def test_agent_url_fetch_rejects_invalid_body(url_route_api, path, body):
    async with url_route_api.client as client:
        response = await client.post(path, json=body)

    assert response.status_code == 422
    url_route_api.catalog.resolve_card.assert_not_awaited()
    url_route_api.center.inspect_agent_card.assert_not_awaited()
    url_route_api.center.inspect_a2a_connection.assert_not_awaited()


async def test_signed_in_owner_fetches_card_for_private_url(url_route_api):
    async with url_route_api.client as client:
        response = await client.post(
            "/api/v1/agent/getAgentCardFromUrl",
            json={"agent_url": "http://192.168.1.20:9000"},
        )

    assert response.status_code == 200
    url_route_api.catalog.resolve_card.assert_awaited_once_with(
        agent_url="http://192.168.1.20:9000"
    )


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/inspectionCenter/inspectAgentCard", "inspect_agent_card"),
        ("/api/v1/inspectionCenter/inspectA2AConnection", "inspect_a2a_connection"),
    ],
)
async def test_signed_in_owner_inspects_url(url_route_api, path, method):
    async with url_route_api.client as client:
        response = await client.post(path, json={"agent_url": "http://127.0.0.1:9"})

    assert response.status_code == 200
    request = getattr(url_route_api.center, method).await_args.args[0]
    assert request.agent_url == "http://127.0.0.1:9"


def _card_bytes() -> bytes:
    import json

    return json.dumps(MessageToDict(make_agent_card(url="http://agent.test"))).encode()


def _serve(monkeypatch, handler):
    monkeypatch.setattr(
        bounded_http, "_network_transport", lambda: httpx.MockTransport(handler)
    )


def _oversized_response(declared: bool):
    body = b" " * (bounded_http.MAX_AGENT_CARD_BYTES + 1)
    if declared:
        return httpx.Response(200, content=body)

    async def chunks():
        yield body

    return httpx.Response(200, content=chunks())


@pytest.mark.parametrize("declared", [True, False])
async def test_card_resolver_refuses_oversized_card(monkeypatch, declared):
    _serve(monkeypatch, lambda request: _oversized_response(declared))
    resolver = AgentCardResolverImpl(cache_ttl=0, log_failures=False)
    try:
        assert await resolver.resolve_card("http://agent.test") is None
    finally:
        await resolver.aclose()


async def test_card_resolver_accepts_normal_card(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=_card_bytes()))
    resolver = AgentCardResolverImpl(cache_ttl=0, log_failures=False)
    try:
        card = await resolver.resolve_card("http://agent.test")
    finally:
        await resolver.aclose()

    assert card is not None
    assert card.name == "TestAgent"


@pytest.mark.parametrize("declared", [True, False])
async def test_facade_card_fetch_refuses_oversized_card(monkeypatch, declared):
    _serve(monkeypatch, lambda request: _oversized_response(declared))

    with pytest.raises(client_facade.A2AClientFacadeError):
        await client_facade.fetch_agent_card_with_fallback("http://agent.test")


async def test_health_probe_refuses_oversized_card(monkeypatch):
    _serve(monkeypatch, lambda request: _oversized_response(False))

    result = await probe_agent_card_for_health("http://agent.test", timeout=1)

    assert result.is_healthy is False
    assert result.card is None


class _AnyUrlCardClient:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    async def get(self, url):
        self.requested_urls.append(url)
        return httpx.Response(
            200, content=_card_bytes(), request=httpx.Request("GET", url)
        )


async def test_card_resolver_cache_is_bounded():
    client = _AnyUrlCardClient()
    resolver = AgentCardResolverImpl(client=client, cache_ttl=300)

    max_entries = card_resolver.MAX_CARD_CACHE_ENTRIES
    for index in range(max_entries + 25):
        assert await resolver.resolve_card(f"http://agent-{index}.test") is not None

    assert len(resolver._cache) <= max_entries
    requests = len(client.requested_urls)
    last = f"http://agent-{max_entries + 24}.test"
    await resolver.resolve_card(last)
    assert len(client.requested_urls) == requests


async def test_inspection_probe_uses_bounded_timeouts(monkeypatch):
    card_data = MessageToDict(
        make_agent_card(url="http://127.0.0.1:9060", capabilities={"streaming": False})
    )
    fetch_card = AsyncMock(return_value=card_data)
    probe = AsyncMock(
        return_value={
            "kind": "message",
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": "m",
                "parts": [{"text": "hi"}],
            },
            "error": None,
        }
    )
    monkeypatch.setattr(inspection, "fetch_agent_card_with_fallback", fetch_card)
    monkeypatch.setattr(inspection, "send_message", probe)

    await inspection.inspect_a2a_connection("http://127.0.0.1:9060")

    assert fetch_card.await_args.kwargs["timeout"] <= 10
    assert probe.await_args.kwargs["timeout"] <= 120


async def test_inspection_probe_does_not_return_exception_text(monkeypatch):
    card_data = MessageToDict(
        make_agent_card(url="http://127.0.0.1:9060", capabilities={"streaming": False})
    )
    monkeypatch.setattr(
        inspection, "fetch_agent_card_with_fallback", AsyncMock(return_value=card_data)
    )
    monkeypatch.setattr(
        inspection, "send_message", AsyncMock(side_effect=RuntimeError(_SECRET_DETAIL))
    )

    result = await inspection.inspect_a2a_connection("http://127.0.0.1:9060")

    assert result["status_code"] == 500
    assert result["result"]
    assert _SECRET_DETAIL not in " ".join(result["result"])


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError(_SECRET_DETAIL), RuntimeError(_SECRET_DETAIL)],
)
async def test_card_inspection_does_not_return_exception_text(monkeypatch, error):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.fetch_agent_card_for_inspection",
        AsyncMock(side_effect=error),
    )

    response = await AgentInspectionService().inspect_agent_card(
        InspectionCenterRequest(agent_url="http://agent.test")
    )

    assert response.status_code in {500, 502}
    assert response.result
    assert _SECRET_DETAIL not in " ".join(response.result)


async def test_connection_inspection_does_not_return_exception_text(monkeypatch):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.inspect_a2a_connection",
        AsyncMock(side_effect=RuntimeError(_SECRET_DETAIL)),
    )

    response = await AgentInspectionService().inspect_a2a_connection(
        InspectionCenterRequest(agent_url="http://agent.test")
    )

    assert response.status_code == 500
    assert response.result
    assert _SECRET_DETAIL not in " ".join(response.result)


def _slow_drip(request):
    async def chunks():
        while True:
            yield b" "
            await asyncio.sleep(0.05)

    return httpx.Response(200, content=chunks())


async def test_card_fetches_have_an_overall_deadline(monkeypatch):
    # Each read is quick, so only an overall deadline stops this fetch.
    _serve(monkeypatch, _slow_drip)
    resolver = AgentCardResolverImpl(cache_ttl=0, timeout=0.3, log_failures=False)

    async with asyncio.timeout(5):
        assert await resolver.resolve_card("http://agent.test") is None
        with pytest.raises(client_facade.A2AClientFacadeError):
            await client_facade.fetch_agent_card_with_fallback(
                "http://agent.test", timeout=0.3
            )
        health = await probe_agent_card_for_health("http://agent.test", timeout=0.3)

    assert health.is_healthy is False


async def test_inspection_probe_has_an_overall_deadline(monkeypatch):
    card_data = MessageToDict(
        make_agent_card(url="http://127.0.0.1:9060", capabilities={"streaming": False})
    )
    monkeypatch.setattr(
        inspection, "fetch_agent_card_with_fallback", AsyncMock(return_value=card_data)
    )

    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(inspection, "send_message", hang)

    async with asyncio.timeout(5):
        result = await inspection.inspect_a2a_connection(
            "http://127.0.0.1:9060", timeout=0.2
        )

    assert result["status_code"] == 500
    assert result["result"] == ["The agent did not reply within 0.2 seconds."]


def _wrapped(cause: BaseException) -> Exception:
    try:
        try:
            raise cause
        except BaseException as exc:
            raise client_facade.A2AClientFacadeError("wrapped") from exc
    except client_facade.A2AClientFacadeError as wrapped:
        return wrapped


@pytest.mark.parametrize(
    ("error", "status_code", "message"),
    [
        (
            _wrapped(bounded_http.A2AResponseTooLargeError("too big")),
            502,
            "The agent card is larger than the 1 MiB limit.",
        ),
        (
            _wrapped(TimeoutError()),
            504,
            "The agent did not return its card in time.",
        ),
        (
            client_facade.A2AClientFacadeError("missing", status_code=404),
            404,
            "No agent card was found at this URL.",
        ),
        (
            _wrapped(httpx.ConnectError(_SECRET_DETAIL)),
            502,
            "Failed to connect to the agent.",
        ),
        (
            _wrapped(ValueError(_SECRET_DETAIL)),
            500,
            "The agent card could not be fetched or read.",
        ),
    ],
)
async def test_card_inspection_names_the_failure_without_its_text(
    monkeypatch, error, status_code, message
):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    monkeypatch.setattr(
        "agent.inspection.a2a_inspection.fetch_agent_card_for_inspection",
        AsyncMock(side_effect=error),
    )

    response = await AgentInspectionService().inspect_agent_card(
        InspectionCenterRequest(agent_url="http://agent.test")
    )

    assert (response.status_code, response.result) == (status_code, [message])


class _FlakyCardClient(_AnyUrlCardClient):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures = failures

    async def get(self, url):
        if self._failures:
            self._failures -= 1
            raise httpx.ConnectError("down")
        return await super().get(url)


async def test_card_resolver_does_not_cache_failures():
    client = _FlakyCardClient(failures=1)
    resolver = AgentCardResolverImpl(client=client, cache_ttl=300, log_failures=False)

    assert await resolver.resolve_card("http://agent.test") is None
    assert await resolver.resolve_card("http://agent.test") is not None
    assert resolver._cache.keys() == {"http://agent.test"}


async def test_card_resolver_evicts_the_oldest_entry_first():
    client = _AnyUrlCardClient()
    resolver = AgentCardResolverImpl(client=client, cache_ttl=300)
    max_entries = card_resolver.MAX_CARD_CACHE_ENTRIES

    for index in range(max_entries + 1):
        await resolver.resolve_card(f"http://agent-{index}.test")

    assert "http://agent-0.test" not in resolver._cache
    assert "http://agent-1.test" in resolver._cache
