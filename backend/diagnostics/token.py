"""The per-start diagnostics token the image entrypoint hands over."""

from __future__ import annotations

import json
import re
from pathlib import Path

_TOKEN = re.compile(r"[0-9a-f]{64}")


def read_token(path: str) -> str:
    """The handoff file's token, or "" when it is absent or malformed.

    Anything but 64 lowercase hex characters reads as no token, so a weak or
    empty value can never be accepted.
    """
    if not path:
        return ""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return ""
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and _TOKEN.fullmatch(token) else ""


__all__ = ["read_token"]
