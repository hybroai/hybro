"""The AST reachability report that backs every C1 deletion."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from scripts.report_orphan_modules import analyze

BACKEND = Path(__file__).resolve().parents[1]


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def _orphans(root: Path) -> set[str]:
    return {orphan.module for orphan in analyze(root).orphans}


def test_modules_reached_from_roots_are_not_orphans(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "import alpha\nfrom beta import thing\n",
            "alpha.py": "",
            "beta.py": "thing = 1\n",
            "orphan.py": "",
        },
    )

    assert _orphans(root) == {"orphan"}


def test_every_documented_root_counts(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "",
            "__main__.py": "import from_dunder_main\n",
            "container.py": "import from_container\n",
            "orchestrator_composition.py": "import from_composition\n",
            "runtime_entrypoint.py": "import from_entrypoint\n",
            "volume_migration.py": "import from_migration\n",
            "scripts/__init__.py": "",
            "scripts/tool.py": "import from_script\n",
            "from_dunder_main.py": "",
            "from_container.py": "",
            "from_composition.py": "",
            "from_entrypoint.py": "",
            "from_migration.py": "",
            "from_script.py": "",
        },
    )

    assert _orphans(root) == set()


def test_dotted_import_reaches_the_module_and_its_parent_packages(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "import pkg.sub.leaf\n",
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/leaf.py": "",
            "pkg/sub/sibling.py": "",
        },
    )

    assert _orphans(root) == {"pkg.sub.sibling"}


def test_from_package_import_submodule(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import sub\n",
            "pkg/__init__.py": "",
            "pkg/sub.py": "",
            "pkg/other.py": "",
        },
    )

    assert _orphans(root) == {"pkg.other"}


def test_relative_imports_resolve_against_the_importing_package(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg.inner import worker\n",
            "pkg/__init__.py": "",
            "pkg/helper.py": "",
            "pkg/inner/__init__.py": "",
            "pkg/inner/worker.py": (
                "from . import peer\nfrom ..helper import x\nfrom .. import top\n"
            ),
            "pkg/inner/peer.py": "",
            "pkg/top.py": "",
        },
    )

    assert _orphans(root) == set()


def test_unused_package_reexport_does_not_keep_its_module_alive(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import Used\n",
            "pkg/__init__.py": (
                '"""Package."""\n'
                "from .used import Used\n"
                "from .unused import Unused\n"
                '__all__ = ["Used", "Unused"]\n'
            ),
            "pkg/used.py": "class Used: ...\n",
            "pkg/unused.py": "class Unused: ...\n",
        },
    )

    report = analyze(root)

    assert {orphan.module for orphan in report.orphans} == {"pkg.unused"}
    (orphan,) = report.orphans
    assert orphan.direct is False


def test_reexport_chains_through_nested_packages(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from outer import Name\n",
            "outer/__init__.py": "from .inner import Name\n",
            "outer/inner/__init__.py": "from .impl import Name\nfrom .dead import Dead\n",
            "outer/inner/impl.py": "Name = 1\n",
            "outer/inner/dead.py": "Dead = 1\n",
        },
    )

    assert _orphans(root) == {"outer.inner.dead"}


def test_plain_package_import_keeps_every_reexport(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "import pkg\n",
            "pkg/__init__.py": "from .a import A\nfrom .b import B\n",
            "pkg/a.py": "A = 1\n",
            "pkg/b.py": "B = 1\n",
        },
    )

    assert _orphans(root) == set()


def test_star_import_keeps_every_reexport(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import *\n",
            "pkg/__init__.py": "from .a import A\n",
            "pkg/a.py": "A = 1\n",
        },
    )

    assert _orphans(root) == set()


def test_package_init_with_code_keeps_all_its_imports(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import build\n",
            "pkg/__init__.py": (
                "from .helper import make\n\ndef build():\n    return make()\n"
            ),
            "pkg/helper.py": "def make(): ...\n",
        },
    )

    assert _orphans(root) == set()


def test_imports_inside_functions_count(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "def run():\n    from lazy import go\n    go()\n",
            "lazy.py": "def go(): ...\n",
        },
    )

    assert _orphans(root) == set()


def test_chain_imported_only_by_orphans_is_transitive(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "",
            "dead.py": "import dead_child\n",
            "dead_child.py": "",
        },
    )

    report = analyze(root)
    direct = {orphan.module: orphan.direct for orphan in report.orphans}

    assert direct == {"dead": True, "dead_child": False}


def test_tests_and_build_directories_are_ignored(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "",
            "kept_by_test_only.py": "",
            "tests/__init__.py": "",
            "tests/test_x.py": "import kept_by_test_only\n",
            "build/lib/stale.py": "",
            ".venv/lib/site.py": "",
        },
    )

    assert _orphans(root) == {"kept_by_test_only"}


def test_exempt_notice_modules_are_not_orphans(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "",
            "llm_gateway/__init__.py": "",
            "llm_gateway/oauth_notice.py": '"""MIT licence notice."""\n',
        },
    )

    report = analyze(root)

    assert _orphans(root) == {"llm_gateway"}
    assert report.exempt == ["llm_gateway.oauth_notice"]


def test_report_counts_lines(tmp_path: Path):
    root = _tree(tmp_path, {"main.py": "", "orphan.py": "a = 1\nb = 2\nc = 3\n"})

    (orphan,) = analyze(root).orphans

    assert orphan.path == Path("orphan.py")
    assert orphan.lines == 3


def test_cli_lists_orphans(tmp_path: Path):
    root = _tree(tmp_path, {"main.py": "", "orphan.py": "x = 1\n"})
    script = BACKEND / "scripts" / "report_orphan_modules.py"

    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "orphan.py (1)" in result.stdout


def test_cli_reports_unparsable_files_instead_of_crashing(tmp_path: Path):
    root = _tree(tmp_path, {"main.py": "", "broken.py": "def (:\n"})
    script = BACKEND / "scripts" / "report_orphan_modules.py"

    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "broken.py:1: syntax error" in result.stdout


def test_non_utf8_files_are_reported_as_errors(tmp_path: Path):
    root = _tree(tmp_path, {"main.py": ""})
    (root / "latin.py").write_bytes(b"name = '\xe9'\n")

    report = analyze(root)

    assert report.errors == ["latin.py:0: not UTF-8: invalid continuation byte"]


def test_dotted_import_binds_the_top_package_and_its_reexports(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "import pkg.sub\nprint(pkg.Thing)\n",
            "pkg/__init__.py": "from .impl import Thing\n",
            "pkg/sub.py": "",
            "pkg/impl.py": "Thing = 1\n",
        },
    )

    assert _orphans(root) == set()


def test_aliased_dotted_import_binds_only_the_leaf(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "import pkg.sub as sub\n",
            "pkg/__init__.py": "from .impl import Thing\n",
            "pkg/sub.py": "",
            "pkg/impl.py": "Thing = 1\n",
        },
    )

    assert _orphans(root) == {"pkg.impl"}


def test_try_and_type_checking_imports_count(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": (
                "from typing import TYPE_CHECKING\n"
                "try:\n    import fast\nexcept ImportError:\n    import slow\n"
                "if TYPE_CHECKING:\n    from typed import T\n"
            ),
            "fast.py": "",
            "slow.py": "",
            "typed.py": "T = int\n",
        },
    )

    assert _orphans(root) == set()


def test_aliased_reexport_is_requested_by_its_exported_name(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import Public\n",
            "pkg/__init__.py": "from .impl import Private as Public\n",
            "pkg/impl.py": "Private = 1\n",
        },
    )

    assert _orphans(root) == set()


def test_reexported_submodule_in_init_is_conditional(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import used\n",
            "pkg/__init__.py": "from . import unused, used\n",
            "pkg/used.py": "",
            "pkg/unused.py": "",
        },
    )

    assert _orphans(root) == {"pkg.unused"}


def test_annotated_all_keeps_the_init_reexport_only(tmp_path: Path):
    root = _tree(
        tmp_path,
        {
            "main.py": "from pkg import A\n",
            "pkg/__init__.py": (
                'from .a import A\nfrom .b import B\n__all__: list[str] = ["A", "B"]\n'
            ),
            "pkg/a.py": "A = 1\n",
            "pkg/b.py": "B = 1\n",
        },
    )

    assert _orphans(root) == {"pkg.b"}


def test_real_backend_roots_and_live_modules_are_reachable():
    report = analyze(BACKEND)
    orphans = {orphan.module for orphan in report.orphans}

    for live in (
        "main",
        "container",
        "execution.repository.active_runs",
        "execution.orchestrator.kernel",
        "common.config.loader",
    ):
        assert live not in orphans
