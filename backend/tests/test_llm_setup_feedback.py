"""User-flow regressions, synthetic credentials and mocked wire only."""

import json
import traceback
from unittest.mock import AsyncMock

import httpx
import pytest

from common.config.runtime_store import RuntimeConfigStore
from llm_gateway import openai_oauth, setup_bindings
from llm_gateway.catalog import TEXT_MODELS, model_choices, validate_models
from llm_gateway.errors import LLMModelRoutingError, LLMProviderFailure
from llm_gateway.providers.openai_codex import OpenAICodexProvider
from llm_gateway.setup_service import SetupError, SetupService
from tests.fakes.llm_runtime import oauth_config, oauth_credential, runtime_state
from tests.test_openai_codex_provider import provider, text_events, turn, wire
from tests.test_openai_oauth import Body


async def test_oauth_load_resolve_refresh_ignore_api_key(tmp_path):
    store = RuntimeConfigStore(tmp_path)
    initial, updated = (
        oauth_credential(expires_at=1),
        oauth_credential(refresh="rotation"),
    )
    env = {"OPENAI_API_KEY": "embedding-independent"}
    store.save(oauth_config(), initial, env)
    assert store.load(env).authentication.credential == initial
    refresh = AsyncMock(return_value=updated)
    assert (
        await store.resolve_oauth(oauth_config(), initial.account_id, env, refresh)
        == updated
    )
    refresh.assert_awaited_once_with(initial)
    assert store.load(env).authentication.credential == updated
    assert env == {"OPENAI_API_KEY": "embedding-independent"}


OAUTH_MODELS = [m for m in TEXT_MODELS if m.auth == "oauth"]


def test_full_source_confirmed_catalog():
    # Retired for ChatGPT sign-in: gpt-5.5 and every earlier subscription model.
    assert [m.model_id for m in OAUTH_MODELS] == [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    for entry in OAUTH_MODELS:
        assert entry.max_output_tokens == 32768
        assert entry.context_window == 272000
        assert not model_choices(oauth_config().provider).image
    assert model_choices(oauth_config().provider).text[0] == "gpt-6.1-sol"


@pytest.mark.parametrize("entry", OAUTH_MODELS, ids=lambda m: m.model_id)
async def test_every_catalog_model_and_reasoning_level_is_wire_eligible(entry):
    config = oauth_config().model_copy(
        update={
            "models": oauth_config().models.model_copy(update={"text": entry.model_id})
        }
    )
    assert validate_models(config) == entry
    for level in entry.thinking_levels:
        calls = []
        result = [
            event
            async for event in provider(text_events(), calls).stream_turn_once(
                turn(model_id=entry.model_id, thinking_level=level)
            )
        ]
        body = json.loads(calls[0].content)
        expected = "low" if level == "minimal" else level
        assert body["reasoning"] == {"effort": expected, "summary": "auto"}
        assert body["model"] == entry.model_id and body["store"] is False
        assert "max_output_tokens" not in body
        assert result[-1].finish_reason == "stop" and len(calls) == 1
    with pytest.raises(LLMModelRoutingError):
        _ = [
            e
            async for e in provider([], []).stream_turn_once(
                turn(model_id=entry.model_id, thinking_level="ultra")
            )
        ]


@pytest.mark.parametrize(
    "kind", ["response.done", "response.completed", "response.incomplete"]
)
@pytest.mark.parametrize("status", ["absent", None, "failed", "cancelled", "unknown"])
async def test_terminal_status_compatibility_preserves_failure_semantics(kind, status):
    events = text_events()
    events[-1]["type"] = kind
    if status == "absent":
        del events[-1]["response"]["status"]
    else:
        events[-1]["response"]["status"] = status
    adapter = provider(events, [])
    if status in ("absent", None) and kind != "response.incomplete":
        assert (await adapter.generate([], "gpt-6-sol")).content == "ok"
    else:
        with pytest.raises(LLMProviderFailure):
            await adapter.generate([], "gpt-6-sol")


@pytest.mark.parametrize(
    "status,code,category",
    [
        (400, "invalid_request_error", "invalid_request"),
        (403, "model_not_found", "model_access"),
        (403, "private-credential", "permission"),
        (429, "insufficient_quota", "quota"),
        (429, "rate_limit_exceeded", "rate_limit"),
        (500, "server_error", "provider_5xx"),
        (503, ["private-credential"], "provider_5xx"),
        (200, "bad_json", "bad_json"),
        (200, "unfinished", "stream_validation"),
        (200, "empty", "empty_response"),
        (200, "truncated", "stream_validation"),
        (None, "timeout", "timeout"),
        (None, "network", "network"),
    ],
)
def test_authenticated_verification_failures_redacted_no_save(
    tmp_path, monkeypatch, status, code, category
):
    store = RuntimeConfigStore(tmp_path)
    old = runtime_state("deepseek", "deepseek-v4-flash")
    store.save(old.config, old.authentication.credential, {})
    before = {n: (tmp_path / n).read_bytes() for n in ("auth.json", "config.json")}
    secret = "private-credential"
    calls = []

    def handle(request):
        calls.append(request)
        assert (
            "JSON object must conform"
            not in json.loads(request.content)["instructions"]
        )
        if status is None:
            cls = httpx.ReadTimeout if code == "timeout" else httpx.ConnectError
            raise cls(secret, request=request)
        events = text_events("" if code == "empty" else "ordinary greeting")
        if code == "unfinished":
            events = events[:-1]
        if code == "truncated":
            events[-1]["response"].update(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            )
        data = (
            wire(events)
            if status == 200
            else json.dumps(
                {
                    "error": {
                        "code": code,
                        "message": secret,
                        "url": "https://secret.invalid",
                    }
                }
            ).encode()
        )
        if code == "bad_json":
            data = b"data: private-credential\n\n"
        return httpx.Response(
            status, headers={"content-type": "text/event-stream"}, stream=Body(data)
        )

    monkeypatch.setattr(
        openai_oauth, "login", AsyncMock(return_value=oauth_credential())
    )
    monkeypatch.setattr(
        setup_bindings,
        "OpenAICodexProvider",
        lambda *a, **kw: OpenAICodexProvider(
            *a, **kw, transport=httpx.MockTransport(handle)
        ),
    )
    service = SetupService(store, model_choices, setup_bindings.verify_selection)
    prepared = service.authenticate(
        oauth_config().provider,
        {"OPENAI_API_KEY": secret},
        non_interactive=False,
        read_secret=lambda: pytest.fail("secret prompt"),
        report_source=lambda _: None,
    )
    with pytest.raises(SetupError) as caught:
        service.save(
            oauth_config(), prepared, begin_commit=lambda: pytest.fail("commit")
        )
    diagnostic = str(caught.value)
    assert "category=" + category in diagnostic and "stage=" in diagnostic
    if (
        status is not None and code not in {"empty", "truncated"}
        if isinstance(code, str)
        else status is not None
    ):
        assert f"HTTP {status}" in diagnostic
    assert secret not in repr(vars(caught.value)) + diagnostic + "".join(
        traceback.format_exception(caught.value)
    )
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert len(calls) == 1
    assert before == {n: (tmp_path / n).read_bytes() for n in before}


@pytest.mark.parametrize(
    "text,category",
    [("private-credential", "bad_json"), ('{"ok":false}', "structured_validation")],
)
async def test_local_structured_validation_diagnostics_stay_private(text, category):
    with pytest.raises(LLMProviderFailure) as caught:
        await provider(text_events(text), []).generate_structured(
            [],
            "gpt-6-sol",
            schema={
                "type": "object",
                "properties": {"ok": {"const": True}},
                "required": ["ok"],
            },
        )
    assert caught.value._diagnostic.category == category
    assert "private-credential" not in repr(vars(caught.value))
    assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_incomplete_event_cannot_claim_completed_status():
    events = text_events()
    events[-1]["type"] = "response.incomplete"
    with pytest.raises(LLMProviderFailure):
        await provider(events, []).generate([], "gpt-6-sol")
