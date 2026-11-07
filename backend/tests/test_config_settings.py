from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.config.loader import Settings

RUNTIME_CONFIG_ENV_VARS = (
    "ORCHESTRATOR_WORKER_INTERVAL_SECONDS",
    "AGENT_HEALTH_CHECK_INTERVAL",
    "COMPACTION_CONCURRENCY",
)


def _clear_runtime_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in RUNTIME_CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_removed_public_gateway_settings_are_not_exposed():
    removed_fields = {
        "discovery_default_limit",
        "discovery_rate_limit_per_key",
        "discovery_rate_limit_global",
        "hybro_timeout_seconds",
        "gateway_base_url",
        "gateway_rate_limit_per_key",
        "gateway_rate_limit_global",
    }

    assert removed_fields.isdisjoint(Settings.model_fields)
    assert {
        "local_agent_discovery_enabled",
        "local_agent_discovery_host",
        "local_agent_discovery_port_start",
        "local_agent_discovery_port_end",
        "local_agent_discovery_interval_seconds",
        "local_agent_discovery_connect_timeout_seconds",
        "local_agent_discovery_probe_timeout_seconds",
        "local_agent_discovery_excluded_ports",
    }.issubset(Settings.model_fields)


def test_canonical_lifecycle_has_no_runtime_admission_or_worker_switches():
    settings = Settings()

    assert not hasattr(settings, "feature_canonical_turn_lifecycle")
    assert not hasattr(settings, "orchestrator_projection_enabled")
    assert not hasattr(settings, "orchestrator_recovery_enabled")


def test_local_agent_discovery_excluded_ports_parses_the_entrypoint_projection():
    """The entrypoint projects the manifest agent ports as one comma-separated value."""
    settings = Settings.model_validate(
        {"local_agent_discovery_excluded_ports": "7001,6002,7002,7003"}
    )
    assert settings.local_agent_discovery_excluded_ports == frozenset(
        {7001, 6002, 7002, 7003}
    )
    assert Settings().local_agent_discovery_excluded_ports == frozenset()


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_orchestrator_thinking_level_normalizes_to_none(raw: str | None) -> None:
    settings = Settings(
        orchestrator_fast_thinking_level=raw,
        orchestrator_ultimate_thinking_level=raw,
    )

    assert settings.orchestrator_fast_thinking_level is None
    assert settings.orchestrator_ultimate_thinking_level is None


def test_runtime_config_unification_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HEALTH_CHECK_INTERVAL", "120")
    monkeypatch.setenv("COMPACTION_CONCURRENCY", "7")

    settings = Settings(
        agent_health_check_interval=120,
        compaction_concurrency=7,
    )

    assert settings.agent_health_check_interval == 120
    assert settings.compaction_concurrency == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 5),
        ("bad", 5),
        ("0", 1),
        ("-2", 1),
        ("9", 9),
    ],
)
def test_compaction_concurrency_preserves_legacy_fallbacks(
    raw: str,
    expected: int,
) -> None:
    settings = Settings(compaction_concurrency=raw)

    assert settings.compaction_concurrency == expected


def test_wait_semantics_settings_default_to_no_approval_deadline_and_week_ceiling():
    settings = Settings()

    assert settings.approval_deadline_hours is None
    assert settings.orchestrator_call_wait_ceiling_hours == 168.0


@pytest.mark.parametrize("value", [48, 48.5])
def test_wait_semantics_settings_accept_int_and_float_hours(value) -> None:
    settings = Settings.model_validate(
        {
            "approval_deadline_hours": value,
            "orchestrator_call_wait_ceiling_hours": value,
        }
    )

    assert settings.approval_deadline_hours == value
    assert settings.orchestrator_call_wait_ceiling_hours == value


@pytest.mark.parametrize(
    "field", ["approval_deadline_hours", "orchestrator_call_wait_ceiling_hours"]
)
def test_wait_semantics_settings_reject_zero(field) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: 0})


def test_local_agent_discovery_scans_9000_to_9999_by_default():
    from local_agents.config import LocalAgentDiscoveryConfig

    settings = Settings()
    config = LocalAgentDiscoveryConfig()

    assert (
        settings.local_agent_discovery_port_start,
        settings.local_agent_discovery_port_end,
    ) == (9000, 9999)
    assert (config.port_start, config.port_end) == (9000, 9999)


@pytest.mark.parametrize("profile", ["fast", "ultimate"])
@pytest.mark.parametrize(
    "suffix", ["max_agent_calls", "max_parallel_calls", "deadline_seconds"]
)
def test_orchestrator_profile_bounds_reject_zero_at_startup(profile, suffix) -> None:
    field = f"orchestrator_{profile}_{suffix}"
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({field: 0})
    assert caught.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("profile", ["fast", "ultimate"])
def test_parallel_calls_above_agent_calls_names_the_parallel_field(profile) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(
            {
                f"orchestrator_{profile}_max_agent_calls": 2,
                f"orchestrator_{profile}_max_parallel_calls": 3,
            }
        )
    assert [error["loc"] for error in caught.value.errors()] == [
        (f"orchestrator_{profile}_max_parallel_calls",)
    ]
    settings = Settings.model_validate(
        {
            f"orchestrator_{profile}_max_agent_calls": 3,
            f"orchestrator_{profile}_max_parallel_calls": 3,
        }
    )
    assert getattr(settings, f"orchestrator_{profile}_max_parallel_calls") == 3


def test_agent_health_check_interval_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"agent_health_check_interval": 0})


def test_instance_runtime_file_is_private_and_image_only(monkeypatch) -> None:
    from common.config.loader import (
        PRIVATE_FIELDS,
        _apply_image_defaults,
        image_defaults,
    )

    assert "instance_runtime_file" in PRIVATE_FIELDS
    assert Settings().instance_runtime_file == ""
    values: dict = {"instance_runtime_file": "/elsewhere.json"}
    _apply_image_defaults(values)
    assert values["instance_runtime_file"] == "/tmp/hybro/runtime.json"
    monkeypatch.setenv("LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS", "7001")
    defaults = image_defaults()
    assert defaults["log_format"] == "json"
    assert defaults["local_agent_discovery_enabled"] is True
    assert defaults["local_agent_discovery_excluded_ports"] == "7001"
    assert "instance_runtime_file" not in defaults


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"llm_gateway_max_attempts": -1}, "llm_gateway_max_attempts"),
        (
            {"a2a_push_callback_base_url": "https://user:pw@example.org"},
            "a2a_push_callback_base_url",
        ),
        ({"some_port": 70000}, "some_port"),
    ],
)
def test_backend_pre_checks_name_the_field(values, field) -> None:
    from common.config.loader import BackendFieldError, validate_backend

    with pytest.raises(BackendFieldError) as caught:
        validate_backend(values)
    assert caught.value.field == field
    assert "pw" not in str(caught.value)


@pytest.mark.parametrize("profile", ["fast", "ultimate"])
def test_agent_calls_below_the_default_parallel_calls_are_rejected(profile) -> None:
    # Field validators skip defaults unless asked; the rule must still hold.
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({f"orchestrator_{profile}_max_agent_calls": 1})
    assert [error["loc"] for error in caught.value.errors()] == [
        (f"orchestrator_{profile}_max_parallel_calls",)
    ]


def test_discovery_start_above_the_default_end_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({"local_agent_discovery_port_start": 10000})
    assert [error["loc"] for error in caught.value.errors()] == [
        ("local_agent_discovery_port_end",)
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("orchestrator_call_wait_ceiling_hours", 1e12),
        ("approval_deadline_hours", 1e8),
        ("compaction_content_ttl_days", 10**9),
        ("llm_gateway_request_timeout_seconds", 0),
        ("llm_gateway_stream_timeout_seconds", 0),
    ],
)
def test_values_that_break_the_services_are_rejected_at_startup(field, value) -> None:
    # Too large for timedelta date math, or a timeout that fails every call.
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({field: value})
    assert caught.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["max_message_length", "inspection_timeout_ms"])
def test_frontend_numbers_stay_within_what_the_browser_accepts(field) -> None:
    from common.config.loader import FrontendSettings

    with pytest.raises(ValidationError):
        FrontendSettings.model_validate({field: 2**31})
    assert getattr(FrontendSettings.model_validate({field: 2**31 - 1}), field)
