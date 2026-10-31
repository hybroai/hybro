"""Copy Hybro 0.3 Compose volumes into one image volume; run by ``hybro upgrade``.

The CLI runs this once in a container of the new image::

    docker run --rm --entrypoint /app/backend/.venv/bin/python \\
        -v <project>_mongo_data:/source/db:ro -v <project>_room_files:/source/files:ro \\
        -v <target volume>:/data -v <HYBRO_HOME>:/var/lib/hybro/runtime:ro \\
        <image> /app/backend/volume_migration.py <release|dev>

It checks free space, copies with ``cp -a``, opens the copy with a temporary
mongod (repointing the replica set at loopback), counts the documents of each
collection, and writes the completion marker last. The source volumes are
mounted read-only and never written. Progress goes to stderr; stdout carries
only ``port=<n>`` when the 0.3 instance used a custom frontend port.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import runtime_entrypoint as entry

SOURCE_DB = Path("/source/db")
SOURCE_FILES = Path("/source/files")
DATABASE = "hybro"


class MigrationError(Exception):
    """A safe, user-facing migration failure."""


def tree_size(path: Path) -> int:
    """Bytes of the regular files under ``path``; 0 when it is not mounted."""
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def check_space(needed: int, free: int) -> None:
    """The copy and mongod's working room need twice the source size."""
    if free < 2 * needed:
        raise MigrationError(
            f"Not enough free Docker disk space: the 0.3 data is {needed // 2**20} MiB, "
            f"{2 * needed // 2**20} MiB free is needed, {free // 2**20} MiB is free."
        )


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    result = subprocess.run(
        ["cp", "-a", f"{source}/.", str(target)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise MigrationError(f"Copying {source} failed: {result.stderr.strip()}")


def collection_counts(client: Any) -> dict[str, int]:
    database = client[DATABASE]
    return {
        name: database[name].count_documents({})
        for name in sorted(database.list_collection_names())
    }


def custom_port(config_text: str | None, instance: str) -> int | None:
    """The frontend port the 0.3 instance saved in ``instances``, if any."""
    if config_text is None:
        return None
    try:
        config = json.loads(config_text)
        port = config["instances"][instance]["ports"]["frontend"]
    except (ValueError, KeyError, TypeError):
        return None
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def names_compose_mongo(config_text: str | None) -> bool:
    """Whether ``backend.mongodb_url`` points at the 0.3 Compose service ``mongo``."""
    if config_text is None:
        return False
    try:
        url = json.loads(config_text)["backend"]["mongodb_url"]
        return isinstance(url, str) and urlsplit(url).hostname == "mongo"
    except (ValueError, KeyError, TypeError):
        return False


def read_config(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def migrate(
    instance: str, *, prepare: Any = entry.prepare_mongodb
) -> Mapping[str, Any]:
    data = entry.DATA_DIR
    check_space(
        tree_size(SOURCE_DB) + tree_size(SOURCE_FILES), shutil.disk_usage(data).free
    )
    print("Copying the 0.3 MongoDB data and room files...", file=sys.stderr)
    copy_tree(SOURCE_DB, data / "db")
    (data / "files").mkdir(exist_ok=True)
    copy_tree(SOURCE_FILES, data / "files")
    entry.RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        counts = prepare(inspect=collection_counts)
    except entry.EntrypointError as exc:
        raise MigrationError(
            f"The copied MongoDB data cannot be opened. {exc}"
        ) from None
    port = custom_port(read_config(entry.RUNTIME_DIR / "config.json"), instance)
    details: dict[str, Any] = {"migrated_from": instance, "collections": counts}
    if port is not None:
        details["frontend_port"] = port
    entry.mark_volume_complete(details)
    return details


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("release", "dev"):
        print("usage: volume_migration.py <release|dev>", file=sys.stderr)
        return 2
    try:
        details = migrate(argv[1])
    except MigrationError as exc:
        print(f"hybro: {exc}", file=sys.stderr)
        return 1
    total = sum(details["collections"].values())
    print(
        f"Copied {len(details['collections'])} collections, {total} documents.",
        file=sys.stderr,
    )
    if names_compose_mongo(read_config(entry.RUNTIME_DIR / "config.json")):
        print(
            "Warning: backend.mongodb_url names the 0.3 host `mongo`, which no longer "
            "exists; remove it from config.json to use the embedded MongoDB.",
            file=sys.stderr,
        )
    if "frontend_port" in details:
        print(f"port={details['frontend_port']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
