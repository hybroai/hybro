"""Entrypoint of the single Hybro image; runs before the process supervisor.

Root phase: map the service identity from the owner of the mounted config
directory, take ownership of the data volume once, and re-exec as that user.

User phase: load and validate the configuration, generate missing internal
credentials, enforce the public-listener rule, compute every service's scoped
projection, write the Caddy and supervisor configuration, prepare the embedded
MongoDB replica set, then exec supervisord.

Each supervised program starts through ``runtime_entrypoint.py run <name>``,
which reads that program's own environment from a private file, prefixes its
log lines with the program name, and forwards stop signals.

``runtime_entrypoint.py restart [--check]`` is the Settings page's restart
helper: it plans the services from the current config.json like a start does,
refuses changes only a full restart applies (Caddy or the program set), and
restarts every service except Caddy and mongod through supervisorctl.
"""

from __future__ import annotations

import contextlib
import fcntl
import grp
import json
import os
import pwd
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Any, Literal

RUNTIME_DIR = Path("/var/lib/hybro/runtime")
DATA_DIR = Path("/data")
APP_DIR = Path("/app")
RUN_DIR = Path("/tmp/hybro")
MANIFEST = APP_DIR / "bundled_agents.yaml"
# Written last on a fresh or migrated volume; the CLI looks for it (`hybro`).
VOLUME_MARKER = DATA_DIR / "hybro-volume.json"
MONGOD_BINARY = Path("/usr/bin/mongod")
BACKEND_PYTHON = APP_DIR / "backend" / ".venv" / "bin" / "python"
AGENTS_PYTHON = APP_DIR / "default_agents" / ".venv" / "bin" / "python"

CADDY_PORT = 8080
BACKEND_ORIGIN = "http://127.0.0.1:8000"
MONGO_HOST = "127.0.0.1:27017"
REPLICA_SET = "rs0"
# A constant, not a user setting: config.json stays readable by 0.3 (rollback).
WIRED_TIGER_CACHE_GB = "0.25"
MONGO_READY_SECONDS = 60.0
PROBE_TIMEOUT_MS = 1000
# Stop budget, in seconds. Caddy closes proxied streams first, so they end
# normally; the backend's longer bound only cancels connections that bypass
# Caddy. A service's stop wait covers that bound plus the backend's SSE drain
# (shutdown_drain_seconds, 5) before supervisord kills it; mongod stops after
# the services, and both waits together stay under the CLI's --stop-timeout 60.
CADDY_GRACE_SECONDS = 3
BACKEND_GRACEFUL_SHUTDOWN_SECONDS = 5  # MCP/server.py uses the same value.
SERVICE_STOP_WAIT_SECONDS = 15
MONGOD_STOP_WAIT_SECONDS = 30

# The restart helper's status heartbeat; the backend treats a status older
# than 60 seconds as a restart that died.
RESTART_HEARTBEAT_SECONDS = 10.0
SUPERVISORCTL_TIMEOUT_SECONDS = 300
# Services the restart helper never restarts: Caddy keeps the port open and
# mongod keeps the data; a change to either needs a full restart.
KEEP_RUNNING = frozenset({"caddy", "mongod"})
# The helper's summary lines go to PID 1's output, so they show in `hybro logs`.
PID1_STDOUT = Path("/proc/1/fd/1")

# Variables every program may see; everything else is scoped per program.
# Only the backend learns where the config directory is (see projections()).
# All programs share one UID, so this scopes environments, not file access.
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

Listen = Literal["loopback", "public"]


class EntrypointError(Exception):
    """A safe, user-facing startup failure."""


# ---------------------------------------------------------------- root phase


def service_identity(uid: int) -> Literal["root", "existing", "create"]:
    """How the services run for a config directory owned by ``uid``.

    UID 0 is what rootless Docker shows for the host user, so services run as
    root, which maps back to that user on the host.
    """
    if uid == 0:
        return "root"
    try:
        pwd.getpwuid(uid)
    except KeyError:
        return "create"
    return "existing"


def root_phase() -> None:
    owner = RUNTIME_DIR.stat()
    uid, gid = owner.st_uid, owner.st_gid
    identity = service_identity(uid)
    if identity == "create":
        try:
            grp.getgrgid(gid)
        except KeyError:
            subprocess.run(["groupadd", "--gid", str(gid), "hybro"], check=True)
        subprocess.run(
            [
                "useradd",
                "--uid",
                str(uid),
                "--gid",
                str(gid),
                "--no-create-home",
                "--home-dir",
                str(DATA_DIR),
                "--shell",
                "/usr/sbin/nologin",
                "hybro",
            ],
            check=True,
        )
    # Once after a fresh volume or a migration, not on every start.
    if DATA_DIR.stat().st_uid != uid:
        subprocess.run(["chown", "-R", f"{uid}:{gid}", str(DATA_DIR)], check=True)
    if identity == "root":
        return
    # supervisord reopens /dev/stdout for the program logs, which needs access
    # to the container's root-owned output pipes.
    for fd in (1, 2):
        try:
            os.fchown(fd, uid, gid)
        except OSError:
            pass
    os.environ["HOME"] = str(DATA_DIR)
    os.execvp(
        "setpriv",
        [
            "setpriv",
            f"--reuid={uid}",
            f"--regid={gid}",
            "--init-groups",
            "--",
            sys.executable,
            os.path.abspath(__file__),
        ],
    )


# ---------------------------------------------------------------- user phase


@dataclass(frozen=True)
class Listener:
    mode: Listen
    public_port: int
    insecure_no_auth: bool


def read_listener(environment: Mapping[str, str]) -> Listener:
    """The script's transport variables; a missing ``HYBRO_LISTEN`` is public."""
    mode = environment.get("HYBRO_LISTEN", "public")
    if mode not in ("loopback", "public"):
        raise EntrypointError("HYBRO_LISTEN must be loopback or public.")
    raw_port = environment.get("HYBRO_PUBLIC_PORT", str(CADDY_PORT))
    if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise EntrypointError("HYBRO_PUBLIC_PORT must be a port from 1 to 65535.")
    return Listener(
        mode=mode,
        public_port=int(raw_port),
        insecure_no_auth=environment.get("HYBRO_INSECURE_NO_AUTH") == "1",
    )


def check_listener(
    listener: Listener, *, auth_mode: str, server_url: str | None
) -> None:
    """A public listener needs Clerk and a public origin, or an explicit opt-out."""
    if listener.mode != "public":
        return
    if auth_mode == "clerk" and server_url:
        return
    if listener.insecure_no_auth:
        print(
            "WARNING: Hybro is listening publicly without authentication. Anyone "
            "who can reach this port controls the instance and its provider "
            "credential.",
            file=sys.stderr,
        )
        return
    raise EntrypointError(
        'A public listener needs auth_mode "clerk" and frontend.server_url in '
        "config.json. To run without authentication anyway, pass "
        "--insecure-no-auth (HYBRO_INSECURE_NO_AUTH=1)."
    )


@dataclass(frozen=True)
class BundledProcess:
    name: str
    directory: str
    port: int


def manifest_processes(path: Path) -> tuple[BundledProcess, ...]:
    """Enabled manifest agents with their directories, validated like boot registration."""
    import yaml

    from agent.bundled import load_manifest

    agents = load_manifest(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["agents"]
    return tuple(
        BundledProcess(agent.name, str(raw[agent.name]["dir"]), agent.port)
        for agent in agents
    )


@dataclass(frozen=True)
class Program:
    command: list[str]
    cwd: str
    env: dict[str, str]


def projections(
    *,
    listener: Listener,
    api_prefix: str,
    frontend: Mapping[str, Any],
    image_size: str,
    secrets: Mapping[str, str],
    agents: tuple[BundledProcess, ...],
    embedded_mongo: bool,
) -> dict[str, Program]:
    """Each program's command and scoped environment."""
    from common.config.loader import FrontendSettings

    public = listener.mode == "public"
    configured_url = frontend.get("server_url")
    local_origin = f"http://localhost:{listener.public_port}"
    server_url = configured_url if public and configured_url else local_origin
    origins = (
        [server_url]
        if public
        else [local_origin, f"http://127.0.0.1:{listener.public_port}"]
    )
    settings = FrontendSettings.model_validate(
        {
            "api_prefix": api_prefix,
            **frontend,
            # Same origin: Caddy serves the API, MCP, and the UI on one port.
            "api_base_url": "",
            "server_url": server_url,
        }
    )
    frontend_config = {
        **settings.model_dump(),
        # Derived, not user-settable: MCP has no authentication yet.
        "mcp_disabled": public,
    }
    backend_dir = str(APP_DIR / "backend")
    programs = {
        "backend": Program(
            [
                str(BACKEND_PYTHON),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--workers",
                "1",
                # An open SSE stream would otherwise hold the stop until
                # supervisord's kill, skipping the backend's own shutdown.
                "--timeout-graceful-shutdown",
                str(BACKEND_GRACEFUL_SHUTDOWN_SECONDS),
            ],
            backend_dir,
            {
                "HYBRO_HOME": str(RUNTIME_DIR),
                "HYBRO_CONTAINER": "image",
                "HYBRO_FRONTEND_ORIGINS": ",".join(origins),
                "LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS": ",".join(
                    str(agent.port) for agent in agents
                ),
            },
        ),
        "mcp": Program(
            [str(BACKEND_PYTHON), "server.py", "--image"],
            str(APP_DIR / "MCP"),
            {
                "HYBRO_MCP_CONFIG": json.dumps({"api_prefix": api_prefix}),
                # The server imports only the backend's logging modules.
                "PYTHONPATH": str(APP_DIR / "backend"),
            },
        ),
        "frontend": Program(
            ["node", "server.js"],
            str(APP_DIR / "frontend"),
            {
                "NODE_ENV": "production",
                "HOSTNAME": "127.0.0.1",
                "PORT": "3000",
                "HYBRO_FRONTEND_CONFIG": json.dumps(frontend_config),
                "CLERK_SECRET_KEY": secrets.get("clerk_secret_key", ""),
                "CLERK_WEBHOOK_SECRET": secrets.get("clerk_webhook_secret", ""),
            },
        ),
        "caddy": Program(
            [
                "caddy",
                "run",
                "--config",
                str(RUN_DIR / "Caddyfile"),
                "--adapter",
                "caddyfile",
            ],
            str(RUN_DIR),
            {
                "XDG_DATA_HOME": str(RUN_DIR / "caddy"),
                "XDG_CONFIG_HOME": str(RUN_DIR / "caddy"),
            },
        ),
    }
    agent_config = json.dumps(
        {
            "base_url": f"{BACKEND_ORIGIN}{api_prefix}/internal/llm",
            "token": secrets["default_agent_llm_token"],
            "image_size": image_size,
        }
    )
    for agent in agents:
        directory = APP_DIR / "default_agents" / agent.directory
        # Run the file, never `python -m`, so `from agent import ...` resolves
        # to the agent's own directory first.
        programs[f"agent-{agent.name}"] = Program(
            [str(AGENTS_PYTHON), str(directory / "__main__.py")],
            str(directory),
            {
                "HYBRO_AGENT_CONFIG": agent_config,
                "SERVER_HOST": "127.0.0.1",
                "SERVER_DOMAIN": "127.0.0.1",
                "SERVER_PORT": str(agent.port),
                "PYTHONPATH": str(APP_DIR / "default_agents"),
            },
        )
    if embedded_mongo:
        programs["mongod"] = Program(mongod_command(), str(DATA_DIR), {})
    return programs


def mongod_command() -> list[str]:
    return [
        "mongod",
        "--dbpath",
        str(DATA_DIR / "db"),
        "--bind_ip",
        "127.0.0.1",
        "--port",
        "27017",
        "--replSet",
        REPLICA_SET,
        "--wiredTigerCacheSizeGB",
        WIRED_TIGER_CACHE_GB,
    ]


def caddyfile(*, listener: Listener, api_prefix: str) -> str:
    """Route one port by path; loopback accepts only local Host names."""
    lines = [
        "{",
        "\tadmin off",
        "\tauto_https off",
        "\tpersist_config off",
        # The default grace period is unbounded: an open SSE stream would hold
        # the stop until supervisord kills Caddy.
        f"\tgrace_period {CADDY_GRACE_SECONDS}s",
        # Error lines (a 5xx from a handler) carry the request through the
        # default logger, outside the site's access-log filter. Response
        # headers are dropped too, should an access line ever reach it.
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
        "}",
        "",
        f":{CADDY_PORT} {{",
        # JSON access log on stdout, one format with the backend's lines.
        # Request and response headers and the URI (query strings can carry
        # tokens) are dropped; the request id and the bare path are appended.
        # The id is the backend's response header, so it matches the backend's
        # own id even when the client sent an invalid one or none.
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
    if listener.mode == "loopback":
        # A DNS-rebound request carries the attacker's host name. Any port is
        # allowed, so SSH and IDE port forwarding keep working.
        lines += [
            "\t@foreign not host localhost 127.0.0.1 ::1",
            # Named-matcher handle blocks run in file order, first match wins;
            # a bare `respond` would sort after them.
            "\thandle @foreign {",
            "\t\trespond 421",
            "\t}",
        ]
    lines += [
        f"\t@backend path {api_prefix} {api_prefix}/* /health",
        "\thandle @backend {",
        "\t\treverse_proxy 127.0.0.1:8000 {",
        "\t\t\tflush_interval -1",
        "\t\t}",
        "\t}",
    ]
    if listener.mode == "loopback":
        # MCP has no authentication, so a public listener omits the route.
        lines += [
            "\t@mcp path /mcp /mcp/*",
            "\thandle @mcp {",
            "\t\treverse_proxy 127.0.0.1:8001 {",
            "\t\t\tflush_interval -1",
            "\t\t}",
            "\t}",
        ]
    lines += [
        "\thandle {",
        "\t\treverse_proxy 127.0.0.1:3000",
        "\t}",
        "}",
        "",
    ]
    return "\n".join(lines)


def supervisord_conf(programs: Mapping[str, Program]) -> str:
    """mongod starts first and stops last; the services stop together before it."""
    lines = [
        "[supervisord]",
        "nodaemon=true",
        "logfile=/dev/null",
        "logfile_maxbytes=0",
        f"pidfile={RUN_DIR / 'supervisord.pid'}",
        "loglevel=warn",
        "",
        "[unix_http_server]",
        f"file={RUN_DIR / 'supervisor.sock'}",
        "chmod=0700",
        "",
        "[rpcinterface:supervisor]",
        "supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface",
        "",
        "[supervisorctl]",
        f"serverurl=unix://{RUN_DIR / 'supervisor.sock'}",
    ]
    launcher = f"{BACKEND_PYTHON} {APP_DIR / 'backend' / 'runtime_entrypoint.py'} run"
    for name in programs:
        lines += [
            "",
            f"[program:{name}]",
            f"command={launcher} {name}",
            "autorestart=true",
            "startsecs=2",
            "stopsignal=TERM",
            # A launcher killed after stopwaitsecs must not orphan its child.
            "killasgroup=true",
            "stopwaitsecs="
            + str(
                MONGOD_STOP_WAIT_SECONDS
                if name == "mongod"
                else SERVICE_STOP_WAIT_SECONDS
            ),
            f"priority={10 if name == 'mongod' else 100}",
            "stdout_logfile=/dev/stdout",
            "stdout_logfile_maxbytes=0",
            "redirect_stderr=true",
        ]
    services = [name for name in programs if name != "mongod"]
    lines += [
        "",
        "[group:services]",
        f"programs={','.join(services)}",
        "priority=100",
        "",
    ]
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def replace_private(path: Path, text: str) -> None:
    """Replace ``path`` with a fresh 0600 file in one step.

    A reader never sees a partial file, and a loosened mode from an earlier
    write does not survive.
    """
    staging = path.with_name(f".{path.name}.new")
    staging.unlink(missing_ok=True)
    write_private(staging, text)
    os.replace(staging, path)


def write_diagnostics_handoff(api_prefix: str) -> None:
    """Hand ``hybro doctor`` and the backend a token made for this start."""
    replace_private(
        RUN_DIR / "diagnostics.json",
        json.dumps(
            {
                "base_url": f"{BACKEND_ORIGIN}{api_prefix}/diagnostics",
                "token": token_hex(32),
            }
        ),
    )


def mark_volume_complete(
    details: Mapping[str, Any], marker: Path | None = None
) -> None:
    """Write the completion marker atomically, so it exists only when complete."""
    marker = marker or VOLUME_MARKER
    partial = marker.with_name(marker.name + ".partial")
    partial.write_text(
        json.dumps({"complete": True, **details}, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(partial, marker)


# ------------------------------------------------------------------ MongoDB


def ensure_replica_set(client: Any) -> str:
    """Initiate ``rs0`` on a fresh volume, or repoint migrated data at loopback.

    Returns what was done: ``initiated``, ``reconfigured``, or ``ready``.
    """
    from pymongo.errors import OperationFailure

    member = {"_id": 0, "host": MONGO_HOST}
    try:
        config = client.admin.command("replSetGetConfig")["config"]
    except OperationFailure as exc:
        if exc.code != 94:  # NotYetInitialized
            raise
        client.admin.command(
            "replSetInitiate", {"_id": REPLICA_SET, "members": [member]}
        )
        return "initiated"
    hosts = [entry["host"] for entry in config["members"]]
    if hosts == [MONGO_HOST]:
        return "ready"
    # 0.3 data names `mongo:27017`; a forced reconfig is idempotent.
    first = config["members"][0]
    config["members"] = [{**first, "host": MONGO_HOST}]
    config["version"] = int(config["version"]) + 1
    client.admin.command("replSetReconfig", config, force=True)
    return "reconfigured"


def wait_for_primary(client: Any, *, timeout: float = MONGO_READY_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.admin.command("hello").get("isWritablePrimary"):
            return
        time.sleep(0.5)
    raise EntrypointError("MongoDB did not become primary in time.")


def _log_tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def wait_for_mongod(
    process: Any, connect: Any, *, timeout: float = MONGO_READY_SECONDS
) -> Any:
    """Return once ``mongod`` answers a ping; fail fast when it exits first.

    Each probe uses a short server-selection timeout, so an exited process is
    noticed within a second.
    """
    from pymongo.errors import PyMongoError

    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            raise EntrypointError(f"mongod exited with code {code} during preparation.")
        client = connect(PROBE_TIMEOUT_MS)
        try:
            client.admin.command("ping")
            return
        except PyMongoError:
            pass
        finally:
            client.close()
        if time.monotonic() > deadline:
            raise EntrypointError("mongod did not accept connections in time.")
        time.sleep(0.5)


def prepare_mongodb(
    *,
    start: Any = subprocess.Popen,
    connect: Any = None,
    log: Path | None = None,
    inspect: Any = None,
) -> Any:
    """Initiate or repoint the replica set with a temporary ``mongod``.

    mongod's output goes to a private log whose tail is shown on failure, so a
    data directory it cannot use explains itself instead of a driver timeout.
    ``inspect(client)`` runs on the primary before shutdown; its result is
    returned.
    """
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    if connect is None:

        def connect(timeout_ms: int) -> Any:
            return MongoClient(
                f"mongodb://{MONGO_HOST}/?directConnection=true",
                serverSelectionTimeoutMS=timeout_ms,
            )

    log = log or RUN_DIR / "mongod-prepare.log"
    (DATA_DIR / "db").mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as output:
        process = start(mongod_command(), stdout=output, stderr=subprocess.STDOUT)
    client = None
    result = None
    try:
        wait_for_mongod(process, connect)
        # The reconfig and the election can drop connections briefly; this
        # client lets the driver wait for the server to come back.
        client = connect(int(MONGO_READY_SECONDS * 1000))
        action = ensure_replica_set(client)
        wait_for_primary(client)
        print(f"MongoDB replica set {REPLICA_SET}: {action}", file=sys.stderr)
        if inspect is not None:
            result = inspect(client)
        try:
            client.admin.command("shutdown")
        except PyMongoError:  # The server closes the connection while stopping.
            pass
        # Recorded only after a clean stop, so the data is known to reopen.
        if process.wait(timeout=30) == 0:
            record_replica_set()
    except (EntrypointError, PyMongoError, subprocess.TimeoutExpired) as exc:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        detail = str(exc) if isinstance(exc, EntrypointError) else type(exc).__name__
        tail = _log_tail(log)
        raise EntrypointError(
            f"MongoDB preparation failed: {detail}"
            + (f"\nLast mongod output:\n{tail}" if tail else "")
        ) from None
    finally:
        if client is not None:
            client.close()
    return result


def _replica_set_record() -> Path:
    # Inside the database directory, so a wiped or replaced db/ loses it too.
    return DATA_DIR / "db" / "hybro-replica-set.json"


def _expected_record() -> dict[str, str] | None:
    """The replica set, and the mongod binary that last opened the data.

    Another binary (a MongoDB upgrade) prepares again, so data it cannot open
    fails at preparation with mongod's output rather than under supervisord.
    """
    try:
        binary = MONGOD_BINARY.stat()
    except OSError:
        return None
    return {
        "replica_set": REPLICA_SET,
        "host": MONGO_HOST,
        "mongod": f"{binary.st_size}:{binary.st_mtime_ns}",
    }


def record_replica_set() -> None:
    """Note that this data's replica set is ``rs0`` on loopback; best effort.

    Without the record the next start only prepares again, so a failed write
    never fails a start that has already prepared.
    """
    expected = _expected_record()
    record = _replica_set_record()
    partial = record.with_name(record.name + ".partial")
    try:
        if expected is None:
            raise OSError(f"cannot read {MONGOD_BINARY}")
        partial.write_text(json.dumps(expected) + "\n", encoding="utf-8")
        os.replace(partial, record)
    except OSError as exc:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        print(
            f"Warning: cannot record the replica set ({exc.strerror or exc}); "
            "the next start prepares it again.",
            file=sys.stderr,
        )


def replica_set_recorded() -> bool:
    try:
        recorded = json.loads(_replica_set_record().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    expected = _expected_record()
    return expected is not None and recorded == expected


def _stop_preparation(_signum: int, _frame: object) -> None:
    raise EntrypointError("Stopped during MongoDB preparation.")


@dataclass(frozen=True)
class RuntimePlan:
    programs: dict[str, Program]
    caddyfile: str
    embedded_mongo: bool
    api_prefix: str
    # restart_revision of the document the programs were projected from.
    services_revision: str


def plan_runtime(
    store: Any, listener: Listener, *, check_only: bool = False
) -> RuntimePlan:
    """Validate the configuration and project every program from it.

    A check reads the internal credentials without generating missing ones,
    so a pre-flight never writes ``auth.json``.
    """
    from common.config.loader import get_settings
    from common.config.runtime_config import RuntimeConfigurationError

    try:
        # A credential that cannot be resolved still boots (credential_invalid).
        config = store.load({}).config
        if check_only:
            secrets = store.read_service_credentials()
        else:
            secrets = store.ensure_service_credentials()
        settings = get_settings()
    except (RuntimeConfigurationError, ValueError) as exc:
        raise EntrypointError(f"Invalid configuration: {exc}") from None
    check_listener(
        listener,
        auth_mode=settings.auth_mode,
        server_url=config.frontend.get("server_url"),
    )
    # A configured MongoDB URL wins over the embedded server.
    embedded_mongo = (
        "mongodb_url" not in config.backend and "mongodb_url" not in secrets
    )
    if check_only:
        # Generated on the real run; the check compares no environments.
        secrets = {"default_agent_llm_token": "", **secrets}
    programs = projections(
        listener=listener,
        api_prefix=settings.api_prefix,
        frontend=config.frontend,
        image_size=config.image_size,
        secrets=secrets,
        agents=manifest_processes(MANIFEST),
        embedded_mongo=embedded_mongo,
    )
    return RuntimePlan(
        programs=programs,
        caddyfile=caddyfile(listener=listener, api_prefix=settings.api_prefix),
        embedded_mongo=embedded_mongo,
        api_prefix=settings.api_prefix,
        services_revision=config.restart_revision,
    )


def base_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}


def program_spec(program: Program, base: Mapping[str, str]) -> str:
    """The private file ``run_program`` reads at each program start."""
    return json.dumps(
        {"command": program.command, "cwd": program.cwd, "env": {**base, **program.env}}
    )


def write_runtime_record(listener: Listener, plan: RuntimePlan) -> None:
    """This start's transport and services revision; not a configuration source."""
    replace_private(
        RUN_DIR / "runtime.json",
        json.dumps(
            {
                "listen": listener.mode,
                "public_port": listener.public_port,
                "insecure_no_auth": listener.insecure_no_auth,
                "embedded_mongo": plan.embedded_mongo,
                "services_revision": plan.services_revision,
            }
        ),
    )


def user_phase() -> None:
    from common.config.runtime_store import RuntimeConfigStore

    store = RuntimeConfigStore(RUNTIME_DIR)
    listener = read_listener(os.environ)
    plan = plan_runtime(store, listener)
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = base_environment()
    for name, program in plan.programs.items():
        write_private(
            RUN_DIR / "programs" / f"{name}.json", program_spec(program, base)
        )
    write_private(RUN_DIR / "Caddyfile", plan.caddyfile)
    write_private(RUN_DIR / "supervisord.conf", supervisord_conf(plan.programs))
    write_diagnostics_handoff(plan.api_prefix)
    write_runtime_record(listener, plan)
    if plan.embedded_mongo and replica_set_recorded():
        # A temporary mongod start and stop costs seconds on every start; the
        # record is written only after a preparation that completed.
        print(f"MongoDB replica set {REPLICA_SET}: ready (recorded)", file=sys.stderr)
    elif plan.embedded_mongo:
        # A stop during preparation shuts the temporary mongod down cleanly.
        signal.signal(signal.SIGTERM, _stop_preparation)
        prepare_mongodb()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    # A volume that never finished its first start is completed here; a
    # migrated volume already has its marker from the migration.
    if not VOLUME_MARKER.exists():
        mark_volume_complete({"created": "fresh"})
    os.execvpe(
        "supervisord",
        ["supervisord", "-c", str(RUN_DIR / "supervisord.conf")],
        base,
    )


# ------------------------------------------------------------ restart helper

FULL_RESTART = "This change needs a full restart: run `hybro stop` and `hybro`."
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _supervisorctl(command: list[str], *, timeout: float) -> Any:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# The heartbeat thread and the helper share one staging file.
_STATUS_LOCK = threading.Lock()


def write_restart_status(state: str, error: str | None = None) -> None:
    with _STATUS_LOCK:
        replace_private(
            RUN_DIR / "restart.json",
            json.dumps({"state": state, "error": error, "updated_at": time.time()}),
        )


def _announce(line: str) -> None:
    """One summary line in PID 1's output (``hybro logs``); best effort."""
    with contextlib.suppress(OSError):
        with open(PID1_STDOUT, "a", encoding="utf-8") as output:
            output.write(f"restart | {line}\n")


def read_runtime_record() -> tuple[Listener, dict[str, Any]]:
    try:
        record = json.loads((RUN_DIR / "runtime.json").read_text(encoding="utf-8"))
        listener = Listener(
            mode=record["listen"],
            public_port=record["public_port"],
            insecure_no_auth=record["insecure_no_auth"],
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise EntrypointError(
            f"Cannot read this start's runtime record. {FULL_RESTART}"
        ) from None
    if (
        listener.mode not in ("loopback", "public")
        or not isinstance(listener.public_port, int)
        or not isinstance(listener.insecure_no_auth, bool)
    ):
        raise EntrypointError(
            f"Cannot read this start's runtime record. {FULL_RESTART}"
        )
    return listener, record


def _check_restartable(plan: RuntimePlan) -> None:
    """Only the services restart; Caddy and the program set stay as started."""
    try:
        current_caddyfile = (RUN_DIR / "Caddyfile").read_text(encoding="utf-8")
        current_conf = (RUN_DIR / "supervisord.conf").read_text(encoding="utf-8")
    except OSError:
        raise EntrypointError(FULL_RESTART) from None
    # supervisord.conf names exactly the programs, mongod among them when
    # MongoDB is embedded.
    if plan.caddyfile != current_caddyfile or (
        supervisord_conf(plan.programs) != current_conf
    ):
        raise EntrypointError(FULL_RESTART)


@contextlib.contextmanager
def _heartbeat() -> Iterator[None]:
    """Refresh the status while a supervisorctl call runs."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(RESTART_HEARTBEAT_SECONDS):
            with contextlib.suppress(OSError):
                write_restart_status("restarting")

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _call(run: Runner, command: list[str]) -> Any:
    write_restart_status("restarting")
    try:
        with _heartbeat():
            return run(command, timeout=SUPERVISORCTL_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        raise EntrypointError("supervisorctl did not complete.") from None
    finally:
        write_restart_status("restarting")


def _not_running(output: str, names: list[str]) -> list[str]:
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return [
        f"{name} ({states.get(name, 'UNKNOWN')})"
        for name in names
        if states.get(name) != "RUNNING"
    ]


def _restart_services(plan: RuntimePlan, record: dict[str, Any], run: Runner) -> str:
    base = base_environment()
    services = [name for name in plan.programs if name not in KEEP_RUNNING]
    for name in services:
        replace_private(
            RUN_DIR / "programs" / f"{name}.json",
            program_spec(plan.programs[name], base),
        )
    names = [f"services:{name}" for name in services]
    control = ["supervisorctl", "-c", str(RUN_DIR / "supervisord.conf")]
    restarted = _call(run, [*control, "restart", *names])
    if restarted.returncode != 0:
        raise EntrypointError(
            f"supervisorctl restart exited with code {restarted.returncode}."
        )
    # The restart exit code alone does not prove each service came back.
    status = _call(run, [*control, "status", *names])
    failed = _not_running(status.stdout or "", names)
    if failed:
        raise EntrypointError("Some services did not start: " + ", ".join(failed))
    replace_private(
        RUN_DIR / "runtime.json",
        json.dumps({**record, "services_revision": plan.services_revision}),
    )
    return ", ".join(services)


def restart(*, check: bool, run: Runner = _supervisorctl) -> int:
    """Plan from config.json and restart every service but Caddy and mongod.

    Prints one JSON result line. ``check`` validates without side effects.
    No rollback on failure: supervisord keeps retrying the services, and
    the backend reads config.json itself, so old program files would not
    restore the old state.
    """
    from common.config.runtime_store import RuntimeConfigStore

    try:
        listener, record = read_runtime_record()
        plan = plan_runtime(RuntimeConfigStore(RUNTIME_DIR), listener, check_only=check)
        _check_restartable(plan)
    except EntrypointError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        if not check:
            with contextlib.suppress(OSError):
                write_restart_status("failed", str(exc))
            _announce(f"failed: {exc}")
        return 1
    if check:
        print(json.dumps({"ok": True}))
        return 0
    with open(RUN_DIR / "restart.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another restart is running and owns the status file.
            print(json.dumps({"ok": True, "skipped": True}))
            _announce("skipped: another restart is running.")
            return 0
        try:
            restarted = _restart_services(plan, record, run)
        except (EntrypointError, OSError) as exc:
            error = (
                str(exc)
                if isinstance(exc, EntrypointError)
                else "Cannot write the service files."
            )
            print(json.dumps({"ok": False, "error": error}))
            with contextlib.suppress(OSError):
                write_restart_status("failed", error)
            _announce(f"failed: {error}")
            return 1
        write_restart_status("completed")
        _announce(f"completed: restarted {restarted}.")
        print(json.dumps({"ok": True}))
        return 0


# -------------------------------------------------------------- program run


def run_program(name: str) -> int:
    """Start one supervised program with its scoped environment and log prefix."""
    spec = json.loads(
        (RUN_DIR / "programs" / f"{name}.json").read_text(encoding="utf-8")
    )
    child = subprocess.Popen(
        spec["command"],
        cwd=spec["cwd"],
        env=spec["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def forward(signum: int, _frame: object) -> None:
        # Keep waiting until the child exits, so supervisord sees mongod's real stop.
        if child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    prefix = f"{name} | ".encode()
    assert child.stdout is not None
    out = sys.stdout.buffer
    for line in iter(child.stdout.readline, b""):
        out.write(prefix + line)
        out.flush()
    return child.wait()


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "run":
        return run_program(argv[2])
    # Before the root branch: services that run as root (a UID 0 config
    # directory) call this too, and must never start a second supervisord.
    if len(argv) >= 2 and argv[1] == "restart":
        if argv[2:] not in ([], ["--check"]):
            print("usage: runtime_entrypoint.py restart [--check]", file=sys.stderr)
            return 2
        return restart(check=argv[2:] == ["--check"])
    try:
        if os.geteuid() == 0:
            root_phase()
        user_phase()
    except EntrypointError as exc:
        print(f"hybro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
