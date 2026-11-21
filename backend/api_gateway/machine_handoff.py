"""The machine-only routes' handoff file for a backend started from a checkout.

In the image the entrypoint writes ``/tmp/hybro/machine.json`` before the
backend starts. A checkout has no entrypoint, so its backend writes
``<HYBRO_HOME>/run/machine.json`` at startup: the loopback base URL of the
machine-only routes and a fresh token, in a 0700 directory and a 0600 file
owned by this user. ``owner_cli.py`` reads it on the host. The file is a
runtime record of this start, not configuration, and nothing reads it as a
setting.

It fails closed: when the file cannot be written privately, the previous
start's file is removed when it can be, the routes refuse every token, and one
log line names the reason without the token.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from common.config.loader import Settings
from common.observability import events as E
from common.utils.logger import get_logger
from diagnostics import read_token

logger = get_logger(__name__)

# The port of `uv run uvicorn main:app` in a checkout, as CONTRIBUTING runs it.
# The file names it so that `owner_cli.py` on the host finds the backend.
CHECKOUT_BACKEND_PORT = 8000


class _NotPrivate(Exception):
    pass


def _private_directory(directory: Path) -> None:
    # Only `run` itself: the loader creates the home with its own 0700 mode.
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise _NotPrivate


def _write(path: Path, text: str) -> None:
    # A name of its own, so two backends sharing a home never unlink each
    # other's staging file.
    staging = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise _NotPrivate


def _remove_stale(path: Path) -> None:
    # Only a regular file in a real directory; never follow a link elsewhere.
    try:
        if path.parent.is_symlink():
            return
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def write_checkout_handoff(path: Path, base_url: str) -> str:
    """Write a fresh token to ``path`` and return it; "" when it was not private."""
    token = secrets.token_hex(32)
    try:
        _private_directory(path.parent)
        _write(path, json.dumps({"base_url": base_url, "token": token}))
    except (OSError, _NotPrivate) as exc:
        _remove_stale(path)
        reason = "not_private" if isinstance(exc, _NotPrivate) else type(exc).__name__
        logger.emit(E.MACHINE_HANDOFF_UNAVAILABLE, reason=reason)
        return ""
    return token


def machine_token_at_start(settings: Settings) -> str:
    """The token the machine-only routes accept for this start; "" refuses all.

    A checkout's backend uses the token it has just written, never one read
    back from the file; in the image the entrypoint's file holds it.
    """
    if settings.machine_handoff_by_backend:
        return write_checkout_handoff(
            Path(settings.machine_handoff_file),
            f"http://127.0.0.1:{CHECKOUT_BACKEND_PORT}{settings.api_prefix}/machine",
        )
    return read_token(settings.machine_handoff_file)


def remove_checkout_handoff(settings: Settings, token: str) -> None:
    """At shutdown, remove the file when it still holds this start's token.

    A stopped backend leaves no address behind for `owner_cli.py` to send a
    password to; a file another start wrote since, or the entrypoint's, stays.
    """
    if not (settings.machine_handoff_by_backend and token):
        return
    path = Path(settings.machine_handoff_file)
    if read_token(settings.machine_handoff_file) != token:
        return
    try:
        if not path.parent.is_symlink() and not path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.emit(E.MACHINE_HANDOFF_UNAVAILABLE, reason=type(exc).__name__)


__all__ = [
    "CHECKOUT_BACKEND_PORT",
    "machine_token_at_start",
    "remove_checkout_handoff",
    "write_checkout_handoff",
]
