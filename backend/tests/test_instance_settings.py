"""Instance settings: field registry, read view, revisions. No network or Provider."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from common.config.loader import (
    PRIVATE_FIELDS,
    REMOVED_FIELDS,
    ROUTE_FIELDS,
    FrontendSettings,
    Settings,
    image_defaults,
)
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore
from instance_settings.registry import FIELDS, GROUPS, field_default, field_spec
from instance_settings.restart import RestartControl
from instance_settings.service import InstanceSettings

EXPECTED_KEYS = {
    "backend.approval_deadline_hours",
    "backend.orchestrator_call_wait_ceiling_hours",
    "backend.a2a_push_callback_base_url",
    "backend.agent_health_check_enabled",
    "backend.agent_health_check_interval",
    "backend.local_agent_discovery_enabled",
    "backend.local_agent_discovery_port_start",
    "backend.local_agent_discovery_port_end",
    "frontend.max_message_length",
    "backend.compaction_enabled",
    "backend.compaction_content_ttl_days",
    "backend.log_level",
    "backend.log_format",
    *(
        f"backend.orchestrator_{profile}_{suffix}"
        for profile in ("fast", "ultimate")
        for suffix in (
            "max_model_turns",
            "max_agent_calls",
            "max_parallel_calls",
            "deadline_seconds",
        )
    ),
    "backend.orchestrator_passthrough_deadline_seconds",
    "backend.orchestrator_passthrough_max_transport_retries_per_call",
    "backend.session_run_start_grace_seconds",
    "backend.llm_gateway_max_attempts",
    "backend.llm_gateway_request_timeout_seconds",
    "backend.llm_gateway_stream_timeout_seconds",
    "backend.local_agent_discovery_interval_seconds",
    "frontend.inspection_timeout_ms",
}

_KIND_TYPES = {
    "boolean": bool,
    "integer": int,
    "number": float,
    "url": str,
    "enum": str,
}


def selection() -> RuntimeConfig:
    return RuntimeConfig(
        provider=RuntimeProvider(id="openai", auth="api_key"),
        models=RuntimeModels(text="gpt-4o-mini"),
    )


def write_config(home: Path, document: dict | str) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = document if isinstance(document, str) else json.dumps(document)
    (home / "config.json").write_text(data)


def service(
    home: Path,
    *,
    settings: Settings | None = None,
    startup: RuntimeConfig | None = None,
    admins: frozenset[str] | None = None,
) -> InstanceSettings:
    startup = startup if startup is not None else RuntimeConfig()
    settings = (
        settings
        if settings is not None
        else Settings.model_validate(dict(startup.backend))
    )
    runtime_file = settings.instance_runtime_file
    return InstanceSettings(
        store=RuntimeConfigStore(home),
        settings=settings,
        startup_config=startup,
        can_configure=(lambda user: True)
        if admins is None
        else (lambda user: user in admins),
        version="9.9.9",
        restart=RestartControl(
            runtime_file=Path(runtime_file) if runtime_file else None,
            python=sys.executable,
            entrypoint=home / "runtime_entrypoint.py",
        ),
    )


def fields_by_key(view) -> dict:
    return {field.key: field for field in view.fields}


# -- registry -----------------------------------------------------------------


def test_registry_matches_the_models():
    assert {spec.key for spec in FIELDS} == EXPECTED_KEYS
    assert len({spec.key for spec in FIELDS}) == len(FIELDS)
    group_ids = [group.id for group in GROUPS]
    assert group_ids == [
        "runs_agents",
        "discovery",
        "chat_memory",
        "logging",
        "advanced",
    ]
    for spec in FIELDS:
        model = Settings if spec.section == "backend" else FrontendSettings
        assert spec.key == f"{spec.section}.{spec.name}"
        assert spec.name in model.model_fields, spec.key
        assert spec.name not in PRIVATE_FIELDS | ROUTE_FIELDS | REMOVED_FIELDS
        assert spec.group in group_ids
        annotation = model.model_fields[spec.name].annotation
        expected = _KIND_TYPES[spec.kind]
        if spec.nullable:
            assert annotation == expected | None, spec.key
        else:
            assert annotation is expected, spec.key
        assert (spec.options is not None) == (spec.kind == "enum")
    assert "backend.local_agent_discovery_excluded_ports" not in EXPECTED_KEYS


def test_registry_bounds_come_from_the_models():
    deadline = field_spec("backend.approval_deadline_hours")
    assert (deadline.nullable, deadline.exclusive_minimum, deadline.minimum) == (
        True,
        0,
        None,
    )
    port = field_spec("backend.local_agent_discovery_port_start")
    assert (port.minimum, port.maximum) == (1, 65535)
    # Numbers without a declared lower bound are nonnegative, as startup enforces.
    assert field_spec("backend.compaction_content_ttl_days").minimum == 0
    assert field_spec("backend.llm_gateway_max_attempts").minimum == 0
    calls = field_spec("backend.orchestrator_fast_max_agent_calls")
    assert (calls.exclusive_minimum, calls.minimum) == (0, None)
    assert field_spec("frontend.max_message_length").exclusive_minimum == 0
    assert field_spec("backend.log_level").options == (
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    )


def test_defaults_with_and_without_image_mode():
    discovery = field_spec("backend.local_agent_discovery_enabled")
    log_format = field_spec("backend.log_format")
    assert field_default(discovery, image=False) is False
    assert field_default(discovery, image=True) is True
    assert field_default(log_format, image=False) == "auto"
    assert field_default(log_format, image=True) == image_defaults()["log_format"]
    assert field_default(field_spec("frontend.max_message_length"), image=True) == (
        10000
    )


# -- revisions ----------------------------------------------------------------


def test_settings_and_restart_revisions_ignore_live_sections():
    base = RuntimeConfig(backend={"compaction_enabled": False})
    with_provider = base.model_copy(
        update={"provider": selection().provider, "models": selection().models}
    )
    assert with_provider.settings_revision == base.settings_revision
    assert with_provider.revision != base.revision
    debug = RuntimeConfig(backend={"compaction_enabled": False, "log_level": "DEBUG"})
    assert debug.settings_revision != base.settings_revision
    assert debug.restart_revision == base.restart_revision
    image = base.model_copy(update={"image_size": "auto"})
    assert image.restart_revision != base.restart_revision


# -- read view ----------------------------------------------------------------


async def test_view_values_sources_and_notices(tmp_path):
    home = tmp_path / "home"
    write_config(
        home,
        {
            "version": 1,
            "instances": {"default": {"port": 3000}},
            "backend": {
                "log_level": "debug",
                "mongodb_url": "mongodb://private-host-marker:27017",
                "mongodb_host": "retired-marker",
            },
            "frontend": {
                "max_message_length": 5000,
                "clerk_publishable_key": "pk_private_marker",
            },
        },
    )
    view = await service(home).view()
    fields = fields_by_key(view)
    assert fields["backend.log_level"].value == "DEBUG"
    assert fields["backend.log_level"].source == "config"
    assert fields["frontend.max_message_length"].value == 5000
    assert fields["frontend.max_message_length"].source == "config"
    compaction = fields["backend.compaction_enabled"]
    assert (compaction.value, compaction.default, compaction.source) == (
        True,
        True,
        "default",
    )
    assert fields["backend.approval_deadline_hours"].value is None
    assert view.revision == RuntimeConfigStore(home).read_config().settings_revision
    assert view.config_error is None
    notices = {notice.code: notice for notice in view.notices}
    assert notices["removed_fields"].fields == ("backend.mongodb_host",)
    assert notices["instances_ignored"].fields == ()
    assert notices["unlisted_fields"].fields == (
        "backend.mongodb_url",
        "frontend.clerk_publishable_key",
    )
    body = view.model_dump_json()
    for marker in ("private-host-marker", "pk_private_marker", "retired-marker"):
        assert marker not in body
    assert [group.id for group in view.groups][0] == "runs_agents"
    # Restart is not wired yet: the view reports it as unavailable.
    assert (view.restart.available, view.restart.state) == (False, "idle")


async def test_per_field_and_top_level_restart_pending(tmp_path):
    home = tmp_path / "home"
    instance = service(home)
    view = await instance.view()
    assert view.restart_pending is False
    assert not any(field.restart_pending for field in view.fields)

    # log_level applies live, so it alone never marks a restart pending; until
    # it is applied the field still differs from the running level.
    write_config(home, {"version": 1, "backend": {"log_level": "DEBUG"}})
    view = await instance.view()
    assert view.restart_pending is False
    assert fields_by_key(view)["backend.log_level"].restart_pending is True

    write_config(home, {"version": 1, "backend": {"compaction_enabled": False}})
    view = await instance.view()
    assert view.restart_pending is True
    fields = fields_by_key(view)
    assert fields["backend.compaction_enabled"].restart_pending is True
    assert fields["backend.log_level"].restart_pending is False

    # A non-registry edit such as image_size still needs a restart.
    write_config(home, {"version": 1, "image_size": "auto"})
    assert (await instance.view()).restart_pending is True


async def test_provider_change_does_not_mark_a_restart_pending(tmp_path):
    home = tmp_path / "home"
    instance = service(home)
    RuntimeConfigStore(home).save(
        selection(),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )
    view = await instance.view()
    assert view.restart_pending is False
    assert view.revision == RuntimeConfig().settings_revision


async def test_frontend_running_value_is_the_startup_document(tmp_path):
    home = tmp_path / "home"
    startup = RuntimeConfig(frontend={"max_message_length": 5000})
    write_config(home, {"version": 1, "frontend": {"max_message_length": 5000}})
    view = await service(home, startup=startup).view()
    assert fields_by_key(view)["frontend.max_message_length"].restart_pending is False
    write_config(home, {"version": 1})
    view = await service(home, startup=startup).view()
    field = fields_by_key(view)["frontend.max_message_length"]
    assert (field.value, field.source, field.restart_pending) == (
        10000,
        "default",
        True,
    )


async def test_invalid_file_shows_running_values(tmp_path):
    home = tmp_path / "home"
    startup = RuntimeConfig(backend={"compaction_enabled": False})
    write_config(home, '{"backend": {"compaction_enabled": "private-marker"')
    view = await service(home, startup=startup).view()
    assert view.revision is None
    assert view.config_error is not None
    assert view.config_error.startswith("Invalid config.json")
    assert "private-marker" not in view.config_error
    assert view.notices == ()
    assert view.restart_pending is False
    compaction = fields_by_key(view)["backend.compaction_enabled"]
    assert (compaction.value, compaction.source, compaction.restart_pending) == (
        False,
        "config",
        False,
    )
    assert fields_by_key(view)["backend.log_level"].source == "default"


async def test_system_info_in_a_checkout(tmp_path):
    view = await service(tmp_path / "home").view()
    system = view.system
    assert system.version == "9.9.9"
    assert system.deployment == "checkout"
    assert (system.port, system.listen, system.mcp_available) == (None, None, None)
    assert system.auth_mode == "mock"
    assert str(tmp_path) not in view.model_dump_json()


async def test_system_info_and_services_revision_in_the_image(tmp_path):
    home = tmp_path / "home"
    runtime_file = tmp_path / "run" / "runtime.json"
    runtime_file.parent.mkdir()
    startup = RuntimeConfig()
    runtime_file.write_text(
        json.dumps(
            {
                "listen": "loopback",
                "public_port": 3100,
                "insecure_no_auth": False,
                "embedded_mongo": True,
                "services_revision": startup.restart_revision,
            }
        )
    )
    settings = Settings(instance_runtime_file=str(runtime_file))
    instance = service(home, settings=settings, startup=startup)
    view = await instance.view()
    assert view.system.deployment == "image"
    assert (view.system.port, view.system.listen, view.system.mcp_available) == (
        3100,
        "loopback",
        True,
    )
    assert view.restart_pending is False
    assert (view.restart.available, view.restart.state) == (True, "idle")
    (runtime_file.parent / "restart.json").write_text(
        json.dumps({"state": "completed", "error": None, "updated_at": time.time()})
    )
    assert (await instance.view()).restart.state == "completed"
    # Image defaults apply to unset keys.
    assert fields_by_key(view)["backend.local_agent_discovery_enabled"].default is True
    assert str(tmp_path) not in view.model_dump_json()

    # The frontend and agents still run an older document than the backend.
    runtime_file.write_text(
        json.dumps({"listen": "public", "public_port": 8080, "services_revision": "x"})
    )
    view = await instance.view()
    assert view.restart_pending is True
    assert (view.system.port, view.system.listen, view.system.mcp_available) == (
        8080,
        "public",
        False,
    )

    runtime_file.write_text("not json")
    view = await instance.view()
    assert view.restart_pending is False
    assert view.system.deployment == "image"
    assert (view.system.port, view.system.listen) == (None, None)


def test_can_configure_delegates(tmp_path):
    instance = service(tmp_path / "home", admins=frozenset({"user_admin"}))
    assert instance.can_configure("user_admin")
    assert not instance.can_configure("user_other")


def test_registry_specs_are_frozen():
    spec = FIELDS[0]
    with pytest.raises((AttributeError, TypeError)):
        spec.key = "backend.other"  # type: ignore[misc]
    assert isinstance(GROUPS, tuple)


# -- writes -------------------------------------------------------------------


def test_set_log_level_changes_root_and_handlers_only(monkeypatch):
    from common.observability import logging as observability
    from common.observability.logging import set_log_level

    stream, sink = logging.Handler(), logging.Handler()
    monkeypatch.setattr(observability, "_configured_handler", stream)
    monkeypatch.setattr(observability, "_sink_handler", sink)
    root = logging.getLogger()
    previous = root.level
    libraries = [logging.getLogger(name) for name in ("httpx", "pymongo")]
    for library in libraries:
        library.setLevel(logging.WARNING)
    try:
        set_log_level("DEBUG")
        assert (root.level, stream.level, sink.level) == (logging.DEBUG,) * 3
        assert [library.level for library in libraries] == [logging.WARNING] * 2
        set_log_level("ERROR")
        assert (root.level, stream.level, sink.level) == (logging.ERROR,) * 3
    finally:
        root.setLevel(previous)


def test_update_settings_applies_edits_under_the_lock(tmp_path):
    from common.config.runtime_store import SettingsRevisionConflict

    store = RuntimeConfigStore(tmp_path / "home")
    seen: list[dict] = []

    def validate(document: dict) -> dict:
        seen.append(json.loads(json.dumps(document)))
        return document

    previous, current = store.update_settings(
        {("backend", "compaction_enabled"): False, ("frontend", "x"): None},
        expected_revision=RuntimeConfig().settings_revision,
        validate=validate,
    )
    assert previous == RuntimeConfig()
    assert current.backend == {"compaction_enabled": False}
    assert seen == [{"version": 1, "backend": {"compaction_enabled": False}}]
    with pytest.raises(SettingsRevisionConflict):
        store.update_settings(
            {("backend", "compaction_enabled"): True},
            expected_revision=previous.settings_revision,
            validate=validate,
        )
    with pytest.raises(ValueError):
        store.update_settings(
            {("provider", "id"): "openai"},
            expected_revision=current.settings_revision,
            validate=validate,
        )


def test_update_settings_refuses_an_invalid_file(tmp_path):
    from common.config.runtime_config import RuntimeConfigurationError
    from common.config.runtime_store import SettingsFileInvalid

    home = tmp_path / "home"
    write_config(home, "[]")
    with pytest.raises(SettingsFileInvalid) as caught:
        RuntimeConfigStore(home).update_settings(
            {("backend", "log_level"): "DEBUG"},
            expected_revision="x",
            validate=lambda document: document,
        )
    assert isinstance(caught.value, RuntimeConfigurationError)
    assert (home / "config.json").read_text() == "[]"
