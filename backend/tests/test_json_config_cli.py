"""CLI checks use fake subprocesses and synthetic JSON credentials only."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import configuration_cli as cli
from common.config.runtime_store import RuntimeConfigStore
from tests.test_json_runtime_config import configured


def test_docker_problem_distinguishes_the_three_failure_modes(monkeypatch):
    """A missing binary, a stopped daemon, and a missing plugin need different fixes."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.docker_problem() == "Docker is not installed or not on PATH."

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")
    replies = {
        "compose": SimpleNamespace(returncode=1),
        "info": SimpleNamespace(returncode=0),
    }
    monkeypatch.setattr(
        cli.subprocess, "run", lambda arguments, **kwargs: replies[arguments[1]]
    )
    assert "Compose plugin" in cli.docker_problem()

    replies["compose"] = SimpleNamespace(returncode=0)
    replies["info"] = SimpleNamespace(returncode=1)
    assert "daemon is not running" in cli.docker_problem()

    replies["info"] = SimpleNamespace(returncode=0)
    assert cli.docker_problem() is None


def test_docker_problem_reports_a_non_responding_docker(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda arguments, **kwargs: (_ for _ in ()).throw(OSError("exec failed")),
    )
    assert (
        cli.docker_problem() == "Docker did not respond; check the Docker installation."
    )


def test_start_without_setup_does_not_invoke_docker(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "absent"))
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: pytest.fail("unexpected process")
    )
    assert cli.main(["start"]) == 1
    assert "hybro setup" in capsys.readouterr().err


def test_start_projects_only_json_configuration(monkeypatch, tmp_path):
    runtime = configured(tmp_path)
    runtime.update_config(["frontend", "max_message_length"], 5432)
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("NEXT_PUBLIC_PRIVATE_VALUE", "ambient-secret")
    monkeypatch.setenv("HYBRO_AGENT_CONFIG", "ambient-secret")
    monkeypatch.setenv("HYBRO_MCP_CONFIG", "ambient-secret")
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["start", "--build", "--recreate"]) == 0
    arguments, options = calls[-1]
    assert arguments[:4] == ["docker", "compose", "--env-file", "/dev/null"]
    assert "--build" in arguments and "--force-recreate" in arguments
    env = options["env"]
    assert "ambient-secret" not in json.dumps(env)
    public = json.loads(env["HYBRO_FRONTEND_CONFIG"])
    assert public["max_message_length"] == 5432
    assert "fixture-key" not in env["HYBRO_FRONTEND_CONFIG"]
    agent = json.loads(env["HYBRO_AGENT_CONFIG"])
    assert set(agent) == {"base_url", "token", "image_size"}
    assert (
        agent["token"] == runtime.read_service_credentials()["default_agent_llm_token"]
    )
    assert "fixture-key" not in json.dumps(agent)
    assert json.loads(env["HYBRO_MCP_CONFIG"]) == {"api_prefix": "/api/v1"}
    assert not (tmp_path / ".env").exists()


@pytest.fixture
def released_install(monkeypatch):
    """Simulate an installed CLI: bundled data files, no source checkout.

    A real install resolves those from the frozen bundle's sys._MEIPASS; the
    repository root carries the same files, so pointing there exercises the same
    code path without a build.
    """
    import cli_stack

    repo = Path(cli_stack.__file__).resolve().parents[1]
    monkeypatch.setattr(cli_stack, "_frozen_root", lambda: repo)
    monkeypatch.setattr(cli_stack, "checkout_root", lambda: None)
    return cli_stack


def test_released_install_runs_the_published_stack(released_install):
    """Without a checkout the CLI runs the bundled release stack, not a local build."""
    stack = released_install.resolve()

    assert stack.checkout is False
    assert stack.compose.name == released_install.RELEASE_COMPOSE
    assert stack.compose.is_file()
    assert (
        released_install.version()
        == released_install.data_file(released_install.VERSION)
        .read_text(encoding="utf-8")
        .strip()
    )


def test_checkout_install_runs_its_own_stack():
    """A source checkout keeps running the development stack it can rebuild."""
    stack = cli.resolve()

    assert stack.checkout is True
    assert stack.compose.name == "docker-compose.yml"


def test_released_install_rejects_build_and_the_baked_routing_prefix(
    monkeypatch, tmp_path, capsys, released_install
):
    """A released install has no sources and a frontend image with baked routing."""
    runtime = configured(tmp_path)
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: pytest.fail("unexpected")
    )

    assert cli.main(["start", "--build"]) == 1
    assert "no source checkout" in capsys.readouterr().err

    # The API prefix is the one setting the published image cannot follow.
    assert cli.main(["config", "set", "backend.api_prefix", '"/v2"']) == 1
    assert "built into the published frontend image" in capsys.readouterr().err
    assert runtime.read_config().backend.get("api_prefix") is None

    # Everything else applies on recreate, at runtime, without a rebuild.
    assert cli.main(["config", "set", "frontend.max_message_length", "1234"]) == 0
    assert "no rebuild" in capsys.readouterr().out
    assert cli.main(["config", "set", "backend.log_level", '"DEBUG"']) == 0
    config = runtime.read_config()
    assert config.frontend["max_message_length"] == 1234
    assert config.backend["log_level"] == "DEBUG"


def test_extra_compose_files_must_exist_beside_the_stack():
    stack = cli.resolve()
    assert cli.compose_files(stack) == [str(stack.compose)]
    overlay = stack.root / "docker-compose.ci.yml"
    assert overlay.is_file()
    assert cli.compose_files(stack, ["docker-compose.ci.yml"]) == [
        str(stack.compose),
        str(overlay),
    ]
    for rejected in ("/etc/passwd", "../outside.yml", "docker-compose.missing.yml"):
        with pytest.raises(cli.RuntimeConfigurationError):
            cli.compose_files(stack, [rejected])


def test_start_forwards_explicit_compose_overlay(monkeypatch, tmp_path):
    runtime = configured(tmp_path)
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert (
        cli.main(["start", "--build", "--compose-file", "docker-compose.ci.yml"]) == 0
    )
    stack = cli.resolve()
    # The renderer runs in-process; Compose gets the stack file and the overlay.
    assert calls[-1][:8] == [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        str(stack.compose),
        "-f",
        str(stack.root / "docker-compose.ci.yml"),
    ]
    assert calls[-1][-4:] == ["up", "-d", "--remove-orphans", "--build"]


def test_config_set_and_show_do_not_expose_credentials(monkeypatch, tmp_path, capsys):
    runtime = configured(tmp_path)
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    assert cli.main(["config", "set", "backend.log_level", '"DEBUG"']) == 0
    assert runtime.read_config().backend["log_level"] == "DEBUG"
    assert cli.main(["config", "show"]) == 0
    assert "fixture-key" not in capsys.readouterr().out
    assert (
        cli.main(["config", "set", "backend.openai_api_key", '"private-marker"']) == 1
    )
    assert "private-marker" not in capsys.readouterr().err


def test_explicit_migration_preserves_auth_and_leaves_originals(monkeypatch, tmp_path):
    runtime = configured(tmp_path)
    (runtime.home / "config.json").unlink()
    legacy = runtime.home / "config.yaml"
    legacy.write_text(
        "version: 1\nprovider: {id: openai, auth: api_key}\nmodels: {text: gpt-4o-mini}\n"
    )
    env = tmp_path / "legacy.env"
    env.write_text(
        "AUTH_MODE=clerk\nCLERK_SECRET_KEY=fixture-clerk\nNEXT_PUBLIC_MAX_MESSAGE_LENGTH=3456\nMONGODB_URL=mongodb://127.0.0.1:27017/?replicaSet=rs0\n"
    )
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    assert cli.main(["config", "migrate", "--from-env", str(env)]) == 0
    assert legacy.is_file() and env.is_file()
    assert runtime.read_config().backend == {"auth_mode": "clerk"}
    assert runtime.read_config().frontend["max_message_length"] == 3456
    assert runtime.read_service_credentials()["clerk_secret_key"] == "fixture-clerk"
    assert (
        runtime.load({}).authentication.credential.api_key.get_secret_value()
        == "fixture-key"
    )
    assert cli.main(["config", "migrate"]) == 1


def test_invalid_migration_does_not_change_auth(monkeypatch, tmp_path):
    runtime = configured(tmp_path)
    (runtime.home / "config.json").unlink()
    (runtime.home / "config.yaml").write_text(
        "provider: {id: invalid, auth: api_key}\n"
    )
    auth = (runtime.home / "auth.json").read_bytes()
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    assert cli.main(["config", "migrate"]) == 1
    assert (runtime.home / "auth.json").read_bytes() == auth
    assert not (runtime.home / "config.json").exists()


@pytest.mark.parametrize("kind", ["missing", "symlink", "oversized"])
def test_migration_rejects_unsafe_explicit_environment_input(
    monkeypatch, tmp_path, kind
):
    runtime = configured(tmp_path)
    (runtime.home / "config.json").unlink()
    (runtime.home / "config.yaml").write_text(
        "provider: {id: openai, auth: api_key}\nmodels: {text: gpt-4o-mini}\n"
    )
    path = tmp_path / "legacy.env"
    if kind == "symlink":
        path.symlink_to(runtime.home / "auth.json")
    elif kind == "oversized":
        path.write_text("X" * (64 * 1024 + 1))
    before = (runtime.home / "auth.json").read_bytes()
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    assert cli.main(["config", "migrate", "--from-env", str(path)]) == 1
    assert (runtime.home / "auth.json").read_bytes() == before
    assert not (runtime.home / "config.json").exists()


def test_tui_entry_point_injects_host_service_effects(monkeypatch):
    """The gateway TUI never imports the host CLI; the entry point supplies effects."""
    from llm_gateway import cli_tui

    captured = {}
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli_tui, "main", lambda **kwargs: captured.update(kwargs) or 0)
    assert cli.main([]) == 0
    assert captured == {
        "status": cli.service_status,
        "run": cli.main,
        "problem": cli.docker_problem,
        "version": cli.version(),
        "mcp_status": cli.read_mcp_status,
    }


def test_tui_without_a_terminal_prints_help_instead_of_opening(monkeypatch, capsys):
    from llm_gateway import cli_tui

    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(
        cli_tui,
        "main",
        lambda **kwargs: pytest.fail("TUI needs an interactive terminal"),
    )
    assert cli.main([]) == 0
    assert "Usage: hybro [command]" in capsys.readouterr().out


@pytest.mark.parametrize("released", [False, True])
def test_mcp_start_is_service_scoped_and_projects_custom_prefix(
    monkeypatch, tmp_path, released, request
):
    if released:
        request.getfixturevalue("released_install")
    runtime = configured(tmp_path)
    runtime.update_config(["backend", "api_prefix"], "/custom/v2")
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=0),
    )
    assert cli.main(["mcp", "start"]) == 0
    args, kwargs = calls[-1]
    assert args[-4:] == ["up", "-d", "--no-deps", "mcp"]
    assert json.loads(kwargs["env"]["HYBRO_MCP_CONFIG"]) == {"api_prefix": "/custom/v2"}
    assert kwargs["env"]["COMPOSE_DISABLE_ENV_FILE"] == "1"


@pytest.mark.parametrize(
    ("action", "expected"),
    [("stop", ["stop", "mcp"]), ("logs", ["logs", "-f", "--tail", "100", "mcp"])],
)
def test_mcp_stop_and_logs_do_not_load_credentials(monkeypatch, action, expected):
    calls = []
    monkeypatch.setattr(cli, "compose", lambda args: calls.append(args) or 0)
    assert cli.main(["mcp", action]) == 0
    assert calls == [expected]


@pytest.mark.parametrize("status", ["ready", "unavailable", "unsupported_auth"])
def test_mcp_status_is_read_only_and_explains_auth(monkeypatch, capsys, status):
    monkeypatch.setattr(cli, "read_mcp_status", lambda: status)
    monkeypatch.setattr(cli, "store", lambda: pytest.fail("must not read credentials"))
    assert cli.main(["mcp", "status"]) == (0 if status == "ready" else 1)
    output = capsys.readouterr().out
    assert "http://127.0.0.1:8001/mcp" in output
    assert "Streamable HTTP" in output
    assert cli.STATUS_LABELS[status] in output


def test_tui_start_dispatches_validated_up_not_compose_start(monkeypatch, tmp_path):
    from llm_gateway import cli_tui

    runtime = configured(tmp_path)
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli_tui, "main", lambda **kw: kw["run"](("start",)))
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert cli.main([]) == 0
    assert calls[-1][-3:] == ["up", "-d", "--remove-orphans"]


def test_status_does_not_create_runtime_or_read_dotenv(monkeypatch, tmp_path):
    runtime = RuntimeConfigStore(tmp_path / "absent")
    monkeypatch.setenv("HYBRO_HOME", str(runtime.home))
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert cli.main(["status"]) == 0
    assert calls[0][-2:] == ["ps", "--all"]
    assert not runtime.home.exists()
