"""Destination rule: where a registered agent's card may point Hybro.

A card may point Hybro at a private, loopback, or Docker-host address only when
the agent was registered at a private or loopback address. Link-local addresses
are always refused. The registered address is classified by what it resolves
to, and every connection is checked against the address it actually dials.

DNS and the network are faked: ``address_policy._getaddrinfo`` answers from a
table and ``bounded_http._network_transport`` records what would be dialed.
"""

from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from a2a_adapter import (
    address_policy,
    bounded_http,
    client_facade,
    inspection,
    remote_task,
)
from a2a_adapter.agent_card_health import probe_agent_card_for_health
from a2a_adapter.card_resolver import AgentCardResolverImpl
from tests.fakes.a2a_v1 import make_agent_card
from tests.fakes.a2a_v1_agent import A2A10AgentStub, completed_task_payload

PUBLIC_AGENT = "93.184.216.34"
OTHER_PUBLIC = "151.101.1.69"
DOCKER_HOST = "192.168.65.254"

DNS = {
    "agent.example": [PUBLIC_AGENT],
    "other.example": [OTHER_PUBLIC],
    "lan-agent.internal": ["192.168.1.40"],
    "metadata.example": ["169.254.169.254"],
    "host.docker.internal": [DOCKER_HOST],
    "localhost": ["127.0.0.1", "::1"],
    "mixed.example": [PUBLIC_AGENT, "10.0.0.9"],
}


def _install_dns(monkeypatch, table=None, *, calls=None):
    table = DNS if table is None else table

    async def getaddrinfo(host, port):
        if calls is not None:
            calls.append(host)
        answers = table.get(host)
        if callable(answers):
            answers = answers()
        if answers is None:
            try:
                socket.inet_pton(
                    socket.AF_INET6 if ":" in host else socket.AF_INET, host
                )
            except OSError:
                raise socket.gaierror(socket.EAI_NONAME, "unknown host") from None
            answers = [host]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in answers
        ]

    monkeypatch.setattr(address_policy, "_getaddrinfo", getaddrinfo)


class _Network(httpx.AsyncBaseTransport):
    """Records every dialed request and serves it from an in-process app."""

    def __init__(self, app=None, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self._inner = (
            httpx.ASGITransport(app=app)
            if app is not None
            else httpx.MockTransport(handler)
        )

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self._inner.handle_async_request(request)

    @property
    def dialed(self) -> list[str]:
        return [request.url.host for request in self.requests]


def _install_network(monkeypatch, network: _Network) -> _Network:
    monkeypatch.setattr(bounded_http, "_network_transport", lambda: network)
    return network


def _card(url: str, **kwargs):
    return make_agent_card(url=url, capabilities={"streaming": False}, **kwargs)


def _message():
    return {
        "role": "user",
        "message_id": "m-1",
        "parts": [{"kind": "text", "text": "hello"}],
    }


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "169.254.0.1", "fe80::1", "::ffff:169.254.169.254"],
)
@pytest.mark.parametrize("allow_private", [True, False])
def test_link_local_is_always_refused(address, allow_private):
    assert not address_policy.address_allowed(address, allow_private=allow_private)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.4.2",
        "192.168.1.10",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "::ffff:10.0.0.8",
        "0.0.0.0",
    ],
)
def test_private_and_loopback_need_a_private_registration(address):
    assert not address_policy.address_allowed(address, allow_private=False)
    assert address_policy.address_allowed(address, allow_private=True)


@pytest.mark.parametrize("address", [PUBLIC_AGENT, OTHER_PUBLIC, "2606:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert address_policy.address_allowed(address, allow_private=False)
    assert address_policy.address_allowed(address, allow_private=True)


@pytest.mark.parametrize(
    ("registered_url", "expected"),
    [
        ("http://127.0.0.1:9001", True),
        ("http://localhost:9001", True),
        ("http://[::1]:9001", True),
        ("http://192.168.1.40:9000", True),
        ("http://host.docker.internal:9000", True),
        # An internal hostname counts by what it resolves to.
        ("http://lan-agent.internal:9000", True),
        ("https://agent.example", False),
        # One public answer makes the registration public.
        ("https://mixed.example", False),
        # Unresolvable or missing registrations fail closed.
        ("https://unknown.example", False),
        (None, False),
        ("not a url", False),
    ],
)
async def test_registered_address_is_classified_by_resolution(
    monkeypatch, registered_url, expected
):
    _install_dns(monkeypatch)
    policy = address_policy.DestinationPolicy(registered_url)
    try:
        await policy.resolve(*address_policy._host_port(registered_url or ""))
    except (OSError, ValueError):
        pass

    assert await policy.allow_private() is expected


async def test_policy_refuses_private_targets_until_the_registered_host_is_dialed(
    monkeypatch,
):
    # The classification comes only from a lookup the call dials. Before the
    # registered host has been resolved for a request, a private target is
    # refused rather than unlocked by a separate lookup.
    calls: list[str] = []
    _install_dns(monkeypatch, calls=calls)
    policy = address_policy.DestinationPolicy("http://lan-agent.internal:9000")

    with pytest.raises(address_policy.AddressRefusedError):
        await policy.resolve("192.168.1.40", 9000)
    assert calls == ["192.168.1.40"]

    await policy.resolve("lan-agent.internal", 9000)
    assert await policy.resolve("192.168.1.40", 9000) == [
        (socket.AF_INET, "192.168.1.40")
    ]


async def test_registered_host_match_ignores_case_and_trailing_dot(monkeypatch):
    _install_dns(monkeypatch, {**DNS, "lan-agent.internal.": ["192.168.1.40"]})
    policy = address_policy.DestinationPolicy("http://LAN-Agent.Internal.:9000")

    await policy.resolve("lan-agent.internal", 9000)

    assert await policy.allow_private() is True


@pytest.mark.parametrize(
    "registered_url",
    [
        "http://169.254.169.254/",
        "http://[fe80::1]:9000/",
        # 6to4 carrying 169.254.169.254.
        "http://[2002:a9fe:a9fe::1]/",
    ],
)
async def test_link_local_ip_registration_does_not_unlock_private_targets(
    monkeypatch, registered_url
):
    # Link-local can never be dialed, so it is not a private registration.
    _install_dns(monkeypatch)
    policy = address_policy.DestinationPolicy(registered_url)

    assert await policy.allow_private() is False
    with pytest.raises(address_policy.AddressRefusedError):
        await policy.resolve("10.0.0.1", 80)


@pytest.mark.parametrize(
    "answers",
    [
        ["169.254.169.254"],
        ["fe80::1"],
        # A link-local answer beside a private one still fails closed.
        ["192.168.1.40", "169.254.169.254"],
    ],
)
async def test_link_local_dns_answer_does_not_unlock_private_targets(
    monkeypatch, answers
):
    _install_dns(monkeypatch, {**DNS, "metadata.example": answers})
    policy = address_policy.DestinationPolicy("http://metadata.example/")

    with pytest.raises(address_policy.AddressRefusedError):
        await policy.resolve("metadata.example", 80)

    assert await policy.allow_private() is False
    with pytest.raises(address_policy.AddressRefusedError):
        await policy.resolve("10.0.0.1", 80)


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interface_url",
    [
        "http://127.0.0.1:9001/",
        "http://localhost:9001/",
        "http://192.168.1.10/",
        "http://10.0.0.8:8080/",
        "http://host.docker.internal:9000/",
        "http://lan-agent.internal:9000/",
        "http://[::1]:9001/",
    ],
)
async def test_public_agent_card_cannot_point_at_private_addresses(
    monkeypatch, interface_url
):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _Network(handler=lambda r: None))

    with pytest.raises(address_policy.AddressRefusedError):
        await client_facade.send_message(
            _card(interface_url),
            _message(),
            registered_url="https://agent.example",
            timeout=1,
        )

    assert network.requests == []


@pytest.mark.parametrize(
    "interface_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.0.1:8080/",
        "http://[fe80::1]/",
        "http://metadata.example/",
    ],
)
@pytest.mark.parametrize(
    "registered_url", ["https://agent.example", "http://127.0.0.1:9001"]
)
async def test_link_local_targets_are_refused_for_every_agent(
    monkeypatch, interface_url, registered_url
):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _Network(handler=lambda r: None))

    with pytest.raises(address_policy.AddressRefusedError):
        await client_facade.send_message(
            _card(interface_url),
            _message(),
            registered_url=registered_url,
            timeout=1,
        )

    assert network.requests == []


async def test_stream_and_cancel_and_task_fetch_apply_the_rule(monkeypatch):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _Network(handler=lambda r: None))
    card = make_agent_card(url="http://10.0.0.8/", capabilities={"streaming": True})

    with pytest.raises(address_policy.AddressRefusedError):
        async for _frame in client_facade.stream_message(
            card, _message(), registered_url="https://agent.example", timeout=1
        ):
            pass
    assert not await client_facade.cancel_remote_task(
        card, "task-1", registered_url="https://agent.example"
    )
    assert (
        await remote_task.fetch_remote_task(
            card, "task-1", registered_url="https://agent.example"
        )
        is None
    )
    assert network.requests == []


@pytest.mark.parametrize(
    ("registered_url", "registered_ip", "interface_url", "dialed"),
    [
        ("http://127.0.0.1:9001", "127.0.0.1", "http://127.0.0.1:9001/", "127.0.0.1"),
        ("http://localhost:9001", "127.0.0.1", "http://localhost:9001/", "127.0.0.1"),
        (
            "http://192.168.1.40:9000",
            "192.168.1.40",
            "http://192.168.1.41:9000/",
            "192.168.1.41",
        ),
        (
            "http://lan-agent.internal:9000",
            "192.168.1.40",
            "http://10.0.0.8/",
            "10.0.0.8",
        ),
        (
            "https://agent.example",
            PUBLIC_AGENT,
            "https://other.example/a2a",
            OTHER_PUBLIC,
        ),
    ],
)
async def test_allowed_destinations_are_sent_to_the_checked_address(
    monkeypatch, registered_url, registered_ip, interface_url, dialed
):
    _install_dns(monkeypatch)
    stub = A2A10AgentStub(
        card=_card(interface_url), send_result={"task": completed_task_payload()}
    )
    network = _install_network(monkeypatch, _Network(app=stub.app))
    policy = address_policy.DestinationPolicy(registered_url)

    card = await client_facade.fetch_dispatch_card(registered_url, policy=policy)
    result = await client_facade.send_message(
        card,
        _message(),
        registered_url=registered_url,
        policy=policy,
        timeout=1,
    )

    assert result["kind"] == "task"
    assert network.dialed == [registered_ip, dialed]
    request = network.requests[-1]
    assert request.headers["host"] == httpx.URL(interface_url).netloc.decode()
    if interface_url.startswith("https://"):
        assert request.extensions["sni_hostname"] == httpx.URL(interface_url).host


@pytest.mark.parametrize(
    ("registered_url", "interface_url", "allowed"),
    [
        # An IP literal needs no DNS, so it is classified up front.
        ("http://192.168.1.40:9000", "http://192.168.1.41:9000/", True),
        ("http://127.0.0.1:9001", "http://127.0.0.1:9002/", True),
        # A hostname is classified only by a request that dials it.
        ("http://lan-agent.internal:9000", "http://10.0.0.8/", False),
        ("http://localhost:9001", "http://127.0.0.1:9001/", False),
    ],
)
async def test_send_without_a_card_fetch_classifies_only_ip_registrations(
    monkeypatch, registered_url, interface_url, allowed
):
    _install_dns(monkeypatch)
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    network = _install_network(monkeypatch, _Network(app=stub.app))

    send = client_facade.send_message(
        _card(interface_url), _message(), registered_url=registered_url, timeout=1
    )
    if allowed:
        assert (await send)["kind"] == "task"
    else:
        with pytest.raises(address_policy.AddressRefusedError):
            await send
        assert network.requests == []


async def test_loopback_agent_keeps_its_docker_host_fallback(monkeypatch):
    _install_dns(monkeypatch)
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    asgi = httpx.ASGITransport(app=stub.app)

    async def handler(request):
        if request.url.host == "127.0.0.1":
            raise httpx.ConnectError("All connection attempts failed")
        return await asgi.handle_async_request(request)

    class _FailingLoopback(_Network):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return await handler(request)

    network = _install_network(monkeypatch, _FailingLoopback(handler=lambda r: None))

    result = await client_facade.send_message(
        _card("http://127.0.0.1:9001/"),
        _message(),
        registered_url="http://127.0.0.1:9001",
        timeout=1,
    )

    assert result["kind"] == "task"
    assert network.dialed == ["127.0.0.1", DOCKER_HOST]


async def test_connect_time_check_defeats_dns_rebinding(monkeypatch):
    answers = iter([[OTHER_PUBLIC], ["127.0.0.1"]])
    _install_dns(monkeypatch, {**DNS, "rebind.example": lambda: next(answers)})
    network = _install_network(monkeypatch, _Network(handler=lambda r: None))

    # The card check sees a public answer; the connection then resolves to
    # loopback and is refused instead of dialed.
    with pytest.raises(address_policy.AddressRefusedError):
        await client_facade.send_message(
            _card("https://rebind.example/a2a"),
            _message(),
            registered_url="https://agent.example",
            timeout=1,
        )

    assert network.requests == []


async def test_redirect_hops_are_checked(monkeypatch):
    _install_dns(monkeypatch)

    def handler(request):
        return httpx.Response(302, headers={"location": "http://10.0.0.8/card"})

    network = _install_network(monkeypatch, _Network(handler=handler))
    transport = bounded_http.BoundedAsyncTransport(
        policy=address_policy.DestinationPolicy("https://agent.example")
    )

    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(address_policy.AddressRefusedError):
            await client.get("https://agent.example/.well-known/agent-card.json")

    assert network.dialed == [PUBLIC_AGENT]


# ---------------------------------------------------------------------------
# Card resolution, dispatch card, inspection, and health
# ---------------------------------------------------------------------------


def _card_server(card_url: str):
    payload = json.dumps(MessageToDict(_card(card_url))).encode()
    return _Network(handler=lambda request: httpx.Response(200, content=payload))


@pytest.mark.parametrize(
    "agent_url",
    ["http://192.168.1.20:9000", "http://127.0.0.1:9001", "http://lan-agent.internal"],
)
async def test_owner_card_fetch_for_private_url_still_works(monkeypatch, agent_url):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _card_server(agent_url))
    resolver = AgentCardResolverImpl(cache_ttl=0, log_failures=False)

    card = await resolver.resolve_card(agent_url)

    assert card is not None
    assert len(network.requests) == 1


@pytest.mark.parametrize(
    "agent_url",
    ["http://169.254.169.254", "http://metadata.example", "http://[fe80::1]"],
)
async def test_card_fetch_refuses_link_local_urls(monkeypatch, agent_url):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _card_server("http://agent.example"))
    resolver = AgentCardResolverImpl(cache_ttl=0, log_failures=False)

    assert await resolver.resolve_card(agent_url) is None
    with pytest.raises(client_facade.A2AClientFacadeError):
        await client_facade.fetch_agent_card_with_fallback(agent_url)
    health = await probe_agent_card_for_health(agent_url, timeout=1)

    assert health.is_healthy is False
    assert network.requests == []


async def test_dispatch_card_refuses_public_agent_pointing_inward(monkeypatch):
    _install_dns(monkeypatch)
    _install_network(monkeypatch, _card_server("http://127.0.0.1:9001"))

    with pytest.raises(address_policy.AddressRefusedError):
        await client_facade.fetch_dispatch_card("https://agent.example")


async def test_dispatch_card_allows_loopback_agent_and_public_move(monkeypatch):
    _install_dns(monkeypatch)
    _install_network(monkeypatch, _card_server("http://127.0.0.1:9001/"))
    card = await client_facade.fetch_dispatch_card("http://127.0.0.1:9001")
    assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:9001/"

    _install_network(monkeypatch, _card_server("https://other.example/a2a"))
    card = await client_facade.fetch_dispatch_card("https://agent.example")
    assert card["supportedInterfaces"][0]["url"] == "https://other.example/a2a"


async def test_dispatch_card_ignores_unsupported_bindings(monkeypatch):
    _install_dns(monkeypatch)
    card = make_agent_card(
        interfaces=[
            {"url": "https://other.example/a2a", "protocol_binding": "JSONRPC"},
            {"url": "http://10.0.0.8:50051", "protocol_binding": "GRPC"},
        ]
    )
    payload = json.dumps(MessageToDict(card)).encode()
    _install_network(
        monkeypatch,
        _Network(handler=lambda request: httpx.Response(200, content=payload)),
    )

    fetched = await client_facade.fetch_dispatch_card("https://agent.example")

    assert len(fetched["supportedInterfaces"]) == 2


async def test_inspection_probe_reports_a_refused_destination(monkeypatch):
    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _card_server("http://10.0.0.8/"))

    result = await inspection.inspect_a2a_connection("https://agent.example")

    assert result["status_code"] == 500
    assert result["result"] == [address_policy.REFUSED_DESTINATION_MESSAGE]
    # Only the card fetch reached the network; the probe was not sent.
    assert network.dialed == [PUBLIC_AGENT]


async def test_inspection_probe_sends_to_loopback_agent(monkeypatch):
    _install_dns(monkeypatch)
    card_data = MessageToDict(_card("http://127.0.0.1:9060/"))
    monkeypatch.setattr(
        inspection, "fetch_agent_card_with_fallback", AsyncMock(return_value=card_data)
    )
    probe = AsyncMock(return_value={"kind": "error", "error": "x", "result": None})
    monkeypatch.setattr(inspection, "send_message", probe)

    await inspection.inspect_a2a_connection("http://127.0.0.1:9060")

    assert probe.await_args.kwargs["registered_url"] == "http://127.0.0.1:9060"


async def test_compressed_card_is_refused(monkeypatch):
    # The 1 MiB cap counts wire bytes, so a compressed card could inflate past
    # it after the check. Card fetches ask for identity and refuse encodings.
    _install_dns(monkeypatch)
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("accept-encoding", ""))
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=b"\x1f\x8b"
        )

    _install_network(monkeypatch, _Network(handler=handler))
    resolver = AgentCardResolverImpl(cache_ttl=0, log_failures=False)

    assert await resolver.resolve_card("https://agent.example") is None
    assert seen and set(seen) == {"identity"}


async def test_network_send_reports_a_refused_destination(monkeypatch):
    from common.dto.agent import AgentInfo
    from common.errors import UpstreamError
    from execution.direct_agent import DirectAgentExecution
    from models.agent_network import AgentMessageRequest

    _install_dns(monkeypatch)
    network = _install_network(monkeypatch, _card_server("http://127.0.0.1:9001/"))

    class _Targets:
        async def resolve_target(self, agent_id, group_id=None):
            return AgentInfo(
                agent_id="a",
                is_public=False,
                url="https://agent.example",
                raw_card=MessageToDict(_card("https://agent.example/a2a")),
            )

    with pytest.raises(UpstreamError) as failure:
        await DirectAgentExecution(_Targets()).send(
            AgentMessageRequest(
                agent_id="a",
                client_request_id="r-1",
                message={"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            )
        )

    assert str(failure.value) == address_policy.REFUSED_DESTINATION_MESSAGE
    # The live card was fetched from the registered host; nothing was sent.
    assert [(r.method, r.url.host) for r in network.requests] == [("GET", PUBLIC_AGENT)]


async def test_network_send_classifies_from_the_dialed_card_fetch(monkeypatch):
    # The stored card points at loopback. The registered host's DNS answers
    # loopback at send time, which would classify the agent as private. The
    # send follows the live card fetched from the dialed answer instead; the
    # loopback service there serves no card, so nothing is sent.
    from common.dto.agent import AgentInfo
    from common.errors import UpstreamError
    from execution.direct_agent import DirectAgentExecution
    from models.agent_network import AgentMessageRequest

    _install_dns(monkeypatch, {**DNS, "evil.example": ["127.0.0.1"]})
    network = _install_network(
        monkeypatch, _Network(handler=lambda request: httpx.Response(404))
    )

    class _Targets:
        async def resolve_target(self, agent_id, group_id=None):
            return AgentInfo(
                agent_id="a",
                is_public=False,
                url="https://evil.example",
                raw_card=MessageToDict(_card("http://127.0.0.1:8000/")),
            )

    with pytest.raises(UpstreamError):
        await DirectAgentExecution(_Targets()).send(
            AgentMessageRequest(
                agent_id="a",
                client_request_id="r-1",
                message={"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            )
        )

    assert {request.method for request in network.requests} == {"GET"}


async def test_network_send_shares_one_policy_with_its_card_fetch(monkeypatch):
    # The registered host and the card's send host both answer public first
    # and loopback afterwards. The card check passes on the public answers.
    # The send must reuse the classification the card fetch dialed: a fresh
    # policy would look the registered host up again, call the agent private,
    # and dial the send host's loopback answer.
    from common.dto.agent import AgentInfo
    from common.errors import UpstreamError
    from execution.direct_agent import DirectAgentExecution
    from models.agent_network import AgentMessageRequest

    registered = iter([[PUBLIC_AGENT]])
    send_host = iter([[OTHER_PUBLIC]])
    _install_dns(
        monkeypatch,
        {
            **DNS,
            "evil.example": lambda: next(registered, ["127.0.0.1"]),
            "send.example": lambda: next(send_host, ["127.0.0.1"]),
        },
    )
    network = _install_network(monkeypatch, _card_server("https://send.example/a2a"))

    class _Targets:
        async def resolve_target(self, agent_id, group_id=None):
            return AgentInfo(
                agent_id="a",
                is_public=False,
                url="https://evil.example",
                raw_card=MessageToDict(_card("https://evil.example/a2a")),
            )

    with pytest.raises(UpstreamError):
        await DirectAgentExecution(_Targets()).send(
            AgentMessageRequest(
                agent_id="a",
                client_request_id="r-1",
                message={"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            )
        )

    assert "127.0.0.1" not in network.dialed
    assert network.dialed == [PUBLIC_AGENT]


def test_orchestrator_dispatch_uses_the_checked_card_fetch():
    import orchestrator_composition

    assert orchestrator_composition.sdk_fetch_agent_card is (
        client_facade.fetch_dispatch_card
    )


async def test_registered_host_rebinding_cannot_flip_the_classification(monkeypatch):
    # The third party controls the registered host's DNS: public while Hybro
    # fetches the card, loopback afterwards. One call classifies the agent
    # once, from the answer the card fetch dialed, so the loopback card stays
    # refused.
    answers = iter([[PUBLIC_AGENT], [PUBLIC_AGENT]])

    def flip():
        return next(answers, ["127.0.0.1"])

    _install_dns(monkeypatch, {**DNS, "evil.example": flip})
    network = _install_network(monkeypatch, _card_server("http://127.0.0.1:8000/"))

    with pytest.raises(address_policy.AddressRefusedError):
        await client_facade.fetch_dispatch_card("https://evil.example")

    assert network.dialed == [PUBLIC_AGENT]


async def test_one_policy_classifies_the_registered_host_once(monkeypatch):
    calls: list[str] = []
    answers = iter([[PUBLIC_AGENT]])
    _install_dns(
        monkeypatch,
        {**DNS, "evil.example": lambda: next(answers, ["127.0.0.1"])},
        calls=calls,
    )
    policy = address_policy.DestinationPolicy("https://evil.example")

    assert await policy.resolve("evil.example", 443) == [(socket.AF_INET, PUBLIC_AGENT)]
    with pytest.raises(address_policy.AddressRefusedError):
        await policy.resolve("127.0.0.1", 8000)
    assert await policy.allow_private() is False
    assert calls == ["evil.example", "127.0.0.1"]


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1"])
def test_nat64_addresses_are_classified_by_the_embedded_ipv4(address):
    assert not address_policy.address_allowed(address, allow_private=False)


@pytest.mark.parametrize(
    "address",
    [
        "::7f00:1",  # IPv4-compatible 127.0.0.1
        "::a9fe:a9fe",  # IPv4-compatible 169.254.169.254
        "64:ff9b:1::7f00:1",  # local-use NAT64 127.0.0.1
        "2002:7f00:1::",  # 6to4 127.0.0.1
        "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo client 127.0.0.1
    ],
)
def test_ipv6_transition_addresses_are_classified_by_the_embedded_ipv4(address):
    assert not address_policy.address_allowed(address, allow_private=False)


@pytest.mark.parametrize(
    "address",
    [
        "::a9fe:a9fe",
        "64:ff9b:1::a9fe:a9fe",
        "2002:a9fe:a9fe::",
        "2001:0:4136:e378:8000:63bf:5601:5601",  # Teredo client 169.254.169.254
    ],
)
def test_ipv6_transition_link_local_is_refused_for_private_agents(address):
    assert not address_policy.address_allowed(address, allow_private=True)


@pytest.mark.parametrize("address", ["::1", "2606:4700::1111"])
def test_plain_ipv6_addresses_keep_their_own_class(address):
    expected = address == "2606:4700::1111"
    assert address_policy.address_allowed(address, allow_private=False) is expected


def test_nat64_link_local_is_refused_even_for_private_agents():
    assert not address_policy.address_allowed("64:ff9b::a9fe:a9fe", allow_private=True)


async def test_hosts_sharing_an_ip_do_not_share_tls_connections(monkeypatch):
    _install_dns(
        monkeypatch, {**DNS, "a.example": [PUBLIC_AGENT], "b.example": [PUBLIC_AGENT]}
    )
    created: list[_Network] = []

    def network():
        created.append(_Network(handler=lambda request: httpx.Response(200)))
        return created[-1]

    monkeypatch.setattr(bounded_http, "_network_transport", network)
    policy = address_policy.DestinationPolicy("https://a.example")
    async with bounded_http.bounded_client(timeout=1, policy=policy) as client:
        await client.get("https://a.example/x")
        await client.get("https://b.example/y")
        await client.get("https://a.example/z")

    # One connection pool per hostname: a connection verified for a.example is
    # never reused for b.example.
    assert [len(item.requests) for item in created] == [2, 1]


async def test_card_fetch_refusal_reports_the_reason(monkeypatch):
    from agent.inspection import AgentInspectionService
    from models.request import InspectionCenterRequest

    _install_dns(monkeypatch)
    _install_network(monkeypatch, _card_server("http://agent.example"))

    response = await AgentInspectionService().inspect_agent_card(
        InspectionCenterRequest(agent_url="https://mixed.example")
    )

    assert response.status_code == 403
    assert "link-local" not in response.result[0]
    assert "non-public" in response.result[0]
