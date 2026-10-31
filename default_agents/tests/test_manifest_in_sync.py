"""Guard: the agent manifest, the uv workspace, and the image stay in sync.

`default_agents/agents.yaml` names the bundled agents. The workspace lock must
cover exactly those agents, and the root Dockerfile must copy each one, because
uv checks that every workspace member exists.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parents[1]
REPO = DEFAULT_AGENTS_DIR.parent


def _manifest() -> dict:
    return yaml.safe_load(
        (DEFAULT_AGENTS_DIR / "agents.yaml").read_text(encoding="utf-8")
    )


def _workspace_members() -> list[str]:
    workspace = tomllib.loads(
        (DEFAULT_AGENTS_DIR / "pyproject.toml").read_text(encoding="utf-8")
    )
    return workspace["tool"]["uv"]["workspace"]["members"]


def test_workspace_members_are_the_manifest_agents() -> None:
    """One workspace lock covers exactly the agents the manifest names."""
    members = _workspace_members()
    assert sorted(members) == sorted(
        spec["dir"] for spec in _manifest()["agents"].values()
    )
    for member in members:
        assert (DEFAULT_AGENTS_DIR / member / "pyproject.toml").is_file(), member


def test_manifest_requirements_and_ports_are_valid() -> None:
    agents = _manifest()["agents"].values()
    assert {spec["requires"] for spec in agents} <= {"none", "text", "image"}
    ports = [spec["port"] for spec in agents]
    assert len(ports) == len(set(ports))


def test_image_copies_every_workspace_member_and_the_manifest() -> None:
    image_dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    for member in _workspace_members():
        assert (
            f"COPY default_agents/{member}/pyproject.toml {member}/" in image_dockerfile
        )
        assert (
            f"COPY default_agents/{member}/ /app/default_agents/{member}/"
            in image_dockerfile
        )
    assert (
        "COPY default_agents/agents.yaml /app/bundled_agents.yaml" in image_dockerfile
    )
