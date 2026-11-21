"""The container side of ``hybro owner``; run by ``scripts/hybro``::

    ... | docker exec -i <container> /app/backend/.venv/bin/python \\
        /app/backend/owner_cli.py create --name <name> --email <email> \\
        [--owner-id <id>]
    ... | docker exec -i <container> /app/backend/.venv/bin/python \\
        /app/backend/owner_cli.py reset-password
    docker exec <container> /app/backend/.venv/bin/python \\
        /app/backend/owner_cli.py setup-code

From a checkout, on the host: ``uv run python owner_cli.py <command>`` finds
the file the backend wrote, ``<HYBRO_HOME>/run/machine.json``, or
``--handoff <file>`` names one.

The password is the first line of standard input, never an argument.
``setup-code`` reads no password: it prints a new one-time setup code, and
only that, while no owner account exists (exit 1 once it does). The
helper reads ``base_url`` and the per-start token from the handoff file the
entrypoint writes and POSTs to the machine-only routes over loopback. Exit
status: 0 done, 1 refused (nothing was changed), 2 the command could not run
or its outcome is unknown (a server error, or no answer in time).
Standard library only; it reads neither ``config.json`` nor ``auth.json``, and
it prints neither the password nor the token.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import urlsplit

HANDOFF = Path("/tmp/hybro/machine.json")
# The file a backend started from a checkout writes for itself.
CHECKOUT_HANDOFF = Path("run") / "machine.json"
TOKEN_HEADER = "X-Hybro-Machine-Token"
# Hashing a password can wait behind other password checks.
TIMEOUT_SECONDS = 30.0
LOG_HINT = "Check the backend's errors with: hybro logs --service backend --level error"
PASSWORD_MIN = 8
PASSWORD_MAX = 1024
# Enough bytes for the longest password in UTF-8 and its newline.
LINE_LIMIT = PASSWORD_MAX * 4 + 1
USAGE = (
    "usage: owner_cli.py [--handoff <file>] create --name <name> --email <email> [--owner-id <id>]"
    " | reset-password | setup-code\n"
    "The password is the first line of standard input.\n"
)
OPTIONS = {
    "create": {"--name", "--email", "--owner-id"},
    "reset-password": set(),
    "setup-code": set(),
}
REQUIRED = {
    "create": {"--name", "--email"},
    "reset-password": set(),
    "setup-code": set(),
}

Post = Callable[[str, str, bytes], tuple[int, bytes]]


class Failed(Exception):
    """The command did not change anything; the message is safe to print."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# ── Transport ──


def http_post(url: str, token: str, data: bytes) -> tuple[int, bytes]:
    """The status and body of a POST; an HTTP error status is an answer too."""
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={TOKEN_HEADER: token, "Content-Type": "application/json"},
    )
    # No proxies: the loopback call and its token stay local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def in_image() -> bool:
    return os.environ.get("HYBRO_CONTAINER") == "image"


def _owned_privately(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def _read_private(path: Path) -> str:
    """The file's text, when this user alone could have written it.

    On the host the directory and the file must be this user's, open to no one
    else and not links, as the backend writes them; anything else may have
    been put there by another local user.
    """
    directory = path.parent.lstat()
    if not (stat.S_ISDIR(directory.st_mode) and _owned_privately(directory)):
        raise Failed(
            f"the machine handoff file {path} is not in a private directory "
            "of this user, so it is not used",
            2,
        )
    refused = Failed(
        f"the machine handoff file {path} is not a private file of this user, "
        "so it is not used",
        2,
    )
    try:
        # O_NONBLOCK: a FIFO put there opens at once and fails the check below.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise refused from exc
        raise
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not (stat.S_ISREG(info.st_mode) and _owned_privately(info)):
            raise refused
        return handle.read()


def read_handoff(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8") if in_image() else _read_private(path)
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise Failed(
            f"the machine handoff file {path} is missing or unreadable, "
            "so the backend has not started",
            2,
        ) from exc
    base_url = data.get("base_url") if isinstance(data, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if not (isinstance(base_url, str) and isinstance(token, str) and token):
        raise Failed(f"the machine handoff file {path} is incomplete", 2)
    try:
        target = urlsplit(base_url)
        loopback = target.scheme == "http" and target.hostname == "127.0.0.1"
    except ValueError:
        loopback = False
    if not loopback:
        # The password goes only to this machine's backend.
        raise Failed(f"the machine handoff file {path} names another host", 2)
    return base_url.rstrip("/"), token


# ── Input ──


def parse(argv: list[str]) -> tuple[str, dict[str, str]] | None:
    """The command and its options, or None for a usage error."""
    if not argv or argv[0] not in OPTIONS:
        return None
    command, rest = argv[0], argv[1:]
    if len(rest) % 2:
        return None
    options: dict[str, str] = {}
    for flag, value in zip(rest[::2], rest[1::2], strict=True):
        if flag not in OPTIONS[command] or flag in options:
            return None
        options[flag] = value
    if not REQUIRED[command] <= options.keys():
        return None
    return command, options


def read_password(stdin: IO[bytes]) -> str:
    """The first line of stdin without its newline, within the length bounds."""
    raw = stdin.readline(LINE_LIMIT + 1)
    bounds = Failed(
        f"the password needs {PASSWORD_MIN} to {PASSWORD_MAX} characters; "
        "nothing was changed",
        1,
    )
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    elif len(raw) > LINE_LIMIT:
        raise bounds
    try:
        password = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Failed("the password is not valid UTF-8; nothing was changed", 1) from exc
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise bounds
    return password


# ── Answers ──


def _detail(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    detail = data.get("detail") if isinstance(data, dict) else None
    return detail if isinstance(detail, dict) else {}


def refusal(command: str, status: int, body: bytes, *, owner_id: bool) -> Failed:
    detail = _detail(body)
    code = detail.get("code")
    message = detail.get("message")
    # A server error or an unexpected success: the change may have applied.
    if status >= 500 or 200 <= status < 300:
        return Failed(
            f"the backend answered HTTP {status}; check whether the change "
            f"applied. {LOG_HINT}",
            2,
        )
    if code == "machine_token_required":
        return Failed(
            "the backend refused the machine token; restart Hybro and try again", 2
        )
    if code == "invalid_request" and command == "create":
        fields = (
            "the name, the email or the owner id"
            if owner_id
            else "the name or the email"
        )
        return Failed(
            f"{fields} is not valid (an email looks like "
            "name@example.com); nothing was changed",
            1,
        )
    if not isinstance(message, str) or not message:
        return Failed(f"refused (HTTP {status}); nothing was changed.", 1)
    if code == "account_exists":
        message += " To set a new password, run: hybro owner reset-password"
    elif code == "no_account":
        message += " Create it with: hybro owner create --name <name> --email <email>"
    elif code == "owner_id_required":
        choices = detail.get("choices")
        if isinstance(choices, list) and choices:
            message += " Run again with --owner-id and one of: " + ", ".join(
                str(choice) for choice in choices
            )
    return Failed(message, 1)


def _setup_code(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    code = data.get("code") if isinstance(data, dict) else None
    if not (isinstance(code, str) and code):
        raise Failed(f"the backend answered without a setup code. {LOG_HINT}", 2)
    return code


def _profile_email(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) else None


# ── Commands ──


def run(
    command: str,
    options: dict[str, str],
    *,
    stdin: IO[bytes],
    stdout: IO[str],
    handoff: Path,
    post: Post,
) -> int:
    base_url, token = read_handoff(handoff)
    if command == "setup-code":
        status, body = _post(post, base_url + "/setup-code", token, {})
        if status != 200:
            failed = refusal(command, status, body, owner_id=False)
            # Exit 1 means "the account exists, no link needed" to the script;
            # every other refusal is a code it could not get.
            if _detail(body).get("code") != "account_exists":
                failed.code = 2
            raise failed
        stdout.write(_setup_code(body) + "\n")
        stdout.flush()
        return 0
    password = read_password(stdin)
    if command == "create":
        path = "/owner"
        payload: dict[str, str] = {
            "name": options["--name"],
            "email": options["--email"],
            "password": password,
        }
        if "--owner-id" in options:
            payload["owner_id"] = options["--owner-id"]
        expected = 201
    else:
        path = "/owner/password"
        payload = {"password": password}
        expected = 204
    status, body = _post(post, base_url + path, token, payload)
    if status != expected:
        raise refusal(command, status, body, owner_id="--owner-id" in options)
    if command == "create":
        email = _profile_email(body) or options["--email"]
        stdout.write(f"Created the owner account for {email}.\n")
    else:
        stdout.write("Set the new password and signed out every browser.\n")
    stdout.flush()
    return 0


def _post(
    post: Post, url: str, token: str, payload: dict[str, str]
) -> tuple[int, bytes]:
    try:
        return post(url, token, json.dumps(payload).encode())
    except TimeoutError as exc:
        # The request may still complete, for example while hashing waits.
        raise Failed(
            f"the backend did not answer within {TIMEOUT_SECONDS:g} s; the change "
            f"may have applied. {LOG_HINT}",
            2,
        ) from exc
    except OSError as exc:
        raise Failed(
            f"cannot reach the backend ({type(exc).__name__}); a checkout's "
            f"backend must listen on port 8000. {LOG_HINT}",
            2,
        ) from exc


def default_handoff(*, image: Path | None = None) -> Path:
    """The image's handoff file inside the container, else the checkout's.

    On the host the checkout backend's file is ``<HYBRO_HOME>/run/machine.json``
    (``HYBRO_HOME`` defaults to ``~/.hybro`` and, as in the loader, must be
    absolute); only the path is read here.
    """
    # Chosen by where it runs, never by which file exists: /tmp is shared, so
    # on the host a file at the image path may belong to another user.
    if in_image():
        return HANDOFF if image is None else image
    configured = os.environ.get("HYBRO_HOME")
    home = Path(configured) if configured is not None else Path.home() / ".hybro"
    if not home.is_absolute():
        raise Failed("HYBRO_HOME must be an absolute path", 2)
    return home / CHECKOUT_HANDOFF


def main(
    argv: list[str],
    *,
    stdin: IO[bytes],
    stdout: IO[str],
    stderr: IO[str],
    handoff: Path | None = None,
    post: Post = http_post,
) -> int:
    if argv[:1] == ["--handoff"]:
        if len(argv) < 2 or not argv[1]:
            stderr.write(USAGE)
            return 2
        handoff, argv = Path(argv[1]), argv[2:]
    parsed = parse(argv)
    if parsed is None:
        stderr.write(USAGE)
        return 2
    try:
        if handoff is None:
            handoff = default_handoff()
        return run(*parsed, stdin=stdin, stdout=stdout, handoff=handoff, post=post)
    except Failed as exc:
        stderr.write(f"hybro owner: {exc}\n")
        return exc.code
    except Exception as exc:
        # The type only: an exception message can carry the password.
        stderr.write(f"hybro owner: unexpected {type(exc).__name__}\n")
        return 2


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1:],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
