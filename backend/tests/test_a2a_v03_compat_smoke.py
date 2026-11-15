"""Smoke test: Hybro still reaches a pre-1.0 (A2A 0.3) agent.

A2A 1.0 is served by a2a-sdk 1.x. Older agents are not migrated in lockstep, so
Hybro relies on the SDK's 0.3 compatibility transport, selected from the
protocol version each agent card advertises. This test proves that path against
a real 0.3 agent rather than a mock, because the compatibility layer is exactly
what a mock would paper over.

The 0.3 agent must run in its own environment: the SDK cannot be installed twice
under the same module name. Point `A2A_V03_AGENT_URL` at such an agent to run
this test; otherwise it is skipped.

Running it locally::

    # once, in a scratch venv
    uv venv /tmp/a2a03/.venv && uv pip install --python /tmp/a2a03/.venv/bin/python \\
        "a2a-sdk[http-server]==0.3.25" uvicorn
    # a minimal 0.3 server: AgentCard(url=..., protocolVersion="0.3.0") behind
    # A2AStarletteApplication, on a free port
    /tmp/a2a03/.venv/bin/python /tmp/a2a03/legacy_server.py &
    A2A_V03_AGENT_URL=http://127.0.0.1:7901 pytest tests/test_a2a_v03_compat_smoke.py
"""

from __future__ import annotations

import os

import pytest

from a2a_adapter.card_resolver import AgentCardResolverImpl
from a2a_adapter.client_facade import fetch_agent_card_with_fallback, send_message
from a2a_adapter.translators import facade_result_to_model
from common.types import TaskState

AGENT_URL = os.environ.get("A2A_V03_AGENT_URL")

pytestmark = pytest.mark.skipif(
    not AGENT_URL,
    reason="set A2A_V03_AGENT_URL to a running A2A 0.3 agent to run this smoke test",
)


def _message_text(task) -> str:
    message = task.status.message
    if message is None:
        return ""
    return "".join(
        part.root.text
        for part in message.parts
        if getattr(part.root, "kind", None) == "text"
    )


@pytest.mark.asyncio
async def test_legacy_card_resolves_with_its_advertised_protocol_version():
    resolver = AgentCardResolverImpl()
    try:
        snapshot = await resolver.resolve_card(AGENT_URL)
    finally:
        await resolver.aclose()

    assert snapshot is not None
    assert snapshot.url
    versions = [interface.protocol_version for interface in snapshot.interfaces]
    assert versions == ["0.3.0"], snapshot.interfaces


@pytest.mark.asyncio
async def test_message_is_sent_and_reply_is_received_from_legacy_agent():
    card = await fetch_agent_card_with_fallback(AGENT_URL)

    response = await send_message(
        card,
        {
            "role": "user",
            "message_id": "v03-smoke-1",
            "parts": [{"kind": "text", "text": "ping"}],
        },
        accepted_output_modes=["text/plain"],
        timeout=30,
        registered_url="http://agent.test",
    )

    assert response["kind"] == "task"
    task = facade_result_to_model(response)
    assert task.status.state == TaskState.completed
    assert _message_text(task) == "legacy:ping"
