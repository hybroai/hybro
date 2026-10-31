"""One call to each LLM-backed sample agent the other suites do not exercise.

The agent workspace lock moves Story Agent and Image Generator Agent to newer
SDK versions, and an import test would not catch a runtime change. Each agent
gets one session send through the mock LLM, and the session must complete.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.functional]

TERMINAL = {"completed", "rejected", "failed", "closed"}


async def _agent_id(client: httpx.AsyncClient, name: str) -> str:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        response = await client.get("/agent/getAllAgents?active_only=true")
        if response.status_code == 200:
            for agent in response.json().get("agents", []):
                if (agent.get("name") or "").lower() == name.lower():
                    return agent["agent_id"]
        await asyncio.sleep(1.0)
    raise AssertionError(f"{name} is not registered and active")


async def _send_and_wait(client: httpx.AsyncClient, agent_id: str, text: str) -> dict:
    response = await client.post(
        "/sessions/messages",
        json={
            "agent_id": agent_id,
            "text": text,
            "client_request_id": str(uuid.uuid4()),
            "client_label": "Functional client",
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    deadline = time.monotonic() + 120.0
    status: dict = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/sessions/{session_id}")
        assert response.status_code == 200, response.text
        status = response.json()
        if status["state"] in TERMINAL:
            return status
        await asyncio.sleep(1.0)
    raise AssertionError(f"session did not finish: {status}")


@pytest.mark.asyncio
async def test_story_agent_completes_one_call(functional_client: httpx.AsyncClient):
    agent_id = await _agent_id(functional_client, "Story Agent")
    status = await _send_and_wait(
        functional_client, agent_id, "Tell me a short story about a cat"
    )
    assert status["state"] == "completed", status
    assert status["history"][-1]["agent_id"] == agent_id
    assert status["history"][-1]["reply"], status


@pytest.mark.asyncio
async def test_image_generator_agent_completes_one_call(
    functional_client: httpx.AsyncClient,
):
    agent_id = await _agent_id(functional_client, "Image Generator Agent")
    status = await _send_and_wait(
        functional_client, agent_id, "Generate an image of a red bicycle"
    )
    assert status["state"] == "completed", status
    assert status["history"][-1]["agent_id"] == agent_id
