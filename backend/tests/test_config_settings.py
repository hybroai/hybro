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
