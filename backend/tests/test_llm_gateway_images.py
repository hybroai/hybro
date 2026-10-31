"""Gateway-local image tests; all provider traffic uses MockTransport."""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from common.config.runtime_config import RuntimeProvider
from llm_gateway import setup_bindings
from llm_gateway.catalog import model_choices, validate_models
from llm_gateway.errors import LLMProviderConfigurationError
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.image_types import (
    GatewayImage,
    GatewayImageRequest,
    ImageContractError,
)
from llm_gateway.providers.openai_images import IMAGE_MODELS, OpenAIImageProvider
from tests.fakes.llm_runtime import provider_runtime, runtime_state


def png():
    with io.BytesIO() as output:
        with Image.new("RGB", (1, 1)) as image:
            image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode()


class Body(httpx.AsyncByteStream):
    def __init__(self, data=b"", wait=False):
        self.data = data
        self.wait = wait
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        yield self.data

    async def aclose(self):
        self.closed = True


def image_state(model="gpt-image-2.5-flare"):
    state = runtime_state()
    config = state.config.model_copy(
        update={"models": state.config.models.model_copy(update={"image": model})}
    )
    return type(state)(config, state.authentication)


def test_image_selection_is_optional_and_provider_specific():
    state = image_state()
    assert validate_models(state.config).model_id == "gpt-4o-mini"
    assert model_choices(state.config.provider).image == (
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
    )
    assert runtime_state().config.models.image is None
    for provider in ("deepseek", "anthropic"):
        assert not model_choices(RuntimeProvider(id=provider, auth="api_key")).image


@pytest.mark.parametrize("model", IMAGE_MODELS)
@pytest.mark.parametrize("editing", [False, True])
async def test_gateway_selected_image_generation_and_edit(monkeypatch, editing, model):
    encoded = png()
    body = Body(json.dumps({"created": 1, "data": [{"b64_json": encoded}]}).encode())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=body
        )

    created = []
    original = OpenAIImageProvider

    def factory(**kwargs):
        provider = original(**kwargs, transport=httpx.MockTransport(handler))
        created.append(provider)
        return provider

    monkeypatch.setattr(
        "llm_gateway.providers.openai_images.OpenAIImageProvider", factory
    )
    gateway = LLMGatewayImpl.__new__(LLMGatewayImpl)
    gateway._runtime = provider_runtime(image_state(model))
    gateway._image_base_url = "https://provider.test/custom/v1"
    reference = GatewayImage(mime_type="image/png", data_base64=encoded)
    request = GatewayImageRequest(
        prompt="A cat",
        reference_images=(reference,) if editing else (),
        client_request_id=" original ",
    )
    result = await gateway.generate_image(request)
    assert result.image.data_base64 == encoded
    assert result.client_request_id == " original "
    assert len(calls) == 1
    assert calls[0].url.path == "/custom/v1/images/" + (
        "edits" if editing else "generations"
    )
    assert calls[0].headers["authorization"] == "Bearer test-only-key"
    if editing:
        assert b'name="model"\r\n\r\n' + model.encode() + b"\r\n" in calls[0].content
    else:
        assert json.loads(calls[0].content)["model"] == model
    assert body.closed and created[0]._client.is_closed()


@pytest.mark.parametrize("state", [None, runtime_state()])
async def test_missing_image_rejects_before_provider_creation(monkeypatch, state):
    def forbidden(*args, **kwargs):
        pytest.fail("Provider must not be created")

    monkeypatch.setattr(setup_bindings, "create_image_provider", forbidden)
    gateway = LLMGatewayImpl.__new__(LLMGatewayImpl)
    gateway._runtime = provider_runtime(state) if state is not None else None
    with pytest.raises(LLMProviderConfigurationError):
        await gateway.generate_image(GatewayImageRequest(prompt="A cat"))


async def test_image_cancel_closes_http_and_gateway_client(monkeypatch):
    body = Body(wait=True)
    provider = OpenAIImageProvider(
        api_key="test-only-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=body
            )
        ),
    )
    monkeypatch.setattr(setup_bindings, "create_image_provider", lambda *args: provider)
    gateway = LLMGatewayImpl.__new__(LLMGatewayImpl)
    gateway._runtime = provider_runtime(image_state())
    gateway._image_base_url = None
    task = asyncio.create_task(
        gateway.generate_image(GatewayImageRequest(prompt="A cat"))
    )
    await asyncio.wait_for(body.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and provider._client.is_closed()


@pytest.mark.parametrize("status", [200, 502])
async def test_image_rejects_compressed_response_without_retry(status):
    calls = []
    body = Body(b"not an image")

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"content-encoding": "gzip"}, stream=body)

    provider = OpenAIImageProvider(
        api_key="test-only-key", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ImageContractError):
            await provider.generate_image_once(
                GatewayImageRequest(prompt="A cat"), model="gpt-image-2.5-flare"
            )
    finally:
        await provider.aclose()
    assert body.closed and len(calls) == 1
