"""Small capability catalog shared by setup and the canonical runtime.

Limits are backend operating ceilings, never larger than the Provider limits.
Only implemented adapters are eligible. Subscription IDs come from the official
Codex model catalog; account entitlement is checked by setup's single text
verification, not assumed.
"""

from dataclasses import dataclass
from typing import Literal

from common.config.runtime_config import (
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeProvider,
)
from llm_gateway.providers.openai_images import IMAGE_MODELS
from llm_gateway.setup_service import ModelChoices


@dataclass(frozen=True, slots=True)
class TextModel:
    provider: Literal["openai", "deepseek", "anthropic"]
    model_id: str
    api: Literal["chat_completions", "responses", "messages"]
    context_window: int
    max_output_tokens: int
    thinking_levels: tuple[str, ...] = ()
    auth: Literal["api_key", "oauth"] = "api_key"
    operations: frozenset[str] = frozenset({"text_stream", "tools", "structured_json"})


# OpenAI model documentation: /docs/models/gpt-5-mini and /gpt-4o-mini.
# DeepSeek: api-docs.deepseek.com (V4 Flash, Chat Completions, JSON Output).
# Anthropic: platform.claude.com/docs/en/about-claude/models/overview.
# Haiku 4.5: 200K context / 64K output; gateway ceiling is 32K, thinking off.
TEXT_MODELS = (
    # ChatGPT/Codex subscription models, recommended first. IDs and the 272K
    # context window follow openai/codex codex-rs/models-manager/models.json and
    # learn.chatgpt.com/docs/models (2026-10); gpt-5.5 and earlier are retired for
    # ChatGPT sign-in. Catalog membership does not guarantee plan availability.
    *(
        TextModel(
            "openai",
            model,
            "responses",
            272_000,
            32_768,
            ("minimal", "low", "medium", "high", "xhigh"),
            auth="oauth",
        )
        for model in (
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        )
    ),
    TextModel(
        "openai", "gpt-5-mini", "responses", 400_000, 32_768, ("low", "medium", "high")
    ),
    TextModel("openai", "gpt-4o-mini", "chat_completions", 128_000, 16_384),
    TextModel("deepseek", "deepseek-v4-flash", "chat_completions", 128_000, 8192),
    TextModel("anthropic", "claude-haiku-4-5-20251001", "messages", 200_000, 32_768),
)


def model_choices(provider: RuntimeProvider) -> ModelChoices:
    return ModelChoices(
        text=tuple(
            entry.model_id
            for entry in TEXT_MODELS
            if entry.provider == provider.id and provider.auth == entry.auth
        ),
        image=IMAGE_MODELS
        if provider.id == "openai" and provider.auth == "api_key"
        else (),
    )


def validate_models(config: RuntimeConfig) -> TextModel:
    if config.provider is None or config.models is None:
        raise RuntimeConfigurationError(
            "No model provider is configured; open Settings, Model provider."
        )
    choices = model_choices(config.provider)
    if config.models.text not in choices.text or (
        config.models.image is not None and config.models.image not in choices.image
    ):
        raise RuntimeConfigurationError(
            "Selected Provider/auth or models are not eligible; "
            "open Settings, Model provider."
        )
    return next(
        entry
        for entry in TEXT_MODELS
        if entry.provider == config.provider.id
        and entry.model_id == config.models.text
        and entry.auth == config.provider.auth
    )
