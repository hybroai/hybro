"""Mocked Responses wire and original-gateway-chain subscription coverage."""

import asyncio
import json
import traceback
from contextlib import aclosing
from unittest.mock import AsyncMock

import httpx
import pytest

from common.config.loader import Settings
from common.config.runtime_store import RuntimeConfigStore
from common.observability import bind_log_context, get_log_context
from llm_gateway.errors import LLMModelRoutingError, LLMProviderFailure
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.providers.openai_codex import RESPONSES_URL, OpenAICodexProvider
from llm_gateway.turn_types import GatewayTurnRequest
from tests.fakes.llm_runtime import oauth_config, oauth_credential, provider_runtime
from tests.test_openai_oauth import Body


def message(text="ok"):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def terminal(*, output=None, status="completed", **extra):
    return {
        "type": "response.completed"
        if status == "completed"
        else "response.incomplete",
        "response": {
            "id": "response-fixture",
            "status": status,
            "usage": {
                "input_tokens": 15,
                "output_tokens": 3,
                "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 2},
            },
            **({"output": output} if output is not None else {}),
            **extra,
        },
    }


def text_events(text="ok"):
    return [
        {"type": "response.created", "response": {"id": "response-fixture"}},
        {"type": "response.output_item.added", "output_index": 0, "item": message("")},
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "msg_1",
            "delta": text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message(text)},
        terminal(),
    ]


def wire(events, *, newline="\n", eof=False):
    data = (newline * 2).join(
        "data: " + json.dumps(event, ensure_ascii=False) for event in events
    )
    return (data + ("" if eof else newline * 2)).encode()


def provider(events, calls, *, status=200, body=None, content_type="text/event-stream"):
    def handle(request):
        calls.append(request)
        assert str(request.url) == RESPONSES_URL
        return httpx.Response(
            status,
            stream=body or Body(wire(events)),
            headers={"content-type": content_type} if content_type else {},
        )

    return OpenAICodexProvider(
        oauth_credential(), transport=httpx.MockTransport(handle)
    )


def turn(**updates):
    return GatewayTurnRequest.model_validate(
        {
            "provider": "openai",
            "model_id": "gpt-6-sol",
            "api": "responses",
            "system_prompt": "system fixture",
            "messages": [
                {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}
            ],
            "tools": [],
            "tool_choice": "auto",
            "tool_strategy": "native",
            "max_output_tokens": 100,
            "timeout_seconds": 2,
            "turn_id": "original-turn",
            **updates,
        }
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_sse_utf8_eof_correlation_usage_and_body(newline):
    calls = []

    class Chunks(Body):
        async def __aiter__(self):
            for value in self.data:
                yield bytes([value])

    body = Chunks(wire(text_events("你好\u2028ok"), newline=newline, eof=True))
    # Live Codex responses can omit Content-Type despite carrying valid SSE.
    adapter = provider([], calls, body=body, content_type=None)
    request = turn()
    before = request.model_dump()
    with bind_log_context(client_request_id="original-client"):
        events = [event async for event in adapter.stream_turn_once(request)]
        assert get_log_context()["client_request_id"] == "original-client"
    assert request.model_dump() == before and body.closed
    assert events[0].delta == "你好\u2028ok"
    assert events[-1].finish_reason == "stop"
    assert events[-2].usage.model_dump() == {
        "input_tokens": 15,
        "output_tokens": 3,
        "cache_read_tokens": 5,
        "cache_write_tokens": 2,
    }
    assert all(event.provider_request_id == "response-fixture" for event in events)
    sent = calls[0]
    assert (
        sent.headers["originator"] == "hybro" and sent.headers["user-agent"] == "hybro"
    )
    assert sent.headers["chatgpt-account-id"] == "fixture-account"
    assert sent.headers["x-client-request-id"] == "original-client"
    assert "session-id" not in sent.headers
    payload = json.loads(sent.content)
    assert payload["store"] is False and payload["stream"] is True
    assert "max_output_tokens" not in payload and "max_tokens" not in payload
    assert (
        payload["model"] == "gpt-6-sol" and payload["instructions"] == "system fixture"
    )


def tool_events():
    def item(call, args=""):
        return {
            "type": "function_call",
            "id": "fc_" + call,
            "call_id": call,
            "name": "lookup",
            "arguments": args,
        }

    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": item("call-a"),
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": item("call-b", '{"b":'),
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_call-a",
            "delta": '{"a":',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 1,
            "arguments": '{"b":2}',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "arguments": '{"a":1}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": item("call-b", '{"b":2}'),
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": item("call-a", '{"a":1}'),
        },
        terminal(),
    ]


async def test_ordered_history_parallel_tools_and_argument_backfill():
    calls = []
    request = turn(
        tools=[
            {
                "name": "lookup",
                "description": "find",
                "input_schema": {"type": "object"},
            }
        ],
        tool_choice="required",
        messages=[
            {
                "role": "assistant",
                "parts": [
                    {"kind": "text", "text": "before"},
                    {
                        "kind": "tool_call",
                        "call_id": "previous",
                        "tool_name": "lookup",
                        "arguments": {"q": 1},
                    },
                    {"kind": "text", "text": "after"},
                ],
            },
            {
                "role": "tool",
                "parts": [
                    {
                        "kind": "tool_result",
                        "call_id": "previous",
                        "tool_name": "lookup",
                        "content": "result",
                        "is_error": True,
                    }
                ],
            },
        ],
    )
    events = [
        event
        async for event in provider(tool_events(), calls).stream_turn_once(request)
    ]
    arguments = {}
    for event in events:
        if event.kind == "tool_call_arguments_delta":
            arguments[event.call_id] = arguments.get(event.call_id, "") + event.delta
    assert arguments == {"call-a": '{"a":1}', "call-b": '{"b":2}'}
    assert [event.call_id for event in events if event.kind == "tool_call_end"] == [
        "call-b",
        "call-a",
    ]
    assert events[-1].finish_reason == "tool_calls"
    payload = json.loads(calls[0].content)
    from openai.types.responses.response_output_message_param import (
        ResponseOutputMessageParam,
    )
    from pydantic import TypeAdapter

    assert [item["type"] for item in payload["input"]] == [
        "message",
        "function_call",
        "message",
        "function_call_output",
    ]
    for index, text in ((0, "before"), (2, "after")):
        history = payload["input"][index]
        schema = TypeAdapter(ResponseOutputMessageParam)
        assert (
            schema.dump_python(schema.validate_python(history), mode="json") == history
        )
        assert history == {
            "type": "message",
            "role": "assistant",
            "id": f"msg_{index}",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    assert (
        payload["input"][1]["call_id"] == payload["input"][3]["call_id"] == "previous"
    )
    assert (
        payload["tools"][0]["strict"] is None and "function" not in payload["tools"][0]
    )


async def test_tool_item_done_and_terminal_backfill_without_deltas():
    calls = []
    tool = {
        "type": "function_call",
        "id": "fc_x",
        "call_id": "x",
        "name": "lookup",
        "arguments": '{"q":1}',
    }
    request = turn(
        tools=[
            {"name": "lookup", "description": "", "input_schema": {"type": "object"}}
        ]
    )
    events = [
        event
        async for event in provider([terminal(output=[tool])], calls).stream_turn_once(
            request
        )
    ]
    assert [event.kind for event in events][:3] == [
        "tool_call_start",
        "tool_call_arguments_delta",
        "tool_call_end",
    ]
    assert events[1].delta == '{"q":1}'


async def test_reasoning_summary_and_refusal():
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs_x", "type": "reasoning"},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "delta": "summary",
        },
        {"type": "response.reasoning_summary_part.done", "output_index": 0},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "rs_x",
                "type": "reasoning",
                "encrypted_content": "never-print",
            },
        },
        terminal(),
    ]
    result = [event async for event in provider(events, []).stream_turn_once(turn())]
    assert [e.delta for e in result if e.kind == "reasoning_delta"] == [
        "summary",
        "\n\n",
    ]
    assert "never-print" not in repr(result)


@pytest.mark.parametrize(
    "events",
    [
        [],
        text_events()[:-1],
        [{"type": "error", "message": "fixture-secret-marker"}],
        [
            {
                "type": "response.failed",
                "response": {"error": {"message": "fixture-secret-marker"}},
            }
        ],
        [terminal(status="cancelled")],
        [terminal(status="incomplete", incomplete_details={"reason": "other"})],
        [
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": "fixture-secret-marker",
            }
        ],
        [None],
        [[]],
    ],
)
async def test_malformed_terminal_and_provider_errors_redacted(events):
    calls = []
    with pytest.raises(LLMProviderFailure) as error:
        _ = [event async for event in provider(events, calls).stream_turn_once(turn())]
    assert len(calls) == 1
    assert "fixture-secret-marker" not in "".join(
        traceback.format_exception(error.value)
    )
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "status,classification",
    [
        (302, "unknown"),
        (401, "authentication"),
        (429, "rate_limit"),
        (500, "provider_5xx"),
    ],
)
async def test_http_failure_single_attempt(status, classification):
    calls = []
    with pytest.raises(LLMProviderFailure) as error:
        await provider([], calls, status=status).generate([], "gpt-6-sol")
    assert len(calls) == 1
    assert error.value.classification.error_class == classification


async def test_local_bounds_no_unsupported_token_field():
    calls = []
    with pytest.raises(LLMProviderFailure):
        _ = [
            e
            async for e in provider(text_events("a" * 401), calls).stream_turn_once(
                turn()
            )
        ]
    with pytest.raises(LLMModelRoutingError):
        _ = [
            e
            async for e in provider([], calls).stream_turn_once(
                turn(max_output_tokens=32769)
            )
        ]
    assert len(calls) == 1
    events = [
        e
        async for e in provider(
            [
                terminal(
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                )
            ],
            [],
        ).stream_turn_once(turn())
    ]
    assert events[-1].finish_reason == "length"


@pytest.mark.parametrize(
    "text,valid",
    [
        ('{"ok":true}', True),
        ('{"ok":false}', False),
        ("[]", False),
        ("not json fixture-secret-marker", False),
    ],
)
async def test_structured_local_validation_once(text, valid):
    calls = []
    adapter = provider(text_events(text), calls)
    schema = {
        "type": "object",
        "properties": {"ok": {"const": True}},
        "required": ["ok"],
    }
    if valid:
        assert (
            await adapter.generate_structured([], "gpt-6-sol", schema=schema)
        ).data == {"ok": True}
    else:
        with pytest.raises(LLMProviderFailure) as error:
            await adapter.generate_structured([], "gpt-6-sol", schema=schema)
        assert "fixture-secret-marker" not in "".join(
            traceback.format_exception(error.value)
        )
    assert len(calls) == 1
    assert "JSON object must conform" in json.loads(calls[0].content)["instructions"]


async def test_ordinary_function_calls_results_and_named_choice():
    calls = []
    result = await provider(tool_events(), calls).generate(
        [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "before",
                "tool_calls": [
                    {"id": "old", "function": {"name": "lookup", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "old", "content": "result"},
        ],
        "gpt-6-sol",
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    )
    assert len(result.raw_response["choices"][0]["message"]["tool_calls"]) == 2
    assert json.loads(calls[0].content)["tool_choice"] == {
        "type": "function",
        "name": "lookup",
    }


@pytest.mark.parametrize("mode", ["cancel_event", "task", "early_close", "timeout"])
async def test_stream_cancellation_closes_response(mode):
    ready, release = asyncio.Event(), asyncio.Event()

    class Blocking(Body):
        async def __aiter__(self):
            yield wire(text_events()[:3])
            ready.set()
            await release.wait()

    body, cancel = Blocking(b""), asyncio.Event()
    adapter = provider([], [], body=body)

    async def consume():
        async with aclosing(
            adapter.stream_turn_once(
                turn(timeout_seconds=0.03 if mode == "timeout" else 2),
                cancel_event=cancel,
            )
        ) as events:
            async for _event in events:
                if mode == "early_close":
                    return

    task = asyncio.create_task(consume())
    if mode in {"cancel_event", "task"}:
        await ready.wait()
        if mode == "cancel_event":
            cancel.set()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif mode == "timeout":
        with pytest.raises(LLMProviderFailure):
            await task
    else:
        await task
    assert body.closed


@pytest.mark.parametrize("api_key", ["", "independent-embedding-key"])
async def test_original_gateway_all_oauth_entrypoints_refresh_and_embedding_isolation(
    tmp_path, monkeypatch, api_key
):
    from openai import AsyncOpenAI

    from llm_gateway import openai_oauth, setup_bindings

    store, credential = (
        RuntimeConfigStore(tmp_path / "runtime"),
        oauth_credential(expires_at=1),
    )
    store.save(oauth_config(), credential, {})
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    refreshed = oauth_credential(refresh="rotation")
    refresh = AsyncMock(return_value=refreshed)
    monkeypatch.setattr(openai_oauth, "refresh", refresh)
    calls, clients = [], []

    def handle(request):
        calls.append(request)
        assert str(request.url) == RESPONSES_URL
        assert json.loads(request.content)["model"] == "gpt-6-sol"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Body(wire(text_events('{"ok":true}'))),
        )

    original = setup_bindings.OpenAICodexProvider
    monkeypatch.setattr(
        setup_bindings,
        "OpenAICodexProvider",
        lambda *args, **kwargs: original(
            *args, **kwargs, transport=httpx.MockTransport(handle)
        ),
    )

    def sdk(**kwargs):
        client = AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: pytest.fail("OAuth reached API-key SDK")
                )
            ),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("llm_gateway.providers.openai_provider.AsyncOpenAI", sdk)
    monkeypatch.setattr("llm_gateway.providers.deepseek_provider.AsyncOpenAI", sdk)
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    settings = Settings(
        openai_api_key=api_key,
        deepseek_api_key="",
        openai_base_url="https://untrusted.invalid",
    )
    gateway = LLMGatewayImpl(settings_obj=settings)
    try:
        assert (await gateway.generate([], model="legacy")).model == "gpt-6-sol"
        assert (await gateway.generate_structured([], json_mode=True)).data == {
            "ok": True
        }
        assert [x async for x in gateway.generate_stream([])] == ['{"ok":true}']
        await gateway.generate_with_provider([], model="legacy", provider="deepseek")
        await gateway.generate_structured_with_provider(
            [], model="legacy", provider="deepseek", json_mode=True
        )
        assert [
            x
            async for x in gateway.generate_stream_with_provider(
                [], model="legacy", provider="deepseek"
            )
        ] == ['{"ok":true}']
        request = turn(
            provider="deepseek",
            model_id="frozen",
            api="chat_completions",
            tool_strategy="structured_action",
        )
        before = request.model_dump()
        with bind_log_context(client_request_id="preserved"):
            events = [e async for e in gateway.stream_turn_once(request)]
        assert request.model_dump() == before and events[-1].finish_reason == "stop"
        assert calls[-1].headers["x-client-request-id"] == "preserved"
        assert len(calls) == 7
        refresh.assert_awaited_once()
        gateway._providers["openai"].embed = AsyncMock(return_value=[1.0])
        assert await gateway.embed("original") == [1.0]
        assert len(calls) == 7
        assert store.load({}).authentication.credential == refreshed
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize(
    "fault",
    [
        "changed_arguments",
        "wrong_item",
        "unknown_tool",
        "duplicate_call",
        "nonobject_arguments",
        "none_choice",
        "required_missing",
    ],
)
async def test_tool_lifecycle_fail_closed(fault):
    import copy

    raw = copy.deepcopy(tool_events())
    request = turn(
        tools=[
            {"name": "lookup", "description": "", "input_schema": {"type": "object"}}
        ]
    )
    if fault == "changed_arguments":
        raw[4]["arguments"] = '{"changed":1}'
    elif fault == "wrong_item":
        raw[2]["item_id"] = "wrong"
    elif fault == "unknown_tool":
        raw[0]["item"]["name"] = "unadvertised"
    elif fault == "duplicate_call":
        raw[1]["item"]["call_id"] = raw[0]["item"]["call_id"]
    elif fault == "nonobject_arguments":
        raw = [
            terminal(
                output=[
                    {
                        "type": "function_call",
                        "id": "fc_x",
                        "call_id": "x",
                        "name": "lookup",
                        "arguments": "[]",
                    }
                ]
            )
        ]
    elif fault == "none_choice":
        request = request.model_copy(update={"tool_choice": "none"})
    else:
        request = request.model_copy(update={"tool_choice": "required"})
        raw = [terminal()]
    calls = []
    with bind_log_context(client_request_id="error-correlation"):
        with pytest.raises(LLMProviderFailure) as error:
            _ = [
                event async for event in provider(raw, calls).stream_turn_once(request)
            ]
    assert error.value.client_request_id == "error-correlation"
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["malformed_json", "wire_limit", "invalid_counter"])
async def test_sse_limits_and_usage_validation(monkeypatch, fault):
    from llm_gateway.providers import anthropic_provider

    raw = text_events()
    if fault == "invalid_counter":
        raw[-1]["response"]["usage"]["input_tokens"] = True
    body = Body(b"data: not-json\n\n" if fault == "malformed_json" else wire(raw))
    if fault == "wire_limit":
        monkeypatch.setattr(anthropic_provider, "_WIRE_BYTES", 10)
    with pytest.raises(LLMProviderFailure):
        _ = [
            event
            async for event in provider([], [], body=body).stream_turn_once(turn())
        ]
    assert body.closed


async def test_oauth_never_selects_api_models_or_images(tmp_path):
    from common.config.runtime_config import (
        ResolvedCredential,
        RuntimeConfigurationError,
    )
    from common.config.runtime_store import RuntimeState
    from llm_gateway.image_types import GatewayImageRequest
    from llm_gateway.setup_bindings import create_image_provider, create_provider

    state = RuntimeState(
        oauth_config(), ResolvedCredential("stored", oauth_credential())
    )
    assert isinstance(
        create_provider(state, "https://untrusted.invalid"), OpenAICodexProvider
    )
    with pytest.raises(RuntimeConfigurationError):
        create_image_provider(state, "https://untrusted.invalid")
    with pytest.raises(LLMModelRoutingError):
        await provider([], []).generate([], "gpt-5-mini")
    # No request made or generated image DTO/route changes needed.
    assert GatewayImageRequest is not None


async def test_original_gateway_stream_early_close_closes_oauth_client(monkeypatch):
    from common.config.runtime_config import ResolvedCredential
    from common.config.runtime_store import RuntimeState

    class Waiting(Body):
        async def __aiter__(self):
            yield wire(text_events()[:3])
            await asyncio.Event().wait()

    body = Waiting(b"")
    adapter = provider([], [], body=body)
    monkeypatch.setattr(
        "llm_gateway.gateway.load_provider_runtime",
        lambda: provider_runtime(
            RuntimeState(
                oauth_config(), ResolvedCredential("stored", oauth_credential())
            )
        ),
    )
    gateway = LLMGatewayImpl(
        providers={"openai": adapter},
        settings_obj=Settings(openai_api_key="", deepseek_api_key=""),
    )
    async with aclosing(gateway.generate_stream([])) as stream:
        assert await anext(stream) == "ok"
    assert body.closed
