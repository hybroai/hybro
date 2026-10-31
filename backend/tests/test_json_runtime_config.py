"""JSON configuration boundary tests; no application lifespan or Provider calls."""

import base64
import json
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from common.config.loader import Settings, get_settings
from common.config.runtime_config import (
    ApiKeyCredential,
    OAuthCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore, parse_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def selection():
    return RuntimeConfig(
        provider=RuntimeProvider(id="openai", auth="api_key"),
        models=RuntimeModels(text="gpt-4o-mini"),
    )


def configured(tmp_path):
    home = tmp_path / "runtime"
    store = RuntimeConfigStore(home)
    store.save(
        selection(),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )
    return store


@pytest.mark.parametrize(
    "payload",
    [
        b"provider: openai",
        b'{"version":1,"version":1}',
        b'{"x":NaN}',
        b'{"backend":{"terminal_processing_statuses":[1e999]}}',
        b"[]",
    ],
)
def test_rejects_non_json_duplicate_and_nonfinite(payload):
    with pytest.raises(RuntimeConfigurationError):
        parse_config(payload)


@pytest.mark.parametrize(
    "section,value",
    [
        ("backend", {"unknown-secret-marker": "private-value"}),
        ("backend", {"local_agent_discovery_port_start": 90000}),
        ("backend", {"openai_api_key": "private-value"}),
        ("frontend", {"max_message_length": -1}),
        ("frontend", {"api_base_url": "https://user:private-value@example.org"}),
        ("frontend", {"server_url": ""}),
    ],
)
def test_rejects_invalid_config_without_echoing_values(section, value):
    data = selection().model_dump()
    data[section] = value
    with pytest.raises(RuntimeConfigurationError) as caught:
        parse_config(json.dumps(data).encode())
    assert "private-value" not in str(caught.value)
    assert "unknown-secret-marker" not in str(caught.value)


def test_empty_api_base_url_means_same_origin():
    data = selection().model_dump()
    data["frontend"] = {"api_base_url": ""}
    assert parse_config(json.dumps(data).encode()).frontend == {"api_base_url": ""}


def test_environment_key_is_saved_and_no_longer_a_runtime_source(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.save(selection(), None, {"OPENAI_API_KEY": "fixture-key"})
    assert (store.home / "config.json").is_file()
    assert not (store.home / "config.yaml").exists()
    state = store.load({"OPENAI_API_KEY": "wrong-environment-key"})
    assert state.authentication.source == "stored"
    assert state.authentication.credential.api_key.get_secret_value() == "fixture-key"


def test_setup_preserves_application_and_service_settings(tmp_path):
    store = configured(tmp_path)
    store.update_config(["backend", "log_level"], "DEBUG")
    store.update_config(["frontend", "max_message_length"], 4321)
    services = store.ensure_service_credentials()
    before = store.read_stored_credential()
    changed = selection().model_copy(
        update={"models": RuntimeModels(text="gpt-5-mini")}
    )
    store.save(changed, before, {}, expected_stored=before)
    assert store.read_config().backend == {"log_level": "DEBUG"}
    assert store.read_config().frontend == {"max_message_length": 4321}
    assert store.read_service_credentials() == services
    assert store.ensure_service_credentials() == services
    assert set(services) == {"default_agent_llm_token"}


def test_invalid_cli_edit_does_not_write(tmp_path):
    store = configured(tmp_path)
    before = (store.home / "config.json").read_bytes()
    with pytest.raises(RuntimeConfigurationError):
        store.update_config(["frontend", "max_message_length"], -1)
    assert (store.home / "config.json").read_bytes() == before


def test_json_loader_ignores_dotenv_and_environment(monkeypatch, tmp_path):
    store = configured(tmp_path)
    store.update_config(["backend", "log_level"], "DEBUG")
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    (tmp_path / ".env").write_text("LOG_LEVEL=CRITICAL\n")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().log_level == "DEBUG"
    finally:
        get_settings.cache_clear()


def test_settings_load_without_config_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)
    get_settings.cache_clear()
    try:
        # Outside a container a checkout registers the repository's manifest.
        assert get_settings() == Settings(
            bundled_agents_manifest=str(REPO_ROOT / "default_agents" / "agents.yaml")
        )
    finally:
        get_settings.cache_clear()


def test_image_container_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "missing"))
    monkeypatch.setenv("HYBRO_CONTAINER", "image")
    monkeypatch.setenv(
        "HYBRO_FRONTEND_ORIGINS", "http://localhost:3100,http://127.0.0.1:3100"
    )
    monkeypatch.setenv("LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS", "7004,7001")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    assert settings.mongodb_url == "mongodb://127.0.0.1:27017/hybro?replicaSet=rs0"
    assert settings.redis_url == ""
    assert settings.hybro_file_dir == "/data/files"
    assert settings.local_agent_discovery_enabled is True
    assert settings.local_agent_discovery_excluded_ports == frozenset({7004, 7001})
    assert settings.bundled_agents_manifest == "/app/bundled_agents.yaml"
    assert settings.oauth_callback_host == "0.0.0.0"
    assert settings.frontend_origins == [
        "http://localhost:3100",
        "http://127.0.0.1:3100",
    ]


def test_configured_mongodb_url_wins_in_the_image(monkeypatch, tmp_path):
    store = configured(tmp_path)
    store.update_config(["backend", "mongodb_url"], "mongodb://db.example:27017/x")
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    monkeypatch.setenv("HYBRO_CONTAINER", "image")
    get_settings.cache_clear()
    try:
        assert get_settings().mongodb_url == "mongodb://db.example:27017/x"
    finally:
        get_settings.cache_clear()


def test_secret_schema_cannot_be_extended_silently(tmp_path):
    store = configured(tmp_path)
    data = json.loads((store.home / "auth.json").read_text())
    data["services"] = {"unknown": "private-value"}
    (store.home / "auth.json").write_text(json.dumps(data))
    with pytest.raises(RuntimeConfigurationError):
        store.load({})


@pytest.mark.asyncio
async def test_oauth_rotation_preserves_service_credentials(tmp_path):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "fixture-account"
                    }
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    credential = OAuthCredential(
        access_token=SecretStr("fixture." + payload + ".signature"),
        refresh_token=SecretStr("fixture-refresh"),
        expires_at=time.time() - 1,
        account_id="fixture-account",
    )
    config = RuntimeConfig(
        provider=RuntimeProvider(id="openai", auth="oauth"),
        models=RuntimeModels(text="gpt-6.1-sol"),
    )
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.save(config, credential, {})
    services = store.ensure_service_credentials()

    async def refresh(previous):
        return previous.model_copy(
            update={
                "refresh_token": SecretStr("fixture-rotated"),
                "expires_at": time.time() + 3600,
            }
        )

    updated = await store.resolve_oauth(config, "fixture-account", {}, refresh)
    assert updated.refresh_token.get_secret_value() == "fixture-rotated"
    assert store.read_service_credentials() == services
    assert store.load({}).authentication.credential == updated
