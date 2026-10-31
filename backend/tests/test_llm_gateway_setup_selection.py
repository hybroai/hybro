"""Original public calls with mandatory JSON setup and stored credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.config.loader import Settings
from common.config.runtime_config import RuntimeConfigurationError
from common.config.runtime_store import (
    RuntimeConfigStore,
    current_store,
    runtime_home,
)
from common.dto import LLMResponse, LLMStructuredResponse
from llm_gateway.config import LLMGatewayConfig
from llm_gateway.errors import ProviderNotConfigured
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.model_registry import ModelRegistryImpl
from llm_gateway.provider_runtime import ProviderRuntime
from llm_gateway.turn_types import GatewayTurnEvent, GatewayTurnRequest
from tests.fakes.llm_runtime import runtime_state

MODELS = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-v4-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}


class Provider:
    def __init__(self):
        self.calls = []

    async def generate(self, messages, model, **kwargs):
        self.calls.append((model, kwargs))
        return LLMResponse(content="ok", model=model)

    async def generate_structured(self, messages, model, **kwargs):
        self.calls.append((model, kwargs))
        return LLMStructuredResponse(data={"ok": True}, model=model)

    async def generate_stream(self, messages, model, **kwargs):
        self.calls.append((model, kwargs))
        yield "ok"

    async def stream_turn_once(self, request, *, model=None, cancel_event=None):
        self.calls.append(
            (model or request.model_id, {"request": request, "cancel": cancel_event})
        )
        yield GatewayTurnEvent(
            kind="finish", finish_reason="stop", provider_request_id="provider-id"
        )


def composition(settings, providers):
    # Exactly the container construction sequence, without importing it.
    runtime = ProviderRuntime(current_store())
    config = LLMGatewayConfig.from_settings(settings)
    registry = ModelRegistryImpl(settings, provider_runtime=runtime)
    return LLMGatewayImpl(
        model_registry=registry,
        config=config,
        settings_obj=settings,
        providers=providers,
        provider_runtime=runtime,
    )


@pytest.mark.parametrize("selected", MODELS)
@pytest.mark.parametrize("source", ["stored", "environment"])
async def test_setup_controls_all_original_text_entrypoints_and_not_embeddings(
    tmp_path, monkeypatch, selected, source
):
    state = runtime_state(selected, MODELS[selected])
    store = RuntimeConfigStore(tmp_path / "selected")
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    for provider in MODELS:
        monkeypatch.delenv(f"{provider.upper()}_API_KEY", raising=False)
    environment = (
        {f"{selected.upper()}_API_KEY": "fixture-key"}
        if source == "environment"
        else {}
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    store.save(
        state.config,
        state.authentication.credential if source == "stored" else None,
        environment,
    )
    settings = Settings(
        openai_api_key=environment.get("OPENAI_API_KEY", ""),
        deepseek_api_key=environment.get("DEEPSEEK_API_KEY", ""),
    )
    text = Provider()
    embedding = SimpleNamespace(
        embed=AsyncMock(return_value=[1.0]), embed_batch=AsyncMock(return_value=[[2.0]])
    )
    # Separate injected clients prove that embeddings retain the original route.
    providers = {"openai": embedding, selected: text}
    if selected == "openai":
        text.embed = embedding.embed
        text.embed_batch = embedding.embed_batch
    gateway = composition(settings, providers)
    assert gateway._model_registry.get_model("embedding_model").provider == "openai"
    await gateway.generate(
        [], model="lead_ai_model", temperature=0.25, custom_option="kept"
    )
    await gateway.generate_structured([], json_mode=True)
    assert [s async for s in gateway.generate_stream([])] == ["ok"]
    await gateway.generate_with_provider([], model="custom-model", provider="deepseek")
    await gateway.generate_structured_with_provider(
        [], model="custom-model", provider="deepseek", json_mode=True
    )
    assert [
        s
        async for s in gateway.generate_stream_with_provider(
            [], model="custom-model", provider="deepseek"
        )
    ] == ["ok"]
    request = GatewayTurnRequest(
        provider="openai",
        model_id="legacy-frozen-model",
        api="responses",
        system_prompt="",
        messages=[],
        tools=[],
        tool_strategy="native",
        thinking_level="high",
        max_output_tokens=100,
        timeout_seconds=7,
        turn_id="exact-turn-id",
    )
    original = request.model_dump()
    result = [event async for event in gateway.stream_turn_once(request)]
    assert result[0].provider_request_id == "provider-id"
    assert request.model_dump() == original
    assert {model for model, _ in text.calls} == {MODELS[selected]}
    assert text.calls[0][1]["custom_option"] == "kept"
    wire = text.calls[-1][1]["request"]
    assert wire.turn_id == "exact-turn-id" and wire.timeout_seconds == 7
    if selected == "anthropic":
        assert (
            wire is request
        )  # Provider identity travels separately, never impersonated.
    assert await gateway.embed("one") == [1.0]
    assert await gateway.embed_batch(["two"]) == [[2.0]]
    embedding.embed.assert_awaited_once_with("one", model="text-embedding-3-small")
    embedding.embed_batch.assert_awaited_once_with(
        ["two"], model="text-embedding-3-small"
    )


@pytest.mark.parametrize("fault", ["malformed", "missing_key", "oauth"])
async def test_invalid_setup_fails_before_legacy_configuration_can_be_used(
    tmp_path, monkeypatch, fault
):
    state = runtime_state()
    store = RuntimeConfigStore(tmp_path / "selected")
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    store.save(state.config, state.authentication.credential, {})
    if fault == "malformed":
        (store.home / "config.json").write_text("not: [valid")
    elif fault == "missing_key":
        (store.home / "auth.json").unlink()
    elif fault == "oauth":
        (store.home / "config.json").write_text(
            "version: 1\nprovider:\n  id: openai\n  auth: oauth\nmodels:\n  text: gpt-4o-mini\n"
        )
    settings = Settings(openai_api_key="ignored-legacy-key", deepseek_api_key="")
    provider = Provider()
    if fault == "missing_key":
        # A credential problem boots, so setup can repair it; calls still fail.
        gateway = composition(settings, {"openai": provider})
        with pytest.raises(ProviderNotConfigured) as caught:
            await gateway.generate([])
        assert caught.value.code == "credential_invalid"
        assert provider.calls == []
        return
    with pytest.raises(RuntimeConfigurationError):
        composition(settings, {"openai": provider})


def test_runtime_home_has_no_host_mount_alias():
    assert str(runtime_home({"HOME": "/fixture"})) == "/fixture/.hybro"
    assert (
        str(runtime_home({"HOME": "/fixture", "HYBRO_HOME": "/selected"}))
        == "/selected"
    )


async def test_missing_setup_rejects_legacy_model_hints_before_calling_provider(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "absent"))
    settings = Settings(
        openai_api_key="",
        deepseek_api_key="",
        lead_ai_model="custom-old-model",
    )
    provider = Provider()
    provider.embed = AsyncMock(return_value=[3.0])
    provider.embed_batch = AsyncMock(return_value=[[4.0]])
    gateway = composition(settings, {"openai": provider})
    for call in (
        gateway.generate([], model="lead_ai_model"),
        gateway.generate_with_provider([], model="custom", provider="openai"),
        gateway.embed("one"),
    ):
        with pytest.raises(ProviderNotConfigured) as caught:
            await call
        assert caught.value.code == "provider_not_configured"
    assert provider.calls == []
    provider.embed.assert_not_awaited()


@pytest.mark.parametrize("selected", MODELS)
async def test_setup_selected_wire_through_original_gateway(
    tmp_path, monkeypatch, selected
):
    import json

    import httpx
    from openai import AsyncOpenAI

    from llm_gateway.providers import AnthropicProvider
    from tests.test_anthropic_provider import Body, start, stop, text_block, wire

    state = runtime_state(selected, MODELS[selected])
    store = RuntimeConfigStore(tmp_path / "selected")
    store.save(state.config, state.authentication.credential, {})
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    for provider in MODELS:
        monkeypatch.delenv(f"{provider.upper()}_API_KEY", raising=False)
    calls, clients = [], []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.host, payload))
        assert payload["model"] == MODELS[selected]
        if selected == "anthropic":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Body(wire([start(), *text_block('{"ok":true}'), *stop()])),
            )
        if payload.get("stream"):
            event = {
                "id": "provider-id",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
            )
        action = any(
            "Return exactly one action" in message.get("content", "")
            for message in payload["messages"]
        )
        return httpx.Response(
            200,
            json={
                "id": "provider-id",
                "model": MODELS[selected],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"action":"final","content":"ok"}'
                            if action
                            else '{"ok":true}',
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def sdk(**kwargs):
        client = AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("llm_gateway.providers.openai_provider.AsyncOpenAI", sdk)
    monkeypatch.setattr("llm_gateway.providers.deepseek_provider.AsyncOpenAI", sdk)
    monkeypatch.setattr(
        "llm_gateway.setup_bindings.AnthropicProvider",
        lambda **kwargs: AnthropicProvider(
            **kwargs, transport=httpx.MockTransport(handle)
        ),
    )
    gateway = composition(Settings(openai_api_key="", deepseek_api_key=""), None)
    request = GatewayTurnRequest(
        provider="openai",
        model_id="original-model",
        api="responses",
        system_prompt="",
        messages=[],
        tools=[],
        tool_strategy="native",
        max_output_tokens=100,
        timeout_seconds=1,
        turn_id="turn-id",
    )
    before = request.model_dump()
    try:
        assert (await gateway.generate([])).model == MODELS[selected]
        assert (await gateway.generate_structured([], json_mode=True)).data == {
            "ok": True
        }
        assert [chunk async for chunk in gateway.generate_stream([])] == ['{"ok":true}']
        events = [event async for event in gateway.stream_turn_once(request)]
        assert events[-1].finish_reason == "stop"
        assert len(calls) == 4
        assert all(
            host
            == {
                "anthropic": "api.anthropic.com",
                "openai": "api.openai.com",
                "deepseek": "api.deepseek.com",
            }[selected]
            for host, _ in calls
        )
        assert request.model_dump() == before
    finally:
        for client in clients:
            await client.close()
