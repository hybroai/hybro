from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from delivery.snapshot import RoomEventFold, SnapshotService
from tests.fakes.room_events import InMemoryRoomEventStore

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _run_event(seq: int, kind: str, payload: dict) -> dict:
    return {
        "room_seq": seq,
        "kind": "run_event",
        "ts": NOW.isoformat(),
        "payload_public": {
            "event_id": f"public:run-1:{kind}:{seq}",
            "run_id": "run-1",
            "seq": seq,
            "type": kind,
            "payload": payload,
            "correlation_id": "client-1",
        },
    }


def _hitl_event(
    seq: int,
    kind: str,
    *,
    request_id: str,
    interaction_id: str,
    question_index: int = 0,
    question_count: int = 1,
) -> dict:
    payload = {
        "request_id": request_id,
        "message_id": "orchestrator:run-1:inv_agent",
        "source": "agent",
        "interaction_id": interaction_id,
        "question_index": question_index,
        "question_count": question_count,
        "run_id": "run-1",
        "related_user_message_id": "user-1",
        "client_request_id": "client-1",
    }
    if kind == "hitl_request":
        payload.update({"prompt": f"Question {request_id}", "prompt_type": "text"})
    else:
        payload.update({"status": "responded", "answer_ref": f"answer-{request_id}"})
    return {
        "room_seq": seq,
        "kind": kind,
        "ts": NOW.isoformat(),
        "payload_public": payload,
    }


def test_canonical_turn_fold_requires_exact_final_commit_and_child_closure():
    fold = RoomEventFold()
    events = [
        _run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        _run_event(2, "turn_start", {"internal_turn_id": "turn-1", "attempt": 1}),
        _run_event(
            3,
            "message_start",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "role": "assistant",
            },
        ),
        _run_event(
            4,
            "message_update",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "assistant_message_event": {
                    "type": "text_delta",
                    "content_index": 0,
                    "delta_index": 0,
                    "start_offset": 0,
                    "end_offset": 4,
                    "delta": "done",
                },
            },
        ),
        _run_event(
            5,
            "message_end",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "stop_reason": "stop",
                "disposition": "final",
                "text": "done",
            },
        ),
        _run_event(
            6,
            "turn_end",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "tool_call_ids": [],
                "status": "completed",
            },
        ),
    ]
    for event in events:
        fold.apply(event)
    # An unrelated specialist response cannot commit this final.
    fold.apply(
        {
            "room_seq": 7,
            "kind": "agent_response",
            "ts": NOW.isoformat(),
            "payload_public": {
                "message_id": "assistant-1",
                "content": "wrong",
                "client_request_id": "different",
                "related_message_id": "user-1",
            },
        }
    )
    fold.apply(
        _run_event(
            8,
            "run_settled",
            {
                "status": "completed",
                "started_at": NOW.isoformat(),
                "settled_at": (NOW + timedelta(seconds=1)).isoformat(),
                "duration_ms": 1000,
                "final_message_id": "assistant-1",
            },
        )
    )
    turn = fold.state(room_seq=8)["turns"][0]
    assert turn["state"] == "active"
    assert turn["final_committed"] is False

    fold.apply(
        {
            "room_seq": 9,
            "kind": "agent_response",
            "ts": NOW.isoformat(),
            "payload_public": {
                "message_id": "assistant-1",
                "content": "durable done",
                "client_request_id": "client-1",
                "related_message_id": "user-1",
            },
        }
    )
    fold.apply(
        {
            **_run_event(
                10,
                "run_settled",
                {
                    "status": "completed",
                    "started_at": NOW.isoformat(),
                    "settled_at": (NOW + timedelta(seconds=1)).isoformat(),
                    "duration_ms": 1000,
                    "final_message_id": "assistant-1",
                },
            )
        }
    )
    turn = fold.state(room_seq=10)["turns"][0]
    assert turn["state"] == "completed"
    assert turn["final_answer"]["text"] == "durable done"
    assert turn["final_committed"] is True


def test_canonical_fold_projects_model_decision_activity():
    fold = RoomEventFold()
    events = [
        _run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        _run_event(2, "turn_start", {"internal_turn_id": "turn-1", "attempt": 1}),
        _run_event(
            3,
            "message_start",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "role": "assistant",
            },
        ),
        _run_event(
            4,
            "tool_execution_start",
            {
                "internal_turn_id": "turn-1",
                "tool_call_id": "inv_agent_0001",
                "tool_name": "Cyber Broker Agent",
                "input": {},
                "execution_kind": "agent",
                "target": {"name": "Cyber Broker Agent", "source": None},
            },
        ),
        _run_event(
            5,
            "model_decision",
            {
                "internal_turn_id": "turn-1",
                "decision": "interaction_received",
                "agent_label": "Cyber Broker Agent",
                "question_summary": "Which cloud provider?",
            },
        ),
    ]
    for event in events:
        fold.apply(event)
    turn = fold.state(room_seq=5)["turns"][0]
    decisions = [item for item in turn["activity"] if item["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "interaction_received"
    assert decisions[0]["agent_label"] == "Cyber Broker Agent"


def test_canonical_fold_projects_answered_from_context_and_no_progress():
    fold = RoomEventFold()
    base = [
        _run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        _run_event(2, "turn_start", {"internal_turn_id": "turn-1", "attempt": 1}),
        _run_event(
            3,
            "message_start",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "role": "assistant",
            },
        ),
        _run_event(
            4,
            "model_decision",
            {
                "internal_turn_id": "turn-1",
                "decision": "answered_from_context",
                "agent_label": "Cyber Broker Agent",
                "question_summary": "Which cloud provider?",
                "source_summary": "from earlier messages and attachments",
            },
        ),
        _run_event(
            5,
            "model_decision",
            {
                "internal_turn_id": "turn-1",
                "decision": "no_progress",
                "agent_label": "Cyber Broker Agent",
                "question_summary": "Which cloud provider?",
                "reason": "auto_reply_limit_reached",
            },
        ),
    ]
    for event in base:
        fold.apply(event)
    turn = fold.state(room_seq=5)["turns"][0]
    decisions = [item for item in turn["activity"] if item["kind"] == "decision"]
    assert [d["decision"] for d in decisions] == [
        "answered_from_context",
        "no_progress",
    ]
    assert decisions[0]["agent_label"] == "Cyber Broker Agent"
    assert decisions[0]["source_summary"] == "from earlier messages and attachments"
    assert decisions[1]["reason"] == "auto_reply_limit_reached"


def test_snapshot_ignores_new_questions_reusing_a_resumed_interaction_id():
    fold = RoomEventFold()
    events = [
        _run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        _hitl_event(
            2,
            "hitl_request",
            request_id="details",
            interaction_id="interaction-1",
        ),
        _run_event(
            3,
            "run_waiting_input",
            {
                "interaction_id": "interaction-1",
                "request_ids": ["details"],
                "requested_at": NOW.isoformat(),
            },
        ),
        _hitl_event(
            4,
            "hitl_response",
            request_id="details",
            interaction_id="interaction-1",
        ),
        _run_event(
            5,
            "run_resumed",
            {
                "interaction_id": "interaction-1",
                "resolved_request_ids": ["details"],
                "resumed_at": NOW.isoformat(),
            },
        ),
        # A CAS-losing observation must not merge a second questionnaire into
        # the already resumed canonical interaction.
        _hitl_event(
            6,
            "hitl_request",
            request_id="security_training",
            interaction_id="interaction-1",
            question_index=0,
            question_count=2,
        ),
        _hitl_event(
            7,
            "hitl_request",
            request_id="cloud_providers",
            interaction_id="interaction-1",
            question_index=1,
            question_count=2,
        ),
    ]

    assert all(fold.apply(event) for event in events)
    state = fold.state(room_seq=7)
    assert [request["request_id"] for request in state["hitl"]["requests"]] == [
        "details"
    ]
    assert state["hitl"]["requests"][0]["room_seq"] == 2
    interaction = state["turns"][0]["hitl_interactions"][0]
    assert interaction["state"] == "resumed"
    assert interaction["request_ids"] == ["details"]
    assert [request["request_id"] for request in interaction["requests"]] == ["details"]


def test_canonical_message_end_must_equal_already_assembled_deltas():
    fold = RoomEventFold()
    for event in [
        _run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        ),
        _run_event(2, "turn_start", {"internal_turn_id": "turn-1", "attempt": 1}),
        _run_event(
            3,
            "message_start",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "role": "assistant",
            },
        ),
        _run_event(
            4,
            "message_update",
            {
                "internal_turn_id": "turn-1",
                "message_id": "assistant-1",
                "assistant_message_event": {
                    "type": "text_delta",
                    "content_index": 0,
                    "delta_index": 0,
                    "start_offset": 0,
                    "end_offset": 12,
                    "delta": "durable text",
                },
            },
        ),
    ]:
        fold.apply(event)

    with pytest.raises(ValueError, match="contradicts assembled durable deltas"):
        fold.apply(
            _run_event(
                5,
                "message_end",
                {
                    "internal_turn_id": "turn-1",
                    "message_id": "assistant-1",
                    "stop_reason": "stop",
                    "disposition": "final",
                    "text": "short",
                },
            )
        )
    turn = fold.state(room_seq=4)["turns"][0]
    assert turn["current_assistant"]["text"] == "durable text"


@pytest.mark.asyncio
async def test_snapshot_pages_complete_contiguous_history():
    store = InMemoryRoomEventStore()
    for index in range(205):
        await store.append(
            conversation_id="room-1",
            kind="cancellation",
            payload_public={"message_id": f"message-{index}"},
            idempotency_key=f"event-{index}",
            ts=NOW,
        )
    service = SnapshotService(store=store, read_limit=20)
    snapshot = await service.snapshot("room-1", force=True)
    assert snapshot["room_seq"] == 205
    assert len(snapshot["messages"]) == 205
    assert snapshot["turn_lifecycle_schema"] == 1
    assert snapshot["turns"] == []


@pytest.mark.asyncio
async def test_invalid_canonical_event_does_not_advance_snapshot_checkpoint():
    store = InMemoryRoomEventStore()
    started = _run_event(
        1,
        "run_started",
        {
            "hybro_turn_id": "run-1",
            "user_message_id": "user-1",
            "started_at": NOW.isoformat(),
            "mode": "fast",
        },
    )["payload_public"]
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=started,
        idempotency_key="started",
        ts=NOW,
    )
    service = SnapshotService(store=store)
    assert (await service.snapshot("room-1"))["room_seq"] == 1

    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public={**started, "event_id": "bad", "private": "forbidden"},
        idempotency_key="invalid",
        ts=NOW,
    )
    with pytest.raises(ValueError, match="unknown public fields"):
        await service.snapshot("room-1")

    assert service._checkpoints["room-1"][0] == 1


@pytest.mark.asyncio
async def test_semantically_rejected_canonical_event_stops_at_prior_watermark():
    store = InMemoryRoomEventStore()
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_run_event(
            1,
            "run_started",
            {
                "hybro_turn_id": "run-1",
                "user_message_id": "user-1",
                "started_at": NOW.isoformat(),
                "mode": "fast",
            },
        )["payload_public"],
        idempotency_key="started",
        ts=NOW,
    )
    service = SnapshotService(store=store)
    assert (await service.snapshot("room-1"))["room_seq"] == 1
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_run_event(
            2,
            "message_start",
            {
                "internal_turn_id": "missing-turn",
                "message_id": "assistant-1",
                "role": "assistant",
            },
        )["payload_public"],
        idempotency_key="orphan-message",
        ts=NOW,
    )

    with pytest.raises(ValueError, match="canonical fold rejected room_seq=2"):
        await service.snapshot("room-1")

    assert service._checkpoints["room-1"][0] == 1


@pytest.mark.asyncio
async def test_snapshot_builds_for_one_room_are_serialized_and_monotonic():
    inner = InMemoryRoomEventStore()
    await inner.append(
        conversation_id="room-1",
        kind="cancellation",
        payload_public={"message_id": "message-1"},
        idempotency_key="one",
        ts=NOW,
    )

    class MeasuringStore:
        active = 0
        max_active = 0

        async def read_range(self, *args, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            try:
                return await inner.read_range(*args, **kwargs)
            finally:
                self.active -= 1

        async def latest_seq(self, conversation_id):
            return await inner.latest_seq(conversation_id)

    store = MeasuringStore()
    service = SnapshotService(store=store)
    snapshots = await asyncio.gather(
        service.snapshot("room-1", force=True),
        service.snapshot("room-1", force=True),
    )

    assert store.max_active == 1
    assert [snapshot["room_seq"] for snapshot in snapshots] == [1, 1]
    assert service._checkpoints["room-1"][0] == 1
