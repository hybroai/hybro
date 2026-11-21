"""The container side of ``hybro source``; run by ``scripts/hybro``::

    docker exec <container> /app/backend/.venv/bin/python \\
        /app/backend/client_cli.py create --name <name>
    docker exec <container> /app/backend/.venv/bin/python \\
        /app/backend/client_cli.py requirement

``create`` creates a source through the machine-only route over loopback and
prints the new source token once. ``requirement`` prints ``on`` or ``off``,
whether a Source API request needs a source token; ``hybro`` reads it for its
public-listener warning. Exit status: 0 done, 1 refused (nothing was changed),
2 the command could not run or its outcome is unknown. Standard library only;
it reads neither ``config.json`` nor ``auth.json`` and never prints the machine
token.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError

from owner_cli import (
    HANDOFF,
    LOG_HINT,
    TIMEOUT_SECONDS,
    TOKEN_HEADER,
    Failed,
    Post,
    _detail,
    http_post,
    read_handoff,
)

USAGE = "usage: client_cli.py create --name <name> | requirement\n"
# The switch is product state; only this helper and the backend read it.
REQUIREMENT_PATH = "/sources/token-requirement"

Get = Callable[[str, str], tuple[int, bytes]]


def http_get(url: str, token: str) -> tuple[int, bytes]:
    """The status and body of a GET; an HTTP error status is an answer too."""
    request = urllib.request.Request(url, method="GET", headers={TOKEN_HEADER: token})
    # No proxies: the loopback call and its token stay local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def parse(argv: list[str]) -> tuple[str, str] | None:
    """The command and its source name, or None for a usage error."""
    if argv == ["requirement"]:
        return "requirement", ""
    if len(argv) != 3 or argv[0] != "create" or argv[1] != "--name":
        return None
    return "create", argv[2]


def refusal(status: int, body: bytes) -> Failed:
    detail = _detail(body)
    code = detail.get("code")
    message = detail.get("message")
    if status >= 500 or 200 <= status < 300:
        return Failed(
            f"the backend answered HTTP {status}; the source may have been "
            f"created. {LOG_HINT}",
            2,
        )
    if code == "machine_token_required":
        return Failed(
            "the backend refused the machine token; restart Hybro and try again", 2
        )
    if code == "invalid_request":
        return Failed("the name needs 1 to 120 characters; nothing was changed", 1)
    if not isinstance(message, str) or not message:
        return Failed(f"refused (HTTP {status}); nothing was changed.", 1)
    return Failed(message, 1)


def _created(body: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    token, name = data.get("token"), data.get("name")
    if not (isinstance(token, str) and token and isinstance(name, str)):
        return None
    return data


def run(name: str, *, stdout: IO[str], handoff: Path, post: Post) -> int:
    base_url, token = read_handoff(handoff)
    payload = json.dumps({"name": name}).encode()
    try:
        status, body = post(base_url + "/sources", token, payload)
    except TimeoutError as exc:
        raise Failed(
            f"the backend did not answer within {TIMEOUT_SECONDS:g} s; the source "
            f"may have been created. {LOG_HINT}",
            2,
        ) from exc
    except OSError as exc:
        raise Failed(
            f"cannot reach the backend ({type(exc).__name__}). {LOG_HINT}", 2
        ) from exc
    created = _created(body) if status == 201 else None
    if created is None:
        raise refusal(status, body)
    stdout.write(
        f"Created the source {created['name']}. Its token is shown once; "
        "send it as 'Authorization: Bearer <token>':\n"
        f"{created['token']}\n"
    )
    stdout.flush()
    return 0


def read_requirement(*, stdout: IO[str], handoff: Path, get: Get) -> int:
    base_url, token = read_handoff(handoff)
    try:
        status, body = get(base_url + REQUIREMENT_PATH, token)
    except OSError as exc:
        # TimeoutError is an OSError too.
        raise Failed(
            f"cannot read the token requirement ({type(exc).__name__}). {LOG_HINT}",
            2,
        ) from exc
    try:
        data = json.loads(body) if status == 200 else None
    except ValueError:
        data = None
    required = data.get("required") if isinstance(data, dict) else None
    if not isinstance(required, bool):
        raise Failed(
            f"cannot read the token requirement (HTTP {status}). {LOG_HINT}", 2
        )
    stdout.write("on\n" if required else "off\n")
    stdout.flush()
    return 0


def main(
    argv: list[str],
    *,
    stdout: IO[str],
    stderr: IO[str],
    handoff: Path = HANDOFF,
    post: Post = http_post,
    get: Get = http_get,
) -> int:
    parsed = parse(argv)
    if parsed is None:
        stderr.write(USAGE)
        return 2
    command, name = parsed
    try:
        if command == "requirement":
            return read_requirement(stdout=stdout, handoff=handoff, get=get)
        return run(name, stdout=stdout, handoff=handoff, post=post)
    except Failed as exc:
        stderr.write(f"hybro source: {exc}\n")
        return exc.code
    except Exception as exc:
        # The type only: an exception message could carry a token.
        stderr.write(f"hybro source: unexpected {type(exc).__name__}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], stdout=sys.stdout, stderr=sys.stderr))
