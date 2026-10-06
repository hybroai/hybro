"""Settings and services pages; effects remain in setup and the host CLI."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import termios
from collections.abc import Callable, Mapping
from typing import TextIO

from pydantic import ValidationError

from common.config.runtime_config import RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore, runtime_home
from llm_gateway.setup_cli import SetupConsole, _terminal_console
from llm_gateway.setup_panel import SetupPanel
from llm_gateway.setup_service import SetupError, SetupService
from llm_gateway.setup_terminal import SelectionCancelled, SetupOption, SetupScreen

_SERVICE_ACTIONS = (
    SetupOption("start", "Start services"),
    SetupOption("stop", "Stop services"),
    SetupOption(
        "apply",
        "Reload configuration",
        hint="Reload saved configuration: recreate containers. Services will be interrupted.",
    ),
    SetupOption("logs", "View logs"),
    SetupOption("mcp", "MCP connection"),
    SetupOption(
        "upgrade",
        "Upgrade hybro",
        hint="Install the newest release. The stack version moves with the CLI.",
    ),
    SetupOption("models", "Model configuration [Tab]", shortcut=b"\t"),
)

# Actions that change something outside the running stack ask before acting.
# Keyed by action; the value is the confirmation prompt.
_CONFIRMATIONS: dict[str, str] = {
    "apply": "Reload saved configuration? Services will be interrupted.",
    "upgrade": "Install the newest hybro release? This replaces the installed CLI.",
}

# Actions whose CLI arguments differ from the action name.
_ACTION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "apply": ("start", "--recreate"),
    "mcp_start": ("mcp", "start"),
    "mcp_stop": ("mcp", "stop"),
    "mcp_logs": ("mcp", "logs"),
}

# Host capability injected by the CLI entry point; the gateway never imports it.
StatusReader = Callable[[], list[dict[str, object]]]
ProblemReader = Callable[[], str | None]


def _read_status() -> list[dict[str, object]]:
    """Standalone default; the host CLI injects its Compose reader instead."""
    raise RuntimeConfigurationError(
        "Compose status is available through the hybro CLI entry point"
    )


def _read_problem() -> str | None:
    """Standalone default; the host CLI injects its Docker diagnosis instead."""
    return None


def _status_snapshot(
    read: StatusReader | None = None,
    problem: ProblemReader | None = None,
) -> tuple[str, list[dict[str, object]]]:
    try:
        rows = (read or _read_status)()
        running = sum(row.get("State") == "running" for row in rows)
        return f"{running} running" if rows else "No containers", rows
    except (OSError, ValueError, RuntimeConfigurationError, subprocess.SubprocessError):
        return (problem or _read_problem)() or "Status unavailable: check Docker", []


def _service_states(rows: list[dict[str, object]]) -> tuple[tuple[str, bool], ...]:
    states: dict[str, bool] = {}
    for row in rows:
        name = row.get("Service") or row.get("Name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name
        ):
            continue
        if name in {"mongo-setup", "registrar"}:
            continue
        states[name] = states.get(name, True) and row.get("State") == "running"
    return tuple(sorted(states.items()))


def _logs_page(
    console: SetupConsole,
    run: Callable[[tuple[str, ...]], int],
    read: StatusReader | None = None,
    problem: ProblemReader | None = None,
) -> None:
    while True:
        status, rows = _status_snapshot(read, problem)
        names = sorted(
            {
                row["Name"]
                for row in rows
                if isinstance(row.get("Name"), str)
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", row["Name"])
            }
        )
        try:
            choice = console.select(
                SetupScreen(
                    "Container logs",
                    status=status,
                    notice="Last 100 lines, then follow. While following, Ctrl-C returns here.",
                ),
                (
                    SetupOption("all", "All containers"),
                    *(SetupOption(name, name) for name in names),
                    SetupOption("back", "Back"),
                ),
            )
        except SelectionCancelled:
            return
        if choice == "back":
            return
        arguments = (
            ("logs", "--tail", "100")
            if choice == "all"
            else ("logs", "--container", choice)
        )
        try:
            result = run(arguments)
        except KeyboardInterrupt:
            result = 130
        if result != 130:
            console.write(
                "Log stream ended."
                if result == 0
                else "Logs unavailable; check Docker and the selected container."
            )
            try:
                console.pause()
            except SelectionCancelled:
                pass


def _run_command(arguments: tuple[str, ...]) -> int:
    """Standalone default; the host CLI injects its Compose runner instead."""
    raise RuntimeConfigurationError(
        "Service commands are available through the hybro CLI entry point"
    )


def _service_command(
    console: SetupConsole, run: Callable[[tuple[str, ...]], int], action: str
) -> str:
    arguments = _ACTION_ARGUMENTS.get(action, (action,))
    prompt = _CONFIRMATIONS.get(action)
    if prompt is not None and (
        console.select(
            prompt,
            (SetupOption("cancel", "Cancel"), SetupOption("run", "Run command")),
        )
        == "cancel"
    ):
        return "Canceled; nothing changed."
    result = run(arguments)
    notice = (
        "Command canceled."
        if result == 130
        else "Command finished."
        if result == 0
        else "Command failed."
    )
    console.write(notice)
    try:
        console.pause()
    except SelectionCancelled:
        pass
    return notice


def _mcp_page(
    console: SetupConsole,
    run: Callable[[tuple[str, ...]], int],
    read: Callable[[], str] | None = None,
) -> None:
    labels = {
        "ready": "Ready",
        "unavailable": "Unavailable; start services or check MCP logs.",
        "unsupported_auth": "Unavailable with Clerk authentication.",
    }
    while True:
        state = read() if read else "unavailable"
        try:
            action = console.select(
                SetupScreen(
                    "MCP connection",
                    status=labels.get(state, labels["unavailable"]),
                    notice=(
                        "Server URL: http://127.0.0.1:8001/mcp\n"
                        "Transport: Streamable HTTP\n"
                        "For clients on the machine running Hybro."
                    ),
                ),
                (
                    SetupOption("start", "Start MCP"),
                    SetupOption("stop", "Stop MCP"),
                    SetupOption("logs", "View MCP logs"),
                    SetupOption("refresh", "Refresh status"),
                    SetupOption("back", "Back"),
                ),
            )
            if action == "back":
                return
            if action != "refresh":
                try:
                    _service_command(console, run, f"mcp_{action}")
                except KeyboardInterrupt:
                    if action != "logs":
                        raise
        except SelectionCancelled:
            return


def _services_page(
    console: SetupConsole,
    run: Callable[[tuple[str, ...]], int],
    focus: str,
    read: StatusReader | None = None,
    problem: ProblemReader | None = None,
    version: str | None = None,
    mcp_status: Callable[[], str] | None = None,
) -> str:
    from dataclasses import replace

    # The CLI runs the images published for its own version, so one line states
    # both: this is the stack version too.
    version_line = f"CLI and stack {version}" if version else ""
    notice = ""
    while True:
        status, rows = _status_snapshot(read, problem)
        try:
            action = console.select(
                SetupScreen(
                    "Hybro",
                    tab="services",
                    status=status if not rows else "",
                    service_states=_service_states(rows),
                    notice="\n".join(
                        filter(
                            None,
                            (
                                version_line,
                                notice,
                                "More commands: hybro --help. Status refreshes on return.",
                            ),
                        )
                    ),
                ),
                tuple(
                    replace(option, current=option.value == focus)
                    for option in _SERVICE_ACTIONS
                ),
            )
        except SelectionCancelled:
            return focus
        if action == "models":
            return focus
        focus = action
        try:
            if action == "logs":
                _logs_page(console, run, read, problem)
            elif action == "mcp":
                _mcp_page(console, run, mcp_status)
            else:
                notice = _service_command(console, run, action)
        except SelectionCancelled:
            continue
        except (SetupError, OSError, termios.error):
            notice = "Service command failed; check Docker and the terminal."


def _menu(
    console: SetupConsole,
    run: Callable[[tuple[str, ...]], int],
    service: SetupService,
    environment: Mapping[str, str],
    read: StatusReader | None = None,
    problem: ProblemReader | None = None,
    version: str | None = None,
    mcp_status: Callable[[], str] | None = None,
) -> int:
    panel = SetupPanel(service, console, environment)
    service_focus = "start"
    while True:
        try:
            action = console.select(panel.title(), panel.options())
        except SelectionCancelled:
            if panel.can_exit():
                return 0
            continue
        if action == "services":
            service_focus = _services_page(
                console, run, service_focus, read, problem, version, mcp_status
            )
            continue
        try:
            panel.edit(action)
        except SelectionCancelled:
            # Close a picker/confirmation, leaving its parent and draft intact.
            continue
        except (SetupError, RuntimeConfigurationError) as exc:
            panel.notice = str(exc)
        except (OSError, termios.error, ValidationError):
            panel.notice = (
                "Operation failed; check the private configuration and terminal."
            )


def main(
    *,
    console: SetupConsole | None = None,
    run: Callable[[tuple[str, ...]], int] = _run_command,
    output: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
    service: SetupService | None = None,
    status: StatusReader | None = None,
    problem: ProblemReader | None = None,
    version: str | None = None,
    mcp_status: Callable[[], str] | None = None,
) -> int:
    output = output if output is not None else sys.stdout
    try:
        environment = dict(os.environ if environment is None else environment)
        if service is None:
            from llm_gateway.catalog import model_choices
            from llm_gateway.setup_bindings import verify_selection

            service = SetupService(
                RuntimeConfigStore(runtime_home(environment)),
                model_choices,
                verify_selection,
            )
        if console is not None:
            return _menu(
                console, run, service, environment, status, problem, version, mcp_status
            )
        with _terminal_console(output, screen=True) as terminal:
            return _menu(
                terminal,
                run,
                service,
                environment,
                status,
                problem,
                version,
                mcp_status,
            )
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        print("Hybro closed; unsaved changes discarded.", file=output)
        return 130
    except (SetupError, RuntimeConfigurationError, OSError, termios.error):
        print(
            "Cannot use TUI; check configuration/terminal or use an explicit subcommand.",
            file=output,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
