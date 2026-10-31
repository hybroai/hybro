#!/usr/bin/env python3
"""Report production modules that no entry point can reach.

Read-only. Walks the static imports of every production module from the
entry points (``main``, ``__main__``, ``container``,
``orchestrator_composition``, ``runtime_entrypoint``, ``volume_migration``,
and every module under ``scripts``) and lists the modules never reached.

A package ``__init__`` that only re-exports names (imports, a docstring, and
``__all__``) does not keep a re-exported module alive by itself: the module is
reached only when something imports that name from the package, or imports
the package as a whole (``import pkg`` or ``from pkg import *``).

An orphan is "direct" when no production module imports it at all, and
"transitive" when only orphans or unused re-exports import it. Licence and
notice modules are exempt and listed separately.
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {"tests", "build", ".venv", "__pycache__", ".git"}
ROOT_MODULES = {
    "main",
    "__main__",
    "container",
    "orchestrator_composition",
    "runtime_entrypoint",
    "volume_migration",
}
ROOT_PACKAGES = ("scripts",)
EXEMPT_MODULES = {"llm_gateway.oauth_notice"}


@dataclass(frozen=True)
class Orphan:
    module: str
    path: Path
    lines: int
    direct: bool


@dataclass
class Report:
    orphans: list[Orphan]
    exempt: list[str]
    errors: list[str] = field(default_factory=list)


@dataclass
class _Module:
    name: str
    path: Path
    is_package: bool
    lines: int = 0
    # Edges followed whenever the module executes: (module, requested names).
    # ``None`` names means the whole module, every re-export included.
    edges: list[tuple[str, frozenset[str] | None]] = field(default_factory=list)
    # Re-export-only package: exported name -> (source module, source name).
    reexports: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _module_name(relative: Path) -> str:
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _discover(root: Path) -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if relative == Path("__init__.py"):
            continue
        name = _module_name(relative)
        modules[name] = _Module(
            name=name, path=relative, is_package=path.name == "__init__.py"
        )
    return modules


def _resolve_from(module: _Module, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = module.name.split(".")
    if not module.is_package:
        package = package[:-1]
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    base = ".".join(package)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _is_reexport_only(tree: ast.Module) -> bool:
    for index, statement in enumerate(tree.body):
        if isinstance(statement, ast.Import | ast.ImportFrom):
            continue
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Assign) and all(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in statement.targets
        ):
            continue
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "__all__"
        ):
            continue
        return False
    return True


def _add_import(module: _Module, node: ast.Import) -> None:
    for alias in node.names:
        if alias.asname:
            module.edges.append((alias.name, None))
            continue
        # ``import a.b`` binds ``a``: every prefix is usable as a whole.
        parts = alias.name.split(".")
        for end in range(1, len(parts) + 1):
            module.edges.append((".".join(parts[:end]), None))


def _add_import_from(module: _Module, node: ast.ImportFrom, reexport: bool) -> None:
    source = _resolve_from(module, node)
    if not source:
        return
    names = [alias.name for alias in node.names]
    if "*" in names:
        module.edges.append((source, None))
    elif reexport:
        for alias in node.names:
            exported = alias.asname or alias.name
            module.reexports.setdefault(exported, []).append((source, alias.name))
    else:
        module.edges.append((source, frozenset(names)))


def _parse(root: Path, modules: dict[str, _Module]) -> list[str]:
    errors: list[str] = []
    for module in modules.values():
        try:
            source = (root / module.path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(module.path))
        except SyntaxError as exc:
            errors.append(f"{module.path}:{exc.lineno or 0}: syntax error: {exc.msg}")
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{module.path}:0: not UTF-8: {exc.reason}")
            continue
        module.lines = len(source.splitlines())
        conditional = module.is_package and _is_reexport_only(tree)
        top_level = set(map(id, tree.body))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                _add_import(module, node)
            elif isinstance(node, ast.ImportFrom):
                _add_import_from(module, node, conditional and id(node) in top_level)
    return errors


def _is_root(name: str) -> bool:
    top = name.split(".", 1)[0]
    return name in ROOT_MODULES or top in ROOT_PACKAGES


class _Reachability:
    def __init__(self, modules: dict[str, _Module]) -> None:
        self.modules = modules
        self.executed: set[str] = set()
        self.requested: set[tuple[str, str]] = set()
        self.queue: deque[tuple[str, frozenset[str] | None]] = deque()

    def run(self) -> set[str]:
        self.queue.extend((name, None) for name in self.modules if _is_root(name))
        while self.queue:
            name, names = self.queue.popleft()
            self._execute(name)
            if names is None:
                target = self.modules.get(name)
                names = frozenset(target.reexports) if target else frozenset()
            for requested_name in names:
                self._request(name, requested_name)
        return self.executed

    def _execute(self, name: str) -> None:
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            if prefix in self.modules and prefix not in self.executed:
                self.executed.add(prefix)
                self.queue.extend(self.modules[prefix].edges)

    def _request(self, package: str, name: str) -> None:
        if (package, name) in self.requested:
            return
        self.requested.add((package, name))
        submodule = f"{package}.{name}" if package else name
        if submodule in self.modules:
            self.queue.append((submodule, None))
        target = self.modules.get(package)
        if target is not None:
            for source, source_name in target.reexports.get(name, []):
                self.queue.append((source, frozenset({source_name})))


def _imported_by_anyone(modules: dict[str, _Module]) -> set[str]:
    imported: set[str] = set()

    def mark(name: str) -> None:
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            imported.add(".".join(parts[:end]))

    for module in modules.values():
        for source, names in module.edges:
            mark(source)
            for name in names or ():
                mark(f"{source}.{name}")
        for sources in module.reexports.values():
            for source, name in sources:
                mark(source)
                mark(f"{source}.{name}")
    return imported


def analyze(root: Path) -> Report:
    root = root.resolve()
    modules = _discover(root)
    errors = _parse(root, modules)
    reached = _Reachability(modules).run()
    imported = _imported_by_anyone(modules)
    orphans: list[Orphan] = []
    exempt: list[str] = []
    for name, module in sorted(modules.items()):
        if name in reached:
            continue
        if name in EXEMPT_MODULES:
            exempt.append(name)
            continue
        orphans.append(
            Orphan(
                module=name,
                path=module.path,
                lines=module.lines,
                direct=name not in imported,
            )
        )
    return Report(orphans=orphans, exempt=exempt, errors=errors)


def _print(report: Report) -> None:
    for label, direct in (("direct", True), ("transitive", False)):
        group = [orphan for orphan in report.orphans if orphan.direct is direct]
        total = sum(orphan.lines for orphan in group)
        print(f"{label} orphans: {len(group)} modules, {total} lines")
        for orphan in group:
            print(f"  {orphan.path} ({orphan.lines})")
    if report.exempt:
        print("exempt: " + ", ".join(report.exempt))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="backend directory to scan (default: this script's backend)",
    )
    args = parser.parse_args()

    report = analyze(args.root)
    if report.errors:
        print("\n".join(report.errors))
        return 1
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
