"""Direct execution exercised through the real SDK and an in-process agent."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

from a2a_adapter import client_facade
from common.dto.agent import AgentInfo
from common.errors import AppError, NotFoundError, UpstreamError, ValidationError
from common.observability import get_log_context
from execution import direct_agent
from execution.direct_agent import DirectAgentExecution
from models.agent_network import AgentMessageRequest
from tests.fakes.a2a_v1 import make_agent_card
from tests.fakes.a2a_v1_agent import (
    A2A10AgentStub,
    completed_task_payload,
    input_required_task_payload,
)


class _Network:
    def __init__(self, *, url: str = "http://agent.test/") -> None:
        self.target = AgentInfo(
            agent_id="private-agent",
            is_public=False,
            url=url,
            raw_card=MessageToDict(make_agent_card(url=url)),
        )
        self.error: AppError | None = None
        self.resolutions: list[tuple[str, str | None]] = []

    async def resolve_target(
        self, agent_id: str, group_id: str | None = None
    ) -> AgentInfo:
        self.resolutions.append((agent_id, group_id))
        if self.error is not None:
            raise self.error
        return self.target


def _request(**message_fields: Any) -> AgentMessageRequest:
    return AgentMessageRequest(
        agent_id="private-agent",
        client_request_id="request-1",
        group_id="group-1",
        message={
            "role": "user",
            "parts": [{"kind": "text", "text": "hello"}],
            **message_fields,
        },
    )


class _ServesCard(httpx.AsyncBaseTransport):
    """Serve each origin's Agent Card; pass every other request through."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/.well-known/"):
            origin = f"{request.url.scheme}://{request.url.netloc.decode()}/"
            return httpx.Response(200, json=MessageToDict(make_agent_card(url=origin)))
        return await self._inner.handle_async_request(request)


def _transport(
    monkeypatch, transport: httpx.AsyncBaseTransport, *, serve_card: bool = True
) -> None:
    if serve_card:
        transport = _ServesCard(transport)

    @asynccontextmanager
    async def bounded(*, timeout: float, **_policy: Any):
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            yield client

    monkeypatch.setattr(client_facade, "bounded_client", bounded)
    monkeypatch.setattr(client_facade, "card_client", bounded)


def _agent(monkeypatch, stub: A2A10AgentStub) -> None:
    _transport(monkeypatch, httpx.ASGITransport(app=stub.app), serve_card=False)


@pytest.mark.asyncio
async def test_completed_task_preserves_raw_artifacts_history_and_metadata(monkeypatch):
    payload = completed_task_payload(
        artifacts=[
            {
                "artifactId": "report",
                "name": "report.bin",
                "parts": [
                    {"raw": "AAEC", "mediaType": "application/octet-stream"},
                    {"data": {"values": [1.5, True]}, "metadata": {"source": "remote"}},
                ],
                "metadata": {"revision": 2.5},
            }
        ]
    )
    payload["history"] = [payload["status"]["message"]]
    payload["metadata"] = {"remote-only": {"label": "keep me"}}
    stub = A2A10AgentStub(send_result={"task": payload})
    _agent(monkeypatch, stub)
    request = _request()

    response = await DirectAgentExecution(_Network()).send(request)

    assert response.result == {**payload, "kind": "task"}
    assert response.client_request_id == "request-1"
    assert response.agent_id == "private-agent"
    assert stub.last_message()["messageId"] == request.message.message_id
    assert [item["method"] for item in stub.requests] == ["SendMessage"]
    # The send follows the live card, fetched once from the registered URL.
    assert stub.card_requests == ["/.well-known/agent-card.json"]


@pytest.mark.asyncio
async def test_message_response_remains_protojson_not_internal_message(monkeypatch):
    payload = {
        "messageId": "agent-reply",
        "contextId": "remote-context",
        "role": "ROLE_AGENT",
        "parts": [{"text": "hello", "mediaType": "text/plain"}],
        "metadata": {"remote": True},
    }
    stub = A2A10AgentStub(send_result={"message": payload})
    _agent(monkeypatch, stub)

    response = await DirectAgentExecution(_Network()).send(_request())

    assert response.result == {**payload, "kind": "message"}


@pytest.mark.asyncio
async def test_input_required_continuation_keeps_remote_ids_and_resolves_again(
    monkeypatch,
):
    stub = A2A10AgentStub(send_result={"task": input_required_task_payload()})
    _agent(monkeypatch, stub)
    network = _Network()
    execution = DirectAgentExecution(network)

    first = await execution.send(_request(messageId="first-message"))
    assert first.result["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    stub.send_result = {"task": completed_task_payload()}
    second = await execution.send(
        _request(
            messageId="continuation-message",
            taskId=first.result["id"],
            contextId=first.result["contextId"],
            parts=[{"kind": "text", "text": "Paris"}],
        )
    )

    assert second.result["status"]["state"] == "TASK_STATE_COMPLETED"
    assert stub.last_message() == {
        "messageId": "continuation-message",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Paris"}],
    }
    assert network.resolutions == [("private-agent", "group-1")] * 2
    assert [item["method"] for item in stub.requests] == ["SendMessage"] * 2

    network.error = NotFoundError("Agent", "private-agent")
    with pytest.raises(NotFoundError) as failure:
        await execution.send(_request())
    assert failure.value.details["client_request_id"] == "request-1"
    assert len(stub.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["TASK_STATE_FAILED", "TASK_STATE_WORKING"])
async def test_failed_or_nonterminal_task_is_not_promoted_to_completion(
    monkeypatch, state
):
    payload = {
        "id": "remote-task",
        "contextId": "remote-context",
        "status": {"state": state},
    }
    stub = A2A10AgentStub(send_result={"task": payload})
    _agent(monkeypatch, stub)

    response = await DirectAgentExecution(_Network()).send(_request())

    assert response.result == {**payload, "kind": "task"}
    assert [item["method"] for item in stub.requests] == ["SendMessage"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_result,send_error",
    [
        (None, {"code": -32602, "message": "secret-protocol-data"}),
        ({}, None),
        ({"task": {}}, None),
        ({"message": {}}, None),
        ({"statusUpdate": {"taskId": "task-1"}}, None),
        ({"task": {"id": "task-1", "status": {"state": "secret-invalid-enum"}}}, None),
    ],
    ids=[
        "protocol",
        "no-result",
        "empty-task",
        "empty-message",
        "unexpected",
        "malformed",
    ],
)
async def test_protocol_or_malformed_response_is_safe_upstream_error(
    monkeypatch, send_result, send_error
):
    stub = A2A10AgentStub(send_result=send_result, send_error=send_error)
    _agent(monkeypatch, stub)

    with pytest.raises(UpstreamError) as failure:
        await DirectAgentExecution(_Network()).send(_request())

    assert failure.value.code == "UPSTREAM"
    assert failure.value.details["client_request_id"] == "request-1"
    assert "secret" not in str(failure.value)
    assert "secret" not in repr(failure.value.details)
    assert [item["method"] for item in stub.requests] == ["SendMessage"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,expected_code",
    [(httpx.ReadError, "UPSTREAM"), (httpx.ReadTimeout, "UPSTREAM_TIMEOUT")],
)
async def test_ambiguous_transport_failure_never_resends_to_docker_host(
    monkeypatch, error_type, expected_code
):
    attempts: list[str] = []

    async def fail_after_send(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise error_type(
            "secret-token: connection lost after delivery", request=request
        )

    _transport(monkeypatch, httpx.MockTransport(fail_after_send))
    with pytest.raises(AppError) as failure:
        await DirectAgentExecution(_Network(url="http://127.0.0.1:9060/")).send(
            _request()
        )

    assert failure.value.code == expected_code
    assert failure.value.details["client_request_id"] == "request-1"
    assert "secret" not in str(failure.value)
    assert "secret" not in repr(failure.value.details)
    assert attempts == ["http://127.0.0.1:9060/"]


@pytest.mark.asyncio
async def test_whole_operation_deadline_includes_target_resolution(monkeypatch):
    monkeypatch.setattr(direct_agent, "_SEND_TIMEOUT_SECONDS", 0.01)
    resolution_cancelled = asyncio.Event()
    sends: list[httpx.Request] = []

    class HangingNetwork(_Network):
        async def resolve_target(self, agent_id, group_id=None):
            try:
                await asyncio.Event().wait()
            finally:
                resolution_cancelled.set()

    def unexpected_send(request):
        sends.append(request)
        return httpx.Response(500)

    _transport(monkeypatch, httpx.MockTransport(unexpected_send))
    with pytest.raises(AppError) as failure:
        await DirectAgentExecution(HangingNetwork()).send(_request())

    assert failure.value.code == "UPSTREAM_TIMEOUT"
    assert failure.value.details["client_request_id"] == "request-1"
    assert resolution_cancelled.is_set()
    assert sends == []


@pytest.mark.asyncio
async def test_whole_operation_deadline_cancels_hanging_send(monkeypatch):
    monkeypatch.setattr(direct_agent, "_SEND_TIMEOUT_SECONDS", 0.01)
    attempts: list[str] = []
    send_cancelled = asyncio.Event()

    async def hang(request):
        attempts.append(str(request.url))
        try:
            await asyncio.Event().wait()
        finally:
            send_cancelled.set()

    _transport(monkeypatch, httpx.MockTransport(hang))
    with pytest.raises(AppError) as failure:
        await DirectAgentExecution(_Network(url="http://127.0.0.1:9060/")).send(
            _request()
        )

    assert failure.value.code == "UPSTREAM_TIMEOUT"
    assert send_cancelled.is_set()
    assert attempts == ["http://127.0.0.1:9060/"]


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_retry_and_clears_log_context(
    monkeypatch,
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    attempts: list[str] = []
    contexts: list[dict[str, Any]] = []

    async def hang(request):
        attempts.append(str(request.url))
        contexts.append(get_log_context())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    _transport(monkeypatch, httpx.MockTransport(hang))
    previous_context = get_log_context()
    task = asyncio.create_task(DirectAgentExecution(_Network()).send(_request()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert attempts == ["http://agent.test/"]
    assert contexts[0]["client_request_id"] == "request-1"
    assert get_log_context() == previous_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_fields",
    [
        {"parts": [{"kind": "file", "file": {"bytes": "secret-invalid-base64!"}}]},
        {"metadata": {"unsupported": object()}},
    ],
    ids=["base64", "protobuf-struct"],
)
async def test_outbound_conversion_failure_is_validation_not_upstream(
    monkeypatch, message_fields
):
    stub = A2A10AgentStub(send_result={"task": completed_task_payload()})
    _agent(monkeypatch, stub)

    with pytest.raises(ValidationError) as failure:
        await DirectAgentExecution(_Network()).send(_request(**message_fields))

    assert failure.value.code == "VALIDATION"
    assert failure.value.details["client_request_id"] == "request-1"
    assert "secret" not in str(failure.value)
    assert stub.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (httpx.ConnectError, "UPSTREAM"),
        (httpx.ReadTimeout, "UPSTREAM_TIMEOUT"),
        (404, "UPSTREAM"),
    ],
    ids=["connect", "timeout", "not-found"],
)
async def test_card_fetch_failure_fails_the_send_without_leaking(
    monkeypatch, failure, expected_code
):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if isinstance(failure, int):
            return httpx.Response(failure, text="secret-token: not here")
        raise failure("secret-token: card fetch failed", request=request)

    _transport(monkeypatch, httpx.MockTransport(handler), serve_card=False)
    with pytest.raises(AppError) as error:
        await DirectAgentExecution(_Network()).send(_request())

    assert error.value.code == expected_code
    assert error.value.details["client_request_id"] == "request-1"
    assert error.value.details["agent_id"] == "private-agent"
    assert "secret" not in str(error.value)
    assert "secret" not in repr(error.value.details)
    assert all(method == "GET" for method, _path in requests)


@pytest.mark.asyncio
async def test_target_without_registered_url_fails_before_any_request(monkeypatch):
    requests: list[httpx.Request] = []
    _transport(
        monkeypatch,
        httpx.MockTransport(lambda request: requests.append(request)),
        serve_card=False,
    )
    network = _Network()
    network.target = network.target.model_copy(update={"url": None})

    with pytest.raises(UpstreamError) as error:
        await DirectAgentExecution(network).send(_request())

    assert str(error.value) == "Agent has no registered URL"
    assert error.value.details["client_request_id"] == "request-1"
    assert requests == []
