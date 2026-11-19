"""Execution-owned adapter from gateway attempts to durable model events."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from execution.orchestrator.models import (
    ModelStreamEvent,
    ModelTextPart,
    ModelToolCallPart,
    ModelToolResultPart,
    ModelTurnRequest,
    UsageRecord,
)
from execution.orchestrator.ports import CancellationSignal
from execution.orchestrator.profiles import ModelRouteConfiguration
from llm_gateway.error_classification import classify_gateway_error
from llm_gateway.gateway import LLMTurnGateway
from llm_gateway.model_registry import ModelRouteInfo
from llm_gateway.structured_generation import StructuredActionError
from llm_gateway.turn_types import (
    GatewayTextPart,
    GatewayToolCallPart,
    GatewayToolDefinition,
    GatewayToolResultPart,
    GatewayTurnEvent,
    GatewayTurnMessage,
    GatewayTurnRequest,
)


class _SignalAborted(Exception):
    pass


class GatewayModelRuntime:
    """Expose visible bounded retries around a one-attempt LLM gateway."""

    def __init__(
        self,
        gateway: LLMTurnGateway,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_retry_delay_seconds: float = 30.0,
    ) -> None:
        self._gateway = gateway
        self._sleep = sleep
        self._random_value = random_value
        self._now = now
        self._max_retry_delay_seconds = max_retry_delay_seconds

    async def stream_turn(
        self,
        request: ModelTurnRequest,
        *,
        signal: CancellationSignal,
    ) -> AsyncIterator[ModelStreamEvent]:
        max_retries = min(
            request.model.max_provider_retries,
            request.remaining_provider_retries,
        )
        attempt = 1
        while True:
            if (
                request.absolute_deadline_at is not None
                and self._now() >= request.absolute_deadline_at
            ):
                yield ModelStreamEvent(
                    kind="error",
                    error_class="timeout",
                    retryable=False,
                    error_code="deadline",
                )
                return
            yield ModelStreamEvent(kind="attempt_started", attempt=attempt)
            try:
                if signal.cancelled:
                    raise _SignalAborted
                timeout = self._attempt_timeout(request)
                if timeout <= 0:
                    raise TimeoutError
                gateway_request = _to_gateway_request(request, attempt=attempt)
                cancel_event = asyncio.Event()
                async with asyncio.timeout(timeout):
                    async for event in _iterate_with_signal(
                        self._gateway.stream_turn_once(
                            gateway_request, cancel_event=cancel_event
                        ),
                        signal=signal,
                        cancel_event=cancel_event,
                    ):
                        if (
                            event.kind == "finish"
                            and event.finish_reason == "content_filter"
                        ):
                            yield ModelStreamEvent(
                                kind="attempt_failed",
                                attempt=attempt,
                                error_class="content_filter",
                                retryable=False,
                            )
                            yield ModelStreamEvent(
                                kind="error",
                                attempt=attempt,
                                error_class="content_filter",
                                retryable=False,
                                error_code="content_filter",
                            )
                            return
                        yield _to_model_event(event, attempt=attempt)
                return
            except asyncio.CancelledError:
                raise
            except _SignalAborted:
                yield ModelStreamEvent(
                    kind="attempt_failed",
                    attempt=attempt,
                    error_class="aborted",
                    retryable=False,
                )
                yield ModelStreamEvent(
                    kind="error",
                    attempt=attempt,
                    error_class="aborted",
                    retryable=False,
                    error_code="aborted",
                )
                return
            except Exception as exc:
                classified = classify_gateway_error(exc)
                if isinstance(exc, StructuredActionError):
                    classified = type(classified)("invalid_request", False)
                can_retry = classified.retryable and attempt <= max_retries
                delay = self._retry_delay(attempt, classified.retry_after_seconds)
                if can_retry and request.absolute_deadline_at is not None:
                    remaining = (
                        request.absolute_deadline_at - self._now()
                    ).total_seconds()
                    if remaining <= 0 or delay >= remaining:
                        can_retry = False
                yield ModelStreamEvent(
                    kind="attempt_failed",
                    attempt=attempt,
                    error_class=classified.error_class,
                    retryable=can_retry,
                    error_code=str(getattr(exc, "code", "") or "") or None,
                )
                if not can_retry:
                    yield ModelStreamEvent(
                        kind="error",
                        attempt=attempt,
                        error_class=classified.error_class,
                        retryable=False,
                        error_code=str(getattr(exc, "code", "") or "") or None,
                    )
                    return
                next_attempt = attempt + 1
                yield ModelStreamEvent(
                    kind="retry_scheduled",
                    attempt=next_attempt,
                    error_class=classified.error_class,
                    retryable=True,
                    retry_delay_ms=int(delay * 1000),
                )
                if request.absolute_deadline_at is not None:
                    remaining = (
                        request.absolute_deadline_at - self._now()
                    ).total_seconds()
                    if remaining <= 0 or delay >= remaining:
                        yield ModelStreamEvent(
                            kind="error",
                            attempt=attempt,
                            error_class="timeout",
                            retryable=False,
                            error_code="deadline",
                        )
                        return
                try:
                    await _sleep_with_signal(self._sleep, delay, signal)
                except _SignalAborted:
                    yield ModelStreamEvent(kind="attempt_started", attempt=next_attempt)
                    yield ModelStreamEvent(
                        kind="attempt_failed",
                        attempt=next_attempt,
                        error_class="aborted",
                        retryable=False,
                    )
                    yield ModelStreamEvent(
                        kind="error",
                        attempt=next_attempt,
                        error_class="aborted",
                        retryable=False,
                        error_code="aborted",
                    )
                    return
                if (
                    request.absolute_deadline_at is not None
                    and self._now() >= request.absolute_deadline_at
                ):
                    yield ModelStreamEvent(
                        kind="error",
                        attempt=attempt,
                        error_class="timeout",
                        retryable=False,
                        error_code="deadline",
                    )
                    return
                attempt = next_attempt

    def _attempt_timeout(self, request: ModelTurnRequest) -> float:
        timeout = request.model.provider_timeout_seconds
        if request.absolute_deadline_at is not None:
            remaining = (request.absolute_deadline_at - self._now()).total_seconds()
            timeout = min(timeout, remaining)
        return timeout

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(0.0, retry_after), self._max_retry_delay_seconds)
        base = min(0.25 * (2 ** (attempt - 1)), self._max_retry_delay_seconds)
        return min(
            base + base * 0.25 * self._random_value(), self._max_retry_delay_seconds
        )


async def _iterate_with_signal(
    stream: AsyncIterator[GatewayTurnEvent],
    *,
    signal: CancellationSignal,
    cancel_event: asyncio.Event,
) -> AsyncIterator[GatewayTurnEvent]:
    iterator = stream.__aiter__()
    next_task: asyncio.Task[GatewayTurnEvent] | None = None
    cancel_task: asyncio.Task[None] | None = None
    completed = False
    try:
        while True:
            next_task = asyncio.create_task(iterator.__anext__())
            cancel_task = asyncio.create_task(signal.wait())
            done, _ = await asyncio.wait(
                {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                raise _SignalAborted
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            cancel_task = None
            try:
                event = next_task.result()
            except StopAsyncIteration:
                completed = True
                return
            next_task = None
            yield event
    finally:
        for task in (next_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (next_task, cancel_task) if task is not None),
            return_exceptions=True,
        )
        if not completed:
            cancel_event.set()
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def _sleep_with_signal(
    sleep: Callable[[float], Awaitable[None]],
    delay: float,
    signal: CancellationSignal,
) -> None:
    if signal.cancelled:
        raise _SignalAborted
    sleep_task = asyncio.create_task(sleep(delay))
    cancel_task = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            {sleep_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done:
            raise _SignalAborted
    finally:
        for task in (sleep_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleep_task, cancel_task, return_exceptions=True)


def _to_gateway_request(
    request: ModelTurnRequest, *, attempt: int
) -> GatewayTurnRequest:
    messages: list[GatewayTurnMessage] = []
    for message in request.messages:
        parts: list[Any] = []
        for part in message.content:
            if isinstance(part, ModelTextPart):
                parts.append(GatewayTextPart(text=part.text))
            elif isinstance(part, ModelToolCallPart):
                parts.append(
                    GatewayToolCallPart(
                        call_id=part.call_id,
                        tool_name=part.tool_name,
                        arguments=part.arguments,
                    )
                )
            elif isinstance(part, ModelToolResultPart):
                parts.append(
                    GatewayToolResultPart(
                        call_id=part.call_id,
                        tool_name=part.tool_name,
                        content="\n".join(item.text for item in part.content),
                        is_error=part.is_error,
                    )
                )
        messages.append(GatewayTurnMessage(role=message.role, parts=parts))
    return GatewayTurnRequest(
        provider=request.model.provider,  # type: ignore[arg-type]
        model_id=request.model.model_id,
        api=request.model.api,  # type: ignore[arg-type]
        system_prompt=request.system_prompt,
        messages=messages,
        tools=[
            GatewayToolDefinition(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in request.tools
        ],
        tool_choice=request.tool_choice,
        tool_strategy=request.model.tool_strategy,
        temperature=request.model.temperature,
        thinking_level=request.thinking_level,
        max_output_tokens=request.model.max_output_tokens,
        timeout_seconds=request.model.provider_timeout_seconds,
        turn_id=request.turn_id,
    )


def _to_model_event(event: GatewayTurnEvent, *, attempt: int) -> ModelStreamEvent:
    usage = None
    if event.usage is not None:
        usage = UsageRecord(
            input_tokens=event.usage.input_tokens,
            output_tokens=event.usage.output_tokens,
            cache_read_tokens=event.usage.cache_read_tokens,
            cache_write_tokens=event.usage.cache_write_tokens,
        )
    return ModelStreamEvent(
        kind=event.kind,
        attempt=attempt,
        provider_request_id=event.provider_request_id,
        call_id=event.call_id,
        tool_name=event.tool_name,
        delta=event.delta,
        usage=usage,
        finish_reason=event.finish_reason,
    )


def route_configuration_from_gateway(
    route: ModelRouteInfo,
) -> ModelRouteConfiguration:
    """Translate one explicit gateway route into the frozen execution contract."""

    return ModelRouteConfiguration(
        route=route.logical_name,
        provider=route.provider,
        model_id=route.model_id,
        api=route.api,
        supports_native_tools=route.supports_native_tools,
        supports_provider_strict_schema=route.supports_provider_strict_schema,
        supports_local_structured_action=route.supports_local_structured_action,
        context_window=route.context_window,
        max_output_tokens=route.max_output_tokens,
        temperature=route.default_temperature,
        provider_timeout_seconds=route.timeout_seconds,
        max_provider_retries=route.max_provider_retries,
        supported_thinking_levels=list(route.supported_thinking_levels),
    )


__all__ = ["GatewayModelRuntime", "route_configuration_from_gateway"]
