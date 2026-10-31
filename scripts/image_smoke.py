"""Smoke test a running single image through its one port.

Start the image with an empty config directory and a loopback listener, then:

    uv run --project backend --frozen --extra mcp --no-env-file \
        python scripts/image_smoke.py http://localhost:<port>

Checks: the UI, API, and MCP answer through Caddy; a foreign Host is refused;
the sample agents register, the LLM-backed ones inactive with
``provider_not_configured``; Echo answers through the sessions API and MCP.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx
from mcp import Client

API = "/api/v1"
LLM_SAMPLES = {
    "Weather Agent",
    "Story Agent",
    "Travel Planner Agent",
    "Image Generator Agent",
}
SIGNATURE = "Created by Hybro"


def wait_for(check, *, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"Timed out waiting for {what}")
        time.sleep(2)


def agents(http: httpx.Client) -> dict[str, dict]:
    response = http.get(f"{API}/agent/getAllAgents")
    response.raise_for_status()
    body = response.json()
    items = body if isinstance(body, list) else body["agents"]
    return {agent["name"]: agent for agent in items}


def samples_registered(http: httpx.Client) -> dict[str, dict] | None:
    found = agents(http)
    if {"Echo Agent", *LLM_SAMPLES} <= found.keys():
        return found
    return None


def reply_of(http: httpx.Client, session_id: str) -> str | None:
    session = http.get(f"{API}/sessions/{session_id}").json()
    if session.get("state") != "completed":
        return None
    return session["history"][-1]["reply"]


async def mcp_echo(url: str) -> str:
    async with Client(url) as client:
        found = await client.call_tool("find_agents", {"query": "Echo"})
        echo = next(
            agent
            for agent in found.structured_content["agents"]
            if agent["name"] == "Echo Agent"
        )
        sent = await client.call_tool(
            "send_message", {"agent_id": echo["agent_id"], "text": "mcp smoke"}
        )
        session_id = sent.structured_content["session_id"]
        for _ in range(30):
            checked = await client.call_tool(
                "check_session", {"session_id": session_id}
            )
            if checked.structured_content["state"] == "completed":
                return checked.structured_content["history"][-1]["reply"]
            await asyncio.sleep(1)
    raise SystemExit("Echo did not answer through MCP")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="e.g. http://localhost:8080")
    base = parser.parse_args().base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as http:
        wait_for(
            lambda: http.get("/health").status_code == 200,
            timeout=180,
            what="backend health",
        )
        for path in ("/", f"{API}/setup/status", "/hybro-mcp"):
            assert http.get(path).status_code == 200, path
        foreign = http.get("/health", headers={"Host": "rebound.example"})
        assert foreign.status_code == 421, foreign.status_code

        found = wait_for(
            lambda: samples_registered(http), timeout=120, what="sample agents"
        )
        assert found["Echo Agent"]["status"] == "active", found["Echo Agent"]
        for name in LLM_SAMPLES:
            agent = found[name]
            assert agent["status"] == "inactive", agent
            assert agent["inactive_reason"] == "provider_not_configured", agent

        sent = http.post(
            f"{API}/sessions/messages",
            json={
                "agent_id": found["Echo Agent"]["agent_id"],
                "text": "api smoke",
                "client_request_id": "image-smoke-api",
                "client_label": "image smoke",
            },
        )
        sent.raise_for_status()
        reply = wait_for(
            lambda: reply_of(http, sent.json()["session_id"]),
            timeout=60,
            what="Echo through the API",
        )
        assert "api smoke" in reply and SIGNATURE in reply, reply

    reply = asyncio.run(mcp_echo(f"{base}/mcp"))
    assert "mcp smoke" in reply and SIGNATURE in reply, reply
    print("Image smoke test passed.")


if __name__ == "__main__":
    main()
