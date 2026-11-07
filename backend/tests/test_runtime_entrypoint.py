"""Single-image entrypoint: listener rule, projections, generated configs, MongoDB."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest
from pymongo.errors import OperationFailure

import runtime_entrypoint as entry
from common.config.loader import FrontendSettings
from diagnostics import read_token

REPO_MANIFEST = Path(__file__).resolve().parents[2] / "default_agents" / "agents.yaml"
AGENTS = (
    entry.BundledProcess("echo_agent", "echo_agent", 7004),
    entry.BundledProcess("weather_agent", "weather_agent", 7001),
)
SECRETS = {
    "default_agent_llm_token": "t" * 64,
    "clerk_secret_key": "sk_test_secret",
    "clerk_webhook_secret": "whsec_secret",
}


def loopback(port: int = 3000) -> entry.Listener:
    return entry.Listener(mode="loopback", public_port=port, insecure_no_auth=False)


def public(*, insecure: bool = False) -> entry.Listener:
    return entry.Listener(mode="public", public_port=443, insecure_no_auth=insecure)


def build(listener: entry.Listener, *, frontend=None, embedded_mongo: bool = True):
    return entry.projections(
        listener=listener,
        api_prefix="/api/v1",
        frontend=frontend or {},
        image_size="1024x1024",
        secrets=SECRETS,
        agents=AGENTS,
        embedded_mongo=embedded_mongo,
    )


# ---- Listener ----


def test_missing_listen_variable_means_public():
    listener = entry.read_listener({})
    assert listener == entry.Listener("public", 8080, False)


def test_listener_reads_the_transport_variables():
    listener = entry.read_listener(
        {
            "HYBRO_LISTEN": "loopback",
            "HYBRO_PUBLIC_PORT": "3100",
            "HYBRO_INSECURE_NO_AUTH": "1",
        }
    )
    assert listener == entry.Listener("loopback", 3100, True)


@pytest.mark.parametrize(
    "environment",
    [
        {"HYBRO_LISTEN": "everywhere"},
        {"HYBRO_PUBLIC_PORT": "0"},
        {"HYBRO_PUBLIC_PORT": "70000"},
        {"HYBRO_PUBLIC_PORT": "abc"},
    ],
)
def test_invalid_transport_variables_stop_the_container(environment):
    with pytest.raises(entry.EntrypointError):
        entry.read_listener(environment)


def test_loopback_listener_needs_no_authentication():
    entry.check_listener(loopback(), auth_mode="mock", server_url=None)


def test_public_listener_with_clerk_and_server_url_starts():
    entry.check_listener(
        public(), auth_mode="clerk", server_url="https://hybro.example"
    )


@pytest.mark.parametrize(
    ("auth_mode", "server_url"),
    [("mock", None), ("mock", "https://hybro.example"), ("clerk", None)],
)
def test_public_listener_without_clerk_and_origin_is_refused(auth_mode, server_url):
    with pytest.raises(entry.EntrypointError, match="--insecure-no-auth"):
        entry.check_listener(public(), auth_mode=auth_mode, server_url=server_url)


def test_insecure_public_listener_starts_with_a_warning(capsys):
    entry.check_listener(public(insecure=True), auth_mode="mock", server_url=None)
    assert "without authentication" in capsys.readouterr().err


# ---- Projections ----


def test_loopback_projection_is_same_origin_with_mcp_enabled():
    programs = build(loopback(3100))
    frontend = json.loads(programs["frontend"].env["HYBRO_FRONTEND_CONFIG"])
    assert frontend["api_base_url"] == ""
    assert frontend["server_url"] == "http://localhost:3100"
    assert "mcp_url" not in frontend
    assert frontend["mcp_disabled"] is False
    # The projection still satisfies the public schema it is validated against.
    FrontendSettings.model_validate(
        {k: v for k, v in frontend.items() if k != "mcp_disabled"}
    )
    backend = programs["backend"].env
    assert backend["HYBRO_FRONTEND_ORIGINS"] == (
        "http://localhost:3100,http://127.0.0.1:3100"
    )
    assert backend["LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS"] == "7004,7001"
    assert programs["backend"].command[-8:-2] == [
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--workers",
        "1",
    ]


def test_public_projection_uses_the_configured_origin_and_disables_mcp():
    programs = build(
        public(),
        frontend={
            "server_url": "https://hybro.example",
            "api_base_url": "https://ignored.example",
        },
    )
    frontend = json.loads(programs["frontend"].env["HYBRO_FRONTEND_CONFIG"])
    assert frontend["api_base_url"] == ""
    assert frontend["server_url"] == "https://hybro.example"
    assert frontend["mcp_disabled"] is True
    assert programs["backend"].env["HYBRO_FRONTEND_ORIGINS"] == "https://hybro.example"


def test_each_program_sees_only_its_own_credentials():
    programs = build(loopback())
    assert programs["frontend"].env["CLERK_SECRET_KEY"] == "sk_test_secret"
    for name, program in programs.items():
        if name != "frontend":
            assert "sk_test_secret" not in json.dumps(program.env), name
        if not name.startswith("agent-"):
            assert SECRETS["default_agent_llm_token"] not in json.dumps(program.env)
    agent = programs["agent-echo_agent"]
    assert json.loads(agent.env["HYBRO_AGENT_CONFIG"]) == {
        "base_url": "http://127.0.0.1:8000/api/v1/internal/llm",
        "token": SECRETS["default_agent_llm_token"],
        "image_size": "1024x1024",
    }


def test_agents_run_their_file_from_their_own_directory_on_loopback():
    agent = build(loopback())["agent-weather_agent"]
    assert agent.command[1] == "/app/default_agents/weather_agent/__main__.py"
    assert "-m" not in agent.command
    assert agent.cwd == "/app/default_agents/weather_agent"
    assert agent.env["SERVER_HOST"] == "127.0.0.1"
    assert agent.env["SERVER_DOMAIN"] == "127.0.0.1"
    assert agent.env["SERVER_PORT"] == "7001"
    assert agent.env["PYTHONPATH"] == "/app/default_agents"


def test_mcp_runs_in_image_mode_with_the_api_prefix():
    mcp = build(loopback())["mcp"]
    assert mcp.command[1:] == ["server.py", "--image"]
    assert json.loads(mcp.env["HYBRO_MCP_CONFIG"]) == {"api_prefix": "/api/v1"}
    # The server imports only the backend's logging modules from this path.
    assert mcp.env["PYTHONPATH"] == "/app/backend"
    assert set(mcp.env) == {"HYBRO_MCP_CONFIG", "PYTHONPATH"}


def test_configured_mongodb_skips_the_embedded_server():
    assert "mongod" in build(loopback())
    assert "mongod" not in build(loopback(), embedded_mongo=False)


def test_manifest_processes_carry_each_agent_directory():
    processes = entry.manifest_processes(REPO_MANIFEST)
    assert processes[0] == entry.BundledProcess("echo_agent", "echo_agent", 7004)
    assert {process.directory for process in processes} == {
        "echo_agent",
        "weather_agent",
        "image_generator_agent",
        "travel_planner_agent",
        "story_agent",
    }


# ---- Generated configuration ----


def test_backend_bounds_its_graceful_shutdown():
    command = build(loopback()).get("backend").command
    index = command.index("--timeout-graceful-shutdown")
    assert command[index + 1] == str(entry.BACKEND_GRACEFUL_SHUTDOWN_SECONDS)


@pytest.mark.parametrize("listener", [loopback(), public(insecure=True)])
def test_caddy_bounds_its_shutdown_grace_period(listener):
    text = entry.caddyfile(listener=listener, api_prefix="/api/v1")
    global_options = text.split("\n}\n", 1)[0]
    assert "\tgrace_period 3s" in global_options.splitlines()


def test_loopback_caddyfile_checks_the_host_and_serves_mcp():
    text = entry.caddyfile(listener=loopback(), api_prefix="/custom/v2")
    assert "@foreign not host localhost 127.0.0.1 ::1" in text
    # The host check is the first handle block, so it wins over every route.
    assert text.index("handle @foreign {\n\t\trespond 421") < text.index(
        "handle @backend"
    )
    assert "@backend path /custom/v2 /custom/v2/* /health" in text
    assert "@mcp path /mcp /mcp/*" in text
    assert ":8080 {" in text


def test_public_caddyfile_omits_mcp_and_the_host_check():
    text = entry.caddyfile(listener=public(), api_prefix="/api/v1")
    assert "/mcp" not in text
    assert "@foreign" not in text
    assert "reverse_proxy 127.0.0.1:3000" in text


@pytest.mark.parametrize("listener", [loopback, public])
def test_caddyfile_logs_json_without_headers_or_query(listener):
    text = entry.caddyfile(listener=listener(), api_prefix="/api/v1")
    site = text[text.index(":8080 {") :]
    expected = "\n".join(
        [
            ":8080 {",
            "\tlog {",
            "\t\toutput stdout",
            "\t\tformat filter {",
            "\t\t\twrap json",
            "\t\t\tfields {",
            "\t\t\t\trequest>headers delete",
            "\t\t\t\trequest>uri delete",
            "\t\t\t\tresp_headers delete",
            "\t\t\t}",
            "\t\t}",
            "\t}",
            "\tlog_append trace_id {http.response.header.X-Request-ID}",
            "\tlog_append path {http.request.uri.path}",
            "\t@static path /_next/static/*",
            "\tlog_skip @static",
        ]
    )
    assert site.startswith(expected)
    assert "log_credentials" not in text


@pytest.mark.parametrize("listener", [loopback, public])
def test_caddyfile_filters_the_default_logger(listener):
    # Caddy's error logger (5xx lines) carries the request without the
    # access log's filter; it writes to the default logger. Response headers
    # are dropped too, should an access line ever reach it.
    text = entry.caddyfile(listener=listener(), api_prefix="/api/v1")
    options = text[: text.index("\n}\n") + 3]
    expected = "\n".join(
        [
            "\tlog default {",
            "\t\toutput stderr",
            "\t\tformat filter {",
            "\t\t\twrap json",
            "\t\t\tfields {",
            "\t\t\t\trequest>headers delete",
            "\t\t\t\trequest>uri delete",
            "\t\t\t\tresp_headers delete",
            "\t\t\t}",
            "\t\t}",
            "\t}",
        ]
    )
    assert expected in options


def test_supervisor_starts_mongod_first_and_stops_services_together():
    conf = entry.supervisord_conf(build(loopback()))
    sections = conf.split("\n\n")
    mongod = next(s for s in sections if s.startswith("[program:mongod]"))
    assert "priority=10\n" in mongod + "\n"
    assert "stopwaitsecs=30" in mongod
    backend = next(s for s in sections if s.startswith("[program:backend]"))
    assert "stopwaitsecs=15" in backend
    assert "runtime_entrypoint.py run backend" in backend
    group = next(s for s in sections if s.startswith("[group:services]"))
    assert "mongod" not in group
    assert "agent-echo_agent" in group


def test_the_stop_budget_ends_streams_before_any_kill():
    from delivery.config import DeliveryConfig

    drain = DeliveryConfig().shutdown_drain_seconds
    # Caddy closes proxied streams before uvicorn would cancel them, and the
    # backend's whole shutdown fits in its stop wait.
    assert entry.CADDY_GRACE_SECONDS < entry.BACKEND_GRACEFUL_SHUTDOWN_SECONDS
    assert entry.BACKEND_GRACEFUL_SHUTDOWN_SECONDS + drain < (
        entry.SERVICE_STOP_WAIT_SECONDS
    )
    # scripts/hybro runs the container with --stop-timeout 60.
    cli = (Path(__file__).resolve().parents[2] / "scripts" / "hybro").read_text()
    assert "--stop-timeout 60" in cli
    assert entry.SERVICE_STOP_WAIT_SECONDS + entry.MONGOD_STOP_WAIT_SECONDS < 60


def test_private_files_are_owner_only(tmp_path):
    target = tmp_path / "programs" / "backend.json"
    entry.write_private(target, "{}")
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


# ---- Identity ----


def test_service_identity_cases():
    assert entry.service_identity(0) == "root"
    assert entry.service_identity(os.getuid()) == "existing"
    assert entry.service_identity(54_321_987) == "create"


# ---- MongoDB replica set ----


class FakeAdmin:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def command(self, name, value=None, **kwargs):
        self.calls.append((name, value, kwargs))
        if name == "replSetGetConfig":
            if self.config is None:
                raise OperationFailure("no replset config", code=94)
            return {"config": self.config}
        return {"ok": 1}


class FakeClient:
    def __init__(self, config):
        self.admin = FakeAdmin(config)


def test_fresh_volume_initiates_the_replica_set():
    client = FakeClient(None)
    assert entry.ensure_replica_set(client) == "initiated"
    assert client.admin.calls[-1] == (
        "replSetInitiate",
        {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]},
        {},
    )


def test_matching_config_is_left_alone():
    config = {
        "_id": "rs0",
        "version": 1,
        "members": [{"_id": 0, "host": "127.0.0.1:27017"}],
    }
    client = FakeClient(config)
    assert entry.ensure_replica_set(client) == "ready"
    assert [call[0] for call in client.admin.calls] == ["replSetGetConfig"]


def test_migrated_data_is_reconfigured_to_loopback():
    config = {
        "_id": "rs0",
        "version": 3,
        "members": [{"_id": 0, "host": "mongo:27017", "priority": 1}],
    }
    client = FakeClient(config)
    assert entry.ensure_replica_set(client) == "reconfigured"
    name, value, kwargs = client.admin.calls[-1]
    assert name == "replSetReconfig"
    assert kwargs == {"force": True}
    assert value["members"] == [{"_id": 0, "host": "127.0.0.1:27017", "priority": 1}]
    assert value["version"] == 4


def test_other_replica_set_errors_propagate():
    class Broken(FakeAdmin):
        def command(self, name, value=None, **kwargs):
            raise OperationFailure("unauthorized", code=13)

    client = FakeClient(None)
    client.admin = Broken(None)
    with pytest.raises(OperationFailure):
        entry.ensure_replica_set(client)


def test_wait_for_primary_times_out():
    class NeverPrimary:
        class admin:  # noqa: N801 - mimics the pymongo attribute
            @staticmethod
            def command(name):
                return {"isWritablePrimary": False}

    with pytest.raises(entry.EntrypointError, match="primary"):
        entry.wait_for_primary(NeverPrimary(), timeout=0.01)


def test_only_the_backend_learns_where_the_config_directory_is():
    programs = build(loopback())
    backend = programs.pop("backend")
    assert backend.env["HYBRO_HOME"] == "/var/lib/hybro/runtime"
    assert backend.env["HYBRO_CONTAINER"] == "image"
    for name, program in programs.items():
        assert "HYBRO_HOME" not in program.env, name
        assert "HYBRO_CONTAINER" not in program.env, name
    assert "HYBRO_HOME" not in entry._BASE_ENV_KEYS


def test_supervised_launchers_are_killed_with_their_children():
    conf = entry.supervisord_conf(build(loopback()))
    programs = [s for s in conf.split("\n\n") if s.startswith("[program:")]
    assert programs and all("killasgroup=true" in s for s in programs)


# ---- MongoDB preparation failures ----


class FakeProcess:
    def __init__(self, exit_after: int | None = None, code: int = 62):
        self.exit_after = exit_after
        self.code = code
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.terminated:
            return -15
        if self.exit_after is not None and self.polls > self.exit_after:
            return self.code
        return None

    def wait(self, timeout=None):
        code = self.poll()
        # A running fake stops cleanly when waited for, as mongod does after
        # the shutdown command.
        return 0 if code is None else code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def refusing_client(_timeout_ms):
    from pymongo.errors import ServerSelectionTimeoutError

    class Admin:
        def command(self, name, *args, **kwargs):
            raise ServerSelectionTimeoutError("connection refused")

    class Client:
        admin = Admin()

        def close(self):
            pass

    return Client()


@pytest.fixture
def mongo_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "DATA_DIR", tmp_path / "data")
    binary = tmp_path / "mongod"
    binary.write_bytes(b"mongod 7.0")
    monkeypatch.setattr(entry, "MONGOD_BINARY", binary)
    return tmp_path / "mongod-prepare.log"


def test_mongod_exiting_early_fails_fast_with_its_output(mongo_paths, monkeypatch):
    monkeypatch.setattr(entry.time, "sleep", lambda _s: None)
    process = FakeProcess(exit_after=1)

    def start(command, *, stdout, stderr):
        stdout.write(b'{"msg":"Wrong mongod version","attr":{"fcv":"8.0"}}\n')
        return process

    with pytest.raises(entry.EntrypointError) as error:
        entry.prepare_mongodb(start=start, connect=refusing_client, log=mongo_paths)
    message = str(error.value)
    assert "mongod exited with code 62" in message
    assert "Wrong mongod version" in message


def test_mongod_never_answering_times_out_and_is_stopped(mongo_paths, monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(entry.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(entry.time, "sleep", lambda _s: None)
    process = FakeProcess()
    with pytest.raises(entry.EntrypointError, match="did not accept connections"):
        entry.prepare_mongodb(
            start=lambda *a, **k: process, connect=refusing_client, log=mongo_paths
        )
    assert process.terminated


def test_driver_errors_become_entrypoint_errors(mongo_paths):
    from pymongo.errors import OperationFailure

    class Admin:
        def command(self, name, *args, **kwargs):
            if name == "ping":
                return {"ok": 1}
            raise OperationFailure("not authorized", code=13)

    class Client:
        admin = Admin()

        def __init__(self, timeout_ms):
            timeouts.append(timeout_ms)

        def close(self):
            pass

    timeouts = []
    process = FakeProcess()
    with pytest.raises(entry.EntrypointError, match="OperationFailure"):
        entry.prepare_mongodb(
            start=lambda *a, **k: process, connect=Client, log=mongo_paths
        )
    assert process.terminated
    assert not entry.replica_set_recorded()


def test_stop_signal_during_preparation_is_a_clean_failure():
    with pytest.raises(entry.EntrypointError, match="Stopped"):
        entry._stop_preparation(15, None)


def test_successful_preparation_shuts_the_temporary_mongod_down(mongo_paths, capsys):
    calls = []

    class Admin:
        def command(self, name, *args, **kwargs):
            calls.append(name)
            if name == "replSetGetConfig":
                return {
                    "config": {
                        "_id": "rs0",
                        "version": 1,
                        "members": [{"_id": 0, "host": "127.0.0.1:27017"}],
                    }
                }
            if name == "hello":
                return {"isWritablePrimary": True}
            return {"ok": 1}

    class Client:
        admin = Admin()

        def __init__(self, timeout_ms):
            timeouts.append(timeout_ms)

        def close(self):
            pass

    timeouts = []
    process = FakeProcess()
    entry.prepare_mongodb(
        start=lambda *a, **k: process, connect=Client, log=mongo_paths
    )
    assert calls == ["ping", "replSetGetConfig", "hello", "shutdown"]
    # A short probe, then a patient client for the reconfig and the election.
    assert timeouts == [entry.PROBE_TIMEOUT_MS, 60_000]
    assert not process.terminated
    assert "replica set rs0: ready" in capsys.readouterr().err
    # Recorded once mongod has stopped cleanly, so the next start can skip this.
    assert entry.replica_set_recorded()


def ready_client(_timeout_ms):
    """A client for a primary whose replica set already names loopback."""

    class Admin:
        def command(self, name, *args, **kwargs):
            if name == "replSetGetConfig":
                return {
                    "config": {
                        "_id": "rs0",
                        "version": 1,
                        "members": [{"_id": 0, "host": "127.0.0.1:27017"}],
                    }
                }
            if name == "hello":
                return {"isWritablePrimary": True}
            return {"ok": 1}

    class Client:
        admin = Admin()

        def close(self):
            pass

    return Client()


def test_the_record_is_written_only_after_mongod_has_stopped(mongo_paths):
    seen_at_wait = []

    class Stopping(FakeProcess):
        def wait(self, timeout=None):
            seen_at_wait.append(entry.replica_set_recorded())
            return super().wait(timeout)

    entry.prepare_mongodb(
        start=lambda *a, **k: Stopping(), connect=ready_client, log=mongo_paths
    )
    assert seen_at_wait == [False]
    assert entry.replica_set_recorded()


def test_an_unclean_mongod_stop_is_not_recorded(mongo_paths):
    class Unclean(FakeProcess):
        def wait(self, timeout=None):
            return 14

    entry.prepare_mongodb(
        start=lambda *a, **k: Unclean(), connect=ready_client, log=mongo_paths
    )
    assert not entry.replica_set_recorded()


def test_a_mongod_that_does_not_stop_is_not_recorded(mongo_paths):
    import subprocess

    class Hanging(FakeProcess):
        def wait(self, timeout=None):
            if not self.terminated:
                raise subprocess.TimeoutExpired("mongod", timeout or 0)
            return -15

    with pytest.raises(entry.EntrypointError, match="TimeoutExpired"):
        entry.prepare_mongodb(
            start=lambda *a, **k: Hanging(), connect=ready_client, log=mongo_paths
        )
    assert not entry.replica_set_recorded()


def test_inspect_runs_on_the_primary_and_its_result_is_returned(mongo_paths):
    class Admin:
        def command(self, name, *args, **kwargs):
            if name == "replSetGetConfig":
                return {
                    "config": {
                        "_id": "rs0",
                        "version": 1,
                        "members": [{"_id": 0, "host": "127.0.0.1:27017"}],
                    }
                }
            if name == "hello":
                return {"isWritablePrimary": True}
            return {"ok": 1}

    class Client:
        admin = Admin()

        def __init__(self, timeout_ms):
            pass

        def close(self):
            pass

    seen = []
    result = entry.prepare_mongodb(
        start=lambda *a, **k: FakeProcess(),
        connect=Client,
        log=mongo_paths,
        inspect=lambda client: seen.append(client) or {"rooms": 1},
    )
    assert result == {"rooms": 1}
    assert len(seen) == 1


def test_volume_marker_is_written_atomically(tmp_path):
    marker = tmp_path / "hybro-volume.json"
    entry.mark_volume_complete({"created": "fresh"}, marker)
    assert json.loads(marker.read_text()) == {"complete": True, "created": "fresh"}
    assert [path.name for path in tmp_path.iterdir()] == ["hybro-volume.json"]


def test_user_phase_marks_a_fresh_volume_and_keeps_an_existing_marker(
    tmp_path, monkeypatch
):
    marker = tmp_path / "hybro-volume.json"
    monkeypatch.setattr(entry, "VOLUME_MARKER", marker)
    monkeypatch.setattr(entry, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(entry, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(entry, "MANIFEST", REPO_MANIFEST)
    monkeypatch.setattr(entry, "prepare_mongodb", lambda: None)
    monkeypatch.setattr(entry.os, "execvpe", lambda *a: None)
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("HYBRO_LISTEN", "loopback")
    monkeypatch.setenv("HYBRO_PUBLIC_PORT", "3000")
    entry.user_phase()
    assert json.loads(marker.read_text())["created"] == "fresh"
    token = json.loads((tmp_path / "run" / "diagnostics.json").read_text())["token"]
    for program in (tmp_path / "run" / "programs").glob("*.json"):
        text = program.read_text()
        assert token not in text
        assert "diagnostics.json" not in text
    marker.write_text('{"complete": true, "migrated_from": "release"}')
    entry.user_phase()
    assert json.loads(marker.read_text())["migrated_from"] == "release"


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """user_phase with the image paths in tmp_path; returns the preparations run."""
    monkeypatch.setattr(entry, "DATA_DIR", tmp_path / "data")
    binary = tmp_path / "mongod"
    binary.write_bytes(b"mongod 7.0")
    monkeypatch.setattr(entry, "MONGOD_BINARY", binary)
    monkeypatch.setattr(entry, "VOLUME_MARKER", tmp_path / "data" / "hybro-volume.json")
    monkeypatch.setattr(entry, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(entry, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(entry, "MANIFEST", REPO_MANIFEST)
    monkeypatch.setattr(entry.os, "execvpe", lambda *a: None)
    (tmp_path / "runtime").mkdir(mode=0o700)
    (tmp_path / "data" / "db").mkdir(parents=True)
    monkeypatch.setenv("HYBRO_LISTEN", "loopback")
    monkeypatch.setenv("HYBRO_PUBLIC_PORT", "3000")
    prepared = []
    monkeypatch.setattr(entry, "prepare_mongodb", lambda: prepared.append(True))
    return prepared


def test_recorded_replica_set_skips_the_temporary_mongod(boot, capsys):
    entry.record_replica_set()
    entry.user_phase()
    assert boot == []
    assert "replica set rs0: ready (recorded)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "record",
    [
        None,
        "not json",
        json.dumps({"replica_set": "rs0", "host": "mongo:27017"}),
        json.dumps({"replica_set": "other", "host": "127.0.0.1:27017"}),
    ],
)
def test_missing_or_foreign_record_prepares_the_replica_set(boot, record):
    if record is not None:
        (entry.DATA_DIR / "db" / "hybro-replica-set.json").write_text(record)
    entry.user_phase()
    assert boot == [True]


@pytest.mark.parametrize(
    ("field", "value"), [("host", "mongo:27017"), ("replica_set", "other")]
)
def test_a_record_for_another_replica_set_prepares_again(boot, field, value):
    record = {**entry._expected_record(), field: value}
    (entry.DATA_DIR / "db" / "hybro-replica-set.json").write_text(json.dumps(record))
    entry.user_phase()
    assert boot == [True]


def test_an_unreadable_mongod_binary_is_never_recorded(boot, capsys):
    entry.record_replica_set()
    entry.MONGOD_BINARY.unlink()
    assert not entry.replica_set_recorded()
    (entry.DATA_DIR / "db" / "hybro-replica-set.json").unlink()
    entry.record_replica_set()
    assert list((entry.DATA_DIR / "db").iterdir()) == []
    assert "cannot record the replica set" in capsys.readouterr().err


def test_another_mongod_binary_prepares_again(boot):
    entry.record_replica_set()
    entry.MONGOD_BINARY.write_bytes(b"mongod 8.0, a different build")
    entry.user_phase()
    assert boot == [True]


def test_a_failed_record_write_warns_and_leaves_no_partial_file(
    mongo_paths, monkeypatch, capsys
):
    (entry.DATA_DIR / "db").mkdir(parents=True)

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(entry.os, "replace", full_disk)
    entry.record_replica_set()
    assert not entry.replica_set_recorded()
    assert list((entry.DATA_DIR / "db").iterdir()) == []
    assert "cannot record the replica set" in capsys.readouterr().err


def test_diagnostics_handoff_is_private_and_fresh_per_start(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "RUN_DIR", tmp_path / "run")

    entry.write_diagnostics_handoff("/api/v9")
    path = tmp_path / "run" / "diagnostics.json"
    first = json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    assert first["base_url"] == "http://127.0.0.1:8000/api/v9/diagnostics"
    assert read_token(str(path)) == first["token"]

    entry.write_diagnostics_handoff("/api/v9")
    assert json.loads(path.read_text())["token"] != first["token"]


def test_diagnostics_handoff_replaces_a_loosened_file(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "RUN_DIR", tmp_path / "run")
    (tmp_path / "run").mkdir(mode=0o700)
    path = tmp_path / "run" / "diagnostics.json"
    path.write_text("{}")
    path.chmod(0o644)
    stale = tmp_path / "run" / ".diagnostics.json.new"
    stale.write_text("{}")
    stale.chmod(0o644)

    entry.write_diagnostics_handoff("/api/v1")

    assert path.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(path.read_text())["token"]) == 64
    assert [p.name for p in (tmp_path / "run").iterdir()] == ["diagnostics.json"]


# ---- Runtime plan and the restart helper ----


@pytest.fixture
def started(boot, tmp_path, monkeypatch):
    """A completed user_phase start; returns the run directory."""
    from common.config.loader import get_settings

    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "runtime"))
    monkeypatch.setattr(entry, "PID1_STDOUT", tmp_path / "pid1.log")
    get_settings.cache_clear()
    entry.user_phase()
    get_settings.cache_clear()
    yield tmp_path / "run"
    get_settings.cache_clear()


def write_runtime_config(document: dict) -> None:
    from common.config.loader import get_settings

    (entry.RUNTIME_DIR / "config.json").write_text(json.dumps(document))
    get_settings.cache_clear()


def program_files(run: Path) -> dict[str, str]:
    return {p.stem: p.read_text() for p in (run / "programs").glob("*.json")}


def restart_status(run: Path) -> dict:
    return json.loads((run / "restart.json").read_text())


class FakeSupervisor:
    """Answers supervisorctl restart and status; states default to RUNNING."""

    def __init__(self, states: dict[str, str] | None = None, *, exit_code: int = 0):
        self.states = states or {}
        self.exit_code = exit_code
        self.calls: list[list[str]] = []
        self.seen_status: list[dict] = []

    def __call__(self, command, *, timeout):
        import subprocess

        self.calls.append(command)
        with contextlib.suppress(OSError):
            self.seen_status.append(restart_status(entry.RUN_DIR))
        action, names = command[3], command[4:]
        if action == "status":
            lines = [
                f"{name:<32} {self.states.get(name, 'RUNNING'):<10} pid 1, uptime 0:00:03"
                for name in names
            ]
            return subprocess.CompletedProcess(command, 0, "\n".join(lines) + "\n", "")
        return subprocess.CompletedProcess(command, self.exit_code, "", "")


SERVICES = [
    "services:backend",
    "services:mcp",
    "services:frontend",
    "services:agent-echo_agent",
    "services:agent-weather_agent",
    "services:agent-image_generator_agent",
    "services:agent-travel_planner_agent",
    "services:agent-story_agent",
]


def test_plan_runtime_matches_the_user_phase_outputs(started):
    from common.config.runtime_store import RuntimeConfigStore

    plan = entry.plan_runtime(RuntimeConfigStore(entry.RUNTIME_DIR), loopback(3000))
    assert plan.api_prefix == "/api/v1"
    assert plan.embedded_mongo is True
    assert plan.caddyfile == (started / "Caddyfile").read_text()
    assert (
        entry.supervisord_conf(plan.programs)
        == (started / "supervisord.conf").read_text()
    )
    base = {k: os.environ[k] for k in entry._BASE_ENV_KEYS if k in os.environ}
    assert {
        name: entry.program_spec(program, base)
        for name, program in plan.programs.items()
    } == program_files(started)


def test_runtime_record_is_private_and_names_the_services_revision(started):
    from common.config.runtime_config import RuntimeConfig

    path = started / "runtime.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {
        "listen": "loopback",
        "public_port": 3000,
        "insecure_no_auth": False,
        "embedded_mongo": True,
        "services_revision": RuntimeConfig().restart_revision,
    }


def test_restart_matched_before_the_root_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry, "root_phase", lambda: calls.append("root"))
    monkeypatch.setattr(entry, "user_phase", lambda: calls.append("user"))
    monkeypatch.setattr(
        entry, "restart", lambda *, check: calls.append(("restart", check)) or 0
    )
    assert entry.main(["x", "restart", "--check"]) == 0
    assert entry.main(["x", "restart"]) == 0
    assert calls == [("restart", True), ("restart", False)]
    assert entry.main(["x", "restart", "--now"]) == 2
    assert calls == [("restart", True), ("restart", False)]


def test_restart_check_passes_without_side_effects(started, capsys):
    before = program_files(started)
    supervisor = FakeSupervisor()
    assert entry.restart(check=True, run=supervisor) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"ok": True}
    assert supervisor.calls == []
    assert program_files(started) == before
    assert not (started / "restart.json").exists()


def test_restart_check_does_not_write_auth_json(started):
    auth = entry.RUNTIME_DIR / "auth.json"
    auth.unlink()
    assert entry.restart(check=True, run=FakeSupervisor()) == 0
    assert not auth.exists()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{not json", "Invalid configuration"),
        ({"version": 1, "backend": {"api_prefix": "/api/v2"}}, "full restart"),
        (
            {"version": 1, "backend": {"mongodb_url": "mongodb://db:27017/x"}},
            "full restart",
        ),
    ],
)
def test_restart_check_refuses_what_a_service_restart_cannot_apply(
    started, capsys, document, message
):
    if isinstance(document, str):
        (entry.RUNTIME_DIR / "config.json").write_text(document)
    else:
        write_runtime_config(document)
    assert entry.restart(check=True, run=FakeSupervisor()) == 1
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["ok"] is False
    assert message in result["error"]
    assert not (started / "restart.json").exists()


def test_failed_restart_without_check_records_the_failure(started, capsys):
    write_runtime_config({"version": 1, "backend": {"api_prefix": "/api/v2"}})
    supervisor = FakeSupervisor()
    assert entry.restart(check=False, run=supervisor) == 1
    assert supervisor.calls == []
    status = restart_status(started)
    assert status["state"] == "failed"
    assert "hybro stop" in status["error"]
    assert "failed" in (entry.PID1_STDOUT).read_text()


def test_restart_rewrites_the_services_and_restarts_exactly_them(started):
    from common.config.runtime_config import RuntimeConfig

    before = program_files(started)
    write_runtime_config({"version": 1, "frontend": {"max_message_length": 9}})
    supervisor = FakeSupervisor()
    assert entry.restart(check=False, run=supervisor) == 0
    conf = str(started / "supervisord.conf")
    assert supervisor.calls == [
        ["supervisorctl", "-c", conf, "restart", *SERVICES],
        ["supervisorctl", "-c", conf, "status", *SERVICES],
    ]
    after = program_files(started)
    assert after["caddy"] == before["caddy"]
    assert after["mongod"] == before["mongod"]
    assert after["frontend"] != before["frontend"]
    assert '\\"max_message_length\\": 9' in after["frontend"]
    for path in (started / "programs").iterdir():
        assert path.suffix == ".json", path
        assert path.stat().st_mode & 0o777 == 0o600
    status = restart_status(started)
    assert (status["state"], status["error"]) == ("completed", None)
    assert supervisor.seen_status[0]["state"] == "restarting"
    record = json.loads((started / "runtime.json").read_text())
    assert (
        record["services_revision"]
        == RuntimeConfig.model_validate(
            {"frontend": {"max_message_length": 9}}
        ).restart_revision
    )
    assert record["listen"] == "loopback"
    assert "completed" in entry.PID1_STDOUT.read_text()


def test_a_service_not_running_after_restart_fails(started):
    revision = json.loads((started / "runtime.json").read_text())["services_revision"]
    write_runtime_config({"version": 1, "frontend": {"max_message_length": 9}})
    supervisor = FakeSupervisor({"services:frontend": "BACKOFF"})
    assert entry.restart(check=False, run=supervisor) == 1
    status = restart_status(started)
    assert status["state"] == "failed"
    assert "services:frontend" in status["error"]
    record = json.loads((started / "runtime.json").read_text())
    assert record["services_revision"] == revision


def test_a_failed_supervisorctl_restart_fails(started):
    assert entry.restart(check=False, run=FakeSupervisor(exit_code=7)) == 1
    assert restart_status(started)["state"] == "failed"


def test_a_busy_restart_lock_leaves_the_status_alone(started):
    import fcntl

    (started / "restart.json").write_text('{"state": "restarting"}')
    with open(started / "restart.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor = FakeSupervisor()
        assert entry.restart(check=False, run=supervisor) == 0
    assert supervisor.calls == []
    assert (started / "restart.json").read_text() == '{"state": "restarting"}'


def test_the_heartbeat_refreshes_updated_at_during_supervisorctl(started, monkeypatch):
    monkeypatch.setattr(entry, "RESTART_HEARTBEAT_SECONDS", 0.01)
    beats: list[float] = []

    def slow(command, *, timeout):
        first = restart_status(entry.RUN_DIR)["updated_at"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = restart_status(entry.RUN_DIR)
            if current["updated_at"] != first:
                beats.append(current["updated_at"])
                break
            time.sleep(0.005)
        return FakeSupervisor()(command, timeout=timeout)

    assert entry.restart(check=False, run=slow) == 0
    assert len(beats) == 2


def test_pid1_output_is_best_effort(started, monkeypatch):
    monkeypatch.setattr(entry, "PID1_STDOUT", started / "missing" / "fd")
    assert entry.restart(check=False, run=FakeSupervisor()) == 0
