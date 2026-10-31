"""Unconfigured boot, live Provider reload, and the not-configured error contract.

Every Provider adapter is a local recorder; no test reaches a model Provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api_gateway.errors import provider_not_configured_handler
from common.config.loader import Settings
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore
from common.dto import LLMResponse
from execution.orchestrator.model_runtime import GatewayModelRuntime
from execution.orchestrator.models import (
    ModelMessage,
    ModelStreamEvent,
    ModelTextPart,
    ModelTurnRequest,
)
from execution.orchestrator.streaming import ModelStreamAssembler
from llm_gateway.errors import ProviderNotConfigured
from llm_gateway.gateway import LLMGatewayImpl
from llm_gateway.image_types import GatewayImageRequest
from llm_gateway.provider_runtime import ProviderRuntime
from llm_gateway.turn_types import GatewayTurnRequest
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    profile,
)
from tests.fakes.llm_runtime import oauth_config, oauth_credential

MODELS = {"openai": "gpt-4o-mini", "deepseek": "deepseek-v4-flash"}
CODES = ("provider_not_configured", "credential_invalid")


def save(store: RuntimeConfigStore, provider: str, key: str) -> None:
    store.save(
        RuntimeConfig(
            provider=RuntimeProvider(id=provider, auth="api_key"),
            models=RuntimeModels(text=MODELS[provider]),
        ),
        ApiKeyCredential(provider=provider, api_key=SecretStr(key)),
        {},
    )


class Recorder:
    """One pair's text adapter; its label names the credential it was built with."""

    def __init__(self, label: str, gate: asyncio.Event | None) -> None:
        self.label = label
        self.gate = gate
        self.entered = 0
        self.models: list[str] = []

    async def generate(self, messages, model, **kwargs) -> LLMResponse:
        self.entered += 1
        if self.gate is not None:
            await self.gate.wait()
        self.models.append(model)
        return LLMResponse(content=self.label, model=model)


class Adapters:
    def __init__(self) -> None:
        self.built: list[Recorder] = []
        self.gates: dict[str, asyncio.Event] = {}

    def create_provider(self, state, base_url=None, *, oauth_resolve=None):
        provider = state.config.provider.id
        key = state.authentication.credential.api_key.get_secret_value()
        adapter = Recorder(f"{provider}:{key}", self.gates.get(provider))
        self.built.append(adapter)
        return adapter


@pytest.fixture
def store(tmp_path: Path) -> RuntimeConfigStore:
    return RuntimeConfigStore(tmp_path / "runtime")


@pytest.fixture
def adapters(monkeypatch) -> Adapters:
    adapters = Adapters()
    monkeypatch.setattr(
        "llm_gateway.setup_bindings.create_provider", adapters.create_provider
    )
    return adapters


def gateway_for(runtime: ProviderRuntime) -> LLMGatewayImpl:
    return LLMGatewayImpl(settings_obj=Settings(), provider_runtime=runtime)


async def text(gateway: LLMGatewayImpl) -> LLMResponse:
    return await gateway.generate([{"role": "user", "content": "hi"}])


def turn() -> GatewayTurnRequest:
    return GatewayTurnRequest(
        provider="openai",
        model_id="frozen",
        api="chat_completions",
        system_prompt="",
        messages=[],
        tools=[],
        tool_strategy="native",
        max_output_tokens=16,
        timeout_seconds=5,
        turn_id="turn-1",
    )


async def test_boot_without_config_json_reports_every_model_call(store):
    runtime = ProviderRuntime(store)
    assert runtime.current.state == "unconfigured"
    gateway = gateway_for(runtime)

    async def drain(stream):
        return [item async for item in stream]

    calls = {
        "generate": text(gateway),
        "structured": gateway.generate_structured([], json_mode=True),
        "stream": drain(gateway.generate_stream([])),
        "turn": drain(gateway.stream_turn_once(turn())),
        "image": gateway.generate_image(GatewayImageRequest(prompt="cat")),
        "embed": gateway.embed("text"),
        "selected": gateway.select_text(),
    }
    for name, call in calls.items():
        with pytest.raises(ProviderNotConfigured) as caught:
            await call
        assert caught.value.code == "provider_not_configured", name
    assert not (store.home / "config.json").exists()


def test_application_boots_with_an_empty_config_directory(tmp_path):
    """The boot path of main and container.py, with no config.json at all."""
    home = tmp_path / "empty-home"
    home.mkdir(mode=0o700)
    script = (
        "import main\n"
        "from common.config.loader import get_settings\n"
        "from common.config.runtime_store import current_store\n"
        "from execution.adapters.profiles import (\n"
        "    OrchestratorProfileResolver, PromptAssetRegistry)\n"
        "from llm_gateway import LLMGatewayImpl, ModelRegistryImpl\n"
        "from llm_gateway.config import LLMGatewayConfig\n"
        "from llm_gateway.provider_runtime import ProviderRuntime\n"
        "settings = get_settings()\n"
        "runtime = ProviderRuntime(current_store())\n"
        "registry = ModelRegistryImpl(settings, provider_runtime=runtime)\n"
        "LLMGatewayImpl(model_registry=registry,\n"
        "    config=LLMGatewayConfig.from_settings(settings),\n"
        "    settings_obj=settings, provider_runtime=runtime)\n"
        "resolver = OrchestratorProfileResolver(model_registry=registry,\n"
        "    prompt_registry=PromptAssetRegistry(), settings_obj=settings)\n"
        "for profile_id in ('fast', 'ultimate', 'passthrough'):\n"
        "    resolver.resolve(profile_id)\n"
        "print(runtime.current.state)\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith("_API_KEY") and key != "HYBRO_CONTAINER"
    }
    env["HYBRO_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "unconfigured"
    assert not (home / "config.json").exists()


async def test_save_then_call_uses_the_new_provider_without_restart(store, adapters):
    runtime = ProviderRuntime(store)
    gateway = gateway_for(runtime)
    with pytest.raises(ProviderNotConfigured):
        await text(gateway)

    save(store, "openai", "key-a")
    first = await text(gateway)
    assert (first.content, first.model) == ("openai:key-a", "gpt-4o-mini")
    await text(gateway)
    assert len(adapters.built) == 1  # One client per pair, not per call.

    save(store, "deepseek", "key-b")
    second = await text(gateway)
    assert (second.content, second.model) == ("deepseek:key-b", "deepseek-v4-flash")
    route = gateway._model_registry.get_route_configuration("supervisor_model")
    assert route.provider == "deepseek"
    assert (await gateway.select_text()).model.model_id == "deepseek-v4-flash"


async def test_timer_picks_up_another_process_edit_without_a_model_call(store):
    runtime = ProviderRuntime(store, interval_seconds=0.05)
    assert runtime.current.state == "unconfigured"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from pydantic import SecretStr\n"
        "from common.config.runtime_config import (\n"
        "    ApiKeyCredential, RuntimeConfig, RuntimeModels, RuntimeProvider)\n"
        "from common.config.runtime_store import RuntimeConfigStore\n"
        "RuntimeConfigStore(Path(sys.argv[1])).save(RuntimeConfig(\n"
        "    provider=RuntimeProvider(id='deepseek', auth='api_key'),\n"
        "    models=RuntimeModels(text='deepseek-v4-flash')),\n"
        "    ApiKeyCredential(provider='deepseek', api_key=SecretStr('other')), {})\n"
    )
    await runtime.start()
    try:
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", script, str(store.home)],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=120,
        )
        deadline = time.monotonic() + 10
        while runtime.current.state != "configured":
            assert time.monotonic() < deadline, "timer did not reload the pair"
            await asyncio.sleep(0.05)
    finally:
        await runtime.stop()
    assert runtime.current.config.provider.id == "deepseek"


async def test_concurrent_calls_during_a_swap_each_see_one_pair(store, adapters):
    save(store, "openai", "key-a")
    gateway = gateway_for(ProviderRuntime(store))
    adapters.gates["openai"] = asyncio.Event()
    before = [asyncio.create_task(text(gateway)) for _ in range(5)]
    while not adapters.built or adapters.built[0].entered < 5:
        await asyncio.sleep(0.01)

    save(store, "deepseek", "key-b")
    after = [asyncio.create_task(text(gateway)) for _ in range(5)]
    done_after = await asyncio.gather(*after)
    adapters.gates["openai"].set()
    done_before = await asyncio.gather(*before)

    assert {(r.content, r.model) for r in done_before} == {
        ("openai:key-a", "gpt-4o-mini")
    }
    assert {(r.content, r.model) for r in done_after} == {
        ("deepseek:key-b", "deepseek-v4-flash")
    }
    openai, deepseek = adapters.built
    assert set(openai.models) == {"gpt-4o-mini"}
    assert set(deepseek.models) == {"deepseek-v4-flash"}


async def test_oauth_refresh_survives_restart_only_edits(store):
    store.save(oauth_config(), oauth_credential(expires_at=time.time() + 10), {})
    runtime = ProviderRuntime(store)
    expected = runtime.current.config
    store.update_config(["backend", "log_level"], "DEBUG")
    store.update_config(["image_size"], "auto")
    rotated = oauth_credential(expires_at=time.time() + 3600, refresh="rotated")

    async def refresh(credential):
        return rotated

    assert await store.resolve_oauth(expected, "fixture-account", {}, refresh) == (
        rotated
    )
    assert store.read_stored_credential() == rotated

    store.save(
        oauth_config().model_copy(update={"models": RuntimeModels(text="gpt-6-luna")}),
        oauth_credential(expires_at=time.time() + 10),
        {},
    )
    with pytest.raises(RuntimeConfigurationError, match="selection/account"):
        await store.resolve_oauth(expected, "fixture-account", {}, refresh)


async def test_failed_oauth_refresh_is_recorded_on_the_holder(store, monkeypatch):
    store.save(oauth_config(), oauth_credential(expires_at=time.time() + 10), {})

    async def failing(credential):
        raise RuntimeConfigurationError("refresh rejected")

    monkeypatch.setattr("llm_gateway.openai_oauth.refresh", failing)
    runtime = ProviderRuntime(store)
    gateway = gateway_for(runtime)
    adapter = gateway._adapters(runtime.current).text
    with pytest.raises(RuntimeConfigurationError):
        await adapter._resolve()
    assert runtime.last_error == "oauth_refresh_failed"


async def test_stale_oauth_call_does_not_mark_the_new_pair_failed(store):
    store.save(oauth_config(), oauth_credential(expires_at=time.time() + 10), {})
    runtime = ProviderRuntime(store)
    gateway = gateway_for(runtime)
    stale = gateway._adapters(runtime.current).text
    store.save(
        oauth_config().model_copy(update={"models": RuntimeModels(text="gpt-6-luna")}),
        oauth_credential(expires_at=time.time() + 10),
        {},
    )
    await runtime.refresh()
    with pytest.raises(RuntimeConfigurationError, match="selection/account"):
        await stale._resolve()
    assert runtime.last_error is None


async def test_missing_auth_boots_credential_invalid_and_a_save_repairs_it(
    store, adapters
):
    save(store, "openai", "key-a")
    (store.home / "auth.json").unlink()
    runtime = ProviderRuntime(store)
    assert runtime.current.state == "credential_invalid"
    assert "missing" in (runtime.current.runtime.credential_error or "")
    gateway = gateway_for(runtime)
    with pytest.raises(ProviderNotConfigured) as caught:
        await text(gateway)
    assert caught.value.code == "credential_invalid"
    assert adapters.built == []

    save(store, "openai", "key-new")
    assert (await text(gateway)).content == "openai:key-new"
    assert runtime.current.state == "configured"


async def test_invalid_edit_keeps_the_last_pair_until_it_is_fixed(store, adapters):
    save(store, "openai", "key-a")
    runtime = ProviderRuntime(store)
    gateway = gateway_for(runtime)
    valid = (store.home / "config.json").read_bytes()
    (store.home / "config.json").write_text("{not json")
    assert (await text(gateway)).content == "openai:key-a"
    assert runtime.last_error == "config_invalid"

    (store.home / "config.json").write_bytes(valid)
    assert (await text(gateway)).content == "openai:key-a"
    assert runtime.last_error is None
    save(store, "deepseek", "key-b")
    assert (await text(gateway)).content == "deepseek:key-b"


async def test_reverted_auth_mode_change_clears_the_error(store):
    save(store, "deepseek", "key-a")
    runtime = ProviderRuntime(store)
    auth = store.home / "auth.json"
    auth.chmod(0o644)
    await runtime.refresh()
    assert runtime.last_error == "config_invalid"
    assert runtime.current.state == "configured"
    # Same inode, size, mtime and mode as the last good files.
    auth.chmod(0o600)
    await runtime.refresh()
    assert runtime.last_error is None
    assert runtime.current.state == "configured"


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        json.dumps(
            {
                "version": 1,
                "provider": {"id": "openai", "auth": "api_key"},
                "models": {"text": "retired-model"},
            }
        ),
    ],
)
def test_invalid_config_still_fails_at_boot(store, document):
    store.home.mkdir(mode=0o700)
    (store.home / "config.json").write_text(document)
    with pytest.raises(RuntimeConfigurationError):
        ProviderRuntime(store)


@pytest.mark.parametrize("code", CODES)
def test_api_maps_provider_not_configured_to_409(code):
    app = FastAPI()
    app.add_exception_handler(ProviderNotConfigured, provider_not_configured_handler)

    @app.get("/call")
    async def call():
        raise ProviderNotConfigured(code)

    response = TestClient(app).get("/call")
    assert response.status_code == 409
    assert response.json()["code"] == code


def test_application_registers_the_409_mapping():
    from main import app

    assert app.exception_handlers[ProviderNotConfigured] is (
        provider_not_configured_handler
    )


@pytest.mark.parametrize("code", CODES)
async def test_model_runtime_reports_the_code_without_retrying(code):
    class Gateway:
        calls = 0

        async def stream_turn_once(self, request, *, cancel_event):
            self.calls += 1
            raise ProviderNotConfigured(code)
            yield  # pragma: no cover - makes this an async generator

    gateway = Gateway()
    request = ModelTurnRequest(
        turn_id="turn-1",
        model=profile().model,
        system_prompt="system",
        messages=[ModelMessage(role="user", content=[ModelTextPart(text="hi")])],
        tools=[],
        remaining_provider_retries=2,
        absolute_deadline_at=None,
    )
    events = [
        event
        async for event in GatewayModelRuntime(gateway).stream_turn(
            request, signal=NeverCancelled()
        )
    ]
    assert gateway.calls == 1
    assert [(e.kind, e.retryable, e.error_code) for e in events[1:]] == [
        ("attempt_failed", False, code),
        ("error", False, code),
    ]
    assembler = ModelStreamAssembler()
    for event in events:
        assembler.accept(event)
    outcome = assembler.build_outcome(message_id="m", created_at=NOW)
    assert (outcome.kind, outcome.error_code) == ("provider_error", code)


def _terminal_failure(code: str) -> list[ModelStreamEvent]:
    return [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="attempt_failed",
            attempt=1,
            error_class="unknown",
            retryable=False,
            error_code=code,
        ),
        ModelStreamEvent(
            kind="error",
            attempt=1,
            error_class="unknown",
            retryable=False,
            error_code=code,
        ),
    ]


@pytest.mark.parametrize("code", CODES)
async def test_run_fails_at_once_with_the_reason(code):
    kernel, store, model_runtime, _ = await make_kernel([_terminal_failure(code)])
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert (result.run.status, result.run.terminal_reason) == ("failed", code)
    assert len(model_runtime.requests) == 1
    assert result.run.budget.provider_retries_used == 0


async def test_other_provider_errors_keep_the_kernel_retry():
    kernel, store, model_runtime, _ = await make_kernel(
        [_terminal_failure("provider_failure"), final_events("done")]
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert result.outcome == "final_answer"
    assert len(model_runtime.requests) == 2


async def test_listeners_hear_each_swap_and_a_failing_one_does_not_block_it(store):
    runtime = ProviderRuntime(store)
    heard: list[str] = []

    def broken(pair) -> None:
        raise RuntimeError("listener bug")

    runtime.add_listener(broken)
    runtime.add_listener(lambda pair: heard.append(pair.state))

    await runtime.reload()
    assert heard == []  # Same files, no swap.
    save(store, "openai", "key-a")
    assert (await runtime.reload()).state == "configured"
    assert heard == ["configured"]
    await runtime.refresh()
    assert heard == ["configured"]


async def test_reload_waits_for_listener_work_and_refresh_does_not(store):
    runtime = ProviderRuntime(store)
    finished: list[str] = []
    gate = asyncio.Event()

    async def gating(state: str) -> None:
        await gate.wait()
        finished.append(state)

    runtime.add_listener(lambda pair: asyncio.ensure_future(gating(pair.state)))

    save(store, "openai", "key-a")
    reload = asyncio.ensure_future(runtime.reload())
    await asyncio.sleep(0)
    assert not reload.done()  # The save response waits for the gating.
    gate.set()
    await reload
    assert finished == ["configured"]

    gate.clear()
    save(store, "deepseek", "key-b")
    await runtime.refresh()  # A swap found by the timer does not wait.
    assert finished == ["configured"]
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert finished == ["configured", "configured"]
