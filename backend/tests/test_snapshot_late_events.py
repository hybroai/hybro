"""A late event for a settled Turn does not break the Room's snapshot.

A pass that kept running past a Run's end, a listener that loaded the Run
before the end, or a Run stopped before its ``run_started`` reached the Room
can leave a stored event the fold cannot place: an event after its Turn
settled, or a ``run_settled`` with no ``run_started``. The fold skips those,
logs one event with ids only, and builds every later Turn. Real contract
violations are still rejected.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from delivery.snapshot import RoomEventFold, SnapshotService
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.test_canonical_delivery_snapshot import NOW

SKIPPED = "room_snapshot_late_event_skipped"


def _event(
    seq: int, run: int, kind: str, payload: dict, *, correlation: str | None = None
) -> dict:
    return {
        "room_seq": seq,
        "room_id": "room-1",
        "kind": "run_event",
        "ts": NOW.isoformat(),
        "payload_public": {
            "event_id": f"public:run-{run}:{kind}:{seq}",
            "run_id": f"run-{run}",
            "seq": seq,
            "type": kind,
            "payload": payload,
            "correlation_id": correlation or f"client-{run}",
        },
    }


def _started(seq: int, run: int) -> dict:
    return _event(
        seq,
        run,
        "run_started",
        {
            "hybro_turn_id": f"run-{run}",
            "user_message_id": f"user-{run}",
            "started_at": NOW.isoformat(),
            "mode": "fast",
        },
    )


def _settled(seq: int, run: int, status: str = "canceled") -> dict:
    payload = {
        "status": status,
        "started_at": NOW.isoformat(),
        "settled_at": (NOW + timedelta(seconds=1)).isoformat(),
        "duration_ms": 1000,
    }
    if status == "canceled":
        payload["cancellation_code"] = "user_requested"
    else:
        payload["failure_code"] = "tool_failure"
        payload["error_summary"] = "failed"
    return _event(seq, run, "run_settled", payload)


def _turn_start(seq: int, run: int, **kwargs) -> dict:
    return _event(
        seq, run, "turn_start", {"internal_turn_id": "turn-1", "attempt": 1}, **kwargs
    )


def _hitl_request(seq: int, run: int) -> dict:
    return {
        "room_seq": seq,
        "room_id": "room-1",
        "kind": "hitl_request",
        "ts": NOW.isoformat(),
        "payload_public": {
            "request_id": f"request-{seq}",
            "message_id": f"orchestrator:run-{run}:inv_agent",
            "source": "agent",
            "interaction_id": f"interaction-{seq}",
            "question_index": 0,
            "question_count": 1,
            "prompt": "Question",
            "prompt_type": "text",
            "run_id": f"run-{run}",
            "related_user_message_id": f"user-{run}",
            "client_request_id": f"client-{run}",
        },
    }


async def _store(records: list[dict]) -> InMemoryRoomEventStore:
    store = InMemoryRoomEventStore()
    for record in records:
        await store.append(
            conversation_id="room-1",
            kind=record["kind"],
            payload_public=record["payload_public"],
            idempotency_key=f"event-{record['room_seq']}",
            ts=NOW,
        )
    return store


async def test_a_room_with_stored_late_events_builds_with_every_later_turn():
    store = await _store(
        [
            _started(1, 1),
            _settled(2, 1),
            # A pass of run-1 that kept running past the Stop.
            _turn_start(3, 1),
            _hitl_request(4, 1),
            _started(5, 2),
            _settled(6, 2),
            # run-3 was stopped before its run_started reached the Room.
            _settled(7, 3),
            _started(8, 4),
            _turn_start(9, 4),
        ]
    )

    snapshot = await SnapshotService(store=store).snapshot("room-1")

    assert snapshot["room_seq"] == 9
    turns = {turn["run_id"]: turn for turn in snapshot["turns"]}
    assert set(turns) == {"run-1", "run-2", "run-4"}
    assert turns["run-1"]["state"] == "canceled"
    assert turns["run-1"]["internal_turns"] == []
    assert turns["run-1"]["hitl_interactions"] == []
    assert turns["run-2"]["state"] == "canceled"
    assert turns["run-4"]["state"] == "active"
    assert [t["internal_turn_id"] for t in turns["run-4"]["internal_turns"]] == [
        "turn-1"
    ]
    assert snapshot["hitl"]["requests"] == []


async def test_a_skipped_event_logs_ids_only(caplog):
    caplog.set_level(logging.INFO)
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True

    assert fold.apply(_turn_start(3, 1)) is True
    assert fold.apply(_settled(4, 9)) is True

    records = [r for r in caplog.records if r.getMessage() == SKIPPED]
    assert [(r.run_id, r.room_seq, r.event_type, r.reason) for r in records] == [
        ("run-1", 3, "turn_start", "turn_settled"),
        ("run-9", 4, "run_settled", "turn_not_started"),
    ]
    assert all(r.conversation_id == "room-1" for r in records)


def _hitl_response(seq: int, request: dict) -> dict:
    response = _hitl_request(seq, 1)
    response["kind"] = "hitl_response"
    payload = response["payload_public"]
    payload["request_id"] = request["payload_public"]["request_id"]
    payload["interaction_id"] = request["payload_public"]["interaction_id"]
    del payload["prompt"], payload["prompt_type"]
    payload["status"] = "canceled"
    return response


def test_a_late_hitl_response_closes_the_prompt_the_turn_holds():
    # The question was asked while the Run was active; its close arrives
    # after run_settled. The prompt must close, or it reappears answerable
    # on the next snapshot.
    fold = RoomEventFold()
    request = _hitl_request(2, 1)
    for record in (_started(1, 1), request, _settled(3, 1)):
        assert fold.apply(record) is True
    response = _hitl_response(4, request)

    assert fold.apply(response) is True

    state = fold.state(room_seq=4)
    (stored,) = state["hitl"]["requests"]
    assert stored["status"] == "canceled"
    assert [r["request_id"] for r in state["hitl"]["resolved"]] == [
        response["payload_public"]["request_id"]
    ]
    turn = state["turns"][0]
    assert turn["state"] == "canceled"
    (interaction,) = turn["hitl_interactions"]
    assert interaction["state"] == "canceled"
    assert interaction["requests"][0]["status"] == "canceled"


def test_a_late_hitl_response_the_turn_never_held_is_skipped(caplog):
    caplog.set_level(logging.INFO)
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True
    turn_before = fold.state(room_seq=2)["turns"][0]

    assert fold.apply(_hitl_response(3, _hitl_request(3, 1))) is True

    assert fold.state(room_seq=3)["turns"][0] == turn_before
    assert [r.reason for r in caplog.records if r.getMessage() == SKIPPED] == [
        "turn_settled"
    ]


def test_a_repeated_settlement_with_the_same_status_is_skipped():
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True

    assert fold.apply(_settled(3, 1)) is True
    assert fold.state(room_seq=3)["turns"][0]["state"] == "canceled"


# -- Still rejected -----------------------------------------------------------


def test_a_settlement_that_contradicts_the_settled_status_is_rejected():
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True

    assert fold.apply(_settled(3, 1, status="failed")) is False


def test_a_late_event_with_another_correlation_is_rejected():
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True

    assert fold.apply(_turn_start(3, 1, correlation="client-other")) is False


def test_an_invalid_late_event_is_rejected():
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True
    record = _turn_start(3, 1)
    record["payload_public"]["private"] = "forbidden"

    with pytest.raises(ValueError, match="unknown public fields"):
        fold.apply(record)


def test_an_event_other_than_settlement_before_run_started_is_rejected():
    fold = RoomEventFold()

    assert fold.apply(_turn_start(1, 1)) is False


def test_a_late_hitl_request_with_another_root_is_rejected():
    fold = RoomEventFold()
    for record in (_started(1, 1), _settled(2, 1)):
        assert fold.apply(record) is True
    record = _hitl_request(3, 1)
    record["payload_public"]["client_request_id"] = "client-other"

    assert fold.apply(record) is False
