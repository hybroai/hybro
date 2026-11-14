"""Behavioral tests for the A2A client facade.

The facade is driven over the real wire against an in-process A2A 1.0 agent
(`tests.fakes.a2a_v1_agent`), which implements the JSON-RPC binding and SSE
framing from the specification. That keeps these tests honest: they assert what
an agent receives and what callers get back, not which SDK call was made.
"""

import contextlib
import logging
from typing import Any

import httpx
import pytest
from a2a.client.errors import A2AClientError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport
from a2a.compat.v0_3.rest_transport import CompatRestTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_adapter import client_facade, inspection, remote_task
from a2a_adapter.card_data import build_agent_card
from a2a_adapter.translators import a2a_card_to_snapshot, facade_result_to_model
from common.types import AgentCard, Message, MessageRole, Task, TaskState
from tests.fakes.a2a_v1 import (
    JSONRPC_BINDING,
    make_agent_card,
    make_legacy_card,
    make_v03_card_json,
)
from tests.fakes.a2a_v1_agent import (
    A2A10AgentStub,
    completed_task_payload,
    new_message_id,
    text_artifact_update,
    text_status_update,
)

CARD_PATH = "/.well-known/agent-card.json"
LEGACY_CARD_PATH = "/.well-known/agent.json"


class _RoutedTransport(httpx.AsyncBaseTransport):
    """Serve the stub app, optionally failing for one host.

    Used to exercise the Docker host fallback: a request to the failing host
    raises a connection error, and the retried request to the fallback host is
    served normally.
    """

    def __init__(self, app: Any, *, fail_hosts: set[str] | None = None) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._fail_hosts = fail_hosts or set()
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.hosts.append(host)
        if host in self._fail_hosts:
            raise httpx.ConnectError("All connection attempts failed")
        return await self._inner.handle_async_request(request)


def _agent_client(
    monkeypatch,
    app: Any,
    *,
    fail_hosts: set[str] | None = None,
) -> _RoutedTransport:
    """Point the facade's bounded clients at an in-process agent."""
    transport = _RoutedTransport(app, fail_hosts=fail_hosts)
    client = httpx.AsyncClient(transport=transport)

    @contextlib.asynccontextmanager
    async def _bounded(*, timeout: float, **_limits: Any):
        yield client

    monkeypatch.setattr(client_facade, "bounded_client", _bounded)
    monkeypatch.setattr(client_facade, "event_bounded_client", _bounded)
    monkeypatch.setattr(remote_task, "bounded_client", _bounded)
    return transport


def _streaming_card(**overrides: Any):
    """An agent card that advertises streaming, so the SDK returns SSE frames."""
    return make_agent_card(
        url="http://agent.test/", capabilities={"streaming": True}, **overrides
    )


def _user_message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "user",
        "message_id": overrides.pop("message_id", new_message_id()),
        "parts": overrides.pop("parts", [{"kind": "text", "text": "hello"}]),
    }
    message.update(overrides)
    return message


# ---------------------------------------------------------------------------
# Card resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_agent_card_returns_dict_and_falls_back_to_previous_path(
    monkeypatch,
):
    stub = A2A10AgentStub(
        card=make_agent_card(name="Fallback Agent"),
        card_path_status={CARD_PATH: 404},
    )
    _agent_client(monkeypatch, stub.app)

    result = await client_facade.fetch_agent_card_with_fallback(
        "http://agent.test", timeout=1
    )

    assert result["name"] == "Fallback Agent"
    assert stub.card_requests == [CARD_PATH, LEGACY_CARD_PATH]


@pytest.mark.asyncio
async def test_fetch_agent_card_reports_failure_when_no_path_serves_a_card(
    monkeypatch,
):
    stub = A2A10AgentStub(card_path_status={CARD_PATH: 404, LEGACY_CARD_PATH: 404})
    _agent_client(monkeypatch, stub.app)

    with pytest.raises(client_facade.A2AClientFacadeError):
        await client_facade.fetch_agent_card_with_fallback(
            "http://agent.test", timeout=1
        )


@pytest.mark.asyncio
async def test_fetch_agent_card_retries_through_docker_host_for_loopback(
    monkeypatch, caplog
):
    stub = A2A10AgentStub(card=make_agent_card(url="http://127.0.0.1:9060"))
    transport = _agent_client(monkeypatch, stub.app, fail_hosts={"127.0.0.1"})

    with caplog.at_level(logging.DEBUG):
        result = await client_facade.fetch_agent_card_with_fallback(
            "http://127.0.0.1:9060", timeout=1
        )

    assert result["name"] == stub.card.name
    assert transport.hosts == ["127.0.0.1", "host.docker.internal"]
    assert "a2a_docker_host_fallback_selected" in caplog.text


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_returns_plain_dict_and_forwards_the_message(
    monkeypatch,
):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload(text="hi there")})
    _agent_client(monkeypatch, stub.app)

    result = await client_facade.send_message(
        make_agent_card(url="http://agent.test/"),
        _user_message(parts=[{"kind": "text", "text": "hello"}]),
        timeout=1,
    )

    assert result["kind"] == "task"
    assert isinstance(result["result"], dict)
    assert result["error"] is None
    assert result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"

    wire_message = stub.last_message()
    assert wire_message["role"] == "ROLE_USER"
    assert wire_message["parts"][0]["text"] == "hello"
    assert stub.request_headers[-1]["a2a-version"] == "1.0"


@pytest.mark.asyncio
async def test_send_message_preserves_identity_and_metadata(monkeypatch):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent_client(monkeypatch, stub.app)

    await client_facade.send_message(
        make_agent_card(url="http://agent.test/"),
        _user_message(
            message_id="msg-123",
            task_id="task-9",
            context_id="ctx-9",
            metadata={
                "agent_id": "agent-1",
                "hybro.ai/a2a/selected-skill": {"schema_version": 1, "skill_id": "s1"},
            },
        ),
        timeout=1,
    )

    wire_message = stub.last_message()
    assert wire_message["messageId"] == "msg-123"
    assert wire_message["taskId"] == "task-9"
    assert wire_message["contextId"] == "ctx-9"
    assert wire_message["metadata"]["agent_id"] == "agent-1"
    assert wire_message["metadata"]["hybro.ai/a2a/selected-skill"]["skill_id"] == "s1"


@pytest.mark.asyncio
async def test_send_message_surfaces_protocol_errors_as_error_dict(monkeypatch):
    """A JSON-RPC error is a protocol outcome, not a raised transport error."""
    stub = A2A10AgentStub(send_error={"code": -32602, "message": "Invalid params"})
    _agent_client(monkeypatch, stub.app)

    result = await client_facade.send_message(
        make_agent_card(url="http://agent.test/"),
        _user_message(),
        timeout=1,
    )

    assert result["kind"] == "error"
    assert result["result"] is None
    assert result["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_send_message_reports_transport_failure(monkeypatch):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent_client(monkeypatch, stub.app, fail_hosts={"agent.test"})

    with pytest.raises(A2AClientError):
        await client_facade.send_message(
            make_agent_card(url="http://agent.test/"),
            _user_message(),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_send_message_accepts_internal_card_model(monkeypatch):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent_client(monkeypatch, stub.app)

    internal_card = make_agent_card(name="Internal", url="http://agent.test/")

    result = await client_facade.send_message(internal_card, _user_message(), timeout=1)

    assert result["kind"] == "task"


@pytest.mark.asyncio
async def test_send_message_retries_docker_host_for_loopback_card(monkeypatch, caplog):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    transport = _agent_client(monkeypatch, stub.app, fail_hosts={"127.0.0.1"})

    with caplog.at_level(logging.DEBUG):
        result = await client_facade.send_message(
            make_agent_card(url="http://127.0.0.1:9060/"),
            _user_message(),
            timeout=1,
        )

    assert result["kind"] == "task"
    assert transport.hosts == ["127.0.0.1", "host.docker.internal"]
    assert "a2a_docker_host_fallback_selected" in caplog.text


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_message_yields_one_normalized_frame_per_event(monkeypatch):
    stub = A2A10AgentStub(
        frames=[
            {"task": completed_task_payload()},
            {"statusUpdate": text_status_update(state="TASK_STATE_WORKING")},
            {"artifactUpdate": text_artifact_update(text="chunk")},
            {"statusUpdate": text_status_update(state="TASK_STATE_COMPLETED")},
        ]
    )
    _agent_client(monkeypatch, stub.app)

    frames = [
        frame
        async for frame in client_facade.stream_message(
            _streaming_card(), _user_message(), timeout=1
        )
    ]

    assert [frame["kind"] for frame in frames] == [
        "task",
        "status-update",
        "artifact-update",
        "status-update",
    ]
    assert all(isinstance(frame["result"], dict) for frame in frames)
    assert frames[1]["result"]["status"]["state"] == "TASK_STATE_WORKING"
    assert frames[2]["result"]["artifact"]["parts"][0]["text"] == "chunk"


@pytest.mark.asyncio
async def test_stream_message_early_close_logs_cancelled_completion(
    monkeypatch, caplog
):
    stub = A2A10AgentStub(
        frames=[
            {"task": completed_task_payload()},
            {"statusUpdate": text_status_update()},
        ]
    )
    _agent_client(monkeypatch, stub.app)
    caplog.set_level(logging.INFO, logger=client_facade.__name__)

    stream = client_facade.stream_message(_streaming_card(), _user_message(), timeout=1)
    assert (await anext(stream))["kind"] == "task"
    await stream.aclose()

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "a2a_call_completed"
    ]
    assert len(records) == 1
    assert records[0].operation == "message_stream"
    assert records[0].outcome == "cancelled"


# ---------------------------------------------------------------------------
# Cancel and fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_remote_task_reports_success(monkeypatch):
    stub = A2A10AgentStub(task_payload=completed_task_payload())
    _agent_client(monkeypatch, stub.app)

    acknowledged = await client_facade.cancel_remote_task(
        make_agent_card(url="http://agent.test/"), "task-1", timeout=1
    )

    assert acknowledged is True
    assert stub.requests[-1]["method"] == "CancelTask"


@pytest.mark.asyncio
async def test_cancel_remote_task_reports_failure_on_protocol_error(monkeypatch):
    stub = A2A10AgentStub(
        cancel_error={"code": -32002, "message": "Task cannot be canceled"}
    )
    _agent_client(monkeypatch, stub.app)

    acknowledged = await client_facade.cancel_remote_task(
        make_agent_card(url="http://agent.test/"), "task-1", timeout=1
    )

    assert acknowledged is False


@pytest.mark.asyncio
async def test_fetch_remote_task_returns_internal_task(monkeypatch):
    stub = A2A10AgentStub(task_payload=completed_task_payload(text="finished"))
    _agent_client(monkeypatch, stub.app)

    task = await remote_task.fetch_remote_task(
        make_agent_card(url="http://agent.test/"), "task-1", timeout=1
    )

    assert isinstance(task, Task)
    assert type(task).__module__ == "common.types"
    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.completed


@pytest.mark.asyncio
async def test_fetch_remote_task_returns_none_when_agent_reports_missing(monkeypatch):
    stub = A2A10AgentStub(task_payload=None)
    _agent_client(monkeypatch, stub.app)

    task = await remote_task.fetch_remote_task(
        make_agent_card(url="http://agent.test/"), "task-1", timeout=1
    )

    assert task is None


# ---------------------------------------------------------------------------
# A2A 0.3 compatibility
# ---------------------------------------------------------------------------


def _interface(url: str, version: str, binding: str = JSONRPC_BINDING) -> dict:
    return {"url": url, "protocol_binding": binding, "protocol_version": version}


def _transport_for(card_data: Any) -> Any:
    """Build the facade's SDK client for a card and return its transport."""
    config = client_facade._client_config(client=httpx.AsyncClient(), streaming=False)
    client = client_facade._create_client(build_agent_card(card_data), config)
    return client._transport


def test_v03_card_json_parses_onto_versioned_interfaces():
    """The SDK maps 0.3 ``url``/``additionalInterfaces`` onto interfaces."""
    card = build_agent_card(
        make_v03_card_json(
            url="http://agent.test/a2a",
            additionalInterfaces=[
                {"url": "http://agent.test/rest", "transport": "HTTP+JSON"}
            ],
        )
    )

    assert [
        (item.url, item.protocol_binding, item.protocol_version)
        for item in card.supported_interfaces
    ] == [
        ("http://agent.test/a2a", "JSONRPC", "0.3.0"),
        ("http://agent.test/rest", "HTTP+JSON", "0.3.0"),
    ]


def test_card_mixing_0_3_and_1_0_interfaces_parses():
    card = build_agent_card(
        make_agent_card(
            interfaces=[
                _interface("http://agent.test/rpc", "0.3"),
                _interface("http://agent.test/rest", "1.0", binding="HTTP+JSON"),
            ]
        )
    )

    assert [item.protocol_version for item in card.supported_interfaces] == [
        "0.3",
        "1.0",
    ]


@pytest.mark.parametrize(
    ("card_data", "transport_type", "url"),
    [
        (
            make_legacy_card(url="http://agent.test/rpc"),
            CompatJsonRpcTransport,
            "http://agent.test/rpc",
        ),
        (
            make_v03_card_json(url="http://agent.test/rpc"),
            CompatJsonRpcTransport,
            "http://agent.test/rpc",
        ),
        (
            make_v03_card_json(
                url="http://agent.test/rest", preferredTransport="HTTP+JSON"
            ),
            CompatRestTransport,
            "http://agent.test/rest",
        ),
        (
            make_agent_card(
                interfaces=[
                    _interface("http://agent.test/v03", "0.3.0"),
                    _interface("http://agent.test/v1", "1.0"),
                ]
            ),
            JsonRpcTransport,
            "http://agent.test/v1",
        ),
    ],
    ids=["legacy-card", "v03-json", "v03-rest", "mixed-prefers-1.0"],
)
def test_sdk_client_factory_selects_transport_by_interface_version(
    card_data, transport_type, url
):
    transport = _transport_for(card_data)

    assert type(transport) is transport_type
    assert transport.url == url


def test_registered_v03_card_round_trip_keeps_compat_transport():
    """Snapshot -> stored internal card -> SDK card stays on the 0.3 transport."""
    snapshot = a2a_card_to_snapshot(
        build_agent_card(make_v03_card_json(url="http://agent.test/a2a")),
        "http://agent.test",
    )
    stored = AgentCard.model_validate(snapshot.raw_card)

    transport = _transport_for(stored)

    assert stored.url == "http://agent.test"
    assert type(transport) is CompatJsonRpcTransport
    assert transport.url == "http://agent.test/a2a"


class _V03Agent:
    """A minimal A2A 0.3 JSON-RPC agent: 0.3 card and a 0.3 ``message/send``."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], dict[str, str]]] = []
        self.app = Starlette(
            routes=[
                Route(CARD_PATH, self._card, methods=["GET"]),
                Route("/a2a", self._rpc, methods=["POST"]),
            ]
        )

    async def _card(self, request: Request) -> JSONResponse:
        return JSONResponse(make_v03_card_json(url="http://agent.test/a2a"))

    async def _rpc(self, request: Request) -> JSONResponse:
        body = await request.json()
        self.requests.append((body, dict(request.headers)))
        task = {
            "kind": "task",
            "id": "task-1",
            "contextId": body["params"]["message"].get("contextId", "ctx-1"),
            "status": {"state": "completed"},
            "artifacts": [
                {"artifactId": "a1", "parts": [{"kind": "text", "text": "hi"}]}
            ],
        }
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": task})


@pytest.mark.asyncio
async def test_inspection_card_fetch_accepts_v03_card(monkeypatch):
    _agent_client(monkeypatch, _V03Agent().app)

    result = await client_facade.fetch_agent_card_with_fallback(
        "http://agent.test", timeout=1
    )
    internal = await inspection.fetch_agent_card_for_inspection(
        "http://agent.test", timeout=1
    )

    assert result["supportedInterfaces"] == [
        {
            "url": "http://agent.test/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3.0",
        }
    ]
    assert internal.url == "http://agent.test"


@pytest.mark.asyncio
async def test_send_message_to_v03_agent_returns_1_0_shaped_result(monkeypatch):
    agent = _V03Agent()
    _agent_client(monkeypatch, agent.app)

    result = await client_facade.send_message(
        make_v03_card_json(url="http://agent.test/a2a"),
        _user_message(parts=[{"kind": "text", "text": "hello"}], context_id="ctx-1"),
        timeout=1,
    )

    body, headers = agent.requests[-1]
    assert body["method"] == "message/send"
    assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "hello"}]
    assert headers["a2a-version"] == "0.3"
    assert result["kind"] == "task"
    assert result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    task = facade_result_to_model(result)
    assert isinstance(task, Task)
    assert task.status.state == TaskState.completed


# ---------------------------------------------------------------------------
# HITL continuation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_call_is_logged_with_operation_and_outcome(monkeypatch, caplog):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent_client(monkeypatch, stub.app)
    caplog.set_level(logging.INFO, logger=client_facade.__name__)

    await client_facade.send_message(
        make_agent_card(url="http://agent.test/"), _user_message(), timeout=1
    )

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "a2a_call_completed"
    )
    assert record.operation == "message_send"
    assert record.outcome == "success"
    assert isinstance(record.duration_ms, float)


@pytest.mark.asyncio
async def test_failed_call_is_logged_with_error_outcome(monkeypatch, caplog):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent_client(monkeypatch, stub.app, fail_hosts={"agent.test"})
    caplog.set_level(logging.ERROR, logger=client_facade.__name__)

    with pytest.raises(A2AClientError):
        await client_facade.send_message(
            make_agent_card(url="http://agent.test/"), _user_message(), timeout=1
        )

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "a2a_call_completed"
    )
    assert record.operation == "message_send"
    assert record.outcome == "error"
    assert record.error_type


# ---------------------------------------------------------------------------
# Binding selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_advertising_only_http_json_can_be_called(monkeypatch):
    """An agent that publishes only the REST binding is still addressable.

    Hybro prefers JSON-RPC but must not refuse an agent because it advertises
    HTTP+JSON instead; the SDK carries both bindings.
    """
    stub = A2A10AgentStub(
        card=make_agent_card(url="http://agent.test/", protocol_binding="HTTP+JSON")
    )
    _agent_client(monkeypatch, stub.app)

    card = await client_facade.fetch_agent_card_with_fallback(
        "http://agent.test", timeout=1
    )
    assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"

    # The failure this guards against surfaced while building the client, before
    # any request went out.
    client = client_facade._create_client(
        client_facade._build_card(card),
        client_facade._client_config(
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)),
            streaming=False,
        ),
    )
    assert client is not None


@pytest.mark.asyncio
async def test_jsonrpc_wins_over_card_order_when_both_bindings_are_offered(
    monkeypatch,
):
    """Preference follows Hybro's order, not the card's.

    The HTTP+JSON interface points at a host that cannot serve it, so a send
    that succeeds proves JSON-RPC was chosen even though the card lists
    HTTP+JSON first.
    """
    stub = A2A10AgentStub(
        send_result={"task": completed_task_payload()},
        card=make_agent_card(
            interfaces=[
                {
                    "url": "https://rest.invalid",
                    "protocol_binding": "HTTP+JSON",
                    "protocol_version": "1.0",
                },
                {
                    "url": "http://agent.test/",
                    "protocol_binding": "JSONRPC",
                    "protocol_version": "1.0",
                },
            ]
        ),
    )
    _agent_client(monkeypatch, stub.app)

    result = await client_facade.send_message(
        await client_facade.fetch_agent_card_with_fallback(
            "http://agent.test", timeout=1
        ),
        _user_message(),
        timeout=1,
    )

    assert result["kind"] == "task"
    assert stub.requests[-1]["method"] == "SendMessage"


# ---------------------------------------------------------------------------
# Frame conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_frame_converts_to_internal_message(monkeypatch):
    """A message-only response becomes an internal Message.

    This is the response shape of a single-shot agent: send_message yields a
    ``message`` frame rather than a task. Callers branch on the result's kind,
    so it has to come back as a Message with internal role and part spellings.
    """
    stub = A2A10AgentStub(
        send_result={
            "message": {
                "messageId": "response-1",
                "role": "ROLE_AGENT",
                "parts": [
                    {"text": "here is the answer", "mediaType": "text/plain"},
                    {"data": {"records": [1, 2]}},
                ],
            }
        }
    )
    _agent_client(monkeypatch, stub.app)

    response = await client_facade.send_message(
        make_agent_card(url="http://agent.test/"), _user_message(), timeout=1
    )
    assert response["kind"] == "message"

    model = facade_result_to_model(response)

    assert isinstance(model, Message)
    assert model.kind == "message"
    assert model.role is MessageRole.AGENT
    assert model.message_id == "response-1"
    assert [part.root.kind for part in model.parts] == ["text", "data"]
    assert model.parts[0].root.text == "here is the answer"
    assert model.parts[1].root.data["records"] == [1, 2]
