"""startup_completed and shutdown_started from the application runtime."""

import json
import logging
import time
from types import SimpleNamespace

import pytest

import container
from common.config.loader import Settings
from common.observability.logging import StructuredFormatter

_SECRETS = ("sk_test_PLANTEDCLERK", "pw-PLANTED", "tok-PLANTED")

_SUMMARY_KEYS = {
    "app_env",
    "auth_mode",
    "log_level",
    "log_format",
    "redis_configured",
    "local_agent_discovery_enabled",
    "agent_health_check_enabled",
    "compaction_enabled",
    "orchestrator_canary_enabled",
    "orchestrator_worker_interval_seconds",
    "bundled_agents_configured",
}


def _secret_settings() -> Settings:
    return Settings(
        clerk_secret_key="sk_test_PLANTEDCLERK",
        mongodb_url="mongodb://admin:pw-PLANTED@db.example:27017/hybro",
        default_agent_llm_token="tok-PLANTED",
        redis_url="redis://:pw-PLANTED@cache:6379/0",
    )


def _events(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == name]


@pytest.mark.parametrize(
    ("app_env", "reported"),
    [("production", "production"), ("custom free text", "other")],
)
def test_startup_summary_reports_only_known_environments(app_env, reported):
    assert container.startup_summary(Settings(app_env=app_env))["app_env"] == reported


def test_startup_summary_is_an_allowlist_without_secrets():
    summary = container.startup_summary(_secret_settings())

    assert set(summary) == _SUMMARY_KEYS
    assert summary["redis_configured"] is True
    assert summary["bundled_agents_configured"] is False
    rendered = json.dumps(summary)
    for secret in _SECRETS:
        assert secret not in rendered
    assert "db.example" not in rendered


def test_log_startup_completed_emits_the_summary(caplog):
    caplog.set_level(logging.DEBUG)
    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_runtime=SimpleNamespace(
                current=SimpleNamespace(state="unconfigured")
            )
        )
    )
    runtime = container.ApplicationRuntime(settings=_secret_settings())
    runtime.started_monotonic = time.monotonic() - 1.5

    container.log_startup_completed(app, runtime)

    [record] = _events(caplog, "startup_completed")
    assert record.levelno == logging.INFO
    assert record._hybro_system is True
    assert record.provider_state == "unconfigured"
    assert record.auth_mode == "mock"
    assert isinstance(record.startup_duration_ms, (int, float))
    assert record.startup_duration_ms >= 1500
    line = StructuredFormatter(output_format="json", environment="test").format(record)
    assert "[REDACTED]" not in line
    for secret in _SECRETS:
        assert secret not in line
    assert json.loads(line)["config.redis_configured"] is True


class _Context:
    def __init__(self, caplog) -> None:
        self.caplog = caplog
        self.shutdown_logged_before_exit: bool | None = None

    async def __aexit__(self, *_exc) -> None:
        self.shutdown_logged_before_exit = bool(
            _events(self.caplog, "shutdown_started")
        )


@pytest.mark.asyncio
async def test_shutdown_runtime_emits_shutdown_started_before_exit(caplog):
    caplog.set_level(logging.DEBUG)
    runtime = container.ApplicationRuntime(settings=Settings())
    runtime.started_monotonic = time.monotonic() - 2
    context = _Context(caplog)
    runtime._lifespan_context = context

    await container.shutdown_runtime(SimpleNamespace(), runtime)

    assert context.shutdown_logged_before_exit is True
    [record] = _events(caplog, "shutdown_started")
    assert record._hybro_system is True
    assert record.uptime_seconds >= 2
    assert runtime._lifespan_context is None


@pytest.mark.asyncio
async def test_shutdown_of_a_runtime_never_started_emits_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    runtime = container.ApplicationRuntime(settings=Settings())

    await container.shutdown_runtime(SimpleNamespace(), runtime)

    assert _events(caplog, "shutdown_started") == []


@pytest.mark.asyncio
async def test_lifespan_logs_startup_completed_after_bindings_verify(monkeypatch):
    import main

    calls: list[str] = []

    async def startup(app, runtime):
        calls.append("startup")

    async def shutdown(app, runtime):
        calls.append("shutdown")

    monkeypatch.setattr(main, "startup_runtime", startup)
    monkeypatch.setattr(main, "shutdown_runtime", shutdown)
    monkeypatch.setattr(
        main, "validate_runtime_bindings", lambda app, runtime: calls.append("verify")
    )
    monkeypatch.setattr(
        main, "log_startup_completed", lambda app, runtime: calls.append("logged")
    )

    async with main.lifespan(SimpleNamespace()):
        calls.append("serving")

    assert calls == ["startup", "verify", "logged", "serving", "shutdown"]
