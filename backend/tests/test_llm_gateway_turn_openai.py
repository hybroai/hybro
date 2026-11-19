from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution.model_loop.model_runtime import GatewayModelRuntime
from execution.model_loop.streaming import (
    ModelStreamAssembler,
    TruncatedToolCallError,
)
from execution.orchestrator.models import ModelMessage, ModelTextPart, ModelTurnRequest
from llm_gateway.error_classification import classify_gateway_error
from llm_gateway.providers.openai_provider import OpenAIProvider
from llm_gateway.turn_types import (
    GatewayTextPart,
    GatewayToolCallPart,
    GatewayToolDefinition,
    GatewayToolResultPart,
    GatewayTurnMessage,
    GatewayTurnRequest,
)
from tests._orchestrator_helpers import NOW, NeverCancelled, profile


class Stream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)

    async def aclose(self):
        self.closed = True


def request() -> GatewayTurnRequest:
    return GatewayTurnRequest(
        provider="openai",
        model_id="gpt-test",
        api="chat_completions",
        system_prompt="system",
        messages=[
            GatewayTurnMessage(role="user", parts=[GatewayTextPart(text="hello")])
        ],
        tools=[
            GatewayToolDefinition(
                name="echo",
                description="echo",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ],
        tool_strategy="native",
        max_output_tokens=100,
        timeout_seconds=10,
        turn_id="turn-1",
    )


def chunk(*, delta=None, finish=None, usage=None, chunk_id="req-1"):
    choices = []
    if delta is not None or finish is not None:
        choices = [
            SimpleNamespace(delta=delta or SimpleNamespace(), finish_reason=finish)
        ]
    return SimpleNamespace(id=chunk_id, choices=choices, usage=usage)


@pytest.mark.asyncio
async def test_openai_turn_streams_reasoning_text_delayed_parallel_tools_and_usage():
    first_call = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='{"value":'),
    )
    second_call = SimpleNamespace(
        index=1,
        id="call-2",
        function=SimpleNamespace(name="echo", arguments='{"value":"b"}'),
    )
    stream = Stream(
        [
            chunk(delta=SimpleNamespace(reasoning_content="think", content="answer")),
            chunk(delta=SimpleNamespace(tool_calls=[first_call])),
            chunk(
                delta=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call-1",
                            function=SimpleNamespace(name="echo", arguments='"a"'),
                        ),
                        second_call,
                    ]
                )
            ),
            chunk(
                delta=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id=None,
                            function=SimpleNamespace(name=None, arguments="}"),
                        )
                    ]
                ),
                finish="tool_calls",
            ),
            chunk(
                usage=SimpleNamespace(
                    prompt_tokens=4,
                    completion_tokens=5,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                )
            ),
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=stream))
        )
    )

    events = [
        event
        async for event in OpenAIProvider(client=client).stream_turn_once(request())
    ]

    assert [event.kind for event in events] == [
        "text_delta",
        "reasoning_delta",
        "tool_call_start",
        "tool_call_arguments_delta",
        "tool_call_arguments_delta",
        "tool_call_start",
        "tool_call_arguments_delta",
        "tool_call_arguments_delta",
        "tool_call_end",
        "tool_call_end",
        "usage",
        "finish",
    ]
    assert (
        "".join(event.delta or "" for event in events if event.call_id == "call-1")
        == '{"value":"a"}'
    )
    assert events[-2].usage.input_tokens == 4
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_responses_streams_tools_and_replays_prior_tool_evidence():
    req = request().model_copy(
        update={
            "api": "responses",
            "thinking_level": "high",
            "messages": [
                GatewayTurnMessage(
                    role="user", parts=[GatewayTextPart(text="first request")]
                ),
                GatewayTurnMessage(
                    role="assistant",
                    parts=[
                        GatewayTextPart(text="delegating"),
                        GatewayToolCallPart(
                            call_id="prior-call",
                            tool_name="echo",
                            arguments={"value": "prior"},
                        ),
                    ],
                ),
                GatewayTurnMessage(
                    role="tool",
                    parts=[
                        GatewayToolResultPart(
                            call_id="prior-call",
                            tool_name="echo",
                            content='{"value":"done"}',
                        )
                    ],
                ),
                GatewayTurnMessage(
                    role="user", parts=[GatewayTextPart(text="continue")]
                ),
            ],
        }
    )
    response_id = "resp-1"
    response = SimpleNamespace(
        id=response_id,
        status="completed",
        incomplete_details=None,
        output=[SimpleNamespace(type="function_call")],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=7,
            input_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
    )
    stream = Stream(
        [
            SimpleNamespace(
                type="response.created",
                response=SimpleNamespace(id=response_id),
            ),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(type="reasoning"),
            ),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="echo",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=1,
                delta='{"value":"new"}',
            ),
            SimpleNamespace(
                type="response.output_item.done",
                output_index=1,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="echo",
                    arguments='{"value":"new"}',
                ),
            ),
            SimpleNamespace(type="response.completed", response=response),
        ]
    )
    create = AsyncMock(return_value=stream)
    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    events = [
        event async for event in OpenAIProvider(client=client).stream_turn_once(req)
    ]

    assert [event.kind for event in events] == [
        "tool_call_start",
        "tool_call_arguments_delta",
        "tool_call_end",
        "usage",
        "finish",
    ]
    assert events[0].tool_index == 1
    assert events[0].call_id == "call-1"
    assert events[-2].usage.input_tokens == 12
    assert events[-2].usage.cache_read_tokens == 3
    assert events[-1].finish_reason == "tool_calls"
    assert stream.closed is True

    sent = create.await_args.kwargs
    assert sent["instructions"] == "system"
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["store"] is False
    assert sent["max_output_tokens"] == 100
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["name"] == "echo"
    assert "function" not in sent["tools"][0]
    assert sent["input"] == [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "delegating"},
        {
            "type": "function_call",
            "call_id": "prior-call",
            "name": "echo",
            "arguments": '{"value":"prior"}',
        },
        {
            "type": "function_call_output",
            "call_id": "prior-call",
            "output": '{"value":"done"}',
        },
        {"role": "user", "content": "continue"},
    ]


@pytest.mark.asyncio
async def test_openai_responses_maps_text_and_incomplete_length():
    req = request().model_copy(
        update={"api": "responses", "tools": [], "thinking_level": "low"}
    )
    response = SimpleNamespace(
        id="resp-2",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
        usage=None,
    )
    stream = Stream(
        [
            SimpleNamespace(
                type="response.created", response=SimpleNamespace(id="resp-2")
            ),
            SimpleNamespace(type="response.output_text.delta", delta="partial"),
            SimpleNamespace(type="response.incomplete", response=response),
        ]
    )
    create = AsyncMock(return_value=stream)
    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    events = [
        event async for event in OpenAIProvider(client=client).stream_turn_once(req)
    ]

    assert [event.kind for event in events] == ["text_delta", "finish"]
    assert events[0].delta == "partial"
    assert events[-1].finish_reason == "length"
    sent = create.await_args.kwargs
    assert "tools" not in sent
    assert "tool_choice" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "error_class", "retryable"),
    [
        ("server_error", "provider_5xx", True),
        ("rate_limit_exceeded", "rate_limit", True),
        ("invalid_request_error", "invalid_request", False),
    ],
)
async def test_openai_responses_preserves_stream_failure_classification(
    code, error_class, retryable
):
    stream = Stream(
        [
            SimpleNamespace(
                type="response.failed",
                response=SimpleNamespace(
                    id="resp-failed",
                    error={"code": code, "message": "provider failed"},
                ),
            )
        ]
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(return_value=stream))
    )

    with pytest.raises(RuntimeError) as error:
        async for _ in OpenAIProvider(client=client).stream_turn_once(
            request().model_copy(update={"api": "responses"})
        ):
            pass

    classified = classify_gateway_error(error.value)
    assert classified.error_class == error_class
    assert classified.retryable is retryable


@pytest.mark.asyncio
async def test_openai_native_tools_skip_strict_mode_and_keep_schema():
    stream = Stream([chunk(delta=SimpleNamespace(content="ok"), finish="stop")])
    create = AsyncMock(return_value=stream)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    req = request().model_copy(
        update={
            "tools": [
                GatewayToolDefinition(
                    name="echo",
                    description="echo",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "uniqueItems": True,
                            }
                        },
                        "required": ["tags"],
                    },
                )
            ],
        }
    )

    async for _ in OpenAIProvider(client=client).stream_turn_once(req):
        pass

    sent = create.await_args.kwargs
    function = sent["tools"][0]["function"]
    assert "strict" not in function
    # Native mode must not mutate the caller's schema.
    assert function["parameters"]["properties"]["tags"]["uniqueItems"] is True


@pytest.mark.asyncio
async def test_openai_structured_action_tools_are_strictified():
    stream = Stream([chunk(delta=SimpleNamespace(content="ok"), finish="stop")])
    create = AsyncMock(return_value=stream)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    req = request().model_copy(
        update={
            "tool_strategy": "structured_action",
            "tools": [
                GatewayToolDefinition(
                    name="echo",
                    description="echo",
                    input_schema={
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "properties": {
                            "nested": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "uniqueItems": True,
                            },
                        },
                        "required": ["nested"],
                    },
                )
            ],
        }
    )

    async for _ in OpenAIProvider(client=client).stream_turn_once(req):
        pass

    sent = create.await_args.kwargs
    function = sent["tools"][0]["function"]
    assert function["strict"] is True
    parameters = function["parameters"]
    assert "$schema" not in parameters
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["nested"]["additionalProperties"] is False
    assert "uniqueItems" not in parameters["properties"]["tags"]


@pytest.mark.asyncio
async def test_openai_turn_uses_max_completion_tokens_not_deprecated_max_tokens():
    """gpt-5/o-series routes reject max_tokens; the gateway must send
    max_completion_tokens, which every current chat model accepts."""
    stream = Stream([chunk(delta=SimpleNamespace(content="ok"), finish="stop")])
    create = AsyncMock(return_value=stream)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    async for _ in OpenAIProvider(client=client).stream_turn_once(request()):
        pass

    kwargs = create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == 100
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
async def test_openai_usage_terminal_chunk_passes_runtime_and_assembler():
    stream = Stream(
        [
            chunk(delta=SimpleNamespace(content="done"), finish="stop"),
            chunk(
                usage=SimpleNamespace(
                    prompt_tokens=4,
                    completion_tokens=2,
                    prompt_tokens_details=None,
                )
            ),
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=stream))
        )
    )
    runtime = GatewayModelRuntime(OpenAIProvider(client=client), now=lambda: NOW)
    turn = ModelTurnRequest(
        turn_id="turn-openai-usage",
        model=profile().model,
        system_prompt="system",
        messages=[ModelMessage(role="user", content=[ModelTextPart(text="hi")])],
        tools=[],
        remaining_provider_retries=0,
        absolute_deadline_at=None,
    )
    assembler = ModelStreamAssembler()
    async for event in runtime.stream_turn(turn, signal=NeverCancelled()):
        assembler.accept(event)

    outcome = assembler.build_outcome(message_id="assistant", created_at=NOW)

    assert outcome.assistant is not None
    assert outcome.assistant.usage.input_tokens == 4
    assert outcome.assistant.content[0].text == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("length", "length"),
        ("content_filter", "content_filter"),
    ],
)
async def test_openai_finish_reason_mapping(raw, normalized):
    stream = Stream([chunk(delta=SimpleNamespace(), finish=raw)])
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=stream))
        )
    )
    events = [
        event
        async for event in OpenAIProvider(client=client).stream_turn_once(request())
    ]
    assert events[-1].finish_reason == normalized


@pytest.mark.asyncio
async def test_openai_length_finish_surfaces_started_tool_call_as_truncated():
    partial_call = SimpleNamespace(
        index=0,
        id="call-truncated",
        function=SimpleNamespace(name="echo", arguments='{"value":'),
    )
    stream = Stream(
        [chunk(delta=SimpleNamespace(tool_calls=[partial_call]), finish="length")]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=stream))
        )
    )
    runtime = GatewayModelRuntime(OpenAIProvider(client=client), now=lambda: NOW)
    turn = ModelTurnRequest(
        turn_id="turn-openai-truncated",
        model=profile().model,
        system_prompt="system",
        messages=[ModelMessage(role="user", content=[ModelTextPart(text="hi")])],
        tools=[],
        remaining_provider_retries=0,
        absolute_deadline_at=None,
    )
    assembler = ModelStreamAssembler()
    async for event in runtime.stream_turn(turn, signal=NeverCancelled()):
        assembler.accept(event)

    with pytest.raises(TruncatedToolCallError) as error:
        assembler.build_outcome(message_id="assistant", created_at=NOW)

    assert error.value.provider_call_id == "call-truncated"
    assert error.value.tool_name == "echo"
    assert error.value.tool_index == 0
    assert error.value.raw_arguments_digest
