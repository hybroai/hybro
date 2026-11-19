from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from execution.model_loop.model_runtime import (
    GatewayModelRuntime,
    route_configuration_from_gateway,
)
from execution.model_loop.streaming import ModelStreamAssembler
from execution.orchestrator.models import ModelMessage, ModelTextPart, ModelTurnRequest
from execution.orchestrator.session import EventCancellationSignal
from llm_gateway.model_registry import ModelRouteInfo
from llm_gateway.turn_types import GatewayTurnEvent, GatewayUsage
from tests._orchestrator_helpers import NOW, NeverCancelled, profile


class Gateway:
    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.requests = []

    async def stream_turn_once(self, request, *, cancel_event):
        self.requests.append(request)
        attempt = self.attempts.pop(0)
        if isinstance(attempt, BaseException):
            raise attempt
        for event in attempt:
            yield event


class HTTPError(RuntimeError):
    def __init__(self, status_code, retry_after=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers={} if retry_after is None else {"retry-after": str(retry_after)},
        )


def request(*, retries=2):
    model = profile().model.model_copy(update={"max_provider_retries": retries})
    return ModelTurnRequest(
        turn_id="turn-1",
        model=model,
        system_prompt="system",
        messages=[ModelMessage(role="user", content=[ModelTextPart(text="hi")])],
        tools=[],
        remaining_provider_retries=retries,
        absolute_deadline_at=NOW + timedelta(seconds=20),
    )


@pytest.mark.asyncio
async def test_runtime_exposes_retry_and_discards_failed_attempt_output():
    class FlakyGateway:
        def __init__(self):
            self.calls = 0
            self.requests = []

        async def stream_turn_once(self, request, *, cancel_event):
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                yield GatewayTurnEvent(kind="text_delta", delta="discard")
                raise HTTPError(503)
            yield GatewayTurnEvent(kind="text_delta", delta="keep")
            yield GatewayTurnEvent(
                kind="usage", usage=GatewayUsage(input_tokens=2, output_tokens=1)
            )
            yield GatewayTurnEvent(kind="finish", finish_reason="stop")

    gateway = FlakyGateway()

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    runtime = GatewayModelRuntime(
        gateway, sleep=sleep, random_value=lambda: 0, now=lambda: NOW
    )
    events = [
        event async for event in runtime.stream_turn(request(), signal=NeverCancelled())
    ]
    assembler = ModelStreamAssembler()
    for event in events:
        assembler.accept(event)
    outcome = assembler.build_outcome(message_id="assistant", created_at=NOW)

    assert outcome.assistant.content[0].text == "keep"
    assert [
        event.kind
        for event in events
        if event.kind in {"attempt_started", "attempt_failed", "retry_scheduled"}
    ] == ["attempt_started", "attempt_failed", "retry_scheduled", "attempt_started"]
    assert sleeps == [0.25]
    assert gateway.requests[0].provider == "openai"


@pytest.mark.asyncio
async def test_runtime_bounds_retry_after_by_profile_and_global_delay():
    gateway = Gateway(
        [
            HTTPError(429, retry_after=999),
            [GatewayTurnEvent(kind="finish", finish_reason="stop")],
        ]
    )
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    runtime = GatewayModelRuntime(
        gateway, sleep=sleep, now=lambda: NOW, max_retry_delay_seconds=3
    )
    events = [
        event
        async for event in runtime.stream_turn(
            request(retries=1), signal=NeverCancelled()
        )
    ]
    assert sleeps == [3]
    assert (
        next(
            event for event in events if event.kind == "retry_scheduled"
        ).retry_delay_ms
        == 3000
    )


@pytest.mark.asyncio
async def test_retry_backoff_never_crosses_absolute_deadline():
    gateway = Gateway([HTTPError(429, retry_after=30)])
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    near_deadline = request(retries=1).model_copy(
        update={"absolute_deadline_at": NOW + timedelta(milliseconds=100)}
    )
    events = [
        event
        async for event in GatewayModelRuntime(
            gateway, sleep=sleep, now=lambda: NOW
        ).stream_turn(near_deadline, signal=NeverCancelled())
    ]

    assert sleeps == []
    assert [event.kind for event in events] == [
        "attempt_started",
        "attempt_failed",
        "error",
    ]
    assert events[-1].error_class == "rate_limit"


@pytest.mark.asyncio
async def test_deadline_expiry_after_retry_scheduled_prevents_backoff_sleep():
    gateway = Gateway([HTTPError(503)])
    current = NOW
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    runtime = GatewayModelRuntime(gateway, sleep=sleep, now=lambda: current)
    deadline_request = request(retries=1).model_copy(
        update={"absolute_deadline_at": NOW + timedelta(seconds=1)}
    )
    events = []
    async for event in runtime.stream_turn(deadline_request, signal=NeverCancelled()):
        events.append(event)
        if event.kind == "retry_scheduled":
            current = NOW + timedelta(seconds=2)

    assert sleeps == []
    assert [event.kind for event in events] == [
        "attempt_started",
        "attempt_failed",
        "retry_scheduled",
        "error",
    ]
    assert events[-1].error_code == "deadline"


@pytest.mark.asyncio
async def test_expired_compaction_emits_no_attempt_and_calls_no_gateway():
    gateway = Gateway([[GatewayTurnEvent(kind="finish", finish_reason="stop")]])
    expired = request(retries=1).model_copy(
        update={
            "purpose": "compaction",
            "absolute_deadline_at": NOW,
        }
    )

    events = [
        event
        async for event in GatewayModelRuntime(gateway, now=lambda: NOW).stream_turn(
            expired, signal=NeverCancelled()
        )
    ]
    assembler = ModelStreamAssembler()
    for event in events:
        assembler.accept(event)

    assert [event.kind for event in events] == ["error"]
    assert gateway.requests == []
    assert (
        assembler.build_outcome(message_id="none", created_at=NOW).kind
        == "provider_error"
    )


@pytest.mark.asyncio
async def test_runtime_returns_typed_context_overflow_without_retry():
    gateway = Gateway([HTTPError(400)])
    gateway.attempts[0] = type("ContextError", (HTTPError,), {})(400)
    gateway.attempts[0].args = ("maximum context length",)
    events = [
        event
        async for event in GatewayModelRuntime(gateway, now=lambda: NOW).stream_turn(
            request(), signal=NeverCancelled()
        )
    ]
    assert [event.kind for event in events] == [
        "attempt_started",
        "attempt_failed",
        "error",
    ]
    assert events[-1].error_class == "context_overflow"


@pytest.mark.asyncio
async def test_runtime_timeout_closes_stream_and_sets_cancel_event():
    closed = False
    observed_cancel = None

    class BlockingGateway:
        async def stream_turn_once(self, request, *, cancel_event):
            nonlocal closed, observed_cancel
            observed_cancel = cancel_event
            try:
                await asyncio.Event().wait()
                yield GatewayTurnEvent(kind="finish", finish_reason="stop")
            finally:
                closed = True

    timed_request = request().model_copy(
        update={
            "model": request().model.model_copy(
                update={"provider_timeout_seconds": 0.01}
            )
        }
    )
    events = [
        event
        async for event in GatewayModelRuntime(
            BlockingGateway(), now=lambda: NOW
        ).stream_turn(timed_request, signal=NeverCancelled())
    ]

    assert events[-1].error_class == "timeout"
    assert closed is True
    assert observed_cancel is not None and observed_cancel.is_set()


@pytest.mark.asyncio
async def test_content_filter_is_terminal_error_without_assistant():
    gateway = Gateway(
        [[GatewayTurnEvent(kind="finish", finish_reason="content_filter")]]
    )
    events = [
        event
        async for event in GatewayModelRuntime(gateway, now=lambda: NOW).stream_turn(
            request(retries=0), signal=NeverCancelled()
        )
    ]
    assembler = ModelStreamAssembler()
    for event in events:
        assembler.accept(event)
    outcome = assembler.build_outcome(message_id="assistant", created_at=NOW)

    assert [event.kind for event in events] == [
        "attempt_started",
        "attempt_failed",
        "error",
    ]
    assert outcome.kind == "provider_error"
    assert outcome.error_class == "content_filter"
    assert outcome.assistant is None


@pytest.mark.asyncio
async def test_runtime_abort_during_stream_closes_attempt():
    closed = False

    class BlockingGateway:
        async def stream_turn_once(self, request, *, cancel_event):
            nonlocal closed
            try:
                yield GatewayTurnEvent(kind="text_delta", delta="partial")
                await asyncio.Event().wait()
            finally:
                closed = True

    signal = EventCancellationSignal()
    runtime = GatewayModelRuntime(BlockingGateway(), now=lambda: NOW)

    async def collect():
        return [event async for event in runtime.stream_turn(request(), signal=signal)]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    signal.cancel()
    events = await task
    assert events[-1].error_class == "aborted"
    assert closed is True


@pytest.mark.asyncio
async def test_selected_thinking_level_reaches_gateway_request():
    gateway = Gateway([[GatewayTurnEvent(kind="finish", finish_reason="stop")]])
    selected = request(retries=0).model_copy(update={"thinking_level": "enabled"})

    [
        event
        async for event in GatewayModelRuntime(gateway, now=lambda: NOW).stream_turn(
            selected, signal=NeverCancelled()
        )
    ]

    assert gateway.requests[0].thinking_level == "enabled"


def test_gateway_route_metadata_translates_to_execution_configuration():
    route = ModelRouteInfo(
        logical_name="fast",
        provider="openai",
        model_id="gpt-test",
        api="chat_completions",
        supports_native_tools=True,
        supports_provider_strict_schema=True,
        supports_local_structured_action=False,
        context_window=32_000,
        max_output_tokens=2_000,
        default_temperature=0.2,
        timeout_seconds=20,
        max_provider_retries=2,
        supported_thinking_levels=(),
    )

    config = route_configuration_from_gateway(route)

    assert config.route == "fast"
    assert config.provider == "openai"
    assert config.max_output_tokens == 2_000
