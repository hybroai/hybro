from __future__ import annotations

import stat
from pathlib import Path

import pytest

from common.config.runtime_config import RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore


def contents(home: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in home.iterdir()}


def directory(tmp_path: Path, mode: int) -> Path:
    home = tmp_path / "runtime"
    home.mkdir(mode=0o700)
    (home / "config.json").write_bytes(b"fixture config bytes\n")
    (home / "auth.json").write_bytes(b"fixture auth bytes\n")
    home.chmod(mode)
    return home


@pytest.mark.parametrize("mode", [0o755, 0o775])
def test_runtime_still_rejects_insecure_directory_without_repair(
    tmp_path: Path, mode: int
) -> None:
    home = directory(tmp_path, mode)
    before = contents(home)
    with pytest.raises(RuntimeConfigurationError, match="mode 0700"):
        RuntimeConfigStore(home).read_stored_credential()
    assert stat.S_IMODE(home.stat().st_mode) == mode
    assert contents(home) == before
