"""Shared fixtures and utilities for backend functional test suites."""

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Mapping

import httpx
import pytest

API_BASE_URL = os.environ.get("HYBRO_API_URL", "http://localhost:8000/api/v1")
DEFAULT_TRAVEL_PLANNER_ID = "575ee896f1e24823943a1e98aee111c9"
DEFAULT_WEATHER_ID = "c13e753ad4f74c25bfb684de5572622a"
FUNCTIONAL_SKIP_REASON = (
    "functional tests call a live Hybro and its agents; set "
    "HYBRO_FORCE_FUNCTIONAL_TESTS=1 or HYBRO_API_URL to run them"
)


def functional_tests_enabled(environ: Mapping[str, str]) -> bool:
    """Opt-in only: a reachable localhost may be someone's real Hybro."""
    return environ.get("HYBRO_FORCE_FUNCTIONAL_TESTS") == "1" or bool(
        environ.get("HYBRO_API_URL")
    )


def pytest_collection_modifyitems(config, items):
    """Skip functional tests unless the run explicitly targets a live Hybro."""
    if functional_tests_enabled(os.environ):
        return
    skip_marker = pytest.mark.skip(reason=FUNCTIONAL_SKIP_REASON)
    for item in items:
        if "functional" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture
async def functional_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Provides an AsyncClient connected to the running backend service."""
    # Answering an agent question resumes the live A2A call inline, so a
    # single request can take as long as the agent's model turn.
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=180.0) as client:
        yield client


@pytest.fixture
async def travel_planner_agent_id(functional_client: httpx.AsyncClient) -> str:
    """Resolves the live Travel Planner Agent ID, polling until registered."""
    for _ in range(20):
        try:
            resp = await functional_client.get("/agent/getAllAgents?active_only=true")
            if resp.status_code == 200:
                agents = resp.json().get("agents", [])
                for a in agents:
                    name = (a.get("name") or "").lower()
                    if "travel" in name or "planner" in name:
                        return a.get("agent_id")
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return DEFAULT_TRAVEL_PLANNER_ID


@pytest.fixture
async def weather_agent_id(functional_client: httpx.AsyncClient) -> str:
    """Resolves the live Weather Agent ID, polling until registered."""
    for _ in range(20):
        try:
            resp = await functional_client.get("/agent/getAllAgents?active_only=true")
            if resp.status_code == 200:
                agents = resp.json().get("agents", [])
                for a in agents:
                    name = (a.get("name") or "").lower()
                    if "weather" in name:
                        return a.get("agent_id")
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return DEFAULT_WEATHER_ID


@pytest.fixture
def test_room_payload(travel_planner_agent_id: str):
    """Generates a default test room payload using resolved agent IDs."""

    def _factory(
        room_name: str = "Functional Test Room",
        agent_ids: list[str] | None = None,
        use_supervisor: bool = True,
    ) -> dict:
        return {
            "room_name": room_name,
            "room_owner_name": "Developer",
            "membership_seed_input": "manual",
            "room_agent_ids": agent_ids or [travel_planner_agent_id],
            "extend_info": {"use_supervisor": use_supervisor},
        }

    return _factory


async def auto_respond_pending_hitl(
    client: httpx.AsyncClient,
    conversation_id: str,
    answers: list[str] | None = None,
    client_request_id: str | None = None,
    seen_request_ids: set[str] | None = None,
) -> list[dict]:
    """Answer each new pending HITL interaction for a conversation in one batch."""
    if seen_request_ids is None:
        seen_request_ids = set()
    default_answers = answers or [
        "Kyoto",
        "3 days, budget $1500",
        "Mid-range hotels and public transit",
    ]
    answered = []
    pending_resp = await client.get(f"/rooms/{conversation_id}/hitl/pending")
    if pending_resp.status_code != 200:
        return answered

    # One interaction may ask several questions; it is answered as one batch,
    # the same way the frontend submits it.
    # A later round can reuse the interaction id with a new version, so the
    # seen set is keyed by both.
    by_interaction: dict[tuple[str, str], list[dict]] = {}
    for r in pending_resp.json().get("requests", []):
        interaction_id = r.get("interaction_id")
        seen_key = f"{interaction_id}@{r.get('interaction_version')}"
        if interaction_id and seen_key not in seen_request_ids:
            by_interaction.setdefault((interaction_id, seen_key), []).append(r)

    for (interaction_id, seen_key), requests in by_interaction.items():
        seen_request_ids.add(seen_key)
        batch = [
            {
                "request_id": r["request_id"],
                "user_input": default_answers[min(index, len(default_answers) - 1)],
            }
            for index, r in enumerate(requests)
        ]
        submit_resp = await client.post(
            f"/rooms/{conversation_id}/hitl/respond-batch",
            json={
                "interaction_id": interaction_id,
                "answers": batch,
                "client_request_id": requests[0].get("client_request_id")
                or client_request_id,
            },
        )
        if submit_resp.status_code == 200:
            answered.append(
                {
                    "requests": requests,
                    "answers": batch,
                    "response": submit_resp.json(),
                }
            )
    return answered


def _message_text(msg: dict) -> str:
    return ((msg.get("message_content") or {}).get("message_text") or "").strip()


def _task_state(msg: dict) -> str | None:
    task = (msg.get("message_content") or {}).get("message_task") or {}
    status = task.get("status") or {}
    state = status.get("state")
    return state if isinstance(state, str) else None


def find_terminal_synthesis(
    messages: list[dict],
    client_request_id: str | None = None,
) -> dict | None:
    """Return the completed supervisor synthesis for the current client_request_id, if present."""
    for msg in reversed(messages):
        if msg.get("message_type") != "agent":
            continue
        if client_request_id and msg.get("client_request_id") != client_request_id:
            continue
        if msg.get("agent_id") != "system:hybro":
            continue
        text = _message_text(msg)
        if len(text) <= 30:
            continue
        state = _task_state(msg)
        if state in {None, "completed"}:
            return msg
    return None


async def wait_for_completion_with_auto_hitl(
    client: httpx.AsyncClient,
    conversation_id: str,
    answers: list[str] | None = None,
    client_request_id: str | None = None,
    timeout_seconds: float = 65.0,
    poll_interval: float = 1.0,
    min_hitl_answers: int = 0,
) -> tuple[dict | None, list[dict]]:
    """Polls room messages until final agent synthesis is complete, automatically responding to any HITL questions."""
    start_time = time.time()
    seen_request_ids: set[str] = set()
    all_hitl_answered: list[dict] = []
    completed_agent_message = None

    while time.time() - start_time < timeout_seconds:
        newly_answered = await auto_respond_pending_hitl(
            client=client,
            conversation_id=conversation_id,
            answers=answers,
            client_request_id=client_request_id,
            seen_request_ids=seen_request_ids,
        )
        all_hitl_answered.extend(newly_answered)

        history_resp = await client.get(
            f"/rooms/{conversation_id}/messages",
        )
        if history_resp.status_code == 200:
            messages = history_resp.json().get("message_list", [])
            completed_agent_message = find_terminal_synthesis(
                messages,
                client_request_id=client_request_id,
            )
        if completed_agent_message and len(all_hitl_answered) >= min_hitl_answers:
            break

        await asyncio.sleep(poll_interval)

    return completed_agent_message, all_hitl_answered
