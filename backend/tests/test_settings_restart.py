"""Settings restart trigger: status file, check subprocess, detached launch.

The check runs a local fixture script in place of the entrypoint; nothing
touches supervisord, Docker, or the network.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from instance_settings import SettingsRequestError
from instance_settings import restart as restart_module
from instance_settings.restart import STALE_SECONDS, RestartControl


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "entrypoint.py"
    path.write_text(body)
    return path


OK = 'import json, sys\nassert sys.argv[1:] == ["restart", "--check"]\nprint(json.dumps({"ok": True}))\n'
INVALID = (
    "import json, sys\n"
    'print("noise")\n'
    'print(json.dumps({"ok": False, "error": "Invalid configuration: bad"}))\n'
    "sys.exit(1)\n"
)


def control(tmp_path: Path, body: str = OK, **kwargs) -> RestartControl:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    runtime = run / "runtime.json"
    runtime.write_text("{}")
    return RestartControl(
        runtime_file=runtime,
        python=sys.executable,
        entrypoint=script(tmp_path, body),
        **kwargs,
    )


def write_status(tmp_path: Path, **status) -> None:
    (tmp_path / "run" / "restart.json").write_text(json.dumps(status))


def test_unavailable_in_a_checkout(tmp_path):
    restart = RestartControl(
        runtime_file=None, python=sys.executable, entrypoint=tmp_path / "x.py"
    )
    assert restart.available is False
    status = restart.status()
    assert (status.available, status.state) == (False, "idle")


def test_unavailable_without_a_runtime_file(tmp_path):
    restart = RestartControl(
        runtime_file=tmp_path / "runtime.json",
        python=sys.executable,
        entrypoint=tmp_path / "x.py",
    )
    assert restart.available is False


def test_missing_or_unreadable_status_is_idle(tmp_path):
    restart = control(tmp_path)
    assert (restart.status().available, restart.status().state) == (True, "idle")
    (tmp_path / "run" / "restart.json").write_text("not json")
    assert restart.status().state == "idle"
    write_status(tmp_path, state="dancing", updated_at=time.time())
    assert restart.status().state == "idle"


def test_status_is_parsed(tmp_path):
    restart = control(tmp_path)
    now = time.time()
    write_status(
        tmp_path, state="failed", error="Some services did not start.", updated_at=now
    )
    status = restart.status()
    assert (status.state, status.error, status.updated_at) == (
        "failed",
        "Some services did not start.",
        now,
    )
    write_status(tmp_path, state="completed", error=None, updated_at=now - 3600)
    assert restart.status().state == "completed"


@pytest.mark.parametrize("state", ["requested", "restarting"])
def test_a_stale_restart_is_reported_as_failed(tmp_path, state):
    restart = control(tmp_path)
    write_status(tmp_path, state=state, error=None, updated_at=time.time())
    assert restart.status().state == state
    write_status(
        tmp_path, state=state, error=None, updated_at=time.time() - STALE_SECONDS - 1
    )
    status = restart.status()
    assert status.state == "failed"
    assert "hybro logs" in status.error
    write_status(tmp_path, state=state)
    assert restart.status().state == "failed"


async def test_check_passes_and_reports_the_helpers_error(tmp_path):
    assert await control(tmp_path).check() is None
    assert await control(tmp_path, INVALID).check() == "Invalid configuration: bad"
    garbled = await control(tmp_path, "import sys\nsys.exit(3)\n").check()
    assert garbled == "The restart check failed."


async def test_check_runs_with_a_scoped_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRO_HOME", "/var/lib/hybro/runtime")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_private")
    body = (
        "import json, os\n"
        "env = dict(os.environ)\n"
        "env.pop('__CF_USER_TEXT_ENCODING', None)\n"
        "print(json.dumps({'ok': False, 'error': json.dumps(sorted(env))}))\n"
    )
    error = await control(tmp_path, body).check()
    keys = set(json.loads(error))
    assert "HYBRO_HOME" in keys
    assert "CLERK_SECRET_KEY" not in keys
    assert keys <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "HYBRO_HOME",
        "HYBRO_CONTAINER",
        "LC_CTYPE",
    }


async def test_check_timeout_returns_an_error(tmp_path):
    restart = control(tmp_path, "import time\ntime.sleep(30)\n", check_timeout=0.3)
    started = time.monotonic()
    error = await restart.check()
    assert error is not None and "in time" in error
    assert time.monotonic() - started < 10


async def test_request_writes_requested_and_refuses_a_second(tmp_path):
    restart = control(tmp_path)
    status = await restart.request()
    assert (status.available, status.state) == (True, "requested")
    stored = json.loads((tmp_path / "run" / "restart.json").read_text())
    assert stored["state"] == "requested"
    assert (tmp_path / "run" / "restart.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SettingsRequestError) as error:
        await restart.request()
    assert (error.value.code, error.value.status) == ("restart_in_progress", 409)


async def test_request_during_a_check_is_in_progress(tmp_path):
    restart = control(
        tmp_path,
        "import time, json\ntime.sleep(0.5)\nprint(json.dumps({'ok': True}))\n",
    )
    first = asyncio.create_task(restart.request())
    await asyncio.sleep(0.05)
    with pytest.raises(SettingsRequestError) as error:
        await restart.request()
    assert error.value.code == "restart_in_progress"
    assert (await first).state == "requested"


async def test_request_with_an_invalid_config_is_refused(tmp_path):
    restart = control(tmp_path, INVALID)
    with pytest.raises(SettingsRequestError) as error:
        await restart.request()
    assert (error.value.code, error.value.status) == ("config_invalid", 409)
    assert str(error.value) == "Invalid configuration: bad"
    assert not (tmp_path / "run" / "restart.json").exists()


async def test_request_in_a_checkout_is_unavailable(tmp_path):
    restart = RestartControl(
        runtime_file=None, python=sys.executable, entrypoint=tmp_path / "x.py"
    )
    with pytest.raises(SettingsRequestError) as error:
        await restart.request()
    assert (error.value.code, error.value.status) == ("restart_unavailable", 409)


def test_launch_starts_a_detached_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        restart_module, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    restart = control(tmp_path)
    restart.launch()
    [(args, kwargs)] = calls
    assert args[0] == [sys.executable, str(tmp_path / "entrypoint.py"), "restart"]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs[stream] is subprocess.DEVNULL


def test_a_launch_that_cannot_start_is_failed(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(restart_module, "Popen", refuse)
    restart = control(tmp_path)
    restart.launch()
    status = restart.status()
    assert status.state == "failed"
    assert "no such file" not in (status.error or "")
