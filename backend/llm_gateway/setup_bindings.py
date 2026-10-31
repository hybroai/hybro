"""Setup uses the same text adapter selection as the original gateway."""

import asyncio
from typing import TYPE_CHECKING

from common.config.runtime_config import (
    ApiKeyCredential,
    OAuthCredential,
    ResolvedCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
)
from common.config.runtime_store import RuntimeState
from llm_gateway.catalog import validate_models
from llm_gateway.providers import AnthropicProvider, DeepSeekProvider, OpenAIProvider
from llm_gateway.providers.openai_codex import CredentialResolver, OpenAICodexProvider
from llm_gateway.setup_service import _openai_base_url

if TYPE_CHECKING:
    from llm_gateway.providers.openai_images import OpenAIImageProvider


def create_provider(
    state: RuntimeState,
    base_url: str | None = None,
    *,
    oauth_resolve: CredentialResolver | None = None,
) -> OpenAIProvider | DeepSeekProvider | AnthropicProvider | OpenAICodexProvider:
    validate_models(state.config)
    if state.authentication is None:
        raise RuntimeConfigurationError("Provider credential is not available")
    credential = state.authentication.credential
    if isinstance(credential, OAuthCredential) and (
        state.config.provider.id,
        state.config.provider.auth,
    ) == (credential.provider, credential.auth):
        # Never pass deployment base URL to the subscription adapter.
        return OpenAICodexProvider(credential, resolve=oauth_resolve)
    if not isinstance(credential, ApiKeyCredential) or (
        credential.provider,
        credential.auth,
    ) != (state.config.provider.id, state.config.provider.auth):
        raise RuntimeConfigurationError("Selected authentication is not implemented")
    key = credential.api_key.get_secret_value()
    if credential.provider == "anthropic":
        return AnthropicProvider(api_key=key)
    if credential.provider == "deepseek":
        return DeepSeekProvider(api_key=key)
    return OpenAIProvider(
        api_key=key, base_url=_openai_base_url(base_url) or "https://api.openai.com/v1"
    )


def create_image_provider(
    state: RuntimeState, base_url: str | None = None
) -> "OpenAIImageProvider":
    from llm_gateway.providers.openai_images import OpenAIImageProvider

    validate_models(state.config)
    credential = (
        state.authentication.credential if state.authentication is not None else None
    )
    if (
        state.config.models.image is None
        or state.config.provider.id != "openai"
        or not isinstance(credential, ApiKeyCredential)
        or credential.provider != "openai"
        or credential.auth != "api_key"
    ):
        raise RuntimeConfigurationError("Image model is not configured")
    return OpenAIImageProvider(
        api_key=credential.api_key.get_secret_value(),
        base_url=_openai_base_url(base_url),
    )


async def verify_selection(
    config: RuntimeConfig,
    credential: ResolvedCredential,
    base_url: str | None,
) -> None:
    provider = create_provider(RuntimeState(config, credential), base_url)
    try:
        async with asyncio.timeout(60):
            response = await provider.generate(
                [{"role": "user", "content": "Reply with a short greeting."}],
                model=config.models.text,
                max_tokens=4096,
            )
            from llm_gateway._diagnostics import _Diagnostic
            from llm_gateway.error_classification import ClassifiedGatewayError
            from llm_gateway.errors import LLMProviderFailure

            if not response.content.strip():
                raise LLMProviderFailure(
                    ClassifiedGatewayError("invalid_request", False),
                    None,
                    diagnostic=_Diagnostic("verification", "empty_response"),
                )
            choices = (response.raw_response or {}).get("choices", [])
            if choices and choices[0].get("finish_reason") not in (None, "stop"):
                raise LLMProviderFailure(
                    ClassifiedGatewayError("invalid_request", False),
                    None,
                    diagnostic=_Diagnostic("verification", "stream_validation"),
                )
    finally:
        if isinstance(provider, OpenAIProvider):
            await provider._client.close()
