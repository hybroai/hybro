"""End-to-end verification for the Hybro default agents.

Runs against a live stack started with `docker compose up -d --build`, using the
published host ports and the public backend at http://localhost:8000.

Tiers (manifest-driven from agents.yaml):
  1. Card availability  - every agent serves its agent card.
  2. Registration       - every agent is registered with the backend.
  3. Functional         - per-agent validator sends a real A2A message/send.
"""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest

requests = pytest.importorskip("requests")
yaml = pytest.importorskip("yaml")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
API_PREFIX = os.getenv("API_PREFIX", "/api/v1")
AGENT_HOST = os.getenv("AGENT_HOST", "localhost")
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "agents.yaml"

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")


def _fail_missing_stack(msg: str) -> None:
    """Fail: these are end-to-end tests and a running stack is a prerequisite."""
    pytest.fail(f"{msg} (start it with: docker compose up -d --build)")


def _load_agents() -> dict[str, dict]:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    agents = data.get("agents", {}) or {}
    return {n: s for n, s in agents.items() if s.get("enabled", True)}


AGENTS = _load_agents()
AGENT_IDS = sorted(AGENTS.keys())


def _agent_url(spec: dict) -> str:
    return f"http://{AGENT_HOST}:{spec['port']}"


def _require_backend() -> None:
    try:
        resp = requests.get(f"{BACKEND_URL}/health", timeout=5)
    except requests.RequestException as exc:
        _fail_missing_stack(f"backend not reachable at {BACKEND_URL}: {exc}")
        return
    if resp.status_code != 200:
        _fail_missing_stack(
            f"backend unhealthy at {BACKEND_URL}: HTTP {resp.status_code}"
        )


def _require_openai() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; skipping functional (real OpenAI) checks")


def _get_card(agent_url: str) -> dict | None:
    for path in CARD_PATHS:
        try:
            resp = requests.get(f"{agent_url}{path}", timeout=5)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None
    return None


def _send_message(agent_url: str, text: str, timeout: float = 120) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": text}],
            },
            "configuration": {
                "blocking": True,
                "acceptedOutputModes": ["text/plain", "application/json", "image/png"],
            },
        },
    }
    resp = requests.post(agent_url, json=payload, timeout=timeout)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "result" in body, f"no result in response: {body}"
    return body["result"]


def _collect_parts(result: dict) -> list[dict]:
    """Flatten message parts + artifact parts from an A2A task result."""
    parts: list[dict] = []
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    if isinstance(message.get("parts"), list):
        parts.extend(p for p in message["parts"] if isinstance(p, dict))
    for artifact in result.get("artifacts") or []:
        if isinstance(artifact, dict) and isinstance(artifact.get("parts"), list):
            parts.extend(p for p in artifact["parts"] if isinstance(p, dict))
    return parts


# ---------------------------------------------------------------------------
# Tier 1: card availability (every enabled agent)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_card_available(agent_id: str) -> None:
    spec = AGENTS[agent_id]
    agent_url = _agent_url(spec)
    card = _get_card(agent_url)
    if card is None:
        _fail_missing_stack(
            f"{agent_id} not reachable at {agent_url}; is the stack running?"
        )
    assert card.get("name"), f"{agent_id} card missing name: {card}"
    assert card.get("skills"), f"{agent_id} card missing skills: {card}"


# ---------------------------------------------------------------------------
# Tier 2: registration with the backend (every enabled agent)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_registered(agent_id: str) -> None:
    _require_backend()
    spec = AGENTS[agent_id]
    card = _get_card(_agent_url(spec))
    if card is None:
        _fail_missing_stack(f"{agent_id} not reachable; cannot compare registration")
    expected_name = card.get("name")

    resp = requests.get(
        f"{BACKEND_URL}{API_PREFIX}/agent/getAllAgents", timeout=15
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    items = data.get("agents", data) if isinstance(data, dict) else data
    names = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        card_obj = item.get("agent_card") or {}
        names.append(item.get("name") or card_obj.get("name"))
    assert expected_name in names, (
        f"{agent_id} ({expected_name!r}) not registered; found: {names}"
    )


# ---------------------------------------------------------------------------
# Tier 3: functional checks (per-agent validators)
# ---------------------------------------------------------------------------
def test_weather_agent_functional() -> None:
    if "weather_agent" not in AGENTS:
        pytest.skip("weather_agent not in manifest")
    _require_openai()
    agent_url = _agent_url(AGENTS["weather_agent"])
    if _get_card(agent_url) is None:
        _fail_missing_stack("weather_agent not reachable; is the stack running?")

    result = _send_message(agent_url, "What's the weather in Tokyo?")
    state = (result.get("status") or {}).get("state")

    assert state in ("completed", "input-required"), (
        f"weather agent did not complete (state={state!r}): {result}"
    )

    text = (
        " ".join(
            str(p.get("text", ""))
            for p in _collect_parts(result)
            if p.get("kind") == "text"
        )
        .strip()
        .lower()
    )
    assert text, f"weather agent returned no text: {result}"
    assert any(
        k in text
        for k in (
            "weather",
            "tokyo",
            "temperature",
            "°",
            "forecast",
            "rain",
            "sunny",
            "cloud",
        )
    ), f"weather response looks unrelated: {text[:200]}"


def test_image_generator_functional() -> None:
    if "image_generator_agent" not in AGENTS:
        pytest.skip("image_generator_agent not in manifest")
    _require_openai()
    agent_url = _agent_url(AGENTS["image_generator_agent"])
    if _get_card(agent_url) is None:
        _fail_missing_stack(
            "image_generator_agent not reachable; is the stack running?"
        )

    result = _send_message(agent_url, "Generate an image of a red apple", timeout=180)
    file_parts = [p for p in _collect_parts(result) if p.get("kind") == "file"]
    assert file_parts, f"no file part returned: {result}"

    file_obj = file_parts[0].get("file") or {}
    mime = file_obj.get("mimeType") or file_obj.get("mime_type") or ""
    assert mime.startswith("image/"), f"unexpected mime type: {mime!r}"

    b64 = file_obj.get("bytes")
    assert b64, "file part missing base64 bytes"
    raw = base64.b64decode(b64)
    assert len(raw) > 2048, f"image suspiciously small: {len(raw)} bytes"


def _assert_completed_text(result: dict, label: str) -> None:
    """Assert an A2A task completed and returned non-empty text."""
    state = (result.get("status") or {}).get("state")
    assert state == "completed", f"{label} did not complete (state={state!r}): {result}"
    text = " ".join(
        str(p.get("text", ""))
        for p in _collect_parts(result)
        if p.get("kind") == "text"
    ).strip()
    assert text, f"{label} returned no text: {result}"


def test_travel_planner_card_content() -> None:
    if "travel_planner_agent" not in AGENTS:
        pytest.skip("travel_planner_agent not in manifest")
    agent_url = _agent_url(AGENTS["travel_planner_agent"])
    card = _get_card(agent_url)
    if card is None:
        _fail_missing_stack("travel_planner_agent not reachable; is the stack running?")

    assert card.get("name") == "Travel Planner Agent", card
    description = (card.get("description") or "").lower()
    assert "trip planning" in description or "itinerar" in description, card

    skills = card.get("skills") or []
    assert skills, card
    examples = skills[0].get("examples") or []
    assert "Generate a travel plan" in examples, card
    assert "hello" not in examples, card


def test_travel_planner_functional() -> None:
    if "travel_planner_agent" not in AGENTS:
        pytest.skip("travel_planner_agent not in manifest")
    _require_openai()
    agent_url = _agent_url(AGENTS["travel_planner_agent"])
    if _get_card(agent_url) is None:
        _fail_missing_stack("travel_planner_agent not reachable; is the stack running?")

    result = _send_message(agent_url, "Plan a short 2-day trip to Kyoto, Japan.")
    _assert_completed_text(result, "travel planner")


def test_story_functional() -> None:
    if "story_agent" not in AGENTS:
        pytest.skip("story_agent not in manifest")
    _require_openai()
    agent_url = _agent_url(AGENTS["story_agent"])
    if _get_card(agent_url) is None:
        _fail_missing_stack("story_agent not reachable; is the stack running?")

    result = _send_message(
        agent_url, "Tell me a very short story about a brave little robot."
    )
    _assert_completed_text(result, "story")
