"""Host configuration and Compose entry point. Environment files are migration input only."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from cli_mcp import MCP_URL, STATUS_LABELS
from cli_mcp import read_status as read_mcp_status
from cli_stack import HELP, MANIFEST, RENDERER, Stack, data_file, resolve, version
from common.config.loader import (
    PRIVATE_FIELDS,
    ROUTE_FIELDS,
    FrontendSettings,
    Settings,
)
from common.config.runtime_config import RuntimeConfigurationError, resolve_credential
from common.config.runtime_store import (
    SERVICE_KEYS,
    RuntimeConfigStore,
    _auth_object,
    _credential_bytes,
    parse_config,
    parse_credential,
    runtime_home,
)

# Docker's own startup can be slow on a cold daemon; local probes stay bounded.
_DOCKER_TIMEOUT = 10


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RuntimeConfigurationError("Invalid command arguments; run hybro --help.")


def store() -> RuntimeConfigStore:
    return RuntimeConfigStore(runtime_home(os.environ))


def compose_environment(runtime: RuntimeConfigStore, *, start: bool) -> dict[str, str]:
    # Preserve ordinary executable/socket/proxy context, not legacy application settings.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {key.upper() for key in Settings.model_fields}
        and not k.startswith("NEXT_PUBLIC_")
    }
    for key in (
        "CLERK_SECRET_KEY",
        "CLERK_WEBHOOK_SECRET",
        "AGENT_REGISTRAR_TOKEN",
        "DEFAULT_AGENT_LLM_TOKEN",
        "HYBRO_AGENT_CONFIG",
        "HYBRO_MCP_CONFIG",
        "HYBRO_IMAGE_REGISTRY",
        "HYBRO_STACK_TAG",
        "IMAGE_SIZE",
        "OPENAI_MODEL",
        "IMAGE_MODEL",
        "COMPOSE_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_PROJECT_NAME",
    ):
        env.pop(key, None)
    env["HYBRO_HOME"] = str(runtime.home)
    # The released stack file pins every published image to this CLI's version.
    env["HYBRO_STACK_TAG"] = version()
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    if not start:
        return env
    state = runtime.load({})
    from llm_gateway.catalog import validate_models

    validate_models(state.config)
    secrets = runtime.ensure_service_credentials()
    if any(
        key in state.config.backend and key in secrets
        for key in ("mongodb_url", "redis_url")
    ):
        raise RuntimeConfigurationError(
            "Specify each connection URL in config.json or auth.json, not both."
        )
    backend = Settings.model_validate(
        {
            **state.config.backend,
            **{k: v for k, v in secrets.items() if k in Settings.model_fields},
        }
    )
    if backend.auth_mode == "clerk" and not backend.clerk_secret_key:
        raise RuntimeConfigurationError("Clerk mode requires credentials in auth.json.")
    public = FrontendSettings.model_validate(
        {"api_prefix": backend.api_prefix, **state.config.frontend}
    )
    env.update(
        {
            "HYBRO_API_PREFIX": backend.api_prefix,
            "HYBRO_MCP_CONFIG": json.dumps({"api_prefix": backend.api_prefix}),
            "HYBRO_FRONTEND_CONFIG": public.model_dump_json(),
            "HYBRO_AGENT_CONFIG": json.dumps(
                {
                    "base_url": f"http://backend:8000{backend.api_prefix}/internal/llm",
                    "token": secrets["default_agent_llm_token"],
                    "image_size": state.config.image_size,
                }
            ),
            "AGENT_REGISTRAR_TOKEN": secrets["default_agent_registrar_token"],
            "CLERK_SECRET_KEY": secrets.get("clerk_secret_key", ""),
            "CLERK_WEBHOOK_SECRET": secrets.get("clerk_webhook_secret", ""),
        }
    )
    return env


def compose_files(stack: Stack, extra: Sequence[str] = ()) -> list[str]:
    """Stack file first; extras must be existing files beside it.

    Overlays stay explicit CLI input (for example the CI mock-LLM stack) instead
    of ambient Compose discovery.
    """
    files = [stack.compose]
    for value in extra:
        candidate = Path(value)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (stack.root / candidate).resolve()
        )
        if not resolved.is_file() or not resolved.is_relative_to(stack.root):
            raise RuntimeConfigurationError(
                "Additional Compose file must be an existing file beside the stack."
            )
        files.append(resolved)
    return [os.fspath(name) for name in files]


def render_agents(stack: Stack) -> int:
    """Regenerate a checkout's Compose file before starting it.

    A released stack has no manifest or renderer; its file is published already
    rendered, so there is nothing to regenerate.
    """
    if not stack.checkout:
        return 0
    spec = importlib.util.spec_from_file_location(
        "hybro_render_compose", data_file(RENDERER)
    )
    if spec is None or spec.loader is None:
        raise RuntimeConfigurationError("Cannot load the Compose renderer.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(["--manifest", os.fspath(data_file(MANIFEST))])


def compose(
    arguments: list[str], *, start: bool = False, files: Sequence[str] = ()
) -> int:
    runtime = store()
    env = compose_environment(runtime, start=start)
    stack = resolve()
    if start:
        result = render_agents(stack)
        if result:
            return result
    compose_arguments = [
        item for name in compose_files(stack, files) for item in ("-f", name)
    ]
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            *compose_arguments,
            *arguments,
        ],
        cwd=stack.root,
        env=env,
        check=False,
    ).returncode


def service_status() -> list[dict[str, object]]:
    """Read only Compose container metadata; never load application credentials."""
    runtime = store()
    stack = resolve()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            *[item for name in compose_files(stack) for item in ("-f", name)],
            "ps",
            "--all",
            "--orphans=false",
            "--format",
            "json",
        ],
        cwd=stack.root,
        env=compose_environment(runtime, start=False),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    raw = result.stdout.strip()
    rows = (
        json.loads(raw)
        if raw.startswith("[")
        else [json.loads(line) for line in raw.splitlines()]
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid container status")
    return rows


def _import_environment(config, auth, values):
    backend = dict(config.backend)
    public = dict(config.frontend)
    services = dict(_auth_object(auth).get("services", {}))
    defaults = Settings()
    for name, value in values.items():
        key = name.lower()
        if key in SERVICE_KEYS and (
            key not in {"mongodb_url", "redis_url"}
            or urlsplit(value).username
            or urlsplit(value).password
        ):
            services[key] = value
        elif name == "AGENT_REGISTRAR_TOKEN":
            services.setdefault("default_agent_registrar_token", value)
        elif key in Settings.model_fields and key not in PRIVATE_FIELDS | ROUTE_FIELDS:
            parsed = Settings.model_validate({key: value}, strict=False)
            normalized = parsed.model_dump(mode="json")[key]
            # These were host hints which Compose previously overrode unconditionally.
            if key == "mongodb_url" and value in {
                "localhost:27017",
                "mongodb://127.0.0.1:27017/?replicaSet=rs0",
                "mongodb://localhost:27017/?replicaSet=rs0",
            }:
                continue
            if normalized != getattr(defaults, key):
                backend[key] = normalized
        elif name.startswith("NEXT_PUBLIC_"):
            field = name.removeprefix("NEXT_PUBLIC_").lower()
            if field in FrontendSettings.model_fields:
                parsed = FrontendSettings.model_validate({field: value}, strict=False)
                public[field] = getattr(parsed, field)
    return backend, public, services


# Legacy env keys with no JSON equivalent. Importing them silently would discard
# a user's prior model/route selection, so migrate reports them instead.
_SKIPPED_LEGACY_ENV_FIELDS = frozenset({"openai_model"})


def _skipped_legacy_field(name: str) -> bool:
    key = name.lower()
    return key in ROUTE_FIELDS or key in _SKIPPED_LEGACY_ENV_FIELDS


def _read_legacy_environment(path: str) -> dict[str, str]:
    from dotenv import dotenv_values

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeConfigurationError(
                "Legacy environment input must be a regular file."
            )
        data = stream.read(64 * 1024 + 1)
    if len(data) > 64 * 1024:
        raise RuntimeConfigurationError("Legacy environment input exceeds 64 KiB.")
    return {
        key: value
        for key, value in dotenv_values(
            stream=io.StringIO(data.decode("utf-8")), interpolate=False
        ).items()
        if value
    }


def migrate(runtime: RuntimeConfigStore, env_path: str | None) -> None:
    """Explicit one-time conversion; normal startup never reads these inputs."""
    import yaml

    with runtime._locked():
        if runtime._read("config.json") is not None:
            raise RuntimeConfigurationError(
                "config.json already exists; migration will not overwrite it."
            )
        raw = runtime._read("config.yaml")
        if raw is None:
            raise RuntimeConfigurationError(
                "No legacy config.yaml found; run hybro setup."
            )
        # Legacy setup wrote a small mapping, never tags/aliases or merge keys.
        try:
            if any(
                isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(raw)
            ):
                raise RuntimeConfigurationError(
                    "Legacy YAML aliases are not supported."
                )
            document = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise RuntimeConfigurationError(
                "Invalid legacy config.yaml; no files changed."
            ) from None
        config = parse_config(json.dumps(document, allow_nan=False).encode())
        auth = runtime._read("auth.json")
        credential = parse_credential(auth) if auth is not None else None
        values = _read_legacy_environment(env_path) if env_path else {}
        skipped = sorted(name for name in values if _skipped_legacy_field(name))
        credential = resolve_credential(
            config, credential, values if credential is None else {}
        ).credential
        backend, public, services = _import_environment(config, auth, values)
        document = config.model_dump(exclude_none=True)
        document.update(backend=backend, frontend=public)
        if values.get("IMAGE_SIZE"):
            document["image_size"] = values["IMAGE_SIZE"]
        encoded = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode()
        parse_config(encoded)
        auth_document = _auth_object(auth)
        auth_document["services"] = services
        runtime._save_pair(
            encoded,
            _credential_bytes(credential, json.dumps(auth_document).encode()),
            None,
            auth,
        )
    if skipped:
        print(
            "Warning: skipped legacy fields with no JSON equivalent (rerun hybro setup "
            f"to restore model selection): {', '.join(skipped)}"
        )
    print(
        "Migrated config.json/auth.json. Original files were not removed. Restart services to apply."
    )


def docker_problem() -> str | None:
    """Why Docker cannot run the stack, or ``None`` when it can.

    A missing binary, a stopped daemon, and a missing Compose plugin need
    different fixes, so an installed CLI reports which one it found instead of
    one generic message.
    """
    if shutil.which("docker") is None:
        return "Docker is not installed or not on PATH."
    try:
        compose = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "Docker did not respond; check the Docker installation."
    if compose.returncode:
        return "The Docker Compose plugin is unavailable; update Docker."
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "Docker did not respond; check the Docker installation."
    if daemon.returncode:
        return "Docker is installed but its daemon is not running; start Docker."
    return None


def _help(arguments: list[str]) -> int:
    sys.stdout.write(data_file(HELP).read_text(encoding="utf-8"))
    return 0


def _version(arguments: list[str]) -> int:
    print(version())
    return 0


def _setup(arguments: list[str]) -> int:
    from llm_gateway import setup_cli

    return setup_cli.main(arguments)


def _upgrade(arguments: list[str]) -> int:
    from cli_upgrade import run

    parser = Parser(prog="hybro upgrade")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether a newer release exists; exit non-zero if it does.",
    )
    return run(check=parser.parse_args(arguments).check)


def _interactive() -> bool:
    """Whether a TUI can drive this terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _tui(arguments: list[str]) -> int:
    """Open the TUI, or print help when there is no terminal to drive it."""
    if arguments:
        raise RuntimeConfigurationError("hybro tui does not accept arguments.")
    if not _interactive():
        return _help(arguments)
    from llm_gateway import cli_tui

    return cli_tui.main(
        status=service_status,
        run=main,
        problem=docker_problem,
        version=version(),
        mcp_status=read_mcp_status,
    )


def _passthrough(*prefix: str) -> Callable[[list[str]], int]:
    """A command that forwards its arguments to Compose after a fixed prefix."""

    def run(arguments: list[str]) -> int:
        return compose([*prefix, *arguments])

    return run


def config_command(arguments: list[str]) -> int:
    parser = Parser(prog="hybro config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("check")
    edit = sub.add_parser("set")
    edit.add_argument("path")
    edit.add_argument("value", help="JSON value; credentials are not accepted here")
    secret = sub.add_parser("secret")
    secret.add_argument("name", choices=sorted(SERVICE_KEYS))
    migration = sub.add_parser("migrate")
    migration.add_argument(
        "--from-env", help="Explicit legacy deployment file to import once"
    )
    args = parser.parse_args(arguments)
    runtime = store()
    if args.command == "migrate":
        migrate(runtime, args.from_env)
    elif args.command == "set":
        path = args.path.split(".")
        # The published frontend serves browser settings at runtime, but its
        # server-side API rewrite is built into the image with the projection
        # prefix. Changing the prefix would leave that route behind, so a
        # released install refuses it instead of breaking API calls silently.
        if path in (["backend", "api_prefix"], ["frontend", "api_prefix"]) and (
            not resolve().checkout
        ):
            raise RuntimeConfigurationError(
                "The API prefix is built into the published frontend image, so it "
                "cannot change on this install. Use a source checkout and "
                "hybro start --build to change it."
            )
        runtime.update_config(path, json.loads(args.value))
        print(
            "Configuration saved. Apply it with hybro start --recreate; no rebuild "
            "is needed."
        )
    elif args.command == "secret":
        if not sys.stdin.isatty():
            raise RuntimeConfigurationError(
                "Secret input requires an interactive terminal."
            )
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass("Credential (hidden): ")
            except getpass.GetPassWarning:
                raise RuntimeConfigurationError(
                    "Cannot read a hidden credential from this terminal."
                ) from None
        if not value.strip():
            raise RuntimeConfigurationError("Credential must not be blank.")
        if (
            args.name
            in {
                "default_agent_registrar_token",
                "default_agent_llm_token",
                "webhook_signing_key",
            }
            and len(value.encode()) < 32
        ):
            raise RuntimeConfigurationError(
                "Internal credentials must be at least 32 bytes."
            )
        with runtime._locked():
            data = runtime._read("auth.json")
            credential = parse_credential(data) if data else None
            document = _auth_object(data)
            document.setdefault("services", {})[args.name] = value
            runtime._replace(
                "auth.json",
                _credential_bytes(credential, json.dumps(document).encode()),
            )
        print("Credential saved. Restart affected services.")
    elif args.command == "show":
        print(runtime.read_config().model_dump_json(indent=2))
    else:
        runtime.load({})
        print("Configuration and credential structure valid; no Provider request made.")
    return 0


def _start(arguments: list[str]) -> int:
    parser = Parser(prog="hybro start")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Additional Compose overlay beside the stack, e.g. the CI stack.",
    )
    options = parser.parse_args(arguments)
    if options.build and not resolve().checkout:
        raise RuntimeConfigurationError(
            "This install has no source checkout; run hybro upgrade to change versions."
        )
    params = ["up", "-d", "--remove-orphans"]
    if options.build:
        params.append("--build")
    if options.recreate:
        params.append("--force-recreate")
    return compose(params, start=True, files=options.compose_file)


def _mcp(arguments: list[str]) -> int:
    parser = Parser(prog="hybro mcp")
    parser.add_argument("action", choices=("start", "stop", "status", "logs"))
    action = parser.parse_args(arguments).action
    if action == "start":
        # Independent control must not recreate or start unrelated services.
        return compose(["up", "-d", "--no-deps", "mcp"], start=True)
    if action == "stop":
        return compose(["stop", "mcp"])
    if action == "logs":
        return compose(["logs", "-f", "--tail", "100", "mcp"])
    status = read_mcp_status()
    print(
        f"MCP: {STATUS_LABELS[status]}\nServer URL: {MCP_URL}\nTransport: Streamable HTTP"
    )
    print("For clients on the machine running Hybro.")
    return 0 if status == "ready" else 1


def _logs(arguments: list[str]) -> int:
    if arguments[:1] != ["--container"]:
        return compose(["logs", "-f", *arguments])
    parser = Parser(prog="hybro logs --container")
    parser.add_argument("name")
    name = parser.parse_args(arguments[1:]).name
    return subprocess.run(
        ["docker", "logs", "--follow", "--tail", "100", "--", name],
        env=compose_environment(store(), start=False),
        check=False,
    ).returncode


# Direct commands, then plain Compose pass-throughs. Anything else is an
# explicit Compose subcommand. Help stays available without a terminal.
_HANDLERS: dict[str, Callable[[list[str]], int]] = {
    "help": _help,
    "--help": _help,
    "-h": _help,
    "--version": _version,
    "-v": _version,
    "config": config_command,
    "setup": _setup,
    "tui": _tui,
    "upgrade": _upgrade,
    "start": _start,
    "up": _start,
    "logs": _logs,
    "mcp": _mcp,
    "status": _passthrough("ps", "--all"),
    "ps": _passthrough("ps", "--all"),
    "stop": _passthrough("stop"),
    "down": _passthrough("down"),
    "restart": _passthrough("restart"),
}


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    command = arguments.pop(0) if arguments else "tui"
    handler = _HANDLERS.get(command)
    if handler is None:
        print("Unknown command; run hybro --help.", file=sys.stderr)
        return 1
    try:
        return handler(arguments)
    except (RuntimeConfigurationError, ValidationError) as exc:
        print(
            str(exc)
            if isinstance(exc, RuntimeConfigurationError)
            else "Invalid configuration; check field types and ranges.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, RecursionError):
        print(
            "Configuration or command failed; check JSON syntax and file access.",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
