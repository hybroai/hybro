"""Guard: the image ships compiled bytecode for every Python program it runs.

Each `hybro` start runs a new container, so bytecode written at run time is
lost on the next start and every program recompiles its imports. The image
must carry hash-based `.pyc` files for the application sources, both uv
environments, and the managed interpreter's standard library.
"""

from __future__ import annotations

import shlex
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
TARGETS = (
    "/app/backend",
    "/app/MCP",
    "/app/default_agents",
    "/opt/python/*/lib/python3.12",
)


def _final_stage() -> list[str]:
    """Instruction lines of the last build stage, continuations joined."""
    stage: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line, pending = pending + line, ""
        if line.startswith("FROM "):
            stage = []
        stage.append(line)
    return stage


def _compile_step(stage: list[str]) -> tuple[int, list[str]]:
    steps = [
        (i, shlex.split(line)[1:])
        for i, line in enumerate(stage)
        if line.startswith("RUN ") and "compileall" in line
    ]
    assert len(steps) == 1, "the final stage must run compileall exactly once"
    return steps[0]


def test_every_python_tree_is_compiled_with_hash_based_pyc() -> None:
    _, command = _compile_step(_final_stage())
    start = command.index("compileall") - 2
    assert command[start : start + 3] == [
        "/app/backend/.venv/bin/python",
        "-m",
        "compileall",
    ]
    argv = command[start:]
    assert "--invalidation-mode" in argv
    assert argv[argv.index("--invalidation-mode") + 1] == "unchecked-hash"
    # The targets are the trailing positional arguments, after every option.
    assert argv[-len(TARGETS) :] == list(TARGETS)


def test_compilation_follows_every_python_source_copy() -> None:
    stage = _final_stage()
    index, _ = _compile_step(stage)
    python_copies = [
        i
        for i, line in enumerate(stage)
        if line.startswith("COPY ")
        and any(f" {target}" in line for target in TARGETS[:3] + ("/opt/python",))
    ]
    assert python_copies
    assert max(python_copies) < index
