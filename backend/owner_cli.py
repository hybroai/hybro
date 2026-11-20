"""The container side of ``hybro owner``; run by ``scripts/hybro``::

    ... | docker exec -i <container> /app/backend/.venv/bin/python \\
        /app/backend/owner_cli.py create --name <name> --email <email> \\
        [--owner-id <id>]
    ... | docker exec -i <container> /app/backend/.venv/bin/python \\
        /app/backend/owner_cli.py reset-password

The password is the first line of standard input, never an argument. The
helper reads ``base_url`` and the per-start token from the handoff file the
entrypoint writes and POSTs to the machine-only routes over loopback. Exit
status: 0 done, 1 refused (nothing was changed), 2 the command could not run
or its outcome is unknown (a server error, or no answer in time).
Standard library only; it reads neither ``config.json`` nor ``auth.json``, and
it prints neither the password nor the token.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError

HANDOFF = Path("/tmp/hybro/machine.json")
TOKEN_HEADER = "X-Hybro-Machine-Token"
# Hashing a password can wait behind other password checks.
TIMEOUT_SECONDS = 30.0
LOG_HINT = "Check the backend's errors with: hybro logs --service backend --level error"
PASSWORD_MIN = 8
PASSWORD_MAX = 1024
# Enough bytes for the longest password in UTF-8 and its newline.
LINE_LIMIT = PASSWORD_MAX * 4 + 1
USAGE = (
    "usage: owner_cli.py create --name <name> --email <email> [--owner-id <id>]"
    " | reset-password\n"
    "The password is the first line of standard input.\n"
)
OPTIONS = {
    "create": {"--name", "--email", "--owner-id"},
    "reset-password": set(),
}
REQUIRED = {"create": {"--name", "--email"}, "reset-password": set()}

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


def read_handoff(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    try:
        status, body = post(base_url + path, token, json.dumps(payload).encode())
    except TimeoutError as exc:
        # The request may still complete, for example while hashing waits.
        raise Failed(
            f"the backend did not answer within {TIMEOUT_SECONDS:g} s; the change "
            f"may have applied. {LOG_HINT}",
            2,
        ) from exc
    except OSError as exc:
        raise Failed(
            f"cannot reach the backend ({type(exc).__name__}). {LOG_HINT}", 2
        ) from exc
    if status != expected:
        raise refusal(command, status, body, owner_id="--owner-id" in options)
    if command == "create":
        email = _profile_email(body) or options["--email"]
        stdout.write(f"Created the owner account for {email}.\n")
    else:
        stdout.write("Set the new password and signed out every browser.\n")
    stdout.flush()
    return 0


def main(
    argv: list[str],
    *,
    stdin: IO[bytes],
    stdout: IO[str],
    stderr: IO[str],
    handoff: Path = HANDOFF,
    post: Post = http_post,
) -> int:
    parsed = parse(argv)
    if parsed is None:
        stderr.write(USAGE)
        return 2
    try:
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
