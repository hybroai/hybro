"""A checkout's backend writes its own machine handoff file at startup.

The image's entrypoint writes ``/tmp/hybro/machine.json``. A backend started
from a checkout has no entrypoint, so it writes ``<HYBRO_HOME>/run/machine.json``
itself: a fresh token on every start, in a 0700 directory and a 0600 file.
When it cannot, the machine-only routes stay refusing.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from api_gateway.machine_handoff import (
    machine_token_at_start,
    remove_checkout_handoff,
    write_checkout_handoff,
)
from diagnostics import read_token

BASE_URL = "http://127.0.0.1:8000/api/v1/machine"


def reasons(caplog) -> list[str]:
    return [
        r.reason
        for r in caplog.records
        if r.getMessage() == "machine_handoff_unavailable"
    ]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_it_writes_a_private_file_with_a_fresh_token(tmp_path):
    path = tmp_path / "home" / "run" / "machine.json"
    path.parent.parent.mkdir(mode=0o700)

    token = write_checkout_handoff(path, BASE_URL)

    data = json.loads(path.read_text())
    assert data == {"base_url": BASE_URL, "token": token}
    assert read_token(str(path)) == token
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["machine.json"]


def test_each_start_writes_a_new_token(tmp_path):
    path = tmp_path / "run" / "machine.json"
    first = write_checkout_handoff(path, BASE_URL)
    second = write_checkout_handoff(path, BASE_URL)

    assert second not in ("", first)
    assert read_token(str(path)) == second


def test_a_loosened_file_is_replaced_with_a_private_one(tmp_path):
    path = tmp_path / "run" / "machine.json"
    write_checkout_handoff(path, BASE_URL)
    path.chmod(0o644)

    assert write_checkout_handoff(path, BASE_URL) != ""
    assert mode(path) == 0o600


@pytest.mark.parametrize("setup", ["open_directory", "symlinked_directory"])
def test_it_fails_closed_without_a_private_directory(tmp_path, setup, caplog):
    path = tmp_path / "run" / "machine.json"
    write_checkout_handoff(path, BASE_URL)
    stale = read_token(str(path))
    if setup == "open_directory":
        path.parent.chmod(0o755)
    else:
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(target, target_is_directory=True)

    assert write_checkout_handoff(path, BASE_URL) == ""

    # The token of the last start is not left behind to be accepted.
    assert read_token(str(path)) == ""
    assert not (tmp_path / "elsewhere" / "machine.json").exists()
    assert reasons(caplog) == ["not_private"]
    assert stale not in caplog.text


def test_it_fails_closed_when_the_directory_is_not_writable(tmp_path, caplog):
    # The previous file cannot be removed either; the routes still refuse
    # because this start's token stays empty.
    path = tmp_path / "run" / "machine.json"
    write_checkout_handoff(path, BASE_URL)
    path.parent.chmod(0o500)
    try:
        assert write_checkout_handoff(path, BASE_URL) == ""
    finally:
        path.parent.chmod(0o700)
    assert reasons(caplog) == ["not_private"]


def test_it_fails_closed_when_the_file_cannot_be_replaced(tmp_path, caplog):
    path = tmp_path / "run" / "machine.json"
    path.mkdir(parents=True)
    path.parent.chmod(0o700)

    assert write_checkout_handoff(path, BASE_URL) == ""
    # An OSError names its class; the staging file does not stay behind.
    assert reasons(caplog) not in ([], ["not_private"])
    assert [p.name for p in path.parent.iterdir()] == ["machine.json"]


def test_it_does_not_create_a_missing_home(tmp_path, caplog):
    # Only `run` is created: the loader owns the home and its 0700 mode.
    path = tmp_path / "missing-home" / "run" / "machine.json"

    assert write_checkout_handoff(path, BASE_URL) == ""
    assert not path.parent.parent.exists()
    assert reasons(caplog) == ["FileNotFoundError"]


def test_the_previous_file_is_removed_when_it_can_be(tmp_path):
    path = tmp_path / "run" / "machine.json"
    write_checkout_handoff(path, BASE_URL)
    path.parent.chmod(0o750)

    assert write_checkout_handoff(path, BASE_URL) == ""
    assert not path.exists()


def test_the_token_is_never_logged(tmp_path, caplog):
    path = tmp_path / "run" / "machine.json"
    token = write_checkout_handoff(path, BASE_URL)

    assert token and token not in caplog.text


# -- Where the loader points ------------------------------------------------------


def _settings(monkeypatch, tmp_path, container: str | None):
    from common.config import loader

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYBRO_HOME", str(home))
    if container is None:
        monkeypatch.delenv("HYBRO_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("HYBRO_CONTAINER", container)
    loader.get_settings.cache_clear()
    try:
        return home, loader.get_settings()
    finally:
        loader.get_settings.cache_clear()


def test_a_checkout_writes_its_handoff_under_hybro_home(monkeypatch, tmp_path):
    home, settings = _settings(monkeypatch, tmp_path, None)

    assert settings.machine_handoff_file == os.fspath(home / "run" / "machine.json")
    assert settings.machine_handoff_by_backend is True


def test_the_image_leaves_the_handoff_to_the_entrypoint(monkeypatch, tmp_path):
    _, settings = _settings(monkeypatch, tmp_path, "image")

    assert settings.machine_handoff_file == "/tmp/hybro/machine.json"
    assert settings.machine_handoff_by_backend is False


def test_the_handoff_settings_are_not_user_settings():
    from common.config.loader import PRIVATE_FIELDS

    assert {"machine_handoff_file", "machine_handoff_by_backend"} <= PRIVATE_FIELDS


# -- The token the routes accept at startup ------------------------------------


def _start_settings(tmp_path, *, by_backend: bool):
    from common.config.loader import Settings

    path = tmp_path / "run" / "machine.json"
    return path, Settings(
        machine_handoff_file=str(path), machine_handoff_by_backend=by_backend
    )


def test_a_checkout_start_accepts_the_token_it_wrote(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=True)

    token = machine_token_at_start(settings)

    assert token and read_token(str(path)) == token
    assert json.loads(path.read_text())["base_url"] == BASE_URL


def test_a_checkout_start_never_accepts_a_stale_token(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=True)
    assert machine_token_at_start(settings)
    path.parent.chmod(0o755)

    assert machine_token_at_start(settings) == ""
    assert read_token(str(path)) == ""


def test_the_image_start_reads_the_entrypoint_file_and_writes_nothing(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=False)
    path.parent.mkdir(mode=0o700)
    token = "a" * 64
    path.write_text(json.dumps({"base_url": BASE_URL, "token": token}))
    before = path.read_text()

    assert machine_token_at_start(settings) == token
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir()] == ["machine.json"]


def test_the_image_start_without_a_file_accepts_nothing(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=False)

    assert machine_token_at_start(settings) == ""
    assert not path.parent.exists()


def test_shutdown_removes_this_starts_file(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=True)
    token = machine_token_at_start(settings)

    remove_checkout_handoff(settings, token)

    assert not path.exists()


def test_shutdown_leaves_a_file_another_start_wrote(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=True)
    mine = machine_token_at_start(settings)
    machine_token_at_start(settings)

    remove_checkout_handoff(settings, mine)

    assert read_token(str(path)) not in ("", mine)


def test_shutdown_never_removes_the_entrypoint_file(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=False)
    path.parent.mkdir(mode=0o700)
    token = "a" * 64
    path.write_text(json.dumps({"base_url": BASE_URL, "token": token}))

    remove_checkout_handoff(settings, token)

    assert read_token(str(path)) == token


def test_shutdown_with_no_token_removes_nothing(tmp_path):
    path, settings = _start_settings(tmp_path, by_backend=True)
    machine_token_at_start(settings)

    remove_checkout_handoff(settings, "")

    assert path.exists()


def test_startup_takes_the_token_from_machine_token_at_start():
    import inspect

    import container

    source = inspect.getsource(container._runtime_lifespan)
    assert "app.state.machine_token = machine_token_at_start(runtime.settings)" in (
        source
    )
    # Both the failed-start and the normal shutdown paths remove it.
    assert source.count("remove_checkout_handoff(runtime.settings, machine_token)") == 2
