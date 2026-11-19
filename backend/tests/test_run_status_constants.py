"""Run status sets are named once: the terminal set in ``common.run_status``
(re-exported by ``execution.orchestrator.persistence``), the others in
``execution.orchestrator.persistence``."""

from __future__ import annotations

import ast
from pathlib import Path

from execution.orchestrator.persistence import (
    NON_TERMINAL_RUN_STATUSES,
    SUSPENDED_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
)

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {"tests", "build", ".venv", "__pycache__"}
# The one place the terminal set is spelled out.
DEFINITION = ROOT / "common" / "run_status.py"

TERMINAL = {"completed", "failed", "canceled", "budget_exhausted"}
# A run-status set may add these to the terminal set and still be a copy.
TERMINAL_COPY_EXTRAS = {"canceling", "missing"}


def _production_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if not EXCLUDED_DIRS.intersection(path.relative_to(ROOT).parts)
        and path != DEFINITION
    ]


def _string_elements(node: ast.Set | ast.List | ast.Tuple) -> set[str] | None:
    values: set[str] = set()
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        values.add(element.value)
    return values


def test_terminal_run_statuses_value():
    assert TERMINAL_RUN_STATUSES == frozenset(TERMINAL)
    assert isinstance(TERMINAL_RUN_STATUSES, frozenset)


def test_status_sets_partition_without_overlap():
    assert TERMINAL_RUN_STATUSES.isdisjoint(NON_TERMINAL_RUN_STATUSES)
    assert SUSPENDED_RUN_STATUSES <= set(NON_TERMINAL_RUN_STATUSES)


def test_canary_metrics_imports_the_shared_terminal_constant():
    from execution.adapters import canary_metrics

    assert canary_metrics.TERMINAL_RUN_STATUSES is TERMINAL_RUN_STATUSES


def test_session_uses_the_shared_terminal_constant():
    from execution.orchestrator import session

    assert session.TERMINAL_RUN_STATUSES is TERMINAL_RUN_STATUSES


def test_no_production_copy_of_the_terminal_run_status_set():
    copies: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Set | ast.List | ast.Tuple):
                continue
            values = _string_elements(node)
            if values is None or not TERMINAL <= values:
                continue
            if values <= TERMINAL | TERMINAL_COPY_EXTRAS:
                copies.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert copies == []


def test_no_suspended_literal_in_kernel_or_session():
    copies: list[str] = []
    for relative in (
        "execution/orchestrator/kernel.py",
        "execution/orchestrator/run_state.py",
        "execution/orchestrator/session.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Set) and _string_elements(node) == set(
                SUSPENDED_RUN_STATUSES
            ):
                copies.append(f"{relative}:{node.lineno}")
    assert copies == []
