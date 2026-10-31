from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from llm_gateway.errors import UnsupportedConfiguredProvider

if TYPE_CHECKING:
    from common.config.runtime_config import RuntimeProvider
    from llm_gateway.provider_runtime import ProviderRuntime


@dataclass(frozen=True)
class LLMGatewayConfig:
    generation_provider: Literal["deepseek", "openai"] = "openai"
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.2
    request_timeout_seconds: float = 300.0
    stream_timeout_seconds: float = 300.0
    route_timeout_seconds: float = 300.0
    supervisor_json_timeout_seconds: float = 30.0
    supervisor_text_timeout_seconds: float = 90.0
    supervisor_stream_timeout_seconds: float = 90.0
    default_generation_model: str = "lead_ai_model"
    default_embedding_model: str = "embedding_model"
    default_supervisor_model: str = "supervisor_model"

    @classmethod
    def from_settings(cls, settings_obj: Any) -> LLMGatewayConfig:
        defaults = cls()
        return cls(
            generation_provider=resolve_generation_provider(settings_obj),
            max_attempts=_setting(
                settings_obj, "llm_gateway_max_attempts", defaults.max_attempts
            ),
            retry_backoff_seconds=_setting(
                settings_obj,
                "llm_gateway_retry_backoff_seconds",
                defaults.retry_backoff_seconds,
            ),
            request_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_request_timeout_seconds",
                defaults.request_timeout_seconds,
            ),
            stream_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_stream_timeout_seconds",
                defaults.stream_timeout_seconds,
            ),
            route_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_route_timeout_seconds",
                defaults.route_timeout_seconds,
            ),
            supervisor_json_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_supervisor_json_timeout_seconds",
                defaults.supervisor_json_timeout_seconds,
            ),
            supervisor_text_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_supervisor_text_timeout_seconds",
                defaults.supervisor_text_timeout_seconds,
            ),
            supervisor_stream_timeout_seconds=_setting(
                settings_obj,
                "llm_gateway_supervisor_stream_timeout_seconds",
                defaults.supervisor_stream_timeout_seconds,
            ),
            default_generation_model=_string_setting(
                settings_obj,
                "llm_gateway_default_generation_model",
                defaults.default_generation_model,
            ),
            default_embedding_model=_string_setting(
                settings_obj,
                "llm_gateway_default_embedding_model",
                defaults.default_embedding_model,
            ),
            default_supervisor_model=_string_setting(
                settings_obj,
                "llm_gateway_default_supervisor_model",
                defaults.default_supervisor_model,
            ),
        )


def load_provider_runtime() -> ProviderRuntime:
    """The holder for this process's config directory; an absent file is valid."""
    from common.config.runtime_store import current_store
    from llm_gateway.provider_runtime import ProviderRuntime

    return ProviderRuntime(current_store())


def load_saved_provider() -> RuntimeProvider | None:
    """The saved Provider selection, without resolving any credential."""
    from common.config.runtime_store import current_store

    return current_store().read_config().provider


def resolve_generation_provider(
    settings_obj: Any,
) -> Literal["deepseek", "openai"]:
    # A saved Provider wins before validating irrelevant legacy routing/keys.
    # This is a boot-time route hint only: the gateway and registry read the
    # live Provider through ProviderRuntime, and no Provider is required here.
    saved = load_saved_provider()
    if saved is not None:
        return "deepseek" if saved.id == "deepseek" else "openai"
    selected = (
        str(
            getattr(settings_obj, "llm_gateway_generation_provider", "openai")
            or "openai"
        )
        .strip()
        .lower()
    )
    if selected not in {"openai", "deepseek"}:
        raise UnsupportedConfiguredProvider(
            f"Configured LLM provider {selected!r} is not supported"
        )
    selected_model = (
        getattr(settings_obj, "deepseek_model_name", "")
        if selected == "deepseek"
        else getattr(settings_obj, "lead_ai_model", "")
    )
    if not str(selected_model or "").strip():
        raise UnsupportedConfiguredProvider(
            f"Configured {selected} model route is empty"
        )
    gemini_key = getattr(settings_obj, "google_api_key", "") or getattr(
        settings_obj, "gemini_api_key", ""
    )
    provider_keys = {
        "openai": str(getattr(settings_obj, "openai_api_key", "") or "").strip(),
        "deepseek": str(getattr(settings_obj, "deepseek_api_key", "") or "").strip(),
    }
    if str(gemini_key or "").strip() and not any(provider_keys.values()):
        raise UnsupportedConfiguredProvider(
            "Gemini is not supported by this LLM Gateway release"
        )
    if any(provider_keys.values()) and not provider_keys[selected]:
        required_key = "OPENAI_API_KEY" if selected == "openai" else "DEEPSEEK_API_KEY"
        raise UnsupportedConfiguredProvider(
            f"LLM_GATEWAY_GENERATION_PROVIDER selects {selected!r}, but "
            f"{required_key} is not configured"
        )
    return cast(Literal["deepseek", "openai"], selected)


def _setting(settings_obj: Any, name: str, default: Any) -> Any:
    value = getattr(settings_obj, name, default)
    return default if value is None else value


def _string_setting(settings_obj: Any, name: str, default: str) -> str:
    value = _setting(settings_obj, name, default)
    text = str(value).strip()
    return text or default
