"""Cross-module import boundaries.

A domain module imports another domain module only through its package root
or its ``protocols`` module. ``a2a_adapter`` is the A2A SDK boundary and is
open to every module. ``common`` and ``models`` import no domain module, and
``dal`` imports none outside the listed debt. ``api_gateway`` has its own
boundary test; ``container.py`` and ``orchestrator_composition.py`` are the
composition root and are not scanned by the domain rule.

Inside ``execution``, the model loop (``execution/model_loop/``) is reached
only through its package root: the rest of ``execution`` imports the entry
point ``model_loop_for``, and ``orchestrator_composition.py`` imports the five
names it wires. Neither imports a submodule or an old module path.

The debt allowlists are exact: an entry that no longer matches a real import
fails the test, so they can only shrink. They are tracked as cleanup debt.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOMAIN_MODULES = frozenset(
    {
        "account",
        "agent",
        "approval",
        "chat",
        "context_memory",
        "conversation",
        "delivery",
        "diagnostics",
        "execution",
        "instance_settings",
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
        ("execution/model_loop/model_runtime.py", "llm_gateway.error_classification"),
        ("execution/model_loop/model_runtime.py", "llm_gateway.gateway"),
        ("execution/model_loop/model_runtime.py", "llm_gateway.model_registry"),
        (
            "execution/model_loop/model_runtime.py",
            "llm_gateway.structured_generation",
        ),
        ("execution/model_loop/model_runtime.py", "llm_gateway.turn_types"),
        ("execution/model_loop/profiles.py", "llm_gateway.errors"),
        ("execution/model_loop/profiles.py", "llm_gateway.model_registry"),
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


# -- the model loop package ----------------------------------------------------

MODEL_LOOP = "execution.model_loop"
MODEL_LOOP_DIR = "execution/model_loop"
COMPOSITION_ROOT = "orchestrator_composition.py"

# Old module paths of the moved model-loop modules. They were removed and must
# not come back as re-export shims.
MODEL_LOOP_SHIMS = frozenset(
    {
        "execution.adapters.profiles",
        "execution.orchestrator.compaction",
        "execution.orchestrator.context",
        "execution.orchestrator.model_runtime",
        "execution.orchestrator.streaming",
    }
)

# Names each scanned file may import from the package root.
MODEL_LOOP_ENTRY_POINT = frozenset({"model_loop_for"})
MODEL_LOOP_COMPOSITION_NAMES = frozenset(
    {
        "ContextCompiler",
        "GatewayModelRuntime",
        "OrchestratorProfileResolutionError",
        "OrchestratorProfileResolver",
        "PromptAssetRegistry",
    }
)

# (importing file, imported module or root name) pairs that reach the model
# loop other than through its entry point.
MODEL_LOOP_IMPORT_DEBT: frozenset[tuple[str, str]] = frozenset()


def _absolute_imports(path: Path, *, root: Path) -> list[tuple[str, list[str]]]:
    """(module, imported names) per import; plain imports list no names."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = list(path.relative_to(root).parent.parts)
    found: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if module:
                found.append((module, [alias.name for alias in node.names]))
    return found


def _in_model_loop(module: str) -> bool:
    return module == MODEL_LOOP or module.startswith(f"{MODEL_LOOP}.")


def model_loop_violations(root: Path = ROOT) -> set[tuple[str, str]]:
    """Imports of the model loop that bypass its package root, or a shim.

    Files under ``execution/`` outside the package may import only the entry
    point from the package root; the composition root only the five names it
    wires. Neither may import a shim, a submodule, or the bare package.
    """
    scanned = [
        (path, MODEL_LOOP_ENTRY_POINT)
        for path in _source_files(root, "execution")
        if not path.relative_to(root).as_posix().startswith(f"{MODEL_LOOP_DIR}/")
    ]
    composition = root / COMPOSITION_ROOT
    if composition.is_file():
        scanned.append((composition, MODEL_LOOP_COMPOSITION_NAMES))
    violations: set[tuple[str, str]] = set()
    for path, allowed in scanned:
        relative = path.relative_to(root).as_posix()
        for module, names in _absolute_imports(path, root=root):
            if module == MODEL_LOOP and names:
                bad = [f"{module}.{name}" for name in names if name not in allowed]
            elif _in_model_loop(module) or module in MODEL_LOOP_SHIMS:
                bad = [module]
            else:
                bad = [
                    f"{module}.{name}"
                    for name in names
                    if _in_model_loop(f"{module}.{name}")
                    or f"{module}.{name}" in MODEL_LOOP_SHIMS
                ]
            violations.update((relative, target) for target in bad)
    return violations


def test_model_loop_is_reached_only_through_its_package_root():
    assert model_loop_violations() == MODEL_LOOP_IMPORT_DEBT


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


def test_model_loop_rule_allows_only_the_package_root_names(tmp_path):
    root = _tree(
        tmp_path,
        {
            "container.py": "from execution.model_loop.context import X\n",
            "execution/__init__.py": "",
            "execution/model_loop/__init__.py": "",
            "execution/model_loop/context.py": "",
            "execution/model_loop/loop.py": "from execution.model_loop.context import X\n",
            "execution/orchestrator/__init__.py": "",
            "execution/orchestrator/context.py": (
                "from execution.model_loop.context import ContextCompiler\n"
            ),
            "execution/orchestrator/kernel.py": (
                "from execution.model_loop import model_loop_for\n"
                "from execution.model_loop import ContextCompiler\n"
                "from .context import ContextCompiler\n"
                "from execution import model_loop\n"
            ),
            "orchestrator_composition.py": (
                "from execution.model_loop import ContextCompiler, PromptAssetRegistry\n"
                "from execution.model_loop import model_loop_for\n"
                "import execution.model_loop.context\n"
            ),
        },
    )

    assert model_loop_violations(root) == {
        ("execution/orchestrator/context.py", "execution.model_loop.context"),
        ("execution/orchestrator/kernel.py", "execution.model_loop"),
        ("execution/orchestrator/kernel.py", "execution.model_loop.ContextCompiler"),
        ("execution/orchestrator/kernel.py", "execution.orchestrator.context"),
        ("orchestrator_composition.py", "execution.model_loop.context"),
        ("orchestrator_composition.py", "execution.model_loop.model_loop_for"),
    }
