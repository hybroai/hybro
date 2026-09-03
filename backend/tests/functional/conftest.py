"""Shared fixtures and utilities for backend functional test suites."""

import asyncio
import os
import time
from collections.abc import AsyncGenerator

import httpx
import pytest

API_BASE_URL = os.environ.get("HYBRO_API_URL", "http://localhost:8000/api/v1")
DEFAULT_TRAVEL_PLANNER_ID = "575ee896f1e24823943a1e98aee111c9"
DEFAULT_WEATHER_ID = "c13e753ad4f74c25bfb684de5572622a"


def _is_backend_available() -> bool:
    """Check if live backend service is reachable."""
    try:
        resp = httpx.get(
            f"{API_BASE_URL}/roomCenter/inquiryActiveRuns",
            timeout=1.0,
        )
        return resp.status_code in {200, 401, 403, 405, 422}
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Automatically skip functional tests if live backend service is unreachable (e.g. offline CI)."""
    if os.environ.get("HYBRO_FORCE_FUNCTIONAL_TESTS") == "1":
        return

    if not _is_backend_available():
        skip_marker = pytest.mark.skip(
            reason="Live Hybro backend service is not running (set HYBRO_FORCE_FUNCTIONAL_TESTS=1 or start services)"
        )
        for item in items:
            if "functional" in item.keywords:
                item.add_marker(skip_marker)


@pytest.fixture
async def functional_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Provides an AsyncClient connected to the running backend service."""
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0) as client:
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
                    card = a.get("agent_card") or {}
                    name = card.get("name", "").lower()
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
                    card = a.get("agent_card") or {}
                    name = card.get("name", "").lower()
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
            "room_agent_ids": agent_ids or [travel_planner_agent_id],
            "extend_info": {"use_supervisor": use_supervisor},
        }

    return _factory


async def auto_respond_pending_hitl(
    client: httpx.AsyncClient,
    room_id: str,
    answers: list[str] | None = None,
    client_request_id: str | None = None,
    seen_request_ids: set[str] | None = None,
) -> list[dict]:
    """Inspects pending HITL requests for a room and automatically submits answers immediately."""
    if seen_request_ids is None:
        seen_request_ids = set()
    default_answers = answers or [
        "Kyoto",
        "3 days, budget $1500",
        "Mid-range hotels and public transit",
    ]
    answered = []
    pending_resp = await client.get(f"/rooms/{room_id}/hitl/pending")
    if pending_resp.status_code != 200:
        return answered

    requests = pending_resp.json().get("requests", [])
    for r in requests:
        req_id = r.get("request_id")
        if req_id and req_id not in seen_request_ids:
            seen_request_ids.add(req_id)
            answer_idx = min(len(seen_request_ids) - 1, len(default_answers) - 1)
            user_answer = default_answers[answer_idx]

            submit_resp = await client.post(
                f"/rooms/{room_id}/hitl/respond-batch",
                json={
                    "interaction_id": r["interaction_id"],
                    "answers": [
                        {
                            "request_id": req_id,
                            "user_input": user_answer,
                        }
                    ],
                    "client_request_id": r.get("client_request_id")
                    or client_request_id,
                },
            )
            if submit_resp.status_code == 200:
                answered.append(
                    {
                        "request": r,
                        "answer": user_answer,
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
    room_id: str,
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
            room_id=room_id,
            answers=answers,
            client_request_id=client_request_id,
            seen_request_ids=seen_request_ids,
        )
        all_hitl_answered.extend(newly_answered)

        history_resp = await client.post(
            "/roomCenter/inquiryRoomMessagesByRoomId",
            json={"room_id": room_id},
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
