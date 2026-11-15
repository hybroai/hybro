#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from pathlib import Path

EXCLUDED_DIRS = {".git", ".venv", "__pycache__", "build"}


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if EXCLUDED_DIRS & set(path.relative_to(root).parts):
            continue
        yield path


def _violations(path: Path, blocked_roots: set[str]) -> list[str]:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno or 0}: syntax error: {exc.msg}"]

    results: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in blocked_roots:
                    results.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in blocked_roots:
                results.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when Python files import blocked legacy root packages."
    )
    parser.add_argument("blocked_roots", nargs="+")
    args = parser.parse_args()

    blocked_roots = set(args.blocked_roots)
    violations: list[str] = []
    for path in _iter_python_files(Path.cwd()):
        violations.extend(_violations(path, blocked_roots))

    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
