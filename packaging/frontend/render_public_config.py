#!/usr/bin/env python3
"""Generate packaging/frontend/public-config.json from the backend schema.

The single image's entrypoint computes the projection at startup. Running the
frontend outside the image (`npm run dev` against a backend on port 8000) needs
one in `HYBRO_FRONTEND_CONFIG`; this file is the default for that, derived from
the same pydantic model so it cannot drift, and CI verifies the checked-in file.

Usage:
    python packaging/frontend/render_public_config.py           # rewrite the file
    python packaging/frontend/render_public_config.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
OUTPUT = Path(__file__).resolve().parent / "public-config.json"

sys.path.insert(0, str(BACKEND))

from common.config.loader import FrontendSettings, Settings  # noqa: E402


def projection() -> str:
    """The public projection for a default configuration and a host backend."""
    public = FrontendSettings.model_validate({"api_prefix": Settings().api_prefix})
    return public.model_dump_json() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero if the file is stale."
    )
    args = parser.parse_args(argv)

    expected = projection()
    current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
    if args.check:
        if current != expected:
            print(
                f"{OUTPUT} is out of sync with the backend schema.\n"
                "Run: python packaging/frontend/render_public_config.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT} is in sync with the backend schema.")
        return 0
    if current == expected:
        print(f"{OUTPUT} already up to date.")
        return 0
    OUTPUT.write_text(expected, encoding="utf-8")
    print(f"Wrote {OUTPUT}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
