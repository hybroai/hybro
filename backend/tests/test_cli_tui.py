"""TUI keyboard/syscall fakes only; never launch lifecycle commands or a PTY."""

import io
from unittest.mock import Mock

import pytest

from common.config.runtime_config import RuntimeConfigurationError
from llm_gateway import cli_tui
from llm_gateway.setup_cli import SetupConsole
from llm_gateway.setup_terminal import SetupOption, select_option
from tests.fakes.setup_terminal import TerminalReader, mock_keyboard


def console_with(*choices):
    pending = iter(choices)

    def select(title, options):
        try:
            chosen = next(pending)
        except StopIteration:
            raise EOFError from None
        if isinstance(chosen, type) and issubclass(chosen, BaseException):
            raise chosen
        assert chosen in {o.value for o in options}, title
        return chosen

    return SetupConsole(
        select, Mock(side_effect=AssertionError("secret")), Mock(), Mock()
    )


@pytest.fixture(autouse=True)
def mock_status(monkeypatch):
    reader = Mock(
        return_value=[
            {"Name": "hybro-backend-1", "State": "running", "Health": "healthy"}
        ]
    )
    monkeypatch.setattr(cli_tui, "_read_status", reader)
    return reader


@pytest.mark.parametrize("action", ["start", "stop"])
def test_ordinary_lifecycle_actions_do_not_repeat_confirmation(action):
    run = Mock(return_value=0)
    choices = ("services", action)
    console = console_with(*choices)
    assert cli_tui.main(console=console, run=run) == 130
    run.assert_called_once_with((action,))
    console.pause.assert_called_once_with()


@pytest.mark.parametrize("confirmation", ["run", "cancel"])
def test_apply_requires_confirmation(confirmation):
    run = Mock(return_value=0)
    console = console_with("services", "apply", confirmation)
    assert cli_tui.main(console=console, run=run) == 130
    if confirmation == "run":
        run.assert_called_once_with(("start", "--recreate"))
        console.pause.assert_called_once_with()
    else:
        run.assert_not_called()
        console.pause.assert_not_called()


def test_upgrade_replaces_the_cli_only_after_confirmation():
    """Upgrading changes the installed CLI, so it asks before reaching npm."""
    run = Mock(return_value=0)
    console = console_with("services", "upgrade", "run")
    assert cli_tui.main(console=console, run=run) == 130
    run.assert_called_once_with(("upgrade",))

    canceled = Mock(return_value=0)
    console = console_with("services", "upgrade", "cancel")
    assert cli_tui.main(console=console, run=canceled) == 130
    canceled.assert_not_called()


def test_services_page_states_the_version_the_stack_is_pinned_to(mock_status):
    """The CLI version is also the stack version, so the screen says so once."""
    screens = []
    console = console_with("start", "models")
    original = console.select

    def select(title, options):
        screens.append(title)
        return original(title, options)

    console = SetupConsole(select, console.read_secret, console.write, console.pause)
    cli_tui._services_page(console, Mock(return_value=0), "start", None, None, "9.9.9")
    assert "CLI and stack 9.9.9" in screens[0].notice


@pytest.mark.parametrize(
    "target,arguments",
    [
        ("all", ("logs", "--tail", "100")),
        ("hybro-backend-1", ("logs", "--container", "hybro-backend-1")),
    ],
)
@pytest.mark.parametrize("result", [0, 130, KeyboardInterrupt])
def test_logs_choose_container_or_all_and_return_to_picker(target, arguments, result):
    run = (
        Mock(side_effect=result)
        if result is KeyboardInterrupt
        else Mock(return_value=result)
    )
    console = console_with("services", "logs", target, "back", "models")
    assert cli_tui.main(console=console, run=run) == 130
    run.assert_called_once_with(arguments)
    if result == 0:
        console.pause.assert_called_once_with()
    else:
        console.pause.assert_not_called()


def test_failed_log_stream_can_return_to_picker():
    console = console_with("all", "back")
    run = Mock(return_value=1)
    cli_tui._logs_page(console, run)
    console.write.assert_called_once_with(
        "Logs unavailable; check Docker and the selected container."
    )
    console.pause.assert_called_once_with()


def test_status_is_inline_refreshes_and_menu_is_minimal(mock_status):
    mock_status.side_effect = [[{"Service": "backend", "State": "running"}], []]
    console = console_with("start", "models")
    original = console.select
    screens = []

    def select(title, options):
        screens.append(title)
        assert {o.value for o in options} == {
            "start",
            "stop",
            "apply",
            "logs",
            "mcp",
            "upgrade",
            "models",
        }
        return original(title, options)

    console = SetupConsole(select, console.read_secret, console.write, console.pause)
    cli_tui._services_page(console, Mock(return_value=0), "start")
    assert screens[0].status == ""
    assert screens[0].service_states == (("backend", True),)
    assert screens[1].status == "No containers"


def test_unreachable_compose_reports_the_injected_docker_problem(mock_status):
    """An installed CLI says why Docker is unusable instead of a generic notice."""
    mock_status.side_effect = OSError("no docker")
    assert cli_tui._status_snapshot(
        problem=lambda: "The Docker daemon is stopped."
    ) == (
        "The Docker daemon is stopped.",
        [],
    )
    assert cli_tui._status_snapshot() == ("Status unavailable: check Docker", [])


def test_status_summary_only_reports_running(mock_status):
    mock_status.return_value = [
        {"State": "running", "Health": "healthy"},
        {"State": "running", "Health": "unhealthy"},
        {"State": "running", "Health": "starting"},
        {"State": "exited", "ExitCode": 0, "Service": "registrar"},
        {"State": "exited", "ExitCode": 0, "Service": "backend"},
        {"State": "exited", "ExitCode": 1},
    ]
    status, _ = cli_tui._status_snapshot()
    assert status == "3 running"


def test_service_states_hide_jobs_and_distinguish_running_from_stopped():
    rows = [
        {"Service": "backend", "State": "running"},
        {"Service": "frontend", "State": "exited"},
        {"Service": "mongo-setup", "State": "exited", "ExitCode": 0},
        {"Service": "registrar", "State": "exited", "ExitCode": 0},
        {"Service": "redis", "State": "restarting"},
        {"Service": "bad\u001b[31m", "State": "running"},
    ]
    assert cli_tui._service_states(rows) == (
        ("backend", True),
        ("frontend", False),
        ("redis", False),
    )
    # A service is not fully active when one replica is stopped.
    rows.append({"Service": "backend", "State": "exited"})
    assert cli_tui._service_states(rows)[0] == ("backend", False)


def test_status_failure_is_safe_and_logs_escape_runs_nothing(mock_status):
    mock_status.side_effect = OSError("private-diagnostic")
    assert cli_tui._status_snapshot() == ("Status unavailable: check Docker", [])
    run = Mock()
    cli_tui._logs_page(console_with(cli_tui.SelectionCancelled), run)
    run.assert_not_called()


def test_failed_command_returns_to_menu_without_automatic_retry():
    run = Mock(side_effect=[1, 0])
    console = console_with("services", "start", "stop")
    assert cli_tui.main(console=console, run=run) == 130
    assert [c.args[0] for c in run.call_args_list] == [("start",), ("stop",)]
    console.write.assert_any_call("Command failed.")
    assert console.pause.call_count == 2


def test_pages_switch_without_commands():
    run = Mock()
    assert cli_tui.main(console=console_with("services", "models"), run=run) == 130
    run.assert_not_called()


@pytest.mark.parametrize("keys", [b"\x1b", b"\x03", b"\x04", b""])
def test_real_key_cancellation_has_no_effect(monkeypatch, keys):
    _, restore = mock_keyboard(monkeypatch, keys)
    output, run = io.StringIO(), Mock()
    console = SetupConsole(
        lambda title, options: select_option(TerminalReader(), output, title, options),
        Mock(),
        Mock(),
    )
    expected = 0 if keys == b"\x1b" else 130
    assert cli_tui.main(console=console, run=run, output=output) == expected
    run.assert_not_called()
    restore.assert_called_once()


@pytest.mark.parametrize(
    "keys,error",
    [
        (b" \x1b[B\r", None),
        (b"\x1b", KeyboardInterrupt),
        (b"\x03", KeyboardInterrupt),
        (b"", EOFError),
    ],
)
def test_enter_pause_restores_terminal_without_a_menu(monkeypatch, keys, error):
    from llm_gateway.setup_terminal import wait_for_enter

    _, restore = mock_keyboard(monkeypatch, keys)
    output = io.StringIO()
    if error is None:
        wait_for_enter(TerminalReader(), output)
    else:
        with pytest.raises(error):
            wait_for_enter(TerminalReader(), output)
    restore.assert_called_once()
    assert "Press Enter to return" in output.getvalue()
    assert "(default)" not in output.getvalue()


def test_apply_default_confirmation_cancels_with_real_arrows(monkeypatch):
    # Tab -> Services -> apply -> default Cancel -> Escape -> model page EOF.
    _, restore = mock_keyboard(monkeypatch, b"\t" + b"\x1b[B" * 2 + b"\r\r\x1b")
    output, run = io.StringIO(), Mock()
    console = SetupConsole(
        lambda title, options: select_option(TerminalReader(), output, title, options),
        Mock(),
        Mock(),
    )
    assert cli_tui.main(console=console, run=run, output=output) == 130
    run.assert_not_called()
    assert restore.call_count == 5


def test_small_terminal_scrolls_model_list_without_wrapping(monkeypatch):
    from llm_gateway import setup_terminal

    mock_keyboard(monkeypatch, b"\x1b[A\r")
    monkeypatch.setattr(setup_terminal.os, "get_terminal_size", lambda fd: (32, 7))
    output = io.StringIO()
    options = tuple(
        SetupOption(str(i), "Model " + str(i) + " long label" * 10) for i in range(10)
    )
    assert select_option(TerminalReader(), output, "Models", options) == "9"
    assert "Showing 7-10 of 10" in output.getvalue()
    assert "\x1b[5A" in output.getvalue()
    assert "\x1b[10A" not in output.getvalue()
    import re

    plain = re.sub(r"\x1b\[[?0-9;]*[A-Za-z]", "", output.getvalue())
    assert max(map(len, plain.splitlines())) <= 31


@pytest.mark.parametrize(
    "payload",
    [
        '[{"Name":"hybro-backend-1","State":"running"}]',
        '{"Name":"hybro-backend-1","State":"running"}\n',
    ],
)
def test_status_query_is_read_only_bounded_and_ignores_dotenv(monkeypatch, payload):
    import configuration_cli as cli

    run = Mock(return_value=Mock(stdout=payload))
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "store", lambda: Mock(home="/fixture/runtime"))
    monkeypatch.setattr(
        cli.RuntimeConfigStore, "load", Mock(side_effect=AssertionError("credentials"))
    )
    assert cli.service_status() == [{"Name": "hybro-backend-1", "State": "running"}]
    arguments = run.call_args.args[0]
    assert arguments[:4] == ["docker", "compose", "--env-file", "/dev/null"]
    assert arguments[-5:] == ["ps", "--all", "--orphans=false", "--format", "json"]
    assert run.call_args.kwargs["timeout"] == 5
    assert run.call_args.kwargs["capture_output"] is True


def test_single_container_logs_do_not_include_other_replicas(monkeypatch):
    import configuration_cli as cli

    run = Mock(return_value=Mock(returncode=130))
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "store", lambda: Mock(home="/fixture/runtime"))
    assert cli.main(["logs", "--container", "hybro-backend-2"]) == 130
    assert run.call_args.args[0] == [
        "docker",
        "logs",
        "--follow",
        "--tail",
        "100",
        "--",
        "hybro-backend-2",
    ]


@pytest.mark.parametrize("size", [(80, 24), (120, 32), (32, 10)])
def test_service_status_and_actions_fit_terminal(size):
    import re

    from llm_gateway.setup_terminal import SetupScreen, _draw_screen

    output = io.StringIO()
    _draw_screen(
        output,
        SetupScreen(
            "Hybro",
            tab="services",
            service_states=(("backend", True), ("frontend", False)),
        ),
        cli_tui._SERVICE_ACTIONS,
        0,
        *size,
    )
    spans = re.findall(
        r"\x1b\[(\d+);(\d+)H\x1b\[([\d;]+)m([^\x1b]*)\x1b\[0m", output.getvalue()
    )
    assert "completed" not in output.getvalue()
    active = next(span for span in spans if span[2:] == ("32", "active"))
    inactive = next(span for span in spans if span[2:] == ("31", "inactive"))
    start = next(span for span in spans if "Start services" in span[3])
    assert int(active[0]) < int(start[0]) and int(inactive[0]) < int(start[0])
    if size[0] >= 80:
        assert active[0] == inactive[0]
    for row, column, _, text in spans:
        assert int(row) < size[1]
        assert int(column) + len(text) - 1 <= size[0]


@pytest.mark.parametrize("size", [(200, 24), (80, 24), (32, 10), (16, 5)])
def test_many_service_states_keep_actions_and_colors_in_bounds(size):
    import re

    from llm_gateway.setup_terminal import SetupScreen, _draw_screen

    states = tuple(
        (name, i % 2 == 0)
        for i, name in enumerate(
            (
                "backend",
                "frontend",
                "mongo",
                "redis",
                "story-agent",
                "travel-planner-agent",
                "weather-agent",
                "image-generator-agent",
            )
        )
    )
    output = io.StringIO()
    _draw_screen(
        output,
        SetupScreen("Hybro", tab="services", service_states=states),
        cli_tui._SERVICE_ACTIONS,
        0,
        *size,
    )
    spans = re.findall(
        r"\x1b\[(\d+);(\d+)H\x1b\[([\d;]+)m([^\x1b]*)\x1b\[0m", output.getvalue()
    )
    assert any("Start ser" in text for _, _, _, text in spans)
    for row, column, _, text in spans:
        assert 1 <= int(row) < size[1]
        assert 1 <= int(column) and int(column) + len(text) - 1 <= size[0]
    if size[0] >= 80:
        assert sum(style in {"31", "32"} for _, _, style, _ in spans) == len(states)
    if size[0] == 200:
        assert len({row for row, _, style, _ in spans if style in {"31", "32"}}) == 1


def test_standalone_tui_refuses_service_commands_instead_of_spawning():
    """An installed TUI has no lifecycle script; the host CLI injects the runner."""
    with pytest.raises(RuntimeConfigurationError):
        cli_tui._run_command(("status",))


def test_explicit_provider_clients_and_keys_do_not_import_app_settings(monkeypatch):
    import builtins

    from llm_gateway.providers import deepseek_provider, openai_provider

    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "common.config.loader":
            pytest.fail("Explicit CLI credentials must not load application settings")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    openai_factory, deepseek_factory = Mock(), Mock()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", openai_factory)
    monkeypatch.setattr(deepseek_provider, "AsyncOpenAI", deepseek_factory)
    injected = Mock()
    assert openai_provider.OpenAIProvider(client=injected)._client is injected
    assert deepseek_provider.DeepSeekProvider(client=injected)._client is injected
    openai_provider.OpenAIProvider(
        api_key="fixture", base_url="https://fixture.invalid/v1"
    )
    deepseek_provider.DeepSeekProvider(api_key="fixture")
    openai_factory.assert_called_once_with(
        api_key="fixture", base_url="https://fixture.invalid/v1", max_retries=0
    )
    deepseek_factory.assert_called_once_with(
        api_key="fixture", base_url="https://api.deepseek.com", max_retries=0
    )


@pytest.mark.parametrize("configured", [False, True])
def test_legacy_provider_defaults_remain_settings_backed(monkeypatch, configured):
    import importlib
    from types import SimpleNamespace

    from llm_gateway.providers import deepseek_provider, openai_provider

    settings_module = importlib.import_module("common.config.loader")
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            openai_api_key="fixture-openai" if configured else "",
            openai_base_url="https://fixture.invalid/v1" if configured else None,
            deepseek_api_key="fixture-deepseek" if configured else "",
        ),
    )
    openai_factory, deepseek_factory = Mock(), Mock()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", openai_factory)
    monkeypatch.setattr(deepseek_provider, "AsyncOpenAI", deepseek_factory)
    openai_provider.OpenAIProvider()
    deepseek_provider.DeepSeekProvider()
    openai_factory.assert_called_once_with(
        api_key="fixture-openai" if configured else "missing",
        base_url="https://fixture.invalid/v1" if configured else None,
        max_retries=0,
    )
    deepseek_factory.assert_called_once_with(
        api_key="fixture-deepseek" if configured else "missing",
        base_url="https://api.deepseek.com",
        max_retries=0,
    )


def test_mcp_page_shows_live_status_and_only_runs_selected_service_actions():
    console = console_with("start", "stop", "refresh", "back")
    original = console.select
    screens = []

    def select(title, options):
        screens.append(title)
        return original(title, options)

    console = SetupConsole(select, console.read_secret, console.write, console.pause)
    run = Mock(return_value=0)
    status = Mock(
        side_effect=["unavailable", "ready", "unavailable", "unsupported_auth"]
    )
    cli_tui._mcp_page(console, run, status)
    assert [call.args[0] for call in run.call_args_list] == [
        ("mcp", "start"),
        ("mcp", "stop"),
    ]
    assert screens[1].status == "Ready"
    assert "Clerk" in screens[3].status
    assert "http://127.0.0.1:8001/mcp" in screens[0].notice
    assert "Streamable HTTP" in screens[0].notice


def test_services_menu_opens_mcp_without_starting_it():
    run = Mock()
    console = console_with("mcp", "back", "models")
    cli_tui._services_page(console, run, "start", mcp_status=lambda: "ready")
    run.assert_not_called()


def test_mcp_log_interrupt_returns_to_mcp_menu():
    console = console_with("logs", "back")
    run = Mock(side_effect=KeyboardInterrupt)
    cli_tui._mcp_page(console, run)
    run.assert_called_once_with(("mcp", "logs"))


def test_package_initializer_defers_application_imports(monkeypatch):
    import builtins
    import importlib.util
    from pathlib import Path

    path = Path(cli_tui.__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("gateway_import_probe", path)
    module = importlib.util.module_from_spec(spec)
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"gateway", "model_registry", "common.config.loader"}:
            pytest.fail("Package initializer must not load application defaults")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec.loader.exec_module(module)
    assert module.__all__ == ["LLMGatewayImpl", "ModelRegistryImpl"]
    assert "LLMGatewayImpl" not in vars(module)
    assert "ModelRegistryImpl" not in vars(module)


def test_public_gateway_exports_still_resolve_original_classes():
    from llm_gateway import LLMGatewayImpl, ModelRegistryImpl
    from llm_gateway.gateway import LLMGatewayImpl as OriginalGateway
    from llm_gateway.model_registry import ModelRegistryImpl as OriginalRegistry

    assert LLMGatewayImpl is OriginalGateway
    assert ModelRegistryImpl is OriginalRegistry
