"""Smoke test a running single image through its one port.

Start the image with an empty config directory, an empty data volume and a
loopback listener, then:

    uv run --project backend --frozen --extra mcp --no-env-file \
        python scripts/image_smoke.py http://localhost:<port> <container>

Checks: the UI, API, and MCP answer through Caddy; a foreign Host is refused;
before the owner account exists the owner's routes and the Client API refuse;
the owner is created with the container's ``hybro owner`` helper and signs in;
a fresh install requires a client token, so a request without one is refused
and a client is created with the container's ``hybro client`` helper; the
sample agents register, the LLM-backed ones inactive with
``provider_not_configured``; Echo answers through the sessions API and MCP,
which send that client token.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import subprocess
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

API = "/api/v1"
LLM_SAMPLES = {
    "Weather Agent",
    "Story Agent",
    "Travel Planner Agent",
    "Image Generator Agent",
}
SIGNATURE = "Created by Hybro"
OWNER_EMAIL = "smoke-owner@example.com"
OWNER_CLI = (
    "/app/backend/.venv/bin/python",
    "/app/backend/owner_cli.py",
)
CLIENT_CLI = (
    "/app/backend/.venv/bin/python",
    "/app/backend/client_cli.py",
)


def create_owner(container: str, password: str) -> None:
    """``hybro owner create`` inside the container; the password on stdin."""
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            *OWNER_CLI,
            "create",
            "--name",
            "Smoke Owner",
            "--email",
            OWNER_EMAIL,
        ],
        input=f"{password}\n",
        text=True,
        check=True,
    )


def create_client(container: str) -> str:
    """``hybro client create`` inside the container; returns the new token.

    The helper prints the token alone on its last line. It is kept in memory
    and never printed here.
    """
    created = subprocess.run(
        ["docker", "exec", container, *CLIENT_CLI, "create", "--name", "Smoke"],
        capture_output=True,
        text=True,
        check=True,
    )
    return created.stdout.strip().splitlines()[-1]


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


async def mcp_echo(url: str, token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
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
    parser.add_argument("container", help="the running container's name")
    arguments = parser.parse_args()
    base = arguments.base_url.rstrip("/")
    # The cross-site check wants Hybro's own address on sign-in and on every
    # change that carries the cookie.
    owner = httpx.Client(
        base_url=base, timeout=10, trust_env=False, headers={"Origin": base}
    )
    # The Client API as connected programs call it: without a token first,
    # then with the client token once it exists.
    with (
        owner as http,
        httpx.Client(base_url=base, timeout=10, trust_env=False) as client,
    ):
        wait_for(
            lambda: http.get("/health").status_code == 200,
            timeout=180,
            what="backend health",
        )
        for path in ("/", "/hybro-mcp"):
            assert http.get(path).status_code == 200, path
        foreign = http.get("/health", headers={"Host": "rebound.example"})
        assert foreign.status_code == 421, foreign.status_code

        # No account yet: the owner's routes and the Client API refuse.
        status = http.get(f"{API}/auth/status")
        assert status.json() == {"account_exists": False, "owner": None}, status
        refused = http.get(f"{API}/setup/status")
        assert refused.status_code == 401, refused.status_code
        # A fresh install requires a client token, so that refusal comes first.
        refused = client.get(f"{API}/agents/discovery")
        assert refused.status_code == 401, refused.status_code
        assert refused.json()["detail"]["code"] == "client_token_required", refused.text

        password = secrets.token_urlsafe(24)
        create_owner(arguments.container, password)
        signed_in = http.post(
            f"{API}/auth/sign-in",
            json={"email": OWNER_EMAIL, "password": password},
        )
        assert signed_in.status_code == 200, signed_in.status_code
        assert http.get(f"{API}/setup/status").status_code == 200
        requirement = http.get(f"{API}/clients/token-requirement").json()
        assert requirement["required"] is True, requirement

        refused = client.get(f"{API}/agents/discovery")
        assert refused.status_code == 401, refused.status_code
        token = create_client(arguments.container)
        client.headers["Authorization"] = f"Bearer {token}"
        assert client.get(f"{API}/agents/discovery").status_code == 200

        found = wait_for(
            lambda: samples_registered(http), timeout=120, what="sample agents"
        )
        assert found["Echo Agent"]["status"] == "active", found["Echo Agent"]
        for name in LLM_SAMPLES:
            agent = found[name]
            assert agent["status"] == "inactive", agent
            assert agent["inactive_reason"] == "provider_not_configured", agent

        sent = client.post(
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
            lambda: reply_of(client, sent.json()["session_id"]),
            timeout=60,
            what="Echo through the API",
        )
        assert "api smoke" in reply and SIGNATURE in reply, reply

    reply = asyncio.run(mcp_echo(f"{base}/mcp", token))
    assert "mcp smoke" in reply and SIGNATURE in reply, reply
    print("Image smoke test passed.")


if __name__ == "__main__":
    main()
