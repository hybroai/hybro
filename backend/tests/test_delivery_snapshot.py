"""Snapshot fold + incremental materialization (Room Stream Snapshot plan §5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from delivery.snapshot import RoomEventFold, SnapshotService
from tests.fakes.room_events import InMemoryRoomEventStore

NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


def _record(kind: str, data: dict, *, room_seq: int = 0) -> dict:
    return {
        "room_seq": room_seq,
        "room_event_id": f"evt-{kind}-{room_seq}",
        "event_id": f"logical-{kind}-{room_seq}",
        "parent_event_id": None,
        "run_id": None,
        "kind": kind,
        "ts": NOW.isoformat(),
        "payload_public": data,
        "persist_state": "settled",
    }


def test_fold_projects_agent_response_and_partials():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "agent_response_partial",
            {"message_id": "m1", "agent_id": "a1", "content_delta": "Hel"},
        )
    )
    fold.apply(
        _record(
            "agent_response_partial",
            {"message_id": "m1", "agent_id": "a1", "content_delta": "lo"},
        )
    )
    fold.apply(
        _record(
            "agent_response",
            {
                "message_id": "m1",
                "agent_id": "a1",
                "content": "Hello",
                "client_request_id": "cr-1",
            },
        )
    )

    state = fold.state(room_seq=3)
    assert state["room_seq"] == 3
    assert state["streaming"] == {}
    assert state["messages"][0] == {
        "message_id": "m1",
        "agent_id": "a1",
        "agent_name": None,
        "content": "Hello",
        "parts": None,
        "related_message_id": None,
        "client_request_id": "cr-1",
        "status": None,
        "task_status": None,
        "task_content": None,
        "task_error": None,
        "requires_input": False,
        "requires_auth": False,
        "step_number": None,
        "total_steps": None,
        "created_at": None,
        "ts": NOW.isoformat(),
        "artifacts": None,
        "status_logs": [],
    }


def test_fold_keeps_partial_streams_without_terminal_commit():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "agent_response_partial",
            {"message_id": "m1", "agent_id": "a1", "content_delta": "Hel"},
        )
    )
    fold.apply(
        _record(
            "agent_response_partial",
            {"message_id": "m1", "agent_id": "a1", "content_delta": "lo"},
        )
    )

    state = fold.state(room_seq=2)
    assert state["messages"] == []
    assert state["streaming"]["m1"]["text"] == "Hello"
    assert state["streaming"]["m1"]["is_complete"] is False


def test_fold_projects_tasks_and_terminal_cleanup():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "task_submitted",
            {
                "message_id": "m1",
                "task_id": "t1",
                "agent_name": "Weather Agent",
                "agent_id": "a1",
                "status": "working",
                "client_request_id": "cr-1",
            },
        )
    )
    fold.apply(
        _record(
            "task_update",
            {"message_id": "m1", "status": "completed", "content": "Sunny"},
        )
    )

    state = fold.state(room_seq=2)
    assert state["tasks"][0]["status"] == "completed"
    assert state["messages"][0]["task_status"] == "completed"
    assert state["messages"][0]["content"] == "Sunny"


def test_fold_projects_terminal_processing_status_and_log():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "processing_status",
            {
                "message_id": "m1",
                "status": "processing",
                "details": {"message": "Thinking…", "turn_phase": "collecting"},
            },
        )
    )
    fold.apply(
        _record("processing_status", {"message_id": "m1", "status": "completed"})
    )

    state = fold.state(room_seq=2)
    message = state["messages"][0]
    assert message["status"] == "completed"
    assert message["status_logs"] == [
        {
            "message": "Thinking…",
            "timestamp": NOW.isoformat(),
            "turn_phase": "collecting",
        }
    ]


def test_fold_projects_processing_detail_aliases():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "processing_status",
            {
                "message_id": "m1",
                "status": "processing",
                "details": {"status_message": "Contacting agent"},
            },
        )
    )

    state = fold.state(room_seq=1)
    assert state["messages"][0]["status_logs"] == [
        {"message": "Contacting agent", "timestamp": NOW.isoformat()}
    ]


def test_fold_stringifies_non_json_processing_details():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "processing_status",
            {
                "message_id": "m1",
                "status": "processing",
                "details": {"observed_at": NOW},
            },
        )
    )

    state = fold.state(room_seq=1)
    assert state["messages"][0]["status_logs"] == [
        {
            "message": f'{{"observed_at":"{NOW}"}}',
            "timestamp": NOW.isoformat(),
        }
    ]


def test_fold_ignores_stored_non_canonical_run_events():
    fold = RoomEventFold()
    for room_seq, kind in enumerate(
        ("llm_call_completed", "tool_call_accepted", "run_completed"), start=1
    ):
        assert fold.apply(
            _record(
                "run_event",
                {
                    "run_id": "run-1",
                    "event_id": f"public:run-1:{kind}:{room_seq}",
                    "seq": room_seq,
                    "type": kind,
                    "payload": {"tool_name": "weather_lookup"},
                    "correlation_id": "cr-1",
                },
                room_seq=room_seq,
            )
        )

    state = fold.state(room_seq=3)
    assert "runs" not in state
    assert "trace" not in state
    assert state["turns"] == []


def test_fold_projects_hitl_requests_and_responses():
    fold = RoomEventFold()
    fold.apply(
        _record(
            "hitl_request",
            {
                "request_id": "h1",
                "message_id": "m1",
                "prompt": "Approve?",
                "prompt_type": "confirmation",
                "source": "agent",
            },
        )
    )
    fold.apply(
        _record(
            "hitl_response",
            {
                "request_id": "h1",
                "message_id": "m1",
                "source": "agent",
                "status": "responded",
                "interaction_id": "i1",
            },
        )
    )

    state = fold.state(room_seq=2)
    assert state["hitl"]["requests"][0]["status"] == "responded"
    assert len(state["hitl"]["resolved"]) == 1


def test_fold_scopes_reused_question_ids_by_interaction():
    fold = RoomEventFold()
    for interaction_id, message_id in (("i1", "m1"), ("i2", "m2")):
        assert fold.apply(
            _record(
                "hitl_request",
                {
                    "request_id": "cloud_providers",
                    "message_id": message_id,
                    "prompt": "Which providers?",
                    "prompt_type": "text",
                    "source": "agent",
                    "interaction_id": interaction_id,
                },
            )
        )
    assert fold.apply(
        _record(
            "hitl_response",
            {
                "request_id": "cloud_providers",
                "message_id": "m1",
                "source": "agent",
                "status": "responded",
                "interaction_id": "i1",
            },
        )
    )

    requests = {
        request["interaction_id"]: request
        for request in fold.state(room_seq=3)["hitl"]["requests"]
    }
    assert requests["i1"]["status"] == "responded"
    assert "status" not in requests["i2"]


async def _seeded_service() -> SnapshotService:
    store = InMemoryRoomEventStore()
    await store.append(
        conversation_id="room-1",
        kind="agent_response",
        payload_public={"message_id": "m1", "agent_id": "a1", "content": "one"},
        event_id="m1-final",
    )
    await store.append(
        conversation_id="room-1",
        kind="agent_response",
        payload_public={"message_id": "m2", "agent_id": "a1", "content": "two"},
        event_id="m2-final",
    )
    return SnapshotService(store=store)


@pytest.mark.asyncio
async def test_snapshot_watermark_tracks_contiguous_prefix():
    service = await _seeded_service()
    snapshot = await service.snapshot("room-1")
    assert snapshot["room_seq"] == 2
    assert len(snapshot["messages"]) == 2

    # Incremental: no new events, same watermark, same content.
    again = await service.snapshot("room-1")
    assert again["room_seq"] == 2
    assert again["messages"] == snapshot["messages"]


@pytest.mark.asyncio
async def test_snapshot_force_refolds_from_log():
    service = await _seeded_service()
    snapshot = await service.snapshot("room-1")
    assert snapshot["room_seq"] == 2

    forced = await service.snapshot("room-1", force=True)
    assert forced["room_seq"] == 2
    assert forced["messages"] == snapshot["messages"]


@pytest.mark.asyncio
async def test_snapshot_stops_at_first_gap():
    store = InMemoryRoomEventStore()
    await store.append(
        conversation_id="room-1",
        kind="agent_response",
        payload_public={"message_id": "m1", "content": "one"},
        event_id="m1-final",
    )
    await store.append(
        conversation_id="room-1",
        kind="agent_response",
        payload_public={"message_id": "m3", "content": "three"},
        event_id="m3-final",
    )
    # Simulate a permanent hole: seq 2 was burned and tombstoned as skipped
    # (InMemory tombstone takes the next free seq, mirroring the healed hole).
    await store.append(
        conversation_id="room-1",
        kind="skipped",
        payload_public={},
        idempotency_key="skip:room-1:2",
    )

    service = SnapshotService(store=store)
    snapshot = await service.snapshot("room-1", force=True)
    # The fold advanced past the tombstone and folded m3.
    assert snapshot["room_seq"] == 3
    assert [message["message_id"] for message in snapshot["messages"]] == ["m1", "m3"]


def test_fold_keeps_failed_agent_error_code_on_the_tool_item():
    fold = RoomEventFold()
    events = [
        (
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        ("turn_start", {"internal_turn_id": "turn-1", "attempt": 1}),
        (
            "tool_execution_start",
            {
                "internal_turn_id": "turn-1",
                "tool_call_id": "inv_abcdefgh",
                "tool_name": "Weather Agent",
                "input": {"task": "weather"},
            },
        ),
        (
            "tool_execution_end",
            {
                "internal_turn_id": "turn-1",
                "tool_call_id": "inv_abcdefgh",
                "tool_name": "Weather Agent",
                "outcome": "failed",
                "result": "",
                "is_error": True,
                "duration_ms": 2,
                "failure_reason": "execution",
                "error_code": "llm_quota_exceeded",
                "error_message": "Quota exceeded",
            },
        ),
    ]
    for seq, (kind, payload) in enumerate(events, start=1):
        assert fold.apply(
            _record(
                "run_event",
                {
                    "run_id": "run-1",
                    "event_id": f"event-{seq}",
                    "seq": seq,
                    "type": kind,
                    "payload": payload,
                    "correlation_id": "cr-1",
                },
                room_seq=seq,
            )
        )

    tool = fold.state(room_seq=len(events))["turns"][0]["activity"][0]
    assert (tool["status"], tool["error_code"], tool["error_message"]) == (
        "failed",
        "llm_quota_exceeded",
        "Quota exceeded",
    )
