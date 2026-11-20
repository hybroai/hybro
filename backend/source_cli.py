"""The container side of ``hybro source``; run by ``scripts/hybro``::

    docker exec <container> /app/backend/.venv/bin/python \\
        /app/backend/source_cli.py create --name <name>

It creates a source through the machine-only route over loopback and prints
the new source token once. Exit status: 0 done, 1 refused (nothing was
changed), 2 the command could not run or its outcome is unknown. Standard
library only; it reads neither ``config.json`` nor ``auth.json`` and never
prints the machine token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO, Any

from owner_cli import (
    HANDOFF,
    LOG_HINT,
    TIMEOUT_SECONDS,
    Failed,
    Post,
    _detail,
    http_post,
    read_handoff,
)

USAGE = "usage: source_cli.py create --name <name>\n"


def parse(argv: list[str]) -> str | None:
    """The source name, or None for a usage error."""
    if len(argv) != 3 or argv[0] != "create" or argv[1] != "--name":
        return None
    return argv[2]


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


def main(
    argv: list[str],
    *,
    stdout: IO[str],
    stderr: IO[str],
    handoff: Path = HANDOFF,
    post: Post = http_post,
) -> int:
    name = parse(argv)
    if name is None:
        stderr.write(USAGE)
        return 2
    try:
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
