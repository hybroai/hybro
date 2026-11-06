"""Cross-module import boundaries.

A domain module imports another domain module only through its package root
or its ``protocols`` module. ``a2a_adapter`` is the A2A SDK boundary and is
open to every module. ``common`` and ``models`` import no domain module, and
``dal`` imports none outside the listed debt. ``api_gateway`` has its own
boundary test; ``container.py`` and ``orchestrator_composition.py`` are the
composition root and are not scanned.

The debt allowlists are exact: an entry that no longer matches a real import
fails the test, so they can only shrink. They are tracked as cleanup debt.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOMAIN_MODULES = frozenset(
    {
        "agent",
        "approval",
        "chat",
        "context_memory",
        "conversation",
        "delivery",
        "diagnostics",
        "execution",
        "jobs",
        "llm_gateway",
        "local_agents",
        "room_files",
        "session",
    }
)
SHARED_ROOTS = ("common", "models")
DAL_ROOT = "dal"

# (importing file, imported module) pairs that reach past another module's
# package root or protocols.
DOMAIN_IMPORT_DEBT = frozenset(
    {
        ("execution/adapters/profiles.py", "llm_gateway.errors"),
        ("execution/adapters/profiles.py", "llm_gateway.model_registry"),
        ("execution/orchestrator/model_runtime.py", "llm_gateway.error_classification"),
        ("execution/orchestrator/model_runtime.py", "llm_gateway.gateway"),
        ("execution/orchestrator/model_runtime.py", "llm_gateway.model_registry"),
        (
            "execution/orchestrator/model_runtime.py",
            "llm_gateway.structured_generation",
        ),
        ("execution/orchestrator/model_runtime.py", "llm_gateway.turn_types"),
        ("execution/orchestrator_routing.py", "context_memory.resources"),
        ("execution/orchestrator_routing.py", "context_memory.translators"),
        ("jobs/orchestrator_workers.py", "execution.adapters.canary_metrics"),
        ("local_agents/card_probe.py", "agent.url_utils"),
    }
)

# Persistence adapters that still import domain types.
DAL_IMPORT_DEBT: frozenset[str] = frozenset()


def _is_module(root: Path, dotted: str) -> bool:
    path = root.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def imported_modules(path: Path, *, root: Path = ROOT) -> set[str]:
    """Every module a file imports, including submodules named by ``from``.

    ``from a import b`` imports the submodule ``a.b`` when ``a/b.py`` or
    ``a/b/__init__.py`` exists; relative imports resolve against the file's
    package.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    package = list(path.relative_to(root).parent.parts)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            modules.add(module)
            for alias in node.names:
                submodule = f"{module}.{alias.name}"
                if alias.name != "*" and _is_module(root, submodule):
                    modules.add(submodule)
    return modules


def _source_files(root: Path, top: str) -> list[Path]:
    return sorted(
        path for path in (root / top).rglob("*.py") if "__pycache__" not in path.parts
    )


def domain_violations(root: Path = ROOT) -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    for top in sorted(DOMAIN_MODULES):
        for path in _source_files(root, top):
            for module in imported_modules(path, root=root):
                target = module.split(".", 1)[0]
                if target not in DOMAIN_MODULES or target == top:
                    continue
                if module in {target, f"{target}.protocols"}:
                    continue
                violations.add((path.relative_to(root).as_posix(), module))
    return violations


def domain_imports_from(root: Path, tops: tuple[str, ...]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for top in tops:
        for path in _source_files(root, top):
            for module in imported_modules(path, root=root):
                if module.split(".", 1)[0] in DOMAIN_MODULES:
                    found.add((path.relative_to(root).as_posix(), module))
    return found


def test_domain_modules_import_only_package_roots_and_protocols():
    assert domain_violations() == DOMAIN_IMPORT_DEBT


def test_common_and_models_import_no_domain_module():
    assert domain_imports_from(ROOT, SHARED_ROOTS) == set()


def test_dal_imports_no_domain_module_outside_the_debt():
    files = {path for path, _module in domain_imports_from(ROOT, (DAL_ROOT,))}
    assert files == DAL_IMPORT_DEBT


# -- scanner self-tests ---------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_scanner_resolves_both_import_forms_to_submodules(tmp_path):
    root = _tree(
        tmp_path,
        {
            "execution/__init__.py": "",
            "execution/engine.py": (
                "import llm_gateway.errors\n"
                "from llm_gateway import gateway, LLMGatewayImpl\n"
                "from agent import protocols\n"
                "from agent import AgentFacade\n"
            ),
            "llm_gateway/__init__.py": "",
            "llm_gateway/errors.py": "",
            "llm_gateway/gateway.py": "",
            "agent/__init__.py": "",
            "agent/protocols.py": "",
        },
    )

    assert imported_modules(root / "execution/engine.py", root=root) == {
        "llm_gateway.errors",
        "llm_gateway",
        "llm_gateway.gateway",
        "agent",
        "agent.protocols",
    }
    assert domain_violations(root) == {
        ("execution/engine.py", "llm_gateway.errors"),
        ("execution/engine.py", "llm_gateway.gateway"),
    }


def test_scanner_resolves_relative_imports_inside_the_package(tmp_path):
    root = _tree(
        tmp_path,
        {
            "chat/__init__.py": "",
            "chat/service.py": "from .facade import ChatFacade\nfrom . import facade\n",
            "chat/facade.py": "",
        },
    )

    assert imported_modules(root / "chat/service.py", root=root) == {
        "chat",
        "chat.facade",
    }
    assert domain_violations(root) == set()


def test_scanner_ignores_build_copies_and_unscanned_roots(tmp_path):
    root = _tree(
        tmp_path,
        {
            "build/lib/chat/service.py": "from execution.engine import run\n",
            "container.py": "from execution.engine import run\n",
            "execution/__init__.py": "",
            "execution/engine.py": "",
        },
    )

    assert domain_violations(root) == set()
