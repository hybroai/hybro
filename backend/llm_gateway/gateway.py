import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from common.config.loader import settings
from common.config.runtime_config import RuntimeConfigurationError
from common.dto import LLMResponse, LLMStructuredResponse
from common.observability import get_logger, safe_exception_metadata
from llm_gateway.config import LLMGatewayConfig, load_provider_runtime
from llm_gateway.errors import (
    LLMModelRoutingError,
    LLMProviderConfigurationError,
    LLMStreamingUnsupportedError,
    ProviderNotConfigured,
)
from llm_gateway.image_types import GatewayImageRequest, GatewayImageResult
from llm_gateway.model_registry import ModelInfo, ModelRegistryImpl
from llm_gateway.provider_runtime import ProviderPair, ProviderRuntime, TextSelection
from llm_gateway.providers import DeepSeekProvider, OpenAIProvider
from llm_gateway.turn_types import GatewayTurnEvent, GatewayTurnRequest


@runtime_checkable
class LLMProviderAdapter(Protocol):
    async def generate(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> LLMResponse: ...

    async def generate_structured(
        self,
        messages: list[dict[str, Any]],
        model: str,
        schema: dict[str, Any] | None = None,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> LLMStructuredResponse: ...

    async def embed(self, text: str, model: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str], model: str) -> list[list[float]]: ...


ProviderHint = Literal["openai", "deepseek"]
T = TypeVar("T")
logger = get_logger(__name__)


class LLMTurnGateway(Protocol):
    def stream_turn_once(
        self,
        request: GatewayTurnRequest,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[GatewayTurnEvent]: ...


@dataclass(frozen=True, slots=True)
class _PairAdapters:
    """Clients built for one Provider pair; replaced when the pair changes."""

    text: LLMProviderAdapter
    # An OpenAI API-key setup also serves embeddings with its stored key.
    embedding: LLMProviderAdapter | None


class LLMGatewayImpl:
    def __init__(
        self,
        model_registry: ModelRegistryImpl | None = None,
        providers: dict[str, LLMProviderAdapter] | None = None,
        config: LLMGatewayConfig | None = None,
        settings_obj: Any = None,
        provider_runtime: ProviderRuntime | None = None,
    ) -> None:
        settings_obj = settings_obj or settings
        self.config = config or LLMGatewayConfig.from_settings(settings_obj)
        # The holder owns the Provider pair and is read on every call, so a
        # saved change applies without a restart. It is None only when a unit
        # test replaces load_provider_runtime, keeping the fixed legacy routes.
        self._runtime = provider_runtime or load_provider_runtime()
        self._model_registry = model_registry or (
            ModelRegistryImpl(settings_obj, provider_runtime=self._runtime)
            if self._runtime is not None
            else ModelRegistryImpl(
                settings_obj, generation_provider=self.config.generation_provider
            )
        )
        self._enforce_provider_credentials = providers is None
        self._image_base_url = getattr(settings_obj, "openai_base_url", None)
        self._provider_credentials = {
            "openai": str(getattr(settings_obj, "openai_api_key", "") or ""),
            "deepseek": str(getattr(settings_obj, "deepseek_api_key", "") or ""),
        }
        if providers is None:
            providers = {
                "openai": OpenAIProvider(
                    api_key=getattr(settings_obj, "openai_api_key", ""),
                    base_url=getattr(settings_obj, "openai_base_url", None) or None,
                ),
                "deepseek": DeepSeekProvider(
                    api_key=getattr(settings_obj, "deepseek_api_key", ""),
                ),
            }
        self._providers: dict[str, LLMProviderAdapter] = providers
        self._pair_adapters: tuple[ProviderPair, _PairAdapters] | None = None

    async def _selection(self) -> ProviderPair | None:
        """One working pair for the whole call, after the holder's file check."""
        if self._runtime is None:
            return None
        pair = await self._runtime.refresh()
        pair.require()
        return pair

    async def select_text(self) -> TextSelection:
        """The working pair and its text model; raises when none is usable."""
        from llm_gateway.catalog import validate_models

        pair = await self._selection()
        if pair is None:
            raise ProviderNotConfigured("provider_not_configured")
        return TextSelection(pair, validate_models(pair.config))

    def _adapters(self, pair: ProviderPair) -> _PairAdapters:
        cached = self._pair_adapters
        if cached is not None and cached[0] is pair:
            return cached[1]
        state = pair.require()
        selected = state.config.provider
        assert selected is not None and state.authentication is not None
        if not self._enforce_provider_credentials:
            text = self._providers.get(selected.id)
            if text is None:
                raise LLMModelRoutingError("Selected text provider is not configured")
            adapters = _PairAdapters(text=text, embedding=None)
        else:
            from llm_gateway.setup_bindings import create_provider

            credential = state.authentication.credential
            oauth_resolve = None
            if selected.auth == "oauth":
                from common.config.runtime_config import OAuthCredential
                from llm_gateway.openai_oauth import refresh

                if not isinstance(credential, OAuthCredential):
                    raise LLMProviderConfigurationError("OAuth credential missing")
                runtime = self._runtime
                assert runtime is not None
                expected, account = state.config, credential.account_id

                async def oauth_resolve() -> OAuthCredential:
                    try:
                        return await runtime.store.resolve_oauth(
                            expected, account, {}, refresh
                        )
                    except RuntimeConfigurationError:
                        # A call still holding a replaced pair is expected to be
                        # rejected; only the current pair's failure is a state.
                        if runtime.current is pair:
                            runtime.record_failure("oauth_refresh_failed")
                        raise

            base_url = self._image_base_url
            embedding = None
            api_key = getattr(credential, "api_key", None)
            if (
                selected.id == "openai"
                and selected.auth == "api_key"
                and api_key is not None
            ):
                # Embeddings stay on the original OpenAI route and are not part
                # of text/image setup; reuse the stored key instead of the empty
                # Settings.openai_api_key under JSON configuration.
                embedding = OpenAIProvider(
                    api_key=api_key.get_secret_value(), base_url=base_url or None
                )
            adapters = _PairAdapters(
                text=create_provider(state, base_url, oauth_resolve=oauth_resolve),
                embedding=embedding,
            )
        self._pair_adapters = (pair, adapters)
        return adapters

    async def stream_turn_once(
        self,
        request: GatewayTurnRequest,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[GatewayTurnEvent]:
        """Stream exactly one frozen provider attempt with no hidden retry."""

        stream = self._stream_turn(request, await self._selection(), cancel_event)
        async with aclosing(stream):
            async for event in stream:
                yield event

    async def stream_turn_selected(
        self,
        request: GatewayTurnRequest,
        selection: TextSelection,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[GatewayTurnEvent]:
        """``stream_turn_once`` on a pair the caller already selected."""
        stream = self._stream_turn(request, selection.pair, cancel_event)
        async with aclosing(stream):
            async for event in stream:
                yield event

    async def _stream_turn(
        self,
        request: GatewayTurnRequest,
        pair: ProviderPair | None,
        cancel_event: asyncio.Event | None,
    ) -> AsyncIterator[GatewayTurnEvent]:
        if pair is not None:
            from llm_gateway.catalog import validate_models

            selected = validate_models(pair.config)
            text_provider = self._adapters(pair).text
            if selected.provider == "anthropic":
                # GatewayTurnRequest only models the OpenAI/DeepSeek wire, so an
                # Anthropic request cannot be normalized into a copy. Its adapter
                # receives the frozen transcript plus the private model identity
                # and ignores the OpenAI-only shaping fields (provider/api/
                # thinking/tool_strategy); temperature and output limits are read
                # from the original request exactly like the other adapters.
                stream = text_provider.stream_turn_once(
                    request, model=selected.model_id, cancel_event=cancel_event
                )
            else:
                # Private wire route; never mutate the caller's frozen transcript.
                wire_request = request.model_copy(
                    update={
                        "provider": selected.provider,
                        "model_id": selected.model_id,
                        "api": selected.api,
                        "thinking_level": request.thinking_level
                        if request.thinking_level in selected.thinking_levels
                        else None,
                        "temperature": None
                        if selected.thinking_levels
                        else request.temperature,
                        "tool_strategy": "native"
                        if selected.provider == "openai"
                        else "structured_action",
                    }
                )
                stream = text_provider.stream_turn_once(
                    wire_request, cancel_event=cancel_event
                )
            async with aclosing(stream):
                async for event in stream:
                    yield event
            return
        if request.provider not in {"openai", "deepseek"}:
            raise LLMModelRoutingError(
                f"Unsupported turn provider {request.provider!r}"
            )
        if (
            self._enforce_provider_credentials
            and not self._provider_credentials[request.provider].strip()
        ):
            raise LLMProviderConfigurationError(
                f"{request.provider} API key is not configured"
            )
        provider = self._providers.get(request.provider)
        if provider is None:
            raise LLMModelRoutingError(f"No provider configured for {request.provider}")
        stream_method = getattr(provider, "stream_turn_once", None)
        if stream_method is None:
            raise LLMStreamingUnsupportedError(
                f"Provider {request.provider} does not support turn streaming"
            )
        async for event in stream_method(request, cancel_event=cancel_event):
            yield event

    async def generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        started_at = time.perf_counter()
        requested_model = model or self.config.default_generation_model
        try:
            timeout_seconds = _pop_timeout(
                kwargs,
                self.config.request_timeout_seconds,
            )
            pair = await self._selection()
            model_info, provider = self._resolve_provider(requested_model, pair=pair)
        except Exception as exc:
            _log_call_completed(
                operation="generate",
                model=requested_model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider.generate(
                messages,
                model=model_info.model_id,
                **self._text_options(kwargs, timeout_seconds, pair),
            ),
            timeout_seconds=timeout_seconds,
            operation_name="generate",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def generate_structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> LLMStructuredResponse:
        started_at = time.perf_counter()
        requested_model = model or self.config.default_generation_model
        try:
            if schema is None and not json_mode:
                raise LLMModelRoutingError(
                    "generate_structured requires schema or json_mode=True"
                )
            timeout_seconds = _pop_timeout(
                kwargs,
                self.config.request_timeout_seconds,
            )
            pair = await self._selection()
            model_info, provider = self._resolve_provider(requested_model, pair=pair)
        except Exception as exc:
            _log_call_completed(
                operation="generate_structured",
                model=requested_model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider.generate_structured(
                messages,
                model=model_info.model_id,
                schema=schema,
                json_mode=json_mode,
                **self._text_options(kwargs, timeout_seconds, pair),
            ),
            timeout_seconds=timeout_seconds,
            operation_name="generate_structured",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def generate_image(self, request: GatewayImageRequest) -> GatewayImageResult:
        """Generate or edit using the optional setup image model, without retries."""
        from llm_gateway.setup_bindings import create_image_provider

        pair = await self._selection()
        image_model = pair.config.models.image if pair is not None else None
        if pair is None or image_model is None:
            raise LLMProviderConfigurationError("Image model is not configured")
        provider = create_image_provider(pair.runtime, self._image_base_url)
        try:
            return await provider.generate_image_once(request, model=image_model)
        finally:
            await provider.aclose()

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        started_at = time.perf_counter()
        requested_model = model or self.config.default_embedding_model
        try:
            model_info, provider = await self._resolve_embedding_provider(
                requested_model
            )
            if "embedding" not in model_info.capabilities:
                raise ValueError(
                    f"Model {model_info.logical_name} does not support embeddings"
                )
        except Exception as exc:
            _log_call_completed(
                operation="embed",
                model=requested_model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider.embed(text, model=model_info.model_id),
            timeout_seconds=self.config.request_timeout_seconds,
            operation_name="embed",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def embed_batch(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        started_at = time.perf_counter()
        requested_model = model or self.config.default_embedding_model
        try:
            model_info, provider = await self._resolve_embedding_provider(
                requested_model
            )
            if "embedding" not in model_info.capabilities:
                raise ValueError(
                    f"Model {model_info.logical_name} does not support embeddings"
                )
        except Exception as exc:
            _log_call_completed(
                operation="embed_batch",
                model=requested_model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider.embed_batch(texts, model=model_info.model_id),
            timeout_seconds=self.config.request_timeout_seconds,
            operation_name="embed_batch",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def _generate_with_provider_hint(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider_hint: ProviderHint,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self.generate_with_provider(
            messages,
            model=model,
            provider=provider_hint,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    async def generate_with_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider: ProviderHint,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        started_at = time.perf_counter()
        try:
            pair = await self._selection()
            model_info, provider_adapter = self._resolve_provider(
                model, provider_hint=provider, pair=pair
            )
        except Exception as exc:
            _log_call_completed(
                operation="generate",
                provider=provider,
                model=model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider_adapter.generate(
                messages,
                model=model_info.model_id,
                **self._text_options(
                    kwargs, timeout_seconds or self.config.request_timeout_seconds, pair
                ),
            ),
            timeout_seconds=timeout_seconds or self.config.request_timeout_seconds,
            operation_name="generate",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def _generate_structured_with_provider_hint(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider_hint: ProviderHint,
        schema: dict[str, Any] | None = None,
        json_mode: bool = False,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> LLMStructuredResponse:
        return await self.generate_structured_with_provider(
            messages,
            model=model,
            provider=provider_hint,
            schema=schema,
            json_mode=json_mode,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    async def generate_structured_with_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider: ProviderHint,
        schema: dict[str, Any] | None = None,
        json_mode: bool = False,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> LLMStructuredResponse:
        started_at = time.perf_counter()
        try:
            if schema is None and not json_mode:
                raise LLMModelRoutingError(
                    "generate_structured requires schema or json_mode=True"
                )
            pair = await self._selection()
            model_info, provider_adapter = self._resolve_provider(
                model, provider_hint=provider, pair=pair
            )
        except Exception as exc:
            _log_call_completed(
                operation="generate_structured",
                provider=provider,
                model=model,
                started_at=started_at,
                outcome="error",
                error=exc,
            )
            raise
        return await self._with_retry(
            lambda: provider_adapter.generate_structured(
                messages,
                model=model_info.model_id,
                schema=schema,
                json_mode=json_mode,
                **self._text_options(
                    kwargs, timeout_seconds or self.config.request_timeout_seconds, pair
                ),
            ),
            timeout_seconds=timeout_seconds or self.config.request_timeout_seconds,
            operation_name="generate_structured",
            provider=model_info.provider,
            model=model_info.model_id,
            started_at=started_at,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ):
        async with aclosing(
            self._generate_stream(
                messages,
                model=model or self.config.default_generation_model,
                timeout_seconds=timeout_seconds,
                provider_hint=None,
                **kwargs,
            )
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def _generate_stream_with_provider_hint(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider_hint: ProviderHint,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ):
        async with aclosing(
            self.generate_stream_with_provider(
                messages,
                model=model,
                provider=provider_hint,
                timeout_seconds=timeout_seconds,
                **kwargs,
            )
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def generate_stream_with_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        provider: ProviderHint,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ):
        async with aclosing(
            self._generate_stream(
                messages,
                model=model,
                timeout_seconds=timeout_seconds,
                provider_hint=provider,
                **kwargs,
            )
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def _generate_stream(  # noqa: C901
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        timeout_seconds: float | None,
        provider_hint: ProviderHint | None,
        **kwargs: Any,
    ):
        timeout = timeout_seconds or self.config.stream_timeout_seconds
        attempts = max(1, self.config.max_attempts)
        started_at = time.perf_counter()
        for attempt in range(1, attempts + 1):
            yielded = False
            model_info: ModelInfo | None = None
            try:
                pair = await self._selection()
                model_info, provider = self._resolve_provider(
                    model, provider_hint=provider_hint, pair=pair
                )
                stream_method = getattr(provider, "generate_stream", None)
                if stream_method is None:
                    raise LLMStreamingUnsupportedError(
                        f"Provider {model_info.provider} does not support streaming"
                    )
                async with asyncio.timeout(timeout):
                    async with aclosing(
                        stream_method(
                            messages,
                            model=model_info.model_id,
                            **self._text_options(kwargs, timeout, pair),
                        )
                    ) as stream:
                        async for chunk in stream:
                            yielded = True
                            yield chunk
            except asyncio.CancelledError as exc:
                _log_stream_completed(
                    model_info=model_info,
                    requested_model=model,
                    attempt=attempt,
                    started_at=started_at,
                    outcome="cancelled",
                    error=exc,
                )
                raise
            except GeneratorExit as exc:
                _log_stream_completed(
                    model_info=model_info,
                    requested_model=model,
                    attempt=attempt,
                    started_at=started_at,
                    outcome="cancelled",
                    error=exc,
                )
                raise
            except (
                LLMModelRoutingError,
                LLMStreamingUnsupportedError,
                ProviderNotConfigured,
            ) as exc:
                _log_stream_completed(
                    model_info=model_info,
                    requested_model=model,
                    attempt=attempt,
                    started_at=started_at,
                    outcome="error",
                    error=exc,
                )
                raise
            except Exception as exc:
                if yielded or attempt >= attempts:
                    _log_stream_completed(
                        model_info=model_info,
                        requested_model=model,
                        attempt=attempt,
                        started_at=started_at,
                        outcome="error",
                        error=exc,
                    )
                    raise
                try:
                    await asyncio.sleep(self.config.retry_backoff_seconds * attempt)
                except asyncio.CancelledError as cancel_exc:
                    _log_stream_completed(
                        model_info=model_info,
                        requested_model=model,
                        attempt=attempt,
                        started_at=started_at,
                        outcome="cancelled",
                        error=cancel_exc,
                    )
                    raise
            else:
                _log_stream_completed(
                    model_info=model_info,
                    requested_model=model,
                    attempt=attempt,
                    started_at=started_at,
                    outcome="success",
                )
                return

    def _text_options(
        self, kwargs: dict[str, Any], timeout: float, pair: ProviderPair | None
    ) -> dict[str, Any]:
        # Private Codex transport deadline, not a new public adapter/DTO contract.
        # Other providers retain their original options and timeout behavior.
        selected = pair.config.provider if pair is not None else None
        if selected is not None and selected.auth == "oauth":
            return {**kwargs, "timeout_seconds": timeout}
        return kwargs

    def _resolve_provider(
        self,
        model: str,
        provider_hint: ProviderHint | None = None,
        *,
        pair: ProviderPair | None = None,
    ) -> tuple[ModelInfo, LLMProviderAdapter]:
        # Setup wins over logical labels, aliases and explicit hints for text only.
        if pair is not None:
            selected = pair.config
            assert selected.provider is not None and selected.models is not None
            return ModelInfo(
                logical_name=model,
                model_id=selected.models.text,
                provider=selected.provider.id,
                capabilities=["json_schema", "tool_use"],
                max_context_tokens=0,
            ), self._adapters(pair).text
        return self._resolve_original_provider(model, provider_hint)

    async def _resolve_embedding_provider(
        self, model: str
    ) -> tuple[ModelInfo, LLMProviderAdapter]:
        pair = await self._selection()
        model_info, provider = self._resolve_original_provider(model)
        if pair is not None and model_info.provider == "openai":
            provider = self._adapters(pair).embedding or provider
        return model_info, provider

    def _resolve_original_provider(
        self,
        model: str,
        provider_hint: ProviderHint | None = None,
    ) -> tuple[ModelInfo, LLMProviderAdapter]:
        if provider_hint is not None and provider_hint not in {"openai", "deepseek"}:
            raise LLMModelRoutingError(f"Unsupported provider hint {provider_hint!r}")
        try:
            model_info = self._model_registry.get_model(model)
        except KeyError as exc:
            if provider_hint is None:
                raise LLMModelRoutingError(
                    f"Model {model!r} is not registered and no provider hint was supplied"
                ) from exc
            model_info = ModelInfo(
                model_id=model,
                logical_name=model,
                provider=provider_hint,
                capabilities=(
                    ["json_schema"]
                    if provider_hint == "deepseek"
                    else ["json_schema", "tool_use", "vision"]
                ),
                max_context_tokens=0,
            )
        if model_info.provider not in {"openai", "deepseek"}:
            raise LLMModelRoutingError(
                f"Unsupported model provider {model_info.provider!r}"
            )
        if provider_hint is not None and model_info.provider != provider_hint:
            raise LLMModelRoutingError(
                f"Model {model!r} routes to {model_info.provider}, not {provider_hint}"
            )
        provider = self._providers.get(model_info.provider)
        if provider is None:
            raise LLMModelRoutingError(
                f"No provider configured for {model_info.provider}"
            )
        return model_info, provider

    async def _with_retry(
        self,
        operation: Callable[[], Awaitable[T] | Coroutine[Any, Any, T]],
        *,
        timeout_seconds: float,
        operation_name: str,
        provider: str,
        model: str,
        started_at: float | None = None,
    ) -> T:
        attempts = max(1, self.config.max_attempts)
        started_at = time.perf_counter() if started_at is None else started_at
        attempt = 1
        try:
            for attempt in range(1, attempts + 1):
                try:
                    async with asyncio.timeout(timeout_seconds):
                        result = await operation()
                except LLMModelRoutingError as exc:
                    _log_call_completed(
                        operation=operation_name,
                        provider=provider,
                        model=model,
                        attempt=attempt,
                        started_at=started_at,
                        outcome="error",
                        error=exc,
                    )
                    raise
                except Exception as exc:
                    if attempt >= attempts:
                        _log_call_completed(
                            operation=operation_name,
                            provider=provider,
                            model=model,
                            attempt=attempt,
                            started_at=started_at,
                            outcome="error",
                            error=exc,
                        )
                        raise
                    await asyncio.sleep(self.config.retry_backoff_seconds * attempt)
                else:
                    _log_call_completed(
                        operation=operation_name,
                        provider=provider,
                        model=model,
                        attempt=attempt,
                        started_at=started_at,
                        outcome="success",
                    )
                    return result
        except asyncio.CancelledError as exc:
            _log_call_completed(
                operation=operation_name,
                provider=provider,
                model=model,
                attempt=attempt,
                started_at=started_at,
                outcome="cancelled",
                error=exc,
            )
            raise
        raise RuntimeError("unreachable retry state")


def _pop_timeout(kwargs: dict[str, Any], default: float) -> float:
    timeout = kwargs.pop("timeout_seconds", None)
    return default if timeout is None else float(timeout)


def _log_stream_completed(
    *,
    model_info: ModelInfo | None,
    requested_model: str,
    attempt: int,
    started_at: float,
    outcome: str,
    error: BaseException | None = None,
) -> None:
    fields: dict[str, Any] = {
        "provider": model_info.provider if model_info is not None else None,
        "model": model_info.model_id if model_info is not None else requested_model,
        "operation": "generate_stream",
        "attempt": attempt,
        "outcome": outcome,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
    }
    if error is not None:
        fields.update(safe_exception_metadata(error))
        fields["error_code"] = _error_code(error)
    log_method = logger.error if outcome == "error" else logger.info
    log_method("llm_call_completed", extra=fields)


def _log_call_completed(
    *,
    operation: str,
    model: str,
    started_at: float,
    outcome: str,
    provider: str | None = None,
    attempt: int = 1,
    error: BaseException | None = None,
) -> None:
    fields: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "operation": operation,
        "attempt": attempt,
        "outcome": outcome,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
    }
    if error is not None:
        fields.update(safe_exception_metadata(error))
        fields["error_code"] = _error_code(error)
    log_method = logger.error if outcome == "error" else logger.info
    log_method("llm_call_completed", extra=fields)


def _error_code(exc: BaseException) -> str | int | None:
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


__all__ = ["LLMGatewayImpl"]
