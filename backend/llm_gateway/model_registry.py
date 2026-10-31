from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from common.config.loader import settings
from common.dto.base import FrozenDTO
from llm_gateway.config import resolve_generation_provider
from llm_gateway.errors import LLMModelRoutingError

if TYPE_CHECKING:
    from llm_gateway.provider_runtime import ProviderRuntime

_OPENAI_REASONING_LEVELS = ("low", "medium", "high")
_OPENAI_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _supported_thinking_levels(
    provider: Literal["openai", "deepseek"], model_id: str
) -> tuple[str, ...]:
    """Return explicit provider reasoning controls for known model families."""

    normalized = model_id.strip().lower()
    if provider == "openai" and normalized.startswith(_OPENAI_REASONING_MODEL_PREFIXES):
        return _OPENAI_REASONING_LEVELS
    return ()


class ModelInfo(FrozenDTO):
    model_id: str
    logical_name: str
    provider: str
    capabilities: list[str]
    max_context_tokens: int


@dataclass(frozen=True, slots=True)
class ModelRouteInfo:
    logical_name: str
    provider: Literal["openai", "deepseek"]
    model_id: str
    api: Literal["chat_completions", "responses"]
    supports_native_tools: bool
    supports_provider_strict_schema: bool
    supports_local_structured_action: bool
    context_window: int
    max_output_tokens: int
    default_temperature: float | None
    timeout_seconds: float
    max_provider_retries: int
    supported_thinking_levels: tuple[str, ...]


class ModelRegistryImpl:
    def __init__(
        self,
        settings_obj: Any = None,
        *,
        generation_provider: str | None = None,
        provider_runtime: ProviderRuntime | None = None,
    ) -> None:
        """Routes follow ``provider_runtime`` unless a provider is fixed."""
        self._settings = settings_obj or settings
        self._runtime = provider_runtime
        if generation_provider is None and provider_runtime is None:
            generation_provider = resolve_generation_provider(self._settings)
        if generation_provider is not None and generation_provider not in {
            "openai",
            "deepseek",
        }:
            raise LLMModelRoutingError(f"Unsupported provider {generation_provider!r}")
        self._fixed_provider = generation_provider
        self._tables: dict[
            str, tuple[dict[str, ModelInfo], dict[str, ModelRouteInfo]]
        ] = {}

    @property
    def _generation_provider(self) -> Literal["openai", "deepseek"]:
        if self._fixed_provider is not None:
            return self._fixed_provider  # type: ignore[return-value]
        assert self._runtime is not None
        # Read on every call so a saved Provider change applies without restart.
        selected = self._runtime.current.config.provider
        return (
            "deepseek"
            if selected is not None and selected.id == "deepseek"
            else "openai"
        )

    @property
    def _models(self) -> dict[str, ModelInfo]:
        return self._table()[0]

    @property
    def _routes(self) -> dict[str, ModelRouteInfo]:
        return self._table()[1]

    def _table(self) -> tuple[dict[str, ModelInfo], dict[str, ModelRouteInfo]]:
        provider = self._generation_provider
        table = self._tables.get(provider)
        if table is None:
            table = ({}, {})
            self._register_defaults(provider, *table)
            self._tables[provider] = table
        return table

    def get_model(self, logical_name: str) -> ModelInfo:
        return self._models[logical_name]

    def get_route_configuration(self, logical_name: str) -> ModelRouteInfo:
        """Resolve capabilities by logical route, never a colliding model alias."""

        try:
            return self._routes[logical_name]
        except KeyError as exc:
            raise LLMModelRoutingError(
                f"No orchestrator model route configured for {logical_name!r}"
            ) from exc

    def supports_capability(self, model: str, capability: str) -> bool:
        try:
            model_info = self.get_model(model)
        except KeyError:
            return False
        return capability in model_info.capabilities

    def list_models(self, capability: str | None = None) -> list[ModelInfo]:
        unique = {model.logical_name: model for model in self._models.values()}
        models = sorted(unique.values(), key=lambda item: item.logical_name)
        if capability is None:
            return models
        return [model for model in models if capability in model.capabilities]

    def _register_defaults(
        self,
        generation_provider: Literal["openai", "deepseek"],
        models: dict[str, ModelInfo],
        routes: dict[str, ModelRouteInfo],
    ) -> None:
        uses_deepseek = generation_provider == "deepseek"
        generation_model = self._settings.deepseek_model_name if uses_deepseek else None
        generation_capabilities = (
            ["json_schema"] if uses_deepseek else ["json_schema", "tool_use", "vision"]
        )
        route_specs = (
            ("lead_ai_model", self._settings.lead_ai_model, 128000, 8192, 1),
            (
                "classifier_ai_model",
                self._settings.classifier_ai_model,
                128000,
                4096,
                1,
            ),
            (
                "supervisor_model",
                self._settings.supervisor_model or self._settings.lead_ai_model,
                128000,
                8192,
                1,
            ),
        )
        for (
            logical_name,
            fallback_model,
            context_window,
            output_tokens,
            retries,
        ) in route_specs:
            self._register(
                models,
                routes,
                logical_name=logical_name,
                model_id=generation_model or fallback_model,
                provider=generation_provider,
                capabilities=generation_capabilities,
                max_context_tokens=context_window,
                max_output_tokens=output_tokens,
                max_provider_retries=retries,
            )
        self._register(
            models,
            routes,
            logical_name="embedding_model",
            model_id=self._settings.embedding_model,
            provider="openai",
            capabilities=["embedding"],
            max_context_tokens=8192,
            route_enabled=False,
        )

    def _register(
        self,
        models: dict[str, ModelInfo],
        routes: dict[str, ModelRouteInfo],
        *,
        logical_name: str,
        model_id: str,
        provider: Literal["openai", "deepseek"],
        capabilities: list[str],
        max_context_tokens: int,
        route_enabled: bool = True,
        max_output_tokens: int = 8192,
        max_provider_retries: int = 1,
        supported_thinking_levels: tuple[str, ...] | None = None,
    ) -> None:
        # Orchestrator model calls pass this timeout explicitly, so it — not the
        # gateway's request/stream defaults — decides how long one model turn
        # may take before it is abandoned and retried.
        route_timeout = getattr(
            self._settings, "llm_gateway_route_timeout_seconds", 300.0
        )
        resolved_thinking_levels = (
            _supported_thinking_levels(provider, model_id)
            if supported_thinking_levels is None
            else supported_thinking_levels
        )
        model_info = ModelInfo(
            model_id=model_id,
            logical_name=logical_name,
            provider=provider,
            capabilities=capabilities,
            max_context_tokens=max_context_tokens,
        )
        models[logical_name] = model_info
        if model_id != logical_name and model_id not in models:
            models[model_id] = model_info
        if route_enabled:
            routes[logical_name] = ModelRouteInfo(
                logical_name=logical_name,
                provider=provider,
                model_id=model_id,
                api=(
                    "responses"
                    if provider == "openai" and resolved_thinking_levels
                    else "chat_completions"
                ),
                supports_native_tools=provider == "openai",
                supports_provider_strict_schema=provider == "openai",
                supports_local_structured_action=provider == "deepseek",
                context_window=max_context_tokens,
                max_output_tokens=max_output_tokens,
                default_temperature=None,
                timeout_seconds=route_timeout,
                max_provider_retries=max_provider_retries,
                supported_thinking_levels=resolved_thinking_levels,
            )


__all__ = ["ModelRegistryImpl", "ModelRouteInfo"]
