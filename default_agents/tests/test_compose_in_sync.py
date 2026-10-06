"""Guard: docker-compose.yml must stay in sync with agents.yaml.

The default-agent service blocks + registrar in docker-compose.yml are generated
from default_agents/agents.yaml by render_compose.py. This test fails if the two
have diverged, so a manifest edit that wasn't regenerated is caught in CI.

It also guards docker-compose.release.yml, which render_release.py derives from
docker-compose.yml. A released install runs that file, so drift there would ship
a stack that no longer matches the development one.

    Fix: python default_agents/render_compose.py
         python default_agents/render_release.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parents[1]
RENDER_SCRIPT = DEFAULT_AGENTS_DIR / "render_compose.py"

_spec = importlib.util.spec_from_file_location("render_compose", RENDER_SCRIPT)
render_compose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_compose)

_release_spec = importlib.util.spec_from_file_location(
    "render_release", DEFAULT_AGENTS_DIR / "render_release.py"
)
render_release = importlib.util.module_from_spec(_release_spec)
_release_spec.loader.exec_module(render_release)


def test_compose_matches_manifest() -> None:
    agents = render_compose.load_enabled_agents()
    current = render_compose.COMPOSE_PATH.read_text(encoding="utf-8")
    expected = render_compose.build_expected(
        current, render_compose.render_regions(agents)
    )
    import yaml

    services = yaml.safe_load(current)["services"]
    for spec in agents.values():
        environment = services[spec["service"]]["environment"]
        assert "HYBRO_AGENT_CONFIG=${HYBRO_AGENT_CONFIG:-}" in environment
        assert not any("OPENAI_API_KEY" in value for value in environment)
        assert not any("REGISTRAR_TOKEN" in value for value in environment)
    backend_environment = services["backend"]["environment"]
    assert "HYBRO_CONTAINER=1" in backend_environment
    # Discovery would otherwise re-register each agent under its host URL.
    published_ports = ",".join(str(spec["port"]) for spec in agents.values())
    assert (
        f"LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS={published_ports}"
        in backend_environment
    )
    assert "env_file" not in services["backend"]
    assert not any(
        "DEFAULT_AGENT_LLM_TOKEN" in value
        for value in services["frontend"]["environment"]
    )
    assert current == expected, (
        "docker-compose.yml is out of sync with default_agents/agents.yaml. "
        "Run: python default_agents/render_compose.py"
    )


def test_mcp_is_default_loopback_only_and_has_no_credentials() -> None:
    import yaml

    source = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    services = yaml.safe_load(source.read_text(encoding="utf-8"))["services"]
    mcp = services["mcp"]
    assert "profiles" not in mcp
    assert mcp["ports"] == ["127.0.0.1:8001:8001"]
    assert mcp["depends_on"] == {"backend": {"condition": "service_healthy"}}
    assert mcp["environment"] == ["HYBRO_MCP_CONFIG=${HYBRO_MCP_CONFIG:-}"]
    assert not mcp.get("volumes")
    assert "env_file" not in mcp
    assert "HYBRO_MCP_URL=http://mcp:8001" in services["frontend"]["environment"]


def test_release_stack_matches_development_stack() -> None:
    """A released install runs the published images of the same services."""
    import yaml

    source = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    release_path = source.with_name("docker-compose.release.yml")
    expected = render_release.render(source.read_text(encoding="utf-8"))

    assert release_path.read_text(encoding="utf-8") == expected, (
        "docker-compose.release.yml is out of sync with docker-compose.yml. "
        "Run: python default_agents/render_release.py"
    )

    dev = yaml.safe_load(source.read_text(encoding="utf-8"))
    release = yaml.safe_load(expected)
    assert release["name"] == dev["name"] == "hybro"
    assert set(release["services"]) == set(dev["services"])
    # The released stack is what users run, so it must carry the exclusion too.
    assert any(
        value.startswith("LOCAL_AGENT_DISCOVERY_EXCLUDED_PORTS=")
        for value in release["services"]["backend"]["environment"]
    )
    for name, service in release["services"].items():
        assert "build" not in service, f"{name} would rebuild in a released stack"
        if "build" in dev["services"][name]:
            assert service["image"] == render_release.image_for(name)
        else:
            assert service["image"] == dev["services"][name]["image"]


def test_release_workflow_publishes_every_stack_image() -> None:
    """Every image a released stack asks for must be one the release builds.

    A renamed service or a new default agent changes the stack's image names; if
    the release workflow is not updated with it, `hybro start` fails on a user's
    machine rather than in CI.
    """
    import re

    import yaml

    repo = Path(__file__).resolve().parents[2]
    release_stack = yaml.safe_load(
        (repo / "docker-compose.release.yml").read_text(encoding="utf-8")
    )
    required = {
        service["image"]
        for service in release_stack["services"].values()
        if "image" in service and "hybroai" in service["image"]
    }

    workflow = (repo / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    built = {
        render_release.image_for(name)
        for name in re.findall(r"- name: ([a-z0-9-]+)\n\s+context:", workflow)
    }

    assert built == required, (
        "The release workflow and docker-compose.release.yml disagree on image "
        f"names. Missing from the workflow: {sorted(required - built)}; "
        f"not referenced by the stack: {sorted(built - required)}."
    )
