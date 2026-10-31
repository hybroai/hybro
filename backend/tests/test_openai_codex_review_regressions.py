"""Independent-review regressions; synthetic credentials and mocked HTTP only."""

import asyncio
import json
import traceback
from unittest.mock import AsyncMock

import httpx
import pytest
from openai.types.responses.easy_input_message_param import EasyInputMessageParam
from openai.types.responses.response_output_message_param import (
    ResponseOutputMessageParam,
)
from pydantic import TypeAdapter

from common.config.loader import Settings
from common.config.runtime_config import ResolvedCredential, RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore, RuntimeState
from common.observability import bind_log_context
from llm_gateway.config import LLMGatewayConfig
from llm_gateway.errors import LLMProviderFailure, UnsupportedConfiguredProvider
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.model_registry import ModelRegistryImpl
from tests.fakes.llm_runtime import (
    oauth_config,
    oauth_credential,
    provider_runtime,
    runtime_state,
)
from tests.test_openai_codex_provider import (
    provider,
    terminal,
    text_events,
    turn,
    wire,
)
from tests.test_openai_oauth import Body


def gateway_for(monkeypatch, adapter):
    monkeypatch.setattr(
        "llm_gateway.gateway.load_provider_runtime",
        lambda: provider_runtime(
            RuntimeState(
                oauth_config(), ResolvedCredential("stored", oauth_credential())
            )
        ),
    )
    return LLMGatewayImpl(
        providers={"openai": adapter},
        settings_obj=Settings(openai_api_key="", deepseek_api_key=""),
    )


async def test_ordinary_assistant_history_matches_installed_responses_contract():
    calls = []
    await provider(text_events(), calls).generate(
        [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "before",
                "tool_calls": [
                    {
                        "id": "exact-old-call",
                        "function": {"name": "lookup", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "exact-old-call", "content": "result"},
            {"role": "assistant", "content": "after"},
        ],
        "gpt-6-sol",
    )
    inputs = json.loads(calls[0].content)["input"]
    assert [item.get("type", "message") for item in inputs] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    for index, role, content in (
        (0, "user", "question"),
        (1, "assistant", "before"),
        (4, "assistant", "after"),
    ):
        expected = (
            {"role": "user", "content": [{"type": "input_text", "text": content}]}
            if role == "user"
            else {
                "type": "message",
                "role": role,
                "id": f"msg_{index}",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": content, "annotations": []}
                ],
            }
        )
        assert inputs[index] == expected
        schema = TypeAdapter(
            EasyInputMessageParam if role == "user" else ResponseOutputMessageParam
        )
        assert (
            schema.dump_python(schema.validate_python(inputs[index]), mode="json")
            == inputs[index]
        )
    assert inputs[2]["call_id"] == inputs[3]["call_id"] == "exact-old-call"


@pytest.mark.parametrize("trailer", ["blocked", "extra", "same_chunk"])
@pytest.mark.parametrize("operation", ["turn", "generate"])
async def test_terminal_closes_without_reading_http_trailer(trailer, operation):
    resumed = False

    class Trailing(Body):
        async def __aiter__(self):
            nonlocal resumed
            yield wire(text_events()) + (
                b"data: invalid-trailing-json\n\n" if trailer == "same_chunk" else b""
            )
            resumed = True
            if trailer == "blocked":
                await asyncio.Event().wait()
            yield wire([terminal(), {"type": "error", "code": "server_error"}])

    body, calls = Trailing(b""), []
    adapter = provider([], calls, body=body)
    async with asyncio.timeout(0.2):
        if operation == "generate":
            result = await adapter.generate([], "gpt-6-sol", timeout_seconds=0.03)
            assert result.content == "ok"
            assert result.raw_response["choices"][0]["finish_reason"] == "stop"
        else:
            events = [
                event
                async for event in adapter.stream_turn_once(turn(timeout_seconds=0.03))
            ]
            assert [event.kind for event in events].count("finish") == 1
            assert events[-1].finish_reason == "stop"
    assert body.closed and not resumed and len(calls) == 1


def truncated_tool_events(stage):
    item = {
        "type": "function_call",
        "id": "fc_exact",
        "call_id": "exact-call",
        "name": "lookup",
        "arguments": '{"q":',
        "status": "incomplete",
    }
    if stage == "terminal_unmarked":
        item.pop("status")
    events = []
    if stage not in {"terminal_only", "terminal_unmarked"}:
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc_exact",
                    "delta": '{"q"',
                },
            ]
        )
    if stage == "item_done":
        events.append(
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        )
    events.append(
        terminal(
            status="incomplete",
            output=[item],
            incomplete_details={"reason": "max_output_tokens"},
        )
    )
    return events


@pytest.mark.parametrize(
    "stage", ["terminal_only", "terminal_unmarked", "delta", "item_done"]
)
async def test_partial_function_truncation_preserves_deltas_usage_and_length(stage):
    request = turn(
        tools=[
            {"name": "lookup", "description": "", "input_schema": {"type": "object"}}
        ],
        tool_choice="required",
    )
    events = [
        event
        async for event in provider(truncated_tool_events(stage), []).stream_turn_once(
            request
        )
    ]
    assert events[0].kind == "tool_call_start" and events[0].call_id == "exact-call"
    assert (
        "".join(
            event.delta for event in events if event.kind == "tool_call_arguments_delta"
        )
        == '{"q":'
    )
    assert all(event.kind != "tool_call_end" for event in events)
    assert events[-2].usage.output_tokens == 3
    assert events[-1].finish_reason == "length"


@pytest.mark.parametrize("over_limit", [False, True])
async def test_ordinary_gateway_truncation_accounts_usage_without_retry(
    monkeypatch, over_limit
):
    calls = []
    raw = text_events("partial")
    raw[-1] = terminal(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )
    if over_limit:
        raw[-1] = terminal()
        raw[-1]["response"]["usage"]["output_tokens"] = 101
    gateway = gateway_for(monkeypatch, provider(raw, calls))
    assert gateway.config.max_attempts == 2
    result = await gateway.generate([], max_tokens=100)
    assert result.content == "partial"
    assert result.usage.completion_tokens == (101 if over_limit else 3)
    assert result.usage.prompt_tokens == 15
    assert result.raw_response["choices"][0]["finish_reason"] == "length"
    assert len(calls) == 1


async def test_over_limit_turn_retains_actual_usage_and_length():
    raw = text_events()
    raw[-1]["response"]["usage"]["output_tokens"] = 101
    events = [
        event
        async for event in provider(raw, []).stream_turn_once(
            turn(max_output_tokens=100)
        )
    ]
    assert events[-2].kind == "usage" and events[-2].usage.output_tokens == 101
    assert events[-1].finish_reason == "length"


@pytest.mark.parametrize("valid", [False, True])
async def test_structured_truncated_output_is_still_locally_validated(valid):
    calls = []
    raw = text_events('{"ok":true}' if valid else '{"ok":')
    raw[-1] = terminal(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )
    adapter = provider(raw, calls)
    if valid:
        result = await adapter.generate_structured([], "gpt-6-sol", json_mode=True)
        assert result.data == {"ok": True} and result.usage.completion_tokens == 3
    else:
        with pytest.raises(LLMProviderFailure):
            await adapter.generate_structured([], "gpt-6-sol", json_mode=True)
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [None, 90.0])
@pytest.mark.parametrize(
    "operation",
    [
        "generate",
        "generate_structured",
        "generate_stream",
        "generate_with_provider",
        "generate_structured_with_provider",
        "generate_stream_with_provider",
        "_generate_with_provider_hint",
        "_generate_structured_with_provider_hint",
        "_generate_stream_with_provider_hint",
    ],
)
async def test_effective_gateway_timeout_reaches_codex_http(
    monkeypatch, operation, timeout
):
    calls = []
    gateway = gateway_for(monkeypatch, provider(text_events('{"ok":true}'), calls))
    options = {"timeout_seconds": timeout}
    if "structured" in operation:
        options["json_mode"] = True
    if "with_provider" in operation:
        options.update(
            model="stale-model",
            **{"provider_hint" if "_hint" in operation else "provider": "deepseek"},
        )
    result = getattr(gateway, operation)([], **options)
    if "stream" in operation:
        assert [chunk async for chunk in result] == ['{"ok":true}']
    else:
        await result
    # An explicit timeout must win; otherwise the gateway's configured default
    # must reach the HTTP client. Read the default from the gateway so this
    # asserts propagation rather than pinning the configured number.
    effective = (
        timeout
        if timeout is not None
        else (
            gateway.config.stream_timeout_seconds
            if "stream" in operation
            else gateway.config.request_timeout_seconds
        )
    )
    assert calls[0].extensions["timeout"] == {
        key: effective for key in ("connect", "read", "write", "pool")
    }
    assert len(calls) == 1


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("legacy", ["invalid", "openai", "deepseek"])
async def test_setup_precedes_legacy_validation_in_original_composition(
    tmp_path, monkeypatch, configured, legacy
):
    home = tmp_path / "runtime"
    monkeypatch.setenv("HYBRO_HOME", str(home))
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    if configured:
        RuntimeConfigStore(home).save(oauth_config(), oauth_credential(), {})
    settings = Settings(
        openai_api_key="",
        deepseek_api_key="",
        llm_gateway_generation_provider=legacy,
        lead_ai_model="",
        deepseek_model_name="",
        classifier_ai_model="",
        supervisor_model="",
    )
    if not configured:
        # No setup is required at boot; only then do the legacy hints count.
        with pytest.raises(UnsupportedConfiguredProvider):
            LLMGatewayConfig.from_settings(settings)
        return
    config = LLMGatewayConfig.from_settings(settings)
    registry = ModelRegistryImpl(
        settings, generation_provider=config.generation_provider
    )
    # Registry behavior is unchanged. Its stale/empty text hints must
    # not prevent construction or override the gateway's private setup route.
    assert registry.get_model("lead_ai_model").model_id == ""
    assert registry.get_route_configuration("supervisor_model").model_id == ""
    adapter, calls = provider(text_events(), []), []
    adapter._transport = httpx.MockTransport(
        lambda request: (
            calls.append(request)
            or httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Body(wire(text_events())),
            )
        )
    )
    adapter.embed = AsyncMock(return_value=[1.0])
    gateway = LLMGatewayImpl(
        config=config,
        model_registry=registry,
        settings_obj=settings,
        providers={"openai": adapter},
    )
    assert (await gateway.generate([])).model == "gpt-6-sol"
    frozen = turn(model_id=registry.get_model("lead_ai_model").model_id)
    events = [event async for event in gateway.stream_turn_once(frozen)]
    assert frozen.model_id == "" and events[-1].finish_reason == "stop"
    assert all(json.loads(call.content)["model"] == "gpt-6-sol" for call in calls)
    assert await gateway.embed("unchanged") == [1.0]
    adapter.embed.assert_awaited_once_with("unchanged", model="text-embedding-3-small")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "selected,model",
    [
        ("openai", "gpt-4o-mini"),
        ("deepseek", "deepseek-v4-flash"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
def test_api_key_setup_also_overrides_invalid_legacy_provider(
    tmp_path, monkeypatch, selected, model
):
    store = RuntimeConfigStore(tmp_path)
    state = runtime_state(selected, model)
    store.save(state.config, state.authentication.credential, {})
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path))
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        openai_api_key="",
        deepseek_api_key="",
        llm_gateway_generation_provider="invalid",
        lead_ai_model="",
    )
    config = LLMGatewayConfig.from_settings(settings)
    registry = ModelRegistryImpl(
        settings, generation_provider=config.generation_provider
    )
    adapter = object()
    gateway = LLMGatewayImpl(
        config=config,
        model_registry=registry,
        settings_obj=settings,
        providers={selected: adapter},
    )
    resolved, actual = gateway._resolve_provider(
        "lead_ai_model", provider_hint="deepseek", pair=gateway._runtime.current
    )
    assert resolved.model_id == model and resolved.provider == selected
    assert actual is adapter
    assert registry.get_model("embedding_model").provider == "openai"


@pytest.mark.parametrize("kind", ["error", "response.failed"])
@pytest.mark.parametrize(
    "code,classification,retryable",
    [
        ("server_error", "provider_5xx", True),
        ("rate_limit_exceeded", "rate_limit", True),
        ("invalid_api_key", "authentication", False),
        ("invalid_token", "authentication", False),
        ("insufficient_quota", "rate_limit", False),
        ("usage_limit_reached", "rate_limit", False),
        ("context_length_exceeded", "context_overflow", False),
        ("content_filter", "content_filter", False),
        ("unknown", "unknown", False),
        ("x" * 129, "unknown", False),
        ([], "unknown", False),
    ],
)
async def test_streamed_failures_keep_code_only_classification(
    kind, code, classification, retryable
):
    error = {"code": code, "message": "fixture-sensitive-provider-body"}
    event = (
        {"type": kind, **error}
        if kind == "error"
        else {"type": kind, "response": {"error": error}}
    )
    calls = []
    with bind_log_context(client_request_id="exact-request"):
        with pytest.raises(LLMProviderFailure) as caught:
            _ = [
                item async for item in provider([event], calls).stream_turn_once(turn())
            ]
    assert caught.value.classification.error_class == classification
    assert caught.value.classification.retryable is retryable
    assert caught.value.client_request_id == "exact-request"
    assert caught.value.__context__ is None
    assert "fixture-sensitive-provider-body" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert len(calls) == 1


def test_invalid_setup_does_not_bypass_legacy_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("invalid: config")
    with pytest.raises(RuntimeConfigurationError):
        LLMGatewayConfig.from_settings(
            Settings(llm_gateway_generation_provider="invalid")
        )
