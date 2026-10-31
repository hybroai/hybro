import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from common.dto import LLMResponse, LLMStructuredResponse
from llm_gateway.config import LLMGatewayConfig
from llm_gateway.errors import LLMModelRoutingError, LLMStreamingUnsupportedError
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.model_registry import ModelInfo


@pytest.fixture(autouse=True)
def isolate_registry_provider_unit_layer(monkeypatch):
    # These tests exercise injected registry/provider behavior, not application
    # startup. Required JSON setup is covered by the configuration boundary tests.
    monkeypatch.setattr("llm_gateway.gateway.load_provider_runtime", lambda: None)


class FakeRegistry:
    def __init__(self) -> None:
        self.models = {
            "lead_ai_model": ModelInfo(
                logical_name="lead_ai_model",
                model_id="gpt-test",
                provider="openai",
                capabilities=["json_schema"],
                max_context_tokens=128000,
            ),
            "embedding_model": ModelInfo(
                logical_name="embedding_model",
                model_id="embed-test",
                provider="openai",
                capabilities=["embedding"],
                max_context_tokens=8192,
            ),
            "supervisor_model": ModelInfo(
                logical_name="supervisor_model",
                model_id="supervisor-test",
                provider="openai",
                capabilities=["json_schema"],
                max_context_tokens=128000,
            ),
        }
        for model in list(self.models.values()):
            self.models[model.model_id] = model

    def get_model(self, logical_name: str) -> ModelInfo:
        return self.models[logical_name]


class FakeProvider:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.structured_calls: list[dict[str, Any]] = []

    async def generate(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> LLMResponse:
        self.generate_calls.append({"messages": messages, "model": model, **kwargs})
        return LLMResponse(content="ok", model=model)

    async def generate_structured(
        self,
        messages: list[dict[str, Any]],
        model: str,
        schema: dict[str, Any] | None = None,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> LLMStructuredResponse:
        self.structured_calls.append(
            {
                "messages": messages,
                "model": model,
                "schema": schema,
                "json_mode": json_mode,
                **kwargs,
            }
        )
        return LLMStructuredResponse(data={"ok": True}, model=model)

    async def embed(self, text: str, model: str) -> list[float]:
        return [float(len(text))]

    async def embed_batch(self, texts: list[str], model: str) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


class FlakyProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def generate(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> LLMResponse:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary")
        return await super().generate(messages, model, **kwargs)


class StreamingProvider(FakeProvider):
    async def generate_stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        for chunk in ["a", "b", "c"]:
            yield chunk


class FailBeforeStreamProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def generate_stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("before")
        yield "ok"


class FailAfterStreamProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def generate_stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.attempts += 1
        yield "first"
        raise RuntimeError("PRIVATE_LLM_EXCEPTION_SENTINEL")


def _gateway(provider: Any, *, max_attempts: int = 2) -> LLMGatewayImpl:
    return LLMGatewayImpl(
        model_registry=FakeRegistry(),
        providers={"openai": provider},
        config=LLMGatewayConfig(
            max_attempts=max_attempts,
            retry_backoff_seconds=0,
            request_timeout_seconds=0.2,
            stream_timeout_seconds=0.2,
        ),
    )


@pytest.mark.asyncio
async def test_generate_resolves_logical_model_to_provider_and_concrete_model():
    provider = FakeProvider()
    gateway = _gateway(provider)

    response = await gateway.generate([{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert provider.generate_calls[0]["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_registered_concrete_model_alias_routes_through_registry_metadata():
    provider = FakeProvider()
    gateway = _gateway(provider)

    await gateway.generate([{"role": "user", "content": "hi"}], model="gpt-test")

    assert provider.generate_calls[0]["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_unregistered_concrete_model_uses_public_provider_override():
    provider = FakeProvider()
    gateway = _gateway(provider)

    with pytest.raises(LLMModelRoutingError):
        await gateway.generate([{"role": "user", "content": "hi"}], model="custom")

    hinted = await gateway.generate_with_provider(
        [{"role": "user", "content": "hi"}],
        model="custom",
        provider="openai",
    )
    assert hinted.model == "custom"
    assert provider.generate_calls[0]["model"] == "custom"


@pytest.mark.asyncio
async def test_unregistered_model_logs_terminal_routing_failure(caplog):
    gateway = _gateway(FakeProvider())
    caplog.set_level(logging.ERROR, logger="llm_gateway.gateway")

    with pytest.raises(LLMModelRoutingError):
        await gateway.generate(
            [{"role": "user", "content": "PRIVATE_PROMPT_SENTINEL"}],
            model="missing-model",
        )

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].operation == "generate"
    assert records[0].model == "missing-model"
    assert records[0].outcome == "error"
    assert records[0].error_type == "LLMModelRoutingError"
    assert "PRIVATE_PROMPT_SENTINEL" not in records[0].__dict__.values()


@pytest.mark.asyncio
async def test_invalid_timeout_logs_terminal_validation_failure(caplog):
    gateway = _gateway(FakeProvider())
    caplog.set_level(logging.ERROR, logger="llm_gateway.gateway")

    with pytest.raises(ValueError):
        await gateway.generate(
            [{"role": "user", "content": "PRIVATE_PROMPT_SENTINEL"}],
            timeout_seconds="not-a-number",
        )

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].operation == "generate"
    assert records[0].outcome == "error"
    assert records[0].error_type == "ValueError"


@pytest.mark.asyncio
async def test_unregistered_provider_override_is_rejected():
    openai_provider = FakeProvider()
    unregistered_provider = FakeProvider()
    gateway = LLMGatewayImpl(
        model_registry=FakeRegistry(),
        providers={"openai": openai_provider, "unregistered": unregistered_provider},
        config=LLMGatewayConfig(max_attempts=1, request_timeout_seconds=0.2),
    )

    with pytest.raises(LLMModelRoutingError):
        await gateway.generate_structured(
            [{"role": "user", "content": "hi"}],
            model="custom-unregistered-model",
            json_mode=True,
        )

    with pytest.raises(LLMModelRoutingError, match="Unsupported provider hint"):
        await gateway.generate_structured_with_provider(
            [{"role": "user", "content": "hi"}],
            model="custom-unregistered-model",
            provider="unregistered",
            json_mode=True,
        )

    assert unregistered_provider.structured_calls == []
    assert openai_provider.structured_calls == []


@pytest.mark.asyncio
async def test_generate_retries_transient_failure_once():
    provider = FlakyProvider()
    gateway = _gateway(provider, max_attempts=2)

    response = await gateway.generate([{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert provider.attempts == 2


@pytest.mark.asyncio
async def test_generate_structured_supports_schema_less_json_object_mode():
    provider = FakeProvider()
    gateway = _gateway(provider)

    response = await gateway.generate_structured(
        [{"role": "user", "content": "json"}],
        schema=None,
        json_mode=True,
        model="supervisor_model",
    )

    assert response.data == {"ok": True}
    assert provider.structured_calls[0]["json_mode"] is True
    assert provider.structured_calls[0]["schema"] is None


@pytest.mark.asyncio
async def test_generate_structured_rejects_missing_schema_without_json_mode():
    gateway = _gateway(FakeProvider())

    with pytest.raises(LLMModelRoutingError):
        await gateway.generate_structured(
            [{"role": "user", "content": "json"}],
            schema=None,
            json_mode=False,
        )


@pytest.mark.asyncio
async def test_invalid_structured_request_logs_terminal_validation_failure(caplog):
    gateway = _gateway(FakeProvider())
    caplog.set_level(logging.ERROR, logger="llm_gateway.gateway")

    with pytest.raises(LLMModelRoutingError):
        await gateway.generate_structured(
            [{"role": "user", "content": "PRIVATE_PROMPT_SENTINEL"}],
            schema=None,
            json_mode=False,
        )

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].operation == "generate_structured"
    assert records[0].outcome == "error"
    assert records[0].error_type == "LLMModelRoutingError"


@pytest.mark.asyncio
async def test_cancellation_during_retry_backoff_logs_terminal_completion(caplog):
    class AlwaysFailProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.failed = asyncio.Event()

        async def generate(
            self,
            messages: list[dict[str, Any]],
            model: str,
            **kwargs: Any,
        ) -> LLMResponse:
            self.failed.set()
            raise RuntimeError("PRIVATE_RETRY_SENTINEL")

    provider = AlwaysFailProvider()
    gateway = LLMGatewayImpl(
        model_registry=FakeRegistry(),
        providers={"openai": provider},
        config=LLMGatewayConfig(
            max_attempts=2,
            retry_backoff_seconds=60,
            request_timeout_seconds=0.2,
        ),
    )
    caplog.set_level(logging.INFO, logger="llm_gateway.gateway")

    task = asyncio.create_task(
        gateway.generate([{"role": "user", "content": "PRIVATE_PROMPT_SENTINEL"}])
    )
    await provider.failed.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].outcome == "cancelled"
    assert records[0].error_type == "CancelledError"
    assert records[0].attempt == 1


@pytest.mark.asyncio
async def test_generate_stream_rejects_non_streaming_provider(caplog):
    gateway = _gateway(FakeProvider())
    caplog.set_level(logging.ERROR, logger="llm_gateway.gateway")

    with pytest.raises(LLMStreamingUnsupportedError):
        async for _ in gateway.generate_stream([{"role": "user", "content": "hi"}]):
            pass

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    )
    assert record.outcome == "error"
    assert record.error_type == "LLMStreamingUnsupportedError"


@pytest.mark.asyncio
async def test_generate_stream_yields_chunks_in_order():
    gateway = _gateway(StreamingProvider())

    chunks = [
        chunk
        async for chunk in gateway.generate_stream([{"role": "user", "content": "hi"}])
    ]

    assert chunks == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_stream_early_close_logs_cancelled_completion(caplog):
    gateway = _gateway(StreamingProvider())
    caplog.set_level(logging.INFO, logger="llm_gateway.gateway")
    stream = gateway.generate_stream([{"role": "user", "content": "hi"}])

    assert await anext(stream) == "a"
    await stream.aclose()

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].outcome == "cancelled"
    assert records[0].error_type == "GeneratorExit"


@pytest.mark.asyncio
async def test_stream_task_cancellation_logs_cancelled_completion(caplog):
    class BlockingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def generate_stream(
            self,
            messages: list[dict[str, Any]],
            model: str,
            **kwargs: Any,
        ) -> AsyncIterator[str]:
            self.started.set()
            await asyncio.Event().wait()
            yield "unreachable"

    provider = BlockingProvider()
    gateway = _gateway(provider)
    caplog.set_level(logging.INFO, logger="llm_gateway.gateway")

    async def consume() -> None:
        async for _ in gateway.generate_stream([{"role": "user", "content": "hi"}]):
            pass

    task = asyncio.create_task(consume())
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    ]
    assert len(records) == 1
    assert records[0].outcome == "cancelled"
    assert records[0].error_type == "CancelledError"


@pytest.mark.asyncio
async def test_stream_failure_before_first_chunk_is_retried():
    provider = FailBeforeStreamProvider()
    gateway = _gateway(provider)

    chunks = [
        chunk
        async for chunk in gateway.generate_stream([{"role": "user", "content": "hi"}])
    ]

    assert chunks == ["ok"]
    assert provider.attempts == 2


@pytest.mark.asyncio
async def test_stream_failure_after_first_chunk_is_not_retried(caplog):
    provider = FailAfterStreamProvider()
    gateway = _gateway(provider)
    chunks = []
    caplog.set_level(logging.ERROR, logger="llm_gateway.gateway")

    with pytest.raises(RuntimeError, match="PRIVATE_LLM_EXCEPTION_SENTINEL"):
        async for chunk in gateway.generate_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "llm_call_completed"
    )
    assert record.error_type == "RuntimeError"
    assert len(record.error_fingerprint) == 16
    assert "llm_gateway/gateway.py:" in record.error_stack
    assert "PRIVATE_LLM_EXCEPTION_SENTINEL" not in record.error_stack
    assert chunks == ["first"]
    assert provider.attempts == 1


@pytest.mark.asyncio
async def test_stream_timeout_wraps_async_generator_iteration():
    class SlowStreamProvider(FakeProvider):
        async def generate_stream(
            self, messages: list[dict[str, Any]], model: str, **kwargs: Any
        ) -> AsyncIterator[str]:
            await asyncio.sleep(0.5)
            yield "late"

    gateway = _gateway(SlowStreamProvider(), max_attempts=1)

    with pytest.raises(TimeoutError):
        async for _ in gateway.generate_stream(
            [{"role": "user", "content": "hi"}],
            timeout_seconds=0.01,
        ):
            pass
