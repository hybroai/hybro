import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_no_legacy_imports.py"


def test_scanner_flags_python_imports_and_ignores_text(tmp_path):
    bad_import = "services" + ".database_service"
    (tmp_path / "bad.py").write_text(f"import {bad_import}\n")
    (tmp_path / "notes.py").write_text(f'"""Historical text: import {bad_import}."""\n')

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "services"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"bad.py:1: import {bad_import}" in result.stdout
    assert "notes.py" not in result.stdout


def test_scanner_ignores_excluded_directories(tmp_path):
    ignored = tmp_path / ".venv"
    ignored.mkdir()
    (ignored / "bad.py").write_text("import services\n")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "services"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_scanner_ignores_local_build_output(tmp_path):
    ignored = tmp_path / "build" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "bad.py").write_text("import services\n")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "services"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
