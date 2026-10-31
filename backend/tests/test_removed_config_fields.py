"""Retired backend settings are accepted on read, ignored, and named once."""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import SecretStr, ValidationError

from common.config import loader
from common.config.loader import REMOVED_FIELDS, Settings, strip_removed_fields
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
)
from common.config.runtime_store import RuntimeConfigStore, parse_config

RETIRED = {
    "mongodb_host",
    "mongodb_port",
    "bundled_agents_use_service_hosts",
    "mongodb_username",
    "orchestration_outcome_guardrails",
    "supervisor_max_steps",
    "parse_confidence_threshold",
    "cloud_health_cache_ttl",
    "max_tasks_per_user",
    "max_tasks_per_room",
    "run_watchdog_stale_minutes",
    "processing_status_expiry_minutes",
    "a2a_inline_file_max_raw_bytes",
    "a2a_inline_message_max_encoded_bytes",
    "cancellation_ttl_seconds",
    "cancellation_cache_maxsize",
    "redis_cancel_channel",
    "redis_cancel_key_prefix",
    "cs_backoff_base",
    "cs_backoff_max",
    "cs_backoff_factor",
    "cs_jitter_fraction",
    "orphan_threshold_minutes",
    "stale_check_minutes",
    "task_expiry_hours",
    "pending_task_warning_hours",
    "webhook_base_url",
    "webhook_signing_key",
    "feature_run_event_sse",
    "context_model_window",
    "context_system_prompt_tokens",
    "context_tool_schema_tokens",
    "context_response_reserve_tokens",
    "context_room_pct",
    "context_history_pct",
    "context_task_pct",
    "memory_search_enabled",
    "memory_search_temporal_decay_enabled",
    "memory_search_half_life_days",
    "memory_search_max_results",
    "memory_search_max_candidates",
    "memory_search_max_snippet_chars",
}


def _config_bytes(backend: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "provider": {"id": "openai", "auth": "api_key"},
            "models": {"text": "gpt-test"},
            "backend": backend,
        }
    ).encode()


@pytest.fixture
def store(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.save(
        RuntimeConfig.model_validate(
            {
                "provider": {"id": "openai", "auth": "api_key"},
                "models": {"text": "gpt-test"},
            }
        ),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )
    return store


def _write_backend(store: RuntimeConfigStore, backend: dict[str, object]) -> None:
    path = store.home / "config.json"
    document = json.loads(path.read_text())
    document["backend"] = backend
    path.write_text(json.dumps(document))


def test_removed_fields_are_exactly_the_retired_settings():
    assert REMOVED_FIELDS == RETIRED
    assert not REMOVED_FIELDS & set(Settings.model_fields)


@pytest.mark.parametrize("field", sorted(RETIRED))
def test_settings_no_longer_accept_a_retired_field_directly(field):
    with pytest.raises(ValidationError):
        Settings.model_validate({field: 1})


def test_strip_removed_fields_prunes_without_mutating_input():
    values = {"mongodb_host": "db", "log_level": "INFO", "supervisor_max_steps": 3}

    pruned, removed = strip_removed_fields(values)

    assert pruned == {"log_level": "INFO"}
    assert removed == ("mongodb_host", "supervisor_max_steps")
    assert values == {
        "mongodb_host": "db",
        "log_level": "INFO",
        "supervisor_max_steps": 3,
    }


def test_strip_removed_fields_is_a_no_op_without_retired_keys():
    pruned, removed = strip_removed_fields({"log_level": "INFO"})

    assert pruned == {"log_level": "INFO"}
    assert removed == ()


def test_parse_config_accepts_and_prunes_retired_backend_keys():
    document = parse_config(
        _config_bytes(
            {"mongodb_host": "127.0.0.1", "mongodb_port": 27017, "log_level": "DEBUG"}
        )
    )

    assert document.backend == {"log_level": "DEBUG"}
    assert document.removed_backend_fields == ("mongodb_host", "mongodb_port")


def test_compose_service_host_setting_is_retired():
    document = parse_config(
        _config_bytes({"bundled_agents_use_service_hosts": True, "log_level": "INFO"})
    )

    assert document.backend == {"log_level": "INFO"}
    assert document.removed_backend_fields == ("bundled_agents_use_service_hosts",)


def test_parse_config_without_retired_keys_records_none():
    document = parse_config(_config_bytes({"log_level": "DEBUG"}))

    assert document.removed_backend_fields == ()


def test_retired_value_is_ignored_even_when_it_was_invalid():
    document = parse_config(_config_bytes({"mongodb_port": 90000}))

    assert document.backend == {}


def test_unknown_backend_keys_are_still_rejected():
    with pytest.raises(RuntimeConfigurationError):
        parse_config(_config_bytes({"not_a_setting": 1}))


def test_get_settings_warns_once_naming_retired_keys(store, monkeypatch, caplog):
    _write_backend(store, {"supervisor_max_steps": 4, "log_level": "DEBUG"})
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    loader.get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="common.config.loader")
    try:
        settings = loader.get_settings()
        loader.get_settings()
    finally:
        loader.get_settings.cache_clear()

    assert settings.log_level == "DEBUG"
    warnings = [
        record
        for record in caplog.records
        if record.name == "common.config.loader" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "supervisor_max_steps" in warnings[0].getMessage()
    assert "4" not in warnings[0].getMessage()


def test_update_config_drops_retired_keys_when_rewriting(store):
    _write_backend(store, {"mongodb_host": "db", "log_level": "INFO"})

    store.update_config(["backend", "log_level"], "DEBUG")

    written = json.loads((store.home / "config.json").read_text())
    assert written["backend"] == {"log_level": "DEBUG"}


def test_update_config_rejects_setting_a_retired_key(store):
    with pytest.raises(RuntimeConfigurationError, match="mongodb_host"):
        store.update_config(["backend", "mongodb_host"], "db")

    written = json.loads((store.home / "config.json").read_text())
    assert "mongodb_host" not in written.get("backend", {})


def test_setup_save_does_not_carry_retired_keys_forward(store):
    _write_backend(store, {"mongodb_host": "db", "log_level": "INFO"})

    store.save(
        RuntimeConfig.model_validate(
            {
                "provider": {"id": "openai", "auth": "api_key"},
                "models": {"text": "gpt-other"},
            }
        ),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )

    written = json.loads((store.home / "config.json").read_text())
    assert written["backend"] == {"log_level": "INFO"}


def test_update_config_drops_retired_keys_from_a_whole_backend_value(store):
    store.update_config(["backend"], {"mongodb_port": 1, "log_level": "DEBUG"})

    written = json.loads((store.home / "config.json").read_text())
    assert written["backend"] == {"log_level": "DEBUG"}


async def test_pruning_rewrite_does_not_break_oauth_refresh_snapshot(tmp_path):
    import base64
    import time

    from common.config.runtime_config import OAuthCredential

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
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.save(
        RuntimeConfig.model_validate(
            {
                "provider": {"id": "openai", "auth": "oauth"},
                "models": {"text": "gpt-6.1-sol"},
            }
        ),
        credential,
        {},
    )
    _write_backend(store, {"mongodb_host": "db", "log_level": "INFO"})
    startup_config = store.load({}).config
    assert startup_config.removed_backend_fields == ("mongodb_host",)

    # Same value, but the rewrite prunes the retired key from the file.
    store.update_config(["backend", "log_level"], "INFO")

    async def refresh(previous):
        return previous.model_copy(
            update={
                "refresh_token": SecretStr("fixture-rotated"),
                "expires_at": time.time() + 3600,
            }
        )

    updated = await store.resolve_oauth(startup_config, "fixture-account", {}, refresh)
    assert updated.refresh_token.get_secret_value() == "fixture-rotated"


INSTANCES = {"release": {"ports": {"frontend": 3456}}, "dev": {"ports": {"mcp": 9}}}


def _write_instances(store: RuntimeConfigStore) -> None:
    path = store.home / "config.json"
    document = json.loads(path.read_text())
    document["instances"] = INSTANCES
    path.write_text(json.dumps(document))


def test_retired_instances_load_and_are_dropped_on_the_next_save(store):
    _write_instances(store)
    document = store.load({}).config
    assert document.had_instances
    assert "instances" not in document.model_dump()
    assert not parse_config(_config_bytes({})).had_instances

    store.update_config(["backend", "log_level"], "INFO")
    assert "instances" not in json.loads((store.home / "config.json").read_text())


def test_get_settings_warns_about_instances(store, monkeypatch, caplog):
    _write_instances(store)
    monkeypatch.setenv("HYBRO_HOME", str(store.home))
    loader.get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="common.config.loader")
    try:
        loader.get_settings()
    finally:
        loader.get_settings.cache_clear()
    messages = [record.getMessage() for record in caplog.records]
    assert any("instances" in message and "--port" in message for message in messages)


def test_provider_selection_is_not_a_config_path(store):
    with pytest.raises(RuntimeConfigurationError, match="Settings, Model provider"):
        store.update_config(["provider", "id"], "openai")
    with pytest.raises(RuntimeConfigurationError):
        store.update_config(["instances", "release"], {})
