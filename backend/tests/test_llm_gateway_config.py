from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from common.config.loader import Settings
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore
from llm_gateway.config import LLMGatewayConfig
from llm_gateway.errors import ProviderNotConfigured


def _configure(tmp_path, monkeypatch, provider="openai", model="gpt-5-mini"):
    home = tmp_path / "runtime"
    monkeypatch.setenv("HYBRO_HOME", str(home))
    RuntimeConfigStore(home).save(
        RuntimeConfig(
            provider=RuntimeProvider(id=provider, auth="api_key"),
            models=RuntimeModels(text=model),
        ),
        ApiKeyCredential(provider=provider, api_key=SecretStr("fixture-key")),
        {},
    )


@pytest.mark.asyncio
async def test_openai_api_key_setup_wires_stored_key_into_embeddings(
    tmp_path, monkeypatch
):
    """Embeddings reuse the stored OpenAI key instead of an empty settings key."""
    from llm_gateway import gateway as gateway_module
    from llm_gateway.gateway import LLMGatewayImpl

    _configure(tmp_path, monkeypatch, "openai", "gpt-4o-mini")
    built = []

    def factory(*args, **kwargs):
        provider = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        built.append((kwargs, provider))
        return provider

    monkeypatch.setattr(gateway_module, "OpenAIProvider", factory)
    gateway = LLMGatewayImpl(settings_obj=Settings(openai_api_key=""))
    assert await gateway.embed("text") == [1.0]
    used = [provider for _, provider in built if provider.embed.await_count]
    assert [kwargs["api_key"] for kwargs, provider in built if provider in used] == [
        "fixture-key"
    ]


def test_from_settings_wires_runtime_policy_and_setup_provider(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "deepseek", "deepseek-v4-flash")
    settings = Settings(
        llm_gateway_max_attempts=4,
        llm_gateway_retry_backoff_seconds=1.25,
        llm_gateway_request_timeout_seconds=31.0,
        llm_gateway_stream_timeout_seconds=62.0,
        llm_gateway_supervisor_json_timeout_seconds=7.5,
        llm_gateway_supervisor_text_timeout_seconds=8.5,
        llm_gateway_supervisor_stream_timeout_seconds=9.5,
        llm_gateway_default_generation_model="custom_generation_route",
        llm_gateway_default_embedding_model="custom_embedding_route",
        llm_gateway_default_supervisor_model="custom_supervisor_route",
    )
    assert LLMGatewayConfig.from_settings(settings) == LLMGatewayConfig(
        generation_provider="deepseek",
        max_attempts=4,
        retry_backoff_seconds=1.25,
        request_timeout_seconds=31.0,
        stream_timeout_seconds=62.0,
        supervisor_json_timeout_seconds=7.5,
        supervisor_text_timeout_seconds=8.5,
        supervisor_stream_timeout_seconds=9.5,
        default_generation_model="custom_generation_route",
        default_embedding_model="custom_embedding_route",
        default_supervisor_model="custom_supervisor_route",
    )


@pytest.mark.parametrize("legacy_route", ["openai", "deepseek", "invalid"])
def test_setup_is_the_only_provider_source(tmp_path, monkeypatch, legacy_route):
    _configure(tmp_path, monkeypatch)
    settings = Settings(
        llm_gateway_generation_provider=legacy_route,
        deepseek_api_key="ignored",
        google_api_key="ignored",
    )
    assert LLMGatewayConfig.from_settings(settings).generation_provider == "openai"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "ignored-key"])
async def test_no_setup_boots_and_calls_fail_even_with_a_legacy_key(
    tmp_path, monkeypatch, key
):
    from llm_gateway.gateway import LLMGatewayImpl

    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "missing"))
    settings = Settings(openai_api_key=key)
    assert LLMGatewayConfig.from_settings(settings).generation_provider == "openai"
    gateway = LLMGatewayImpl(settings_obj=settings)
    for call in (
        gateway.generate([{"role": "user", "content": "hi"}]),
        gateway.embed("text"),
    ):
        with pytest.raises(ProviderNotConfigured) as caught:
            await call
        assert caught.value.code == "provider_not_configured"
    assert not (tmp_path / "missing" / "config.json").exists()


def test_orchestrator_defaults_resolve_without_env_example(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    from execution.adapters.profiles import OrchestratorProfileResolver
    from llm_gateway.model_registry import ModelRegistryImpl

    settings = Settings()
    registry = ModelRegistryImpl(settings)
    resolver = OrchestratorProfileResolver(
        model_registry=registry, settings_obj=settings
    )
    assert resolver.resolve("fast").thinking_level == "low"
    assert resolver.resolve("ultimate").thinking_level == "high"
