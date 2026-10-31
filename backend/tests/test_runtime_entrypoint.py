"""Single-image entrypoint: listener rule, projections, generated configs, MongoDB."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pymongo.errors import OperationFailure

import runtime_entrypoint as entry
from common.config.loader import FrontendSettings

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
    assert programs["backend"].command[-6:] == [
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


def test_supervisor_starts_mongod_first_and_stops_services_together():
    conf = entry.supervisord_conf(build(loopback()))
    sections = conf.split("\n\n")
    mongod = next(s for s in sections if s.startswith("[program:mongod]"))
    assert "priority=10\n" in mongod + "\n"
    assert "stopwaitsecs=30" in mongod
    backend = next(s for s in sections if s.startswith("[program:backend]"))
    assert "stopwaitsecs=10" in backend
    assert "runtime_entrypoint.py run backend" in backend
    group = next(s for s in sections if s.startswith("[group:services]"))
    assert "mongod" not in group
    assert "agent-echo_agent" in group


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
        return self.poll()

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
    marker.write_text('{"complete": true, "migrated_from": "release"}')
    entry.user_phase()
    assert json.loads(marker.read_text())["migrated_from"] == "release"
