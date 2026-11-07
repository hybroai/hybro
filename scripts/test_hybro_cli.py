"""Shell CLI tests: scripts/hybro runs against stub docker, curl, and npm on PATH.

Run with ``uv run --no-project --with pytest pytest scripts/test_hybro_cli.py``.
Every test runs under each available POSIX shell (sh, dash, busybox sh). No
real Docker, network, or npm call is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hybro"
# The tests follow the script's own version, which Release Please bumps.
VERSION = re.search(r'^HYBRO_VERSION="([0-9.]+)"', SCRIPT.read_text(), re.M).group(1)
_major, _minor, _ = VERSION.split(".")
NEWER = f"{_major}.{int(_minor) + 1}.0"
IMAGE = f"ghcr.io/hybroai/hybro:{VERSION}"
NEW_SCRIPT = "#!/bin/sh\nprintf 'NEW %s\\n' \"$*\"\n"

# Absolute paths: the tests run with a PATH of stubs and a few tools only.
SHELLS = [
    [shutil.which(command[0]), *command[1:]]
    for command in (["sh"], ["dash"], ["busybox", "sh"])
    if shutil.which(command[0])
    and (command[0] != "busybox" or subprocess.run([*command, "-c", ":"]).returncode == 0)
]

TOOLS = (
    "awk", "basename", "cat", "chmod", "date", "dirname", "find", "grep", "head",
    "mkdir", "mv", "readlink", "rm", "sed", "sha256sum", "shasum", "sleep",
    "uname",
)

STUB_PRELUDE = f"""#!{sys.executable}
import json, os, sys
STATE = os.environ["STUB_STATE"]
with open(STATE) as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(os.environ["STUB_CALLS"], "a") as handle:
    handle.write(json.dumps([os.path.basename(sys.argv[0]), *args]) + "\\n")
def save():
    with open(STATE, "w") as handle:
        json.dump(state, handle)
def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)
"""

DOCKER = """
containers = state.setdefault("containers", {})
volumes = state.setdefault("volumes", [])
command = args[0]
def helper():
    if state.get("exec_fail_times"):
        state["exec_fail_times"] -= 1
        save()
        sys.exit(1)
    if "-i" in args:
        state["exec_stdin"] = sys.stdin.read()
    if state.get("exec_stops") in containers:
        containers[state["exec_stops"]]["status"] = "exited"
    if state.get("exec_removes") in containers:
        containers.pop(state["exec_removes"])
    save()
    sys.stdout.write(state.get("exec_out", ""))
    sys.stdout.flush()
    if state.get("exec_block"):
        import signal, time
        signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
        time.sleep(30)
    sys.exit(state.get("exec_exit", 0))
if state.get("info_creates"):
    # Something else creates this file after the script checked for it.
    with open(state["info_creates"], "w") as handle:
        handle.write("other")
if command == "info":
    sys.exit(state.get("info_exit", 0))
if command == "volume":
    action, name = args[1], args[-1]
    if action == "inspect":
        sys.exit(0 if name in volumes else 1)
    if action == "create":
        volumes.append(name)
        save()
        print(name)
        sys.exit(0)
    if action == "rm":
        if name not in volumes:
            fail("no such volume")
        volumes.remove(name)
        save()
        sys.exit(0)
if command == "ps":
    label = next(a for a in args if a.startswith("label="))
    for name in state.get("projects", {}).get(label.split("=", 2)[2], []):
        print(name)
    sys.exit(0)
if command == "container" and args[1] == "inspect":
    template, name = args[3], args[4]
    if name not in containers:
        fail("Error: No such container: " + name)
    container = containers[name]
    if "State.Status" in template:
        print(container["status"])
    elif "ExitCode" in template:
        print(container.get("exit", 0))
    elif "Config.Image" in template:
        print(container["image"])
    elif "Config.Env" in template:
        print("\\n".join(container.get("env", [])))
    sys.exit(0)
if command == "image":
    if args[1] == "inspect":
        sys.exit(0 if args[2] in state.get("images", []) else 1)
    if args[1] == "ls":
        print("\\n".join(state.get("image_list", [])))
        sys.exit(0)
    if args[1] == "rm":
        sys.exit(0)
if command == "pull":
    state.setdefault("images", []).append(args[1])
    save()
    sys.exit(state.get("pull_exit", 0))
if command == "build":
    state.setdefault("images", []).append(args[2])
    save()
    sys.exit(0)
if command == "exec":
    if args[-3:-1] == ["test", "-f"]:
        if state.get("stop_on_probe"):
            containers[args[-4]]["status"] = "exited"
            save()
            sys.exit(1)
        sys.exit(1 if state.get("helper_missing") else 0)
    helper()
if command == "run":
    if "--entrypoint" in args:
        entrypoint = args[args.index("--entrypoint") + 1]
        if entrypoint.endswith("/python") and any(a.endswith("diagnostics_cli.py") for a in args):
            helper()
        if entrypoint == "test":
            sys.exit(state.get("marker_exit", 1))
        print("Copying the 0.3 MongoDB data and room files...", file=sys.stderr)
        if state.get("migrate_out"):
            print(state["migrate_out"])
        sys.exit(state.get("migrate_exit", 0))
    name = args[args.index("--name") + 1]
    ports = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
    if name in containers:
        fail("Conflict. The container name is already in use", 125)
    container = {
        "status": "running",
        "image": args[-1],
        "env": [args[i + 1] for i, a in enumerate(args) if a == "-e"],
        "port": ports[0].rsplit(":", 1)[0],
    }
    if state.get("busy_1455") and any("1455" in p for p in ports):
        containers[name] = {**container, "status": "created"}
        save()
        fail("Error: Bind for 127.0.0.1:1455 failed: port is already allocated", 125)
    containers[name] = container
    save()
    print("0123456789ab")
    sys.exit(0)
if command == "port":
    print(containers[args[1]]["port"])
    sys.exit(0)
if command == "stop":
    if state.get("stop_exit"):
        sys.exit(state["stop_exit"])
    for name in args[1:]:
        if name in containers:
            containers[name]["status"] = "exited"
    save()
    sys.exit(0)
if command == "rm":
    for name in args[1:]:
        containers.pop(name, None)
    save()
    sys.exit(0)
if command == "logs":
    if state.get("logs_exit"):
        print("Error response from daemon: boom", file=sys.stderr)
        sys.exit(state["logs_exit"])
    sample = state.get("logs_sample", "backend | ready")
    if "-t" in args:
        sample = "\\n".join("2026-11-03T00:00:00.000Z " + line for line in sample.splitlines())
    print(sample)
    sys.stdout.flush()
    if "-f" in args and state.get("logs_block"):
        import signal, time
        signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
        time.sleep(30)
    sys.exit(0)
fail("unexpected docker call: " + " ".join(args), 99)
"""

CURL = """
import hashlib
url = args[-1]
out = args[args.index("-o") + 1] if "-o" in args else None
if state.get("curl_exit"):
    sys.exit(state["curl_exit"])
if url.endswith("/releases/latest"):
    print(json.dumps({"url": "x", "tag_name": "v" + state["latest"], "name": "n"}, indent=2))
elif url.endswith("/hybro"):
    with open(out, "w") as handle:
        handle.write(state["new_script"])
elif url.endswith("/SHA256SUMS"):
    digest = state.get("sum") or hashlib.sha256(state["new_script"].encode()).hexdigest()
    with open(out, "w") as handle:
        handle.write(f"{'0' * 64}  install.sh\\n{digest}  hybro\\n")
else:
    fail("unexpected url " + url, 22)
"""

NPM = """
with open(state["npm_target"], "w") as handle:
    handle.write(state["new_script"])
os.chmod(state["npm_target"], 0o755)
"""


class Cli:
    def __init__(self, tmp: Path, shell: list[str]):
        self.tmp = tmp
        self.shell = shell
        self.bin = tmp / "stubs"
        self.bin.mkdir()
        # Only the stubs and these tools are on PATH, so a real docker, curl,
        # or npm is never reached.
        self.tools = tmp / "tools"
        self.tools.mkdir()
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                (self.tools / name).symlink_to(found)
        self.system: Path | None = None
        self.state_path = tmp / "state.json"
        self.calls_path = tmp / "calls.jsonl"
        self.home = tmp / "home"
        self.home.mkdir()
        self.hybro_home = self.home / ".hybro"
        for name, body in (("docker", DOCKER), ("curl", CURL), ("npm", NPM)):
            path = self.bin / name
            path.write_text(STUB_PRELUDE + body)
            path.chmod(0o755)
        self.script = self.install(tmp / "prefix" / "bin" / "hybro")
        self.state: dict = {"latest": VERSION, "new_script": NEW_SCRIPT}

    def install(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCRIPT, path)
        path.chmod(0o755)
        return path

    def checkout(self) -> Path:
        root = self.tmp / "checkout"
        (root / "backend").mkdir(parents=True)
        (root / "Dockerfile").write_text("FROM scratch\n")
        return self.install(root / "scripts" / "hybro")

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:{self.tools}",
            "HOME": str(self.home),
            "STUB_STATE": str(self.state_path),
            "STUB_CALLS": str(self.calls_path),
            "HYBRO_NO_UPDATE_CHECK": "1",
            **extra,
        }

    def run(self, *args: str, script: Path | None = None, **env: str):
        self.state_path.write_text(json.dumps(self.state))
        self.calls_path.write_text("")
        result = subprocess.run(
            [*self.shell, str(script or self.script), *args],
            env=self.env(**env),
            capture_output=True,
            text=True,
            timeout=30,
            # A private directory, so the default bundle names cannot collide.
            cwd=self.tmp,
            # No controlling terminal, so cleanup-old cannot read /dev/tty.
            start_new_session=True,
        )
        self.state = json.loads(self.state_path.read_text())
        self.output = result.stdout + result.stderr
        return result

    def calls(self, program: str = "docker") -> list[list[str]]:
        lines = self.calls_path.read_text().splitlines()
        return [call[1:] for call in map(json.loads, lines) if call[0] == program]

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:1] == ["run"]]

    def started(self) -> list[str]:
        """Arguments of the one `docker run -d`."""
        (run,) = [call for call in self.runs() if "-d" in call]
        return run


@pytest.fixture(params=SHELLS, ids=lambda shell: Path(shell[0]).name)
def cli(request, tmp_path):
    return Cli(tmp_path, request.param)


def options(run: list[str], flag: str) -> list[str]:
    return [run[i + 1] for i, arg in enumerate(run) if arg == flag]


def old_release_data(cli: Cli, *, containers=("hybro-backend-1", "hybro-mongo-1")):
    cli.state["volumes"] = ["hybro_mongo_data", "hybro_room_files", "hybro_redis_data"]
    cli.state["projects"] = {"hybro": list(containers)}


# ---- basics ----


def test_version_and_release_please_marker(cli):
    assert cli.run("--version").stdout == f"hybro {VERSION}\n"
    assert cli.run("version").stdout == f"hybro {VERSION}\n"
    marker = [line for line in SCRIPT.read_text().splitlines() if "x-release-please-version" in line]
    assert marker == [f'HYBRO_VERSION="{VERSION}" # x-release-please-version']


def test_help_and_unknown_commands(cli):
    assert "Usage: hybro [command]" in cli.run("--help").stdout
    result = cli.run("frobnicate")
    assert result.returncode == 2 and "unknown command: frobnicate" in result.stderr
    result = cli.run("setup")
    assert result.returncode == 1 and "Settings page" in result.stderr
    assert cli.calls() == []


def test_docker_missing_or_stopped(cli):
    cli.state["info_exit"] = 1
    result = cli.run("start")
    assert result.returncode == 1 and "Docker is not running" in result.stderr
    (cli.bin / "docker").unlink()
    result = cli.run("start")
    assert "Docker is not installed" in result.stderr
    assert "https://docs.docker.com/get-docker/" in result.stderr


# ---- start ----


def test_bare_hybro_pulls_runs_and_follows_logs(cli):
    result = cli.run()
    assert result.returncode == 1  # The stub's log stream ends: "Hybro stopped".
    assert ["pull", IMAGE] in cli.calls()
    run = cli.started()
    assert run[:4] == ["run", "-d", "--name", "hybro"]
    assert options(run, "-p") == ["127.0.0.1:3000:8080", "127.0.0.1:1455:1455"]
    assert options(run, "-e") == ["HYBRO_LISTEN=loopback", "HYBRO_PUBLIC_PORT=3000"]
    assert options(run, "-v") == [
        "hybro-data:/data",
        f"{cli.hybro_home}:/var/lib/hybro/runtime",
    ]
    assert options(run, "--add-host") == ["host.docker.internal:host-gateway"]
    assert options(run, "--stop-timeout") == ["60"]
    assert run[-1] == IMAGE
    calls = cli.calls()
    (probe,) = _health_probes(cli)
    assert probe[:2] == ["exec", "hybro"]
    assert '(8000, "/health"), (3000, "/"), (8080, "/health")' in probe[-1]
    assert calls.index(probe) < calls.index(["logs", "-f", "hybro"])
    assert f"Starting Hybro {VERSION}..." in cli.output
    assert re.search(r"Hybro is ready at http://localhost:3000 \(\d+s\)\. Press Ctrl-C to stop\.", cli.output)
    assert "Full logs: hybro logs -f" in cli.output
    assert "Hybro is starting at" not in cli.output
    assert cli.hybro_home.stat().st_mode & 0o777 == 0o700


def _health_probes(cli: Cli) -> list[list[str]]:
    return [call for call in cli.calls() if call[:1] == ["exec"] and any("/health" in a for a in call)]


MONGOD_SAMPLE = [
    'mongod | {"t":{"$date":"x"},"s":"I",  "c":"NETWORK",  "id":1, "msg":"Waiting for connections"}',
    'mongod | {"t":{"$date":"x"},"s":"D1", "c":"STORAGE",  "id":2, "msg":"debug"}',
    'mongod | {"t":{"$date":"x"},"s":"W",  "c":"CONTROL",  "id":3, "msg":"warn"}',
    'mongod | {"t":{"$date":"x"},"s":"E",  "c":"STORAGE",  "id":4, "msg":"error"}',
    'mongod | {"t":{"$date":"x"},"s":"F",  "c":"STORAGE",  "id":5, "msg":"fatal"}',
]


def test_foreground_shows_only_errors(cli):
    supervisord = "2026-11-07 02:27:30,001 WARN exited: backend (exit status 1; not expected)"
    cli.state["logs_sample"] = "\n".join([*LOGS_SAMPLE.splitlines(), *MONGOD_SAMPLE, supervisord])
    result = cli.run()
    assert result.returncode == 1  # The stub's log stream ends.
    shown = result.stdout.splitlines()
    shown = shown[shown.index("Full logs: hybro logs -f") + 1 :]
    assert shown == [
        'backend | {"timestamp":"t","level":"error","event":"job_iteration_failed","trace_id":"t-2"}',
        'caddy | {"level":"error","msg":"handled request","status":502}',
        *MONGOD_SAMPLE[3:],
        supervisord,
    ]
    assert "Hybro stopped (exit code" in result.stderr


def test_foreground_keeps_probing_until_ready(cli):
    cli.state["images"] = [IMAGE]
    cli.state["exec_fail_times"] = 2
    cli.run()
    assert len(_health_probes(cli)) == 3
    assert "Hybro is ready at http://localhost:3000" in cli.output
    assert ["logs", "-f", "hybro"] in cli.calls()


def test_foreground_timeout_leaves_hybro_running(cli):
    cli.state["images"] = [IMAGE]
    cli.state["exec_exit"] = 1
    # Each date call moves the clock 100 seconds, so the second check times out.
    clock = cli.tmp / "clock"
    clock.write_text("0")
    (cli.tools / "date").unlink()
    (cli.tools / "date").write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"p = Path({str(clock)!r}); now = int(p.read_text()) + 100; p.write_text(str(now)); print(now)\n"
    )
    (cli.tools / "date").chmod(0o755)
    result = cli.run()
    assert result.returncode == 1
    assert len(_health_probes(cli)) == 2
    assert "did not become ready within 180s; it is still running" in result.stderr
    assert ["logs", "--tail", "30", "hybro"] in cli.calls()
    assert not [call for call in cli.calls() if call[:1] == ["stop"]]
    assert cli.state["containers"]["hybro"]["status"] == "running"


def test_foreground_start_failure_prints_the_log_tail(cli):
    cli.state["images"] = [IMAGE]
    cli.state["exec_exit"] = 1
    cli.state["exec_stops"] = "hybro"
    cli.state["logs_sample"] = "backend | Traceback: boom"
    result = cli.run()
    assert result.returncode == 1
    assert ["logs", "--tail", "30", "hybro"] in cli.calls()
    assert ["logs", "-f", "hybro"] not in cli.calls()
    assert "backend | Traceback: boom" in cli.output
    assert "Hybro stopped before it was ready" in result.stderr
    assert "Hybro is ready" not in cli.output


def test_start_detached_with_an_existing_image(cli):
    cli.state["images"] = [IMAGE]
    result = cli.run("start", "-d")
    assert result.returncode == 0, cli.output
    assert not [call for call in cli.calls() if call[0] in ("pull", "logs", "exec")]
    assert "Hybro is starting at http://localhost:3000" in result.stdout
    assert "Stop it with: hybro stop" in result.stdout


def test_public_listener_with_port_and_insecure_flag(cli):
    assert cli.run("-d", "--listen", "0.0.0.0", "--port=8443", "--insecure-no-auth").returncode == 0
    run = cli.started()
    assert options(run, "-p")[0] == "0.0.0.0:8443:8080"
    assert options(run, "-e") == [
        "HYBRO_LISTEN=public",
        "HYBRO_PUBLIC_PORT=8443",
        "HYBRO_INSECURE_NO_AUTH=1",
    ]
    assert "<this machine's address>:8443" in cli.output


@pytest.mark.parametrize(
    ("address", "bind", "mode"),
    [
        ("localhost", "127.0.0.1", "loopback"),
        ("::1", "[::1]", "loopback"),
        ("192.168.1.5", "192.168.1.5", "public"),
        ("fd00::5", "[fd00::5]", "public"),
    ],
)
def test_listen_addresses(cli, address, bind, mode):
    assert cli.run("-d", "--listen", address).returncode == 0, cli.output
    run = cli.started()
    assert options(run, "-p")[0] == f"{bind}:3000:8080"
    assert f"HYBRO_LISTEN={mode}" in options(run, "-e")


@pytest.mark.parametrize(
    "args",
    [
        ["--port", "0"],
        ["--port", "65536"],
        ["--port", "08080"],
        ["--port", "http"],
        ["--port"],
        ["--listen", "example.com"],
        ["--listen", "1.2.3.4;rm"],
        ["--compose-file", "x.yml"],
    ],
)
def test_invalid_start_flags_start_nothing(cli, args):
    result = cli.run("start", *args)
    assert result.returncode != 0
    assert cli.runs() == []


def test_busy_oauth_port_falls_back_to_the_paste_flow(cli):
    cli.state["busy_1455"] = True
    assert cli.run("-d").returncode == 0, cli.output
    runs = [call for call in cli.runs() if "-d" in call]
    assert len(runs) == 2
    assert "127.0.0.1:1455:1455" not in options(runs[1], "-p")
    assert ["rm", "-f", "hybro"] in cli.calls()
    assert "ChatGPT login will use the paste flow" in cli.output
    assert cli.state["containers"]["hybro"]["status"] == "running"


def test_running_container_prints_its_address(cli):
    cli.state["containers"] = {
        "hybro": {"status": "running", "image": IMAGE, "port": "0.0.0.0:8443"}
    }
    result = cli.run("start", "--port", "9000")
    assert result.returncode == 0
    assert "already running at http://<this machine's address>:8443" in result.stdout
    assert cli.runs() == []


def test_stopped_container_is_replaced(cli):
    cli.state["images"] = [IMAGE]
    cli.state["containers"] = {"hybro": {"status": "exited", "image": IMAGE, "port": "x"}}
    assert cli.run("-d").returncode == 0, cli.output
    calls = cli.calls()
    assert calls.index(["rm", "-f", "hybro"]) < calls.index(cli.started())


def test_release_notes_checkout_only_flags(cli):
    cli.state["images"] = [IMAGE]
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    result = cli.run("start", "--recreate", "--build")
    assert result.returncode == 0
    assert "--recreate applies to a checkout only" in result.stdout
    assert "--build applies to a checkout only" in result.stdout
    assert not [call for call in cli.calls() if call[0] in ("build", "stop")]


def test_ctrl_c_stops_the_container(cli):
    cli.state["images"] = [IMAGE]
    cli.state["logs_block"] = True
    cli.state_path.write_text(json.dumps(cli.state))
    cli.calls_path.write_text("")
    process = subprocess.Popen(
        [*cli.shell, str(cli.script)],
        env=cli.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while ["logs", "-f", "hybro"] not in cli.calls():
        assert time.monotonic() < deadline, "logs never followed"
        time.sleep(0.1)
    time.sleep(0.3)
    os.killpg(process.pid, signal.SIGINT)
    output, _ = process.communicate(timeout=15)
    assert process.returncode == 0, output
    assert "Stopping Hybro" in output
    assert ["stop", "hybro"] in cli.calls()


@pytest.mark.parametrize("phase", ["probe", "sleep"])
def test_ctrl_c_while_starting_stops_the_container(cli, phase):
    cli.state["images"] = [IMAGE]
    if phase == "probe":
        cli.state["exec_block"] = True
    else:
        # SIGINT lands in the sleep after a failed probe; the next would pass.
        cli.state["exec_fail_times"] = 1
    cli.state_path.write_text(json.dumps(cli.state))
    cli.calls_path.write_text("")
    process = subprocess.Popen(
        [*cli.shell, str(cli.script)],
        env=cli.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while not _health_probes(cli):
        assert time.monotonic() < deadline, "health never probed"
        time.sleep(0.1)
    time.sleep(0.3)
    os.killpg(process.pid, signal.SIGINT)
    output, _ = process.communicate(timeout=15)
    assert process.returncode == 0, output
    assert "Stopping Hybro" in output
    assert ["stop", "hybro"] in cli.calls()
    assert ["logs", "-f", "hybro"] not in cli.calls()
    assert "Hybro is ready" not in output


# ---- dev mode ----


def test_checkout_builds_the_dev_image_and_never_checks_updates(cli):
    script = cli.checkout()
    result = cli.run("-d", script=script, HYBRO_NO_UPDATE_CHECK="")
    assert result.returncode == 0, cli.output
    assert ["build", "-t", "hybro:dev", str(script.parents[1])] in cli.calls()
    run = cli.started()
    assert run[3] == "hybro-dev" and run[-1] == "hybro:dev"
    assert options(run, "-p")[0] == "127.0.0.1:3100:8080"
    assert "hybro-dev-data:/data" in options(run, "-v")
    assert cli.calls("curl") == []
    assert "./scripts/hybro stop" in result.stdout


def test_checkout_rebuilds_only_with_build_and_recreates(cli):
    script = cli.checkout()
    cli.state["images"] = ["hybro:dev"]
    assert cli.run("-d", script=script).returncode == 0
    assert not [call for call in cli.calls() if call[0] == "build"]
    assert cli.run("-d", "--build", "--recreate", script=script).returncode == 0, cli.output
    calls = cli.calls()
    assert ["stop", "hybro-dev"] in calls
    assert [call for call in calls if call[0] == "build"]


def test_symlinked_checkout_script_is_still_dev_mode(cli):
    script = cli.checkout()
    link_dir = cli.tmp / "elsewhere" / "bin"
    link_dir.mkdir(parents=True)
    (cli.tmp / "elsewhere" / "hop").symlink_to(Path("../checkout/scripts/hybro"))
    (link_dir / "hybro").symlink_to(Path("../hop"))
    cli.state["images"] = ["hybro:dev"]
    assert cli.run("-d", script=link_dir / "hybro").returncode == 0, cli.output
    assert cli.started()[3] == "hybro-dev"
    assert script.exists()


# ---- 0.3 layout detection ----


@pytest.mark.parametrize("args", [[], ["start", "--recreate"], ["start", "--compose-file", "x"]])
def test_old_layout_only_notifies(cli, args):
    old_release_data(cli)
    result = cli.run(*args)
    assert result.returncode == 1
    assert "found data from Hybro 0.3" in result.stderr
    for name in ("hybro_mongo_data", "hybro_room_files", "hybro-backend-1"):
        assert name in result.stderr
    assert "hybro upgrade" in result.stderr and "hybro --fresh" in result.stderr
    assert [call for call in cli.calls() if call[0] in ("run", "pull", "stop", "rm", "volume") and call[1:2] != ["inspect"]] == []


def test_old_layout_with_only_containers(cli):
    cli.state["projects"] = {"hybro": ["hybro-frontend-1"]}
    result = cli.run("start")
    assert result.returncode == 1 and "hybro-frontend-1" in result.stderr


def test_checkout_detects_the_dev_project_only(cli):
    script = cli.checkout()
    old_release_data(cli)
    cli.state["images"] = ["hybro:dev"]
    assert cli.run("-d", script=script).returncode == 0
    cli.state["volumes"].append("hybro-dev_mongo_data")
    cli.state["containers"] = {}
    result = cli.run("-d", script=script)
    assert result.returncode == 1
    assert "hybro-dev_mongo_data" in result.stderr
    assert "./scripts/hybro upgrade" in result.stderr


def test_incomplete_migration_is_detected_with_the_pulled_image(cli):
    old_release_data(cli, containers=())
    cli.state["volumes"].append("hybro-data")
    result = cli.run("start")
    assert result.returncode == 1
    assert "migration that did not finish" in result.stderr
    calls = cli.calls()
    probe = ["run", "--rm", "--entrypoint", "test", "-v", "hybro-data:/data", IMAGE, "-f", "/data/hybro-volume.json"]
    assert calls.index(["pull", IMAGE]) < calls.index(probe)


def test_completed_volume_starts_despite_old_volumes(cli):
    old_release_data(cli, containers=())
    cli.state["volumes"].append("hybro-data")
    cli.state["marker_exit"] = 0
    assert cli.run("-d").returncode == 0, cli.output
    assert cli.started()


def test_marker_probe_failure_is_not_an_incomplete_migration(cli):
    old_release_data(cli, containers=())
    cli.state["volumes"].append("hybro-data")
    cli.state["marker_exit"] = 125
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 1
    assert "cannot read the completion marker" in result.stderr
    assert "hybro-data" in cli.state["volumes"]


def test_fresh_volume_without_old_data_is_not_probed(cli):
    cli.state["volumes"] = ["hybro-data"]
    cli.state["images"] = [IMAGE]
    assert cli.run("-d").returncode == 0
    assert [call for call in cli.runs() if "--entrypoint" in call] == []


def test_fresh_starts_over_old_data_and_replaces_an_unfinished_volume(cli):
    old_release_data(cli, containers=())
    assert cli.run("--fresh", "-d").returncode == 0, cli.output
    assert cli.started()
    cli.state["containers"] = {}
    cli.state["volumes"].append("hybro-data")
    # An unfinished migration volume is removed, the old volumes are not.
    assert cli.run("start", "--fresh", "-d").returncode == 0, cli.output
    assert ["volume", "rm", "hybro-data"] in cli.calls()
    assert "hybro_mongo_data" in cli.state["volumes"]


# ---- update notice ----


def test_daily_update_notice_is_cached(cli):
    cli.state["images"] = [IMAGE]
    cli.state["latest"] = NEWER
    result = cli.run("-d", HYBRO_NO_UPDATE_CHECK="")
    assert f"Hybro {NEWER} is available (you have {VERSION})" in result.stdout
    assert (cli.hybro_home / "update-check").read_text() == f"{NEWER}\n"
    assert len(cli.calls("curl")) == 1
    assert "--max-time" in cli.calls("curl")[0]
    cli.state["containers"] = {}
    result = cli.run("-d", HYBRO_NO_UPDATE_CHECK="")
    assert f"{NEWER} is available" in result.stdout
    assert cli.calls("curl") == []


def test_update_notice_can_be_disabled_and_survives_failures(cli):
    cli.state["images"] = [IMAGE]
    cli.state["latest"] = NEWER
    assert "available" not in cli.run("-d").stdout
    assert cli.calls("curl") == []
    cli.state["containers"] = {}
    cli.state["curl_exit"] = 28
    result = cli.run("-d", HYBRO_NO_UPDATE_CHECK="")
    assert result.returncode == 0 and "available" not in result.stdout


def test_stale_or_invalid_cache_is_ignored(cli):
    cli.state["images"] = [IMAGE]
    cli.hybro_home.mkdir(mode=0o700)
    cache = cli.hybro_home / "update-check"
    cache.write_text("9.9.9; rm -rf /\n")
    assert "available" not in cli.run("-d", HYBRO_NO_UPDATE_CHECK="").stdout
    cache.write_text("0.5.0\n")
    old = time.time() - 2 * 86400
    os.utime(cache, (old, old))
    cli.state["containers"] = {}
    cli.run("-d", HYBRO_NO_UPDATE_CHECK="")
    assert len(cli.calls("curl")) == 1


# ---- upgrade ----


def test_upgrade_check(cli):
    cli.state["latest"] = NEWER
    result = cli.run("upgrade", "--check")
    assert result.returncode == 1 and f"Hybro {NEWER} is available" in result.stdout
    cli.state["latest"] = VERSION
    result = cli.run("upgrade", "--check")
    assert result.returncode == 0 and "latest release" in result.stdout
    assert cli.calls() == []


def test_upgrade_installs_the_verified_script_and_continues(cli):
    cli.state["latest"] = NEWER
    result = cli.run("upgrade")
    assert result.returncode == 0, cli.output
    assert cli.script.read_text() == NEW_SCRIPT
    assert "NEW upgrade --continue" in result.stdout
    urls = [call[-1] for call in cli.calls("curl")]
    assert urls[1:] == [
        f"https://github.com/hybroai/hybro/releases/download/v{NEWER}/hybro",
        f"https://github.com/hybroai/hybro/releases/download/v{NEWER}/SHA256SUMS",
    ]
    assert sorted(p.name for p in cli.script.parent.iterdir()) == ["hybro"]


def test_upgrade_refuses_a_checksum_mismatch(cli):
    cli.state["latest"] = NEWER
    cli.state["sum"] = "f" * 64
    result = cli.run("upgrade")
    assert result.returncode == 1 and "does not match SHA256SUMS" in result.stderr
    assert cli.script.read_bytes() == SCRIPT.read_bytes()
    assert sorted(p.name for p in cli.script.parent.iterdir()) == ["hybro"]


def test_upgrade_through_npm(cli):
    package = cli.tmp / "npm" / "lib" / "node_modules" / "@hybroai" / "cli" / "bin" / "hybro"
    cli.install(package)
    link = cli.tmp / "npm" / "bin" / "hybro"
    link.parent.mkdir(parents=True)
    link.symlink_to(Path("../lib/node_modules/@hybroai/cli/bin/hybro"))
    cli.state["latest"] = NEWER
    cli.state["npm_target"] = str(package)
    result = cli.run("upgrade", script=link)
    assert result.returncode == 0, cli.output
    assert cli.calls("npm") == [["install", "-g", f"@hybroai/cli@{NEWER}"]]
    assert [call for call in cli.calls("curl") if "-o" in call] == []
    assert "NEW upgrade --continue" in result.stdout


def test_upgrade_migrates_0_3_data_and_starts(cli):
    old_release_data(cli)
    cli.state["migrate_out"] = "port=3456"
    cli.hybro_home.mkdir(mode=0o700)
    result = cli.run("upgrade")
    assert result.returncode == 0, cli.output
    calls = cli.calls()
    stop = ["stop", "hybro-backend-1", "hybro-mongo-1"]
    create = ["volume", "create", "hybro-data"]
    (migration,) = [call for call in cli.runs() if "/app/backend/volume_migration.py" in call]
    assert calls.index(["pull", IMAGE]) < calls.index(stop) < calls.index(create) < calls.index(migration)
    assert ["rm", "hybro-backend-1", "hybro-mongo-1"] in calls
    assert options(migration, "-v") == [
        "hybro_mongo_data:/source/db:ro",
        "hybro_room_files:/source/files:ro",
        "hybro-data:/data",
        f"{cli.hybro_home}:/var/lib/hybro/runtime:ro",
    ]
    assert migration[-3:] == [IMAGE, "/app/backend/volume_migration.py", "release"]
    assert calls.index(migration) < calls.index(cli.started())
    assert "hybro cleanup-old" in result.stdout
    assert "--port 3456" in result.stdout
    # Copy, never delete: no source volume was touched.
    assert [call for call in calls if call[:2] == ["volume", "rm"]] == []
    assert {"hybro_mongo_data", "hybro_room_files", "hybro_redis_data"} <= set(cli.state["volumes"])


def test_failed_migration_removes_only_the_new_volume(cli):
    old_release_data(cli, containers=())
    cli.state["migrate_exit"] = 1
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 1
    assert "0.3 volumes are unchanged" in result.stderr
    assert ["volume", "rm", "hybro-data"] in cli.calls()
    assert "hybro-data" not in cli.state["volumes"]
    assert "hybro_mongo_data" in cli.state["volumes"]
    assert not [call for call in cli.runs() if "-d" in call]


def test_unfinished_migration_is_redone(cli):
    old_release_data(cli, containers=())
    cli.state["volumes"].append("hybro-data")
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 0, cli.output
    calls = cli.calls()
    assert calls.index(["volume", "rm", "hybro-data"]) < calls.index(["volume", "create", "hybro-data"])
    assert "--port" not in result.stdout


def test_migration_aborts_when_old_containers_cannot_stop(cli):
    old_release_data(cli)
    cli.state["stop_exit"] = 1
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 1 and "cannot stop the 0.3 containers" in result.stderr
    assert ["volume", "create", "hybro-data"] not in cli.calls()


def test_upgrade_recreates_a_running_container_with_its_address(cli):
    cli.state["volumes"] = ["hybro-data"]
    cli.state["containers"] = {
        "hybro": {
            "status": "running",
            "image": "ghcr.io/hybroai/hybro:0.2.9",
            "port": "0.0.0.0:8443",
            "env": ["PATH=/usr/bin", "HYBRO_LISTEN=public", "HYBRO_INSECURE_NO_AUTH=1"],
        }
    }
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 0, cli.output
    run = cli.started()
    assert options(run, "-p")[0] == "0.0.0.0:8443:8080"
    assert options(run, "-e") == ["HYBRO_LISTEN=public", "HYBRO_PUBLIC_PORT=8443", "HYBRO_INSECURE_NO_AUTH=1"]
    assert run[-1] == IMAGE
    calls = cli.calls()
    assert calls.index(["stop", "hybro"]) < calls.index(run)


def test_upgrade_leaves_a_current_container_alone(cli):
    cli.state["volumes"] = ["hybro-data"]
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    result = cli.run("upgrade", "--continue")
    assert result.returncode == 0 and "up to date" in result.stdout
    assert cli.runs() == []


def test_checkout_upgrade_only_migrates(cli):
    script = cli.checkout()
    cli.state["volumes"] = ["hybro-dev_mongo_data"]
    cli.state["images"] = ["hybro:dev"]
    result = cli.run("upgrade", script=script)
    assert result.returncode == 0, cli.output
    assert cli.calls("curl") == [] and not [c for c in cli.calls() if c[0] in ("pull", "build")]
    (migration,) = [call for call in cli.runs() if "/app/backend/volume_migration.py" in call]
    assert "hybro-dev_mongo_data:/source/db:ro" in options(migration, "-v")
    assert migration[-1] == "dev"
    assert "git pull" in result.stdout


# ---- stop, status, logs ----


def test_stop_status_and_logs(cli):
    result = cli.run("status")
    assert result.returncode == 1 and "not running" in result.stdout
    assert "not running" in cli.run("stop").stdout
    assert cli.run("logs").returncode == 1
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    result = cli.run("status")
    assert result.returncode == 0 and "running at http://localhost:3000" in result.stdout
    assert IMAGE in result.stdout
    assert "backend | ready" in cli.run("logs", "--tail", "5").stdout
    assert ["logs", "--tail", "5", "hybro"] in cli.calls()
    assert cli.run("stop").returncode == 0
    assert ["stop", "hybro"] in cli.calls()
    assert cli.state["containers"]["hybro"]["status"] == "exited"


LOGS_SAMPLE = "\n".join(
    [
        'backend | {"timestamp":"t","level":"info","event":"http_request_completed","trace_id":"t-1"}',
        'backend | {"timestamp":"t","level":"error","event":"job_iteration_failed","trace_id":"t-2"}',
        'caddy | {"level":"info","msg":"handled request","status":200,"trace_id":"t-1","path":"/api/v1/rooms"}',
        'caddy | {"level":"info","msg":"handled request","status":404}',
        'caddy | {"level":"error","msg":"handled request","status":502}',
        'caddy | {"level":"warn","msg":"synthetic warn line for the alias test"}',
        'mcp | {"level":"warning","event":"mcp_backend_call_failed","trace_id":"t-1"}',
        'backend | {"level":"info","event":"http_request_completed","trace_id":"t-10"}',
        'mongod | {"t":{"$date":"x"},"s":"I","msg":"m"}',
        "backend | timestamp=t level=warning event=redis_lock_operation_failed risk_level=info trace_id=t-3",
        "agent | log_level=error plain text",
    ]
)


def _logs(cli: Cli, *args: str) -> list[str]:
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    cli.state["logs_sample"] = LOGS_SAMPLE
    result = cli.run("logs", *args)
    assert result.returncode == 0, cli.output
    return result.stdout.splitlines()


def test_logs_filter_by_service(cli):
    lines = _logs(cli, "--service", "backend")
    assert lines and all(line.startswith("backend | ") for line in lines)
    assert len(lines) == 4
    assert ["logs", "hybro"] in cli.calls()


def test_logs_level_is_a_threshold_across_formats(cli):
    lines = _logs(cli, "--tail", "5", "--level", "warning")
    assert ["logs", "--tail", "5", "hybro"] in cli.calls()
    assert lines == [
        'backend | {"timestamp":"t","level":"error","event":"job_iteration_failed","trace_id":"t-2"}',
        'caddy | {"level":"error","msg":"handled request","status":502}',
        'caddy | {"level":"warn","msg":"synthetic warn line for the alias test"}',
        'mcp | {"level":"warning","event":"mcp_backend_call_failed","trace_id":"t-1"}',
        "backend | timestamp=t level=warning event=redis_lock_operation_failed risk_level=info trace_id=t-3",
    ]


def test_logs_trace_matches_exactly(cli):
    lines = _logs(cli, "--trace=t-1")
    assert len(lines) == 3
    assert all("t-10" not in line for line in lines)
    assert any(line.startswith("caddy | ") for line in lines)


def test_logs_filters_combine_and_follow_passes_through(cli):
    lines = _logs(cli, "-f", "--service=caddy", "--level=error")
    assert lines == ['caddy | {"level":"error","msg":"handled request","status":502}']
    assert ["logs", "-f", "hybro"] in cli.calls()


def test_logs_level_accepts_uppercase_and_warn(cli):
    assert _logs(cli, "--level", "ERROR") == _logs(cli, "--level", "error")
    assert _logs(cli, "--level", "warn") == _logs(cli, "--level", "warning")


def _logs_of(cli: Cli, sample: list[str], *args: str) -> list[str]:
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    cli.state["logs_sample"] = "\n".join(sample)
    result = cli.run("logs", *args)
    assert result.returncode == 0, cli.output
    return result.stdout.splitlines()


def test_logs_level_counts_caddy_fatal_levels_as_critical(cli):
    severe = [
        'caddy | {"level":"dpanic","msg":"d"}',
        'caddy | {"level":"panic","msg":"p"}',
        'caddy | {"level":"fatal","msg":"f"}',
    ]

    assert _logs_of(cli, severe, "--level", "critical") == severe
    assert _logs_of(cli, severe, "--level", "debug") == severe


def test_logs_level_reads_only_the_line_level(cli):
    sample = [
        'backend | {"timestamp":"t","level":"info","event":"library_message","message":"x level=error y"}',
        'backend | {"timestamp":"t","level":"info","event":"e","detail":{"level":"error"}}',
        'mcp | timestamp=t level=info event=library_message message="x level=error y"',
        "mcp | timestamp=t level=error event=mcp_tool_called",
        "mcp | level=error event=e",
        "agent | INFO retry failed level=error",
    ]

    assert _logs_of(cli, sample, "--level", "error") == [
        "mcp | timestamp=t level=error event=mcp_tool_called",
        "mcp | level=error event=e",
    ]


def test_logs_level_reads_mongod_severity(cli):
    assert _logs_of(cli, MONGOD_SAMPLE, "--level", "warning") == MONGOD_SAMPLE[2:]
    assert _logs_of(cli, MONGOD_SAMPLE, "--level", "critical") == MONGOD_SAMPLE[4:]
    assert _logs_of(cli, MONGOD_SAMPLE, "--level", "debug") == MONGOD_SAMPLE


def test_logs_service_filter_works_with_timestamps(cli):
    lines = _logs(cli, "-t", "--service", "caddy")
    assert len(lines) == 4
    assert all(" caddy | " in line for line in lines)


@pytest.mark.parametrize("args", [("--service", "backend"), ("--level", "error")])
def test_logs_filter_keeps_docker_errors_and_exit_status(cli, args):
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    cli.state["logs_exit"] = 3
    result = cli.run("logs", *args)
    assert result.returncode == 3
    assert "Error response from daemon: boom" in cli.output


@pytest.mark.parametrize(
    "args",
    [("--level", "nope"), ("--trace",), ("--service=",), ("--trace", "")],
    ids=["unknown_level", "missing_value", "empty_service", "empty_trace"],
)
def test_logs_rejects_bad_filters_before_docker(cli, args):
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    result = cli.run("logs", *args)
    assert result.returncode == 1
    assert args[0].split("=")[0] in cli.output
    assert cli.calls() == []


def test_logs_rejects_an_unknown_level_before_docker(cli):
    cli.state["containers"] = {"hybro": {"status": "running", "image": IMAGE, "port": "127.0.0.1:3000"}}
    result = cli.run("logs", "--level", "nope")
    assert result.returncode == 1
    assert "--level" in cli.output
    assert cli.calls() == []


# ---- doctor ----

HELPER = "/app/backend/diagnostics_cli.py"
PYTHON = "/app/backend/.venv/bin/python"
BUNDLE_NAME = re.compile(r"hybro-diagnostics-[0-9]{8}-[0-9]{6}\.tar\.gz")


def container(cli: Cli, status: str = "running") -> None:
    cli.state["containers"] = {"hybro": {"status": status, "image": IMAGE, "port": "127.0.0.1:3000"}}


def helper_runs(cli: Cli) -> list[list[str]]:
    return [call for call in cli.calls() if HELPER in call and call[-2:] != ["-f", HELPER]]


def bundles(cli: Cli) -> list[str]:
    return sorted(path.name for path in cli.tmp.iterdir() if path.name.startswith(("hybro-diagnostics-", "out.tgz")))


@pytest.mark.parametrize(("code", "expected"), [(0, 0), (1, 1), (2, 2), (126, 2), (137, 2)])
def test_doctor_runs_the_helper_in_the_container(cli, code, expected):
    container(cli)
    cli.state["exec_out"] = "Hybro 0.4.4, instance x\nProblems: none\n"
    cli.state["exec_exit"] = code
    result = cli.run("doctor")
    assert result.returncode == expected, cli.output
    assert result.stdout == cli.state["exec_out"]
    calls = cli.calls()
    probe = ["exec", "hybro", "test", "-f", HELPER]
    assert calls.index(probe) < calls.index(["exec", "hybro", PYTHON, HELPER, "doctor"])


def test_doctor_json_passes_the_flag(cli):
    container(cli)
    cli.state["exec_out"] = "{}"
    assert cli.run("doctor", "--json").returncode == 0
    assert helper_runs(cli) == [["exec", "hybro", PYTHON, HELPER, "doctor", "--json"]]


@pytest.mark.parametrize("case", ["no_container", "stopped", "docker_stopped"])
def test_doctor_that_cannot_run_exits_2_before_exec(cli, case):
    if case == "stopped":
        container(cli, "exited")
    if case == "docker_stopped":
        container(cli)
        cli.state["info_exit"] = 1
    result = cli.run("doctor")
    assert result.returncode == 2, cli.output
    assert not [call for call in cli.calls() if call[0] == "exec"]
    expected = {
        "no_container": "Start Hybro with: hybro",
        "stopped": "doctor --bundle",
        "docker_stopped": "Docker is not running",
    }[case]
    assert expected in result.stderr
    if case == "stopped":
        assert "exited" in result.stderr


def test_doctor_bundle_pipes_the_logs_into_the_helper(cli):
    container(cli)
    cli.state["logs_sample"] = "backend | ready\ncaddy | {}"
    cli.state["exec_out"] = "ARCHIVE"
    result = cli.run("doctor", "--bundle")
    assert result.returncode == 0, cli.output
    calls = cli.calls()
    assert ["logs", "--tail", "2000", "hybro"] in calls
    assert ["exec", "-i", "hybro", PYTHON, HELPER, "bundle"] in calls
    assert cli.state["exec_stdin"] == "backend | ready\ncaddy | {}\n"
    (name,) = bundles(cli)
    assert BUNDLE_NAME.fullmatch(name)
    assert (cli.tmp / name).read_text() == "ARCHIVE"
    assert not list(cli.tmp.glob("*.partial"))
    assert name in result.stdout
    assert "runtime snapshot" in result.stdout
    assert "review it before sharing" in result.stdout
    assert (cli.tmp / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("flag", [["--output", "out.tgz"], ["--output=out.tgz"]])
def test_doctor_bundle_output_option(cli, flag):
    container(cli)
    cli.state["exec_out"] = "ARCHIVE"
    assert cli.run("doctor", "--bundle", *flag).returncode == 0, cli.output
    assert bundles(cli) == ["out.tgz"]
    assert (cli.tmp / "out.tgz").read_text() == "ARCHIVE"


def test_doctor_bundle_refuses_an_existing_partial_file(cli):
    container(cli)
    (cli.tmp / "out.tgz.partial").write_text("other")
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert "out.tgz.partial" in result.stderr
    assert (cli.tmp / "out.tgz.partial").read_text() == "other"
    assert not (cli.tmp / "out.tgz").exists()
    assert not [call for call in cli.calls() if call[0] in ("logs", "exec", "run")]


def test_doctor_bundle_refuses_an_existing_file(cli):
    container(cli)
    (cli.tmp / "out.tgz").write_text("old")
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert (cli.tmp / "out.tgz").read_text() == "old"
    assert not [call for call in cli.calls() if call[0] in ("logs", "exec", "run")]


def test_doctor_bundle_of_a_stopped_container_runs_the_image_offline(cli):
    container(cli, "exited")
    cli.state["logs_sample"] = "backend | stopped"
    cli.state["exec_out"] = "OFFLINE"
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 0, cli.output
    (run,) = cli.runs()
    assert run == ["run", "--rm", "-i", "--entrypoint", PYTHON, IMAGE, HELPER, "bundle", "--offline"]
    calls = cli.calls()
    assert calls.index(["pull", IMAGE]) < calls.index(run)
    assert ["logs", "--tail", "2000", "hybro"] in calls
    assert not [call for call in calls if call[0] == "exec"]
    assert cli.state["exec_stdin"] == "backend | stopped\n"
    assert (cli.tmp / "out.tgz").read_text() == "OFFLINE"
    assert "container logs only" in result.stdout
    assert "runtime snapshot" not in result.stdout


def test_doctor_on_an_older_image_suggests_the_bundle_first(cli):
    container(cli)
    cli.state["helper_missing"] = True
    result = cli.run("doctor")
    assert result.returncode == 2
    assert "older image" in result.stderr
    message = result.stderr
    assert message.index("hybro doctor --bundle") < message.index("hybro stop") < message.index("hybro start")
    assert helper_runs(cli) == []
    assert not [call for call in cli.calls() if call[0] == "logs"]


def test_doctor_bundle_on_an_older_image_runs_the_script_image_offline(cli):
    container(cli)
    cli.state["helper_missing"] = True
    cli.state["exec_out"] = "OFFLINE"
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 0, cli.output
    (run,) = cli.runs()
    assert run == ["run", "--rm", "-i", "--entrypoint", PYTHON, IMAGE, HELPER, "bundle", "--offline"]
    assert not [call for call in cli.calls() if call[:2] == ["exec", "-i"]]
    assert ["logs", "--tail", "2000", "hybro"] in cli.calls()
    assert (cli.tmp / "out.tgz").read_text() == "OFFLINE"
    assert "container logs only" in result.stdout


def test_doctor_reports_a_container_that_stopped_during_the_check(cli):
    container(cli)
    cli.state["stop_on_probe"] = True
    result = cli.run("doctor")
    assert result.returncode == 2
    assert "not running" in result.stderr and "exited" in result.stderr
    assert "older image" not in result.stderr
    assert helper_runs(cli) == []


def test_doctor_bundle_without_a_container(cli):
    result = cli.run("doctor", "--bundle")
    assert result.returncode == 2
    assert "Start Hybro with: hybro" in result.stderr
    assert not [call for call in cli.calls() if call[0] in ("logs", "run", "pull", "exec")]
    assert bundles(cli) == []


def test_doctor_bundle_when_the_image_cannot_be_pulled(cli):
    container(cli, "exited")
    cli.state["pull_exit"] = 1
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert "cannot pull" in result.stderr
    assert cli.runs() == []
    assert sorted(path.name for path in cli.tmp.glob("out.tgz*")) == []


def test_doctor_bundle_removes_the_partial_file_when_the_helper_fails(cli):
    container(cli)
    cli.state["exec_out"] = "half"
    cli.state["exec_exit"] = 1
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert sorted(path.name for path in cli.tmp.glob("out.tgz*")) == []


@pytest.mark.parametrize(
    "args",
    [["--frobnicate"], ["--json", "--bundle"], ["--output", "x.tgz"], ["--bundle", "--output"], ["--bundle", "--output="]],
)
def test_doctor_usage_errors_exit_2_before_docker(cli, args):
    container(cli)
    result = cli.run("doctor", *args)
    assert result.returncode == 2
    assert cli.calls() == []


def test_doctor_status_1_from_a_container_that_stopped_is_exit_2(cli):
    container(cli)
    cli.state["exec_stops"] = "hybro"
    cli.state["exec_exit"] = 1
    result = cli.run("doctor")
    assert result.returncode == 2
    assert "not running" in result.stderr and "exited" in result.stderr


def test_doctor_bundle_refuses_a_partial_file_created_after_the_check(cli):
    container(cli)
    cli.state["info_creates"] = str(cli.tmp / "out.tgz.partial")
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert (cli.tmp / "out.tgz.partial").read_text() == "other"
    assert not (cli.tmp / "out.tgz").exists()
    assert not [call for call in cli.calls() if call[0] in ("logs", "run") or call[:2] == ["exec", "-i"]]


def test_doctor_bundle_of_a_container_removed_meanwhile_is_refused(cli):
    container(cli)
    cli.state["exec_removes"] = "hybro"
    cli.state["exec_out"] = "ARCHIVE"
    result = cli.run("doctor", "--bundle", "--output", "out.tgz")
    assert result.returncode == 2
    assert "removed" in result.stderr
    assert sorted(path.name for path in cli.tmp.glob("out.tgz*")) == []


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)], ids=["TERM", "INT"])
def test_doctor_bundle_interrupted_leaves_no_file(cli, sig, code):
    container(cli)
    cli.state["exec_out"] = "PARTIAL"
    cli.state["exec_block"] = True
    cli.state_path.write_text(json.dumps(cli.state))
    cli.calls_path.write_text("")
    partial = cli.tmp / "out.tgz.partial"
    process = subprocess.Popen(
        [*cli.shell, str(cli.script), "doctor", "--bundle", "--output", "out.tgz"],
        env=cli.env(),
        cwd=cli.tmp,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while not (partial.exists() and partial.read_text() == "PARTIAL"):
        assert process.poll() is None, process.communicate()[0]
        assert time.monotonic() < deadline, "the helper never wrote"
        time.sleep(0.05)
    # The whole group, as a terminal would: POSIX shells run a trap only once
    # the foreground child has ended, so the stub must receive it too.
    os.killpg(process.pid, sig)
    output, _ = process.communicate(timeout=15)
    assert process.returncode == code, output
    assert sorted(path.name for path in cli.tmp.glob("out.tgz*")) == []


# ---- cleanup-old ----


OLD_IMAGES = [
    "ghcr.io/hybroai/hybro-backend:0.3.0",
    "ghcr.io/hybroai/hybro-echo-agent:0.3.0",
    IMAGE,
    "mongo:7.0",
    "hybro-dev-backend:latest",
]


def test_cleanup_refuses_before_a_migration(cli):
    old_release_data(cli)
    result = cli.run("cleanup-old", "--yes")
    assert result.returncode == 1 and "migrate first" in result.stderr
    cli.state["volumes"].append("hybro-data")
    result = cli.run("cleanup-old", "--yes")
    assert result.returncode == 1 and "did not finish" in result.stderr
    assert not [call for call in cli.calls() if call[0] == "rm" or call[1:2] == ["rm"]]


def test_cleanup_asks_and_removes_only_0_3_items(cli):
    old_release_data(cli)
    cli.state["volumes"].append("hybro-data")
    cli.state["marker_exit"] = 0
    cli.state["image_list"] = OLD_IMAGES
    result = cli.run("cleanup-old")
    assert "Nothing was removed" in result.stdout
    assert "ghcr.io/hybroai/hybro-backend:0.3.0" in result.stdout
    result = cli.run("cleanup-old", "-y")
    assert result.returncode == 0, cli.output
    calls = cli.calls()
    assert ["rm", "-f", "hybro-backend-1"] in calls
    assert ["volume", "rm", "hybro_redis_data"] in calls
    removed = [call[2] for call in calls if call[:2] == ["image", "rm"]]
    assert removed == ["ghcr.io/hybroai/hybro-backend:0.3.0", "ghcr.io/hybroai/hybro-echo-agent:0.3.0"]
    assert cli.state["volumes"] == ["hybro-data"]


def test_cleanup_with_nothing_to_remove(cli):
    cli.state["image_list"] = [IMAGE]
    result = cli.run("cleanup-old")
    assert result.returncode == 0 and "No Hybro 0.3" in result.stdout


def test_install_script_is_valid_posix_sh():
    for shell in SHELLS:
        for path in (SCRIPT, ROOT / "install.sh"):
            assert subprocess.run([*shell, "-n", str(path)]).returncode == 0, (shell, path)


def test_help_lists_every_command():
    text = subprocess.run(["sh", str(SCRIPT), "help"], capture_output=True, text=True).stdout
    for command in ("start", "stop", "status", "logs", "doctor", "upgrade", "cleanup-old", "version"):
        assert f"\n  {command} " in text
    for option in ("--service", "--level", "--trace", "--bundle", "--json", "--output"):
        assert f"\n  {option} " in text


# ---- install.sh ----

INSTALLED = '#!/bin/sh\nHYBRO_VERSION="0.4.0" # x-release-please-version\n'


@pytest.fixture
def installer(cli):
    """install.sh with its system directory moved into the test's tree."""
    system = cli.tmp / "usr-local-bin"
    system.mkdir()
    path = cli.tmp / "install.sh"
    path.write_text((ROOT / "install.sh").read_text().replace("SYSTEM_BIN=/usr/local/bin", f"SYSTEM_BIN={system}"))
    cli.state["new_script"] = INSTALLED
    cli.system = system
    return path


def test_install_into_local_bin_on_path(cli, installer):
    local = cli.home / ".local" / "bin"
    result = cli.run(script=installer, PATH=f"{cli.bin}:{cli.tools}:{local}")
    assert result.returncode == 0, cli.output
    assert (local / "hybro").read_text() == INSTALLED
    assert os.access(local / "hybro", os.X_OK)
    assert sorted(p.name for p in local.iterdir()) == ["hybro"]
    assert ["pull", "ghcr.io/hybroai/hybro:0.4.0"] in cli.calls()
    urls = [call[-1] for call in cli.calls("curl")]
    assert urls == [
        "https://github.com/hybroai/hybro/releases/latest/download/hybro",
        "https://github.com/hybroai/hybro/releases/latest/download/SHA256SUMS",
    ]
    assert "Start Hybro with: hybro" in result.stdout


def test_install_prefers_a_writable_system_directory_off_path(cli, installer):
    result = cli.run(script=installer)
    assert result.returncode == 0, cli.output
    assert (cli.system / "hybro").read_text() == INSTALLED
    assert not (cli.home / ".local" / "bin").exists()


def test_install_prints_the_path_line_otherwise(cli, installer):
    cli.system.chmod(0o555)
    try:
        result = cli.run(script=installer)
    finally:
        cli.system.chmod(0o755)
    assert result.returncode == 0, cli.output
    local = cli.home / ".local" / "bin"
    assert (local / "hybro").exists()
    assert f'export PATH="{local}:$PATH"' in result.stdout


def test_install_refuses_a_checksum_mismatch(cli, installer):
    cli.state["sum"] = "0" * 63 + "1"
    result = cli.run(script=installer)
    assert result.returncode == 1 and "does not match SHA256SUMS" in result.stderr
    assert list(cli.system.iterdir()) == []
    assert cli.calls() == []


def test_install_needs_docker_and_a_supported_system(cli, installer):
    (cli.bin / "docker").unlink()
    result = cli.run(script=installer)
    assert result.returncode == 1 and "https://docs.docker.com/get-docker/" in result.stderr
    uname = cli.bin / "uname"
    uname.write_text('#!/bin/sh\n[ "$1" = -s ] && echo FreeBSD || echo amd64\n')
    uname.chmod(0o755)
    result = cli.run(script=installer)
    assert result.returncode == 1 and "macOS and Linux" in result.stderr
    assert list(cli.system.iterdir()) == []


def test_install_without_running_docker_skips_the_pull(cli, installer):
    cli.state["info_exit"] = 1
    result = cli.run(script=installer)
    assert result.returncode == 0, cli.output
    assert "Docker is not running" in result.stdout
    assert [call for call in cli.calls() if call[0] == "pull"] == []


def test_fresh_validates_flags_before_removing_an_unfinished_volume(cli):
    old_release_data(cli, containers=())
    cli.state["volumes"].append("hybro-data")
    result = cli.run("--fresh", "--port", "abc")
    assert result.returncode != 0
    assert "hybro-data" in cli.state["volumes"]
