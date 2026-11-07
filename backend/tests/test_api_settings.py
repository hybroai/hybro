"""Settings API routes over a temporary config directory; no Provider or network."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from api_gateway.errors import settings_request_error_handler
from api_gateway.routes.settings_routes import router
from common.auth import ClerkUser, get_current_user
from common.config.loader import Settings
from common.config.runtime_config import (
    ApiKeyCredential,
    RuntimeConfig,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore
from instance_settings import InstanceSettings, SettingsRequestError
from instance_settings import restart as restart_module
from instance_settings.restart import RestartControl

BASE = "http://fixture/api/v1"
ROUTES = (("GET", "/settings"), ("PUT", "/settings"), ("POST", "/settings/restart"))


class Harness:
    def __init__(
        self,
        home: Path,
        *,
        admins: frozenset[str] | None = None,
        wired: bool = True,
        restart: RestartControl | None = None,
    ) -> None:
        self.store = RuntimeConfigStore(home)
        self.restart = restart or RestartControl(
            runtime_file=None, python=sys.executable, entrypoint=home / "missing.py"
        )
        self.settings = InstanceSettings(
            store=self.store,
            settings=Settings(),
            startup_config=RuntimeConfig(),
            can_configure=(lambda user: True)
            if admins is None
            else (lambda user: user in admins),
            version="9.9.9",
            restart=self.restart,
        )
        self.user = "user_local_developer"
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.add_exception_handler(SettingsRequestError, settings_request_error_handler)
        if wired:
            app.state.instance_settings = self.settings
        app.dependency_overrides[get_current_user] = lambda: ClerkUser(
            self.user, "session", {}
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def harness(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    await harness.aclose()


async def test_mock_mode_reads_the_settings(harness):
    response = await harness.http.get("/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == RuntimeConfig().settings_revision
    assert body["system"]["version"] == "9.9.9"
    keys = {field["key"] for field in body["fields"]}
    assert "backend.log_level" in keys
    log_level = next(f for f in body["fields"] if f["key"] == "backend.log_level")
    assert log_level["apply"] == "live"
    assert log_level["options"] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


async def test_clerk_non_admin_is_forbidden_on_every_route(tmp_path):
    harness = Harness(tmp_path / "home", admins=frozenset({"user_admin"}))
    try:
        harness.user = "user_other"
        for method, path in ROUTES:
            response = await harness.http.request(
                method, path, json={"revision": "x", "values": {}}
            )
            assert response.status_code == 403, path
            assert response.json()["code"] == "forbidden"
        harness.user = "user_admin"
        assert (await harness.http.get("/settings")).status_code == 200
    finally:
        await harness.aclose()


async def test_missing_service_is_unavailable(tmp_path):
    harness = Harness(tmp_path / "home", wired=False)
    try:
        response = await harness.http.get("/settings")
        assert response.status_code == 503
        assert response.json()["code"] == "settings_unavailable"
    finally:
        await harness.aclose()


def test_application_registers_settings_routes_and_error_mapping():
    from main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/settings" in paths
    assert SettingsRequestError in app.exception_handlers


# -- PUT ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_log_levels():
    from common.observability import logging as observability

    root = logging.getLogger()
    handlers = [
        handler
        for handler in (observability._configured_handler, observability._sink_handler)
        if handler is not None
    ]
    levels = [root.level, *(handler.level for handler in handlers)]
    yield
    root.setLevel(levels[0])
    for handler, level in zip(handlers, levels[1:], strict=True):
        handler.setLevel(level)


async def revision(harness: Harness) -> str:
    return (await harness.http.get("/settings")).json()["revision"]


async def put(harness: Harness, values: dict, *, token: str | None = None):
    token = token if token is not None else await revision(harness)
    return await harness.http.put(
        "/settings", json={"revision": token, "values": values}
    )


def field(body: dict, key: str) -> dict:
    return next(f for f in body["fields"] if f["key"] == key)


@pytest.mark.parametrize(
    ("key", "submitted", "stored"),
    [
        ("backend.compaction_enabled", False, False),
        ("frontend.max_message_length", 8000, 8000),
        ("backend.approval_deadline_hours", 48.5, 48.5),
        (
            "backend.a2a_push_callback_base_url",
            "https://hybro.example.com/",
            "https://hybro.example.com",
        ),
        ("backend.log_format", "LOGFMT", "logfmt"),
        ("backend.local_agent_discovery_port_start", 9100, 9100),
    ],
)
async def test_put_saves_each_kind_normalized(harness, key, submitted, stored):
    response = await put(harness, {key: submitted})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == [key]
    assert body["restart_required"] is True
    section, name = key.split(".")
    assert getattr(harness.store.read_config(), section)[name] == stored
    saved = field(body["settings"], key)
    assert (saved["value"], saved["source"], saved["restart_pending"]) == (
        stored,
        "config",
        True,
    )
    assert body["settings"]["revision"] == (
        harness.store.read_config().settings_revision
    )


async def test_null_resets_a_key_to_its_default(harness):
    assert (await put(harness, {"backend.approval_deadline_hours": 24})).is_success
    response = await put(harness, {"backend.approval_deadline_hours": None})
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["backend.approval_deadline_hours"]
    assert "approval_deadline_hours" not in harness.store.read_config().backend
    reset = field(response.json()["settings"], "backend.approval_deadline_hours")
    assert (reset["value"], reset["source"]) == (None, "default")
    assert response.json()["restart_required"] is False


async def test_no_op_edit_writes_nothing(harness):
    assert (await put(harness, {"backend.compaction_enabled": False})).is_success
    before = (harness.store.home / "config.json").read_bytes()
    response = await put(harness, {"backend.compaction_enabled": False})
    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert (harness.store.home / "config.json").read_bytes() == before
    missing = await put(harness, {"frontend.max_message_length": None})
    assert missing.json()["changed"] == []


def field_errors(response) -> list[tuple[str | None, str]]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_settings"
    return [(error["field"], error["code"]) for error in body["errors"]]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            {"frontend.max_message_length": "12"},
            [("frontend.max_message_length", "type_mismatch")],
        ),
        (
            {"frontend.max_message_length": True},
            [("frontend.max_message_length", "type_mismatch")],
        ),
        ({"backend.log_level": False}, [("backend.log_level", "type_mismatch")]),
        ({"backend.log_format": ""}, [("backend.log_format", "invalid_option")]),
        ({"backend.log_level": "LOUD"}, [("backend.log_level", "invalid_option")]),
        (
            {"backend.agent_health_check_enabled": 1},
            [("backend.agent_health_check_enabled", "type_mismatch")],
        ),
        (
            {"frontend.max_message_length": 0},
            [("frontend.max_message_length", "greater_than")],
        ),
        (
            {"backend.agent_health_check_interval": 1.5},
            [("backend.agent_health_check_interval", "type_mismatch")],
        ),
        (
            {"backend.local_agent_discovery_port_start": 70000},
            [("backend.local_agent_discovery_port_start", "less_than_equal")],
        ),
        (
            {
                "backend.local_agent_discovery_port_start": 9500,
                "backend.local_agent_discovery_port_end": 9100,
            },
            [("backend.local_agent_discovery_port_end", "value_error")],
        ),
        (
            {"backend.a2a_push_callback_base_url": "ftp://example.org"},
            [("backend.a2a_push_callback_base_url", "value_error")],
        ),
        (
            {"backend.orchestrator_call_wait_ceiling_hours": -1},
            [("backend.orchestrator_call_wait_ceiling_hours", "value_error")],
        ),
        (
            {"backend.a2a_push_callback_base_url": "https://user:pw@example.org"},
            [("backend.a2a_push_callback_base_url", "value_error")],
        ),
    ],
)
async def test_field_errors_name_the_field(harness, values, expected):
    response = await put(harness, values)
    assert field_errors(response) == expected
    assert not (harness.store.home / "config.json").exists()


@pytest.mark.parametrize("profile", ["fast", "ultimate"])
async def test_parallel_calls_above_agent_calls_are_capped_with_a_notice(
    harness, profile
):
    agent_calls = f"backend.orchestrator_{profile}_max_agent_calls"
    parallel = f"backend.orchestrator_{profile}_max_parallel_calls"
    # Only the agent calls: the default parallel calls are capped.
    response = await put(harness, {agent_calls: 2})
    assert response.status_code == 200, response.text
    view = response.json()["settings"]
    assert (field(view, parallel)["value"], field(view, parallel)["source"]) == (
        2,
        "default",
    )
    capped = [n for n in view["notices"] if n["code"] == "capped_fields"]
    assert [n["fields"] for n in capped] == [[parallel]]
    # The entered value is kept, so raising the agent calls restores it.
    response = await put(harness, {parallel: 5})
    assert response.status_code == 200, response.text
    assert field(response.json()["settings"], parallel)["value"] == 2
    profile_values = harness.store.read_config().backend
    assert profile_values[parallel.removeprefix("backend.")] == 5
    # The stored value changed even though the capped value did not.
    response = await put(harness, {parallel: 6})
    assert response.json()["changed"] == [parallel]
    view = (await put(harness, {agent_calls: 8})).json()["settings"]
    assert field(view, parallel)["value"] == 6
    assert not [n for n in view["notices"] if n["code"] == "capped_fields"]


async def test_port_end_is_checked_against_the_files_start(harness):
    assert (
        await put(harness, {"backend.local_agent_discovery_port_start": 9500})
    ).is_success
    response = await put(harness, {"backend.local_agent_discovery_port_end": 9100})
    assert field_errors(response) == [
        ("backend.local_agent_discovery_port_end", "value_error")
    ]


async def test_unknown_keys_are_rejected_without_echoing_them(harness):
    response = await put(
        harness,
        {
            "backend.mongodb_url": "mongodb://private-marker",
            "backend.secret-key-marker": 1,
            "backend.log_level": "DEBUG",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == [
        {
            "field": None,
            "code": "unknown_field",
            "message": "2 fields are not editable settings.",
        }
    ]
    assert "marker" not in response.text
    assert not (harness.store.home / "config.json").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"revision": "x", "values": {}},
        {"revision": "x", "values": {"backend.log_level": ["private-marker"]}},
        {"values": {"backend.log_level": "private-marker"}},
        {"revision": "x", "values": {"backend.log_level": "x" * 5000}},
    ],
)
async def test_malformed_requests_never_echo_input(harness, body):
    response = await harness.http.put("/settings", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "private-marker" not in response.text


async def test_errors_never_echo_submitted_strings(harness):
    for values in (
        {"backend.log_level": "private-marker"},
        {"backend.log_format": "private-marker"},
        {"backend.a2a_push_callback_base_url": "http://private-marker.example"},
        {"backend.a2a_push_callback_base_url": "https://u:private-marker@x.org"},
        {"frontend.max_message_length": "private-marker"},
    ):
        response = await put(harness, values)
        assert response.status_code == 422, values
        assert "private-marker" not in response.text


async def test_revision_conflict_after_a_concurrent_edit(harness):
    token = await revision(harness)
    harness.store.update_config(["backend", "compaction_enabled"], False)
    response = await put(harness, {"backend.log_level": "DEBUG"}, token=token)
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"
    assert "log_level" not in harness.store.read_config().backend


async def test_setup_save_does_not_conflict(harness):
    token = await revision(harness)
    harness.store.save(
        RuntimeConfig(
            provider=RuntimeProvider(id="openai", auth="api_key"),
            models=RuntimeModels(text="gpt-4o-mini"),
        ),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )
    response = await put(harness, {"backend.compaction_enabled": False}, token=token)
    assert response.status_code == 200, response.text


async def test_invalid_file_is_a_conflict(harness):
    harness.store.home.mkdir(mode=0o700, parents=True)
    (harness.store.home / "config.json").write_text("{not json")
    response = await put(harness, {"backend.log_level": "DEBUG"}, token="x")
    assert response.status_code == 409
    assert response.json()["code"] == "config_invalid"
    assert (harness.store.home / "config.json").read_text() == "{not json"


async def test_a_huge_integer_is_a_field_error_not_a_crash(harness):
    response = await put(harness, {"backend.agent_health_check_interval": 10**400})
    assert response.status_code == 422, response.text
    assert [error["field"] for error in response.json()["errors"]] == [
        "backend.agent_health_check_interval"
    ]


async def test_a_run_limit_mongodb_cannot_store_is_a_field_error(harness):
    response = await put(harness, {"backend.orchestrator_fast_max_model_turns": 2**63})
    assert response.status_code == 422, response.text
    assert [error["field"] for error in response.json()["errors"]] == [
        "backend.orchestrator_fast_max_model_turns"
    ]


async def test_a_huge_integer_in_the_file_is_reported_as_invalid(harness):
    harness.store.home.mkdir(mode=0o700, parents=True)
    (harness.store.home / "config.json").write_text(
        '{"version": 1, "backend": {"agent_health_check_interval": 1' + "0" * 400 + "}}"
    )
    response = await harness.http.get("/settings")
    assert response.status_code == 200, response.text
    assert response.json()["revision"] is None
    assert response.json()["config_error"]


async def test_other_sections_and_auth_are_untouched(harness):
    harness.store.save(
        RuntimeConfig(
            provider=RuntimeProvider(id="openai", auth="api_key"),
            models=RuntimeModels(text="gpt-4o-mini"),
        ),
        ApiKeyCredential(provider="openai", api_key=SecretStr("fixture-key")),
        {},
    )
    harness.store.update_config(["image_size"], "auto")
    harness.store.update_config(["backend", "mongodb_url"], "mongodb://db:27017/x")
    harness.store.update_config(["frontend", "clerk_sign_in_url"], "/in")
    harness.store.ensure_service_credentials()
    auth = (harness.store.home / "auth.json").read_bytes()
    before = harness.store.read_config()
    response = await put(
        harness, {"backend.log_level": "DEBUG", "frontend.max_message_length": 9}
    )
    assert response.status_code == 200, response.text
    after = harness.store.read_config()
    assert (after.provider, after.models, after.image_size) == (
        before.provider,
        before.models,
        before.image_size,
    )
    assert after.backend == {
        "mongodb_url": "mongodb://db:27017/x",
        "log_level": "DEBUG",
    }
    assert after.frontend == {"clerk_sign_in_url": "/in", "max_message_length": 9}
    assert (harness.store.home / "auth.json").read_bytes() == auth


async def test_retired_keys_and_instances_are_dropped_on_save(harness):
    harness.store.home.mkdir(mode=0o700, parents=True)
    (harness.store.home / "config.json").write_text(
        json.dumps({"version": 1, "instances": {}, "backend": {"mongodb_host": "x"}})
    )
    response = await put(harness, {"backend.compaction_enabled": False})
    assert response.status_code == 200, response.text
    document = json.loads((harness.store.home / "config.json").read_text())
    assert document == {"version": 1, "backend": {"compaction_enabled": False}}
    assert response.json()["settings"]["notices"] == []


async def test_log_level_applies_live_and_never_needs_a_restart(harness):
    root = logging.getLogger()
    response = await put(harness, {"backend.log_level": "debug"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == ["backend.log_level"]
    assert body["restart_required"] is False
    assert harness.store.read_config().backend == {"log_level": "DEBUG"}
    assert root.level == logging.DEBUG
    assert field(body["settings"], "backend.log_level")["restart_pending"] is False

    response = await put(harness, {"backend.log_level": None})
    body = response.json()
    assert body["changed"] == ["backend.log_level"]
    assert root.level == logging.INFO
    assert body["restart_required"] is False
    log_level = field(body["settings"], "backend.log_level")
    assert (log_level["value"], log_level["restart_pending"]) == ("INFO", False)
    view = (await harness.http.get("/settings")).json()
    assert view["restart_pending"] is False


async def test_a_hand_edited_log_level_applies_on_the_next_save(harness):
    root = logging.getLogger()
    harness.store.update_config(["backend", "log_level"], "WARNING")
    response = await put(harness, {"backend.compaction_enabled": False})
    assert response.status_code == 200, response.text
    assert root.level == logging.WARNING
    log_level = field(response.json()["settings"], "backend.log_level")
    assert (log_level["value"], log_level["restart_pending"]) == ("WARNING", False)
    await put(harness, {"backend.log_level": None})
    assert root.level == logging.INFO


async def test_two_saves_with_one_revision_conflict(harness):
    token = await revision(harness)
    responses = await asyncio.gather(
        put(harness, {"backend.compaction_enabled": False}, token=token),
        put(harness, {"frontend.max_message_length": 9}, token=token),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]


# -- POST restart -------------------------------------------------------------

CHECK_OK = "import json\nprint(json.dumps({'ok': True}))\n"
CHECK_INVALID = (
    "import json, sys\n"
    "print(json.dumps({'ok': False, 'error': 'Invalid configuration: bad'}))\n"
    "sys.exit(1)\n"
)


@pytest.fixture
def image_harness(tmp_path, monkeypatch):
    """A harness whose restart control runs a fixture check script."""
    launches: list[list[str]] = []
    monkeypatch.setattr(
        restart_module, "Popen", lambda command, **kwargs: launches.append(command)
    )

    async def make(check: str = CHECK_OK) -> Harness:
        run = tmp_path / "run"
        run.mkdir(exist_ok=True)
        (run / "runtime.json").write_text("{}")
        entrypoint = tmp_path / "entrypoint.py"
        entrypoint.write_text(check)
        harness = Harness(
            tmp_path / "home",
            restart=RestartControl(
                runtime_file=run / "runtime.json",
                python=sys.executable,
                entrypoint=entrypoint,
            ),
        )
        harness.launches = launches
        return harness

    return make


async def test_restart_in_a_checkout_is_unavailable(harness):
    response = await harness.http.post("/settings/restart", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "restart_unavailable"
    view = (await harness.http.get("/settings")).json()
    assert view["restart"] == {
        "available": False,
        "state": "idle",
        "error": None,
        "updated_at": None,
    }


async def test_restart_is_accepted_and_launched_after_the_response(image_harness):
    harness = await image_harness()
    try:
        view = (await harness.http.get("/settings")).json()
        assert (view["restart"]["available"], view["restart"]["state"]) == (
            True,
            "idle",
        )
        response = await harness.http.post("/settings/restart", json={})
        assert response.status_code == 202, response.text
        restart = response.json()["restart"]
        assert (restart["available"], restart["state"]) == (True, "requested")
        assert harness.launches == [
            [sys.executable, str(harness.restart.entrypoint), "restart"]
        ]
        view = (await harness.http.get("/settings")).json()
        assert view["restart"]["state"] == "requested"

        second = await harness.http.post("/settings/restart", json={})
        assert second.status_code == 409
        assert second.json()["code"] == "restart_in_progress"
        assert len(harness.launches) == 1
    finally:
        await harness.aclose()


@pytest.mark.parametrize(
    "request_options",
    [
        {},
        {"content": b"{}"},
        {"data": {"x": "1"}},
        {"content": b"{}", "headers": {"content-type": "text/plain"}},
    ],
)
async def test_restart_needs_a_json_request(image_harness, request_options):
    # A cross-site page can send these without a CORS preflight.
    harness = await image_harness()
    try:
        response = await harness.http.post("/settings/restart", **request_options)
        assert response.status_code == 415
        assert response.json()["code"] == "json_required"
        assert harness.launches == []
    finally:
        await harness.aclose()


async def test_restart_with_an_invalid_config_is_a_conflict(image_harness):
    harness = await image_harness(CHECK_INVALID)
    try:
        response = await harness.http.post("/settings/restart", json={})
        assert response.status_code == 409
        body = response.json()
        assert (body["code"], body["detail"]) == (
            "config_invalid",
            "Invalid configuration: bad",
        )
        assert harness.launches == []
    finally:
        await harness.aclose()
