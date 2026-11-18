"""The sweep that fails accepted sends whose Run never started."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from common.dto import ProcessingStatusEvent
from delivery.config import DEFAULT_TERMINAL_PROCESSING_STATUSES
from delivery.room_events import MongoRoomEventStore
from execution.events import START_FAILED_DETAILS
from execution.repository.user_message_runs import UserMessageRunReader
from execution.unstarted_sends import UnstartedSendSweep

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _accepted(
    message_id: str,
    *,
    age: timedelta,
    conversation_id: str = "conv-1",
) -> dict[str, Any]:
    return {
        "room_event_id": f"evt-{message_id}",
        "conversation_id": conversation_id,
        "ts": (NOW - age).isoformat(),
        "payload_public": {
            "status": "processing",
            "message_id": message_id,
            "related_message_id": f"root-{message_id}",
            "client_request_id": f"cr-{message_id}",
        },
    }


class FakeSendLog:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = sorted(records, key=lambda r: (r["ts"], r["room_event_id"]))
        self.statuses: dict[tuple[str, str], set[str]] = {}
        self.pages: list[tuple[str, str] | None] = []

    async def list_accepted_sends(self, *, oldest, newest, after=None, limit=200):
        self.pages.append(after)
        page = [
            r
            for r in self.records
            if oldest.isoformat() <= r["ts"] <= newest.isoformat()
            and (after is None or (r["ts"], r["room_event_id"]) > after)
        ]
        return page[:limit]

    async def has_status(self, conversation_id, message_id, statuses) -> bool:
        logged = self.statuses.get((conversation_id, message_id), set())
        return bool(logged & set(statuses))


class FakeRuns:
    def __init__(self, with_run: set[str] | None = None) -> None:
        self.with_run = with_run or set()
        self.error: Exception | None = None

    async def has_run(self, conversation_id: str, user_message_id: str) -> bool:
        if self.error is not None and user_message_id == "msg-broken":
            raise self.error
        return user_message_id in self.with_run


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[ProcessingStatusEvent] = []

    async def emit(self, event) -> bool:
        self.events.append(event)
        return True


class PassThroughResolver:
    async def resolve_client_request_id(self, message_id, client_request_id):
        return client_request_id


def _sweep(
    log: FakeSendLog,
    runs: FakeRuns | None = None,
    *,
    pending: set[str] | None = None,
    page_size: int = 200,
) -> tuple[UnstartedSendSweep, FakePublisher]:
    publisher = FakePublisher()
    sweep = UnstartedSendSweep(
        sends=log,
        runs=runs or FakeRuns(),
        event_publisher=publisher,
        client_request_id_resolver=PassThroughResolver(),
        is_orchestration_pending=lambda message_id: message_id in (pending or set()),
        terminal_statuses=DEFAULT_TERMINAL_PROCESSING_STATUSES,
        page_size=page_size,
        now=lambda: NOW,
    )
    return sweep, publisher


@pytest.mark.asyncio
async def test_send_without_run_or_terminal_status_is_failed():
    log = FakeSendLog([_accepted("msg-1", age=timedelta(minutes=30))])
    sweep, publisher = _sweep(log)

    assert await sweep.sweep() == 1

    [event] = publisher.events
    assert event.conversation_id == "conv-1"
    assert event.message_id == "msg-1"
    assert event.status == "failed"
    assert event.related_message_id == "root-msg-1"
    assert event.client_request_id == "cr-msg-1"
    assert event.details == {"message": START_FAILED_DETAILS}


@pytest.mark.asyncio
async def test_sends_with_a_run_or_terminal_status_are_left_alone():
    log = FakeSendLog(
        [
            _accepted("msg-run", age=timedelta(minutes=30)),
            _accepted("msg-done", age=timedelta(minutes=30)),
            _accepted("msg-failed", age=timedelta(minutes=30)),
        ]
    )
    log.statuses[("conv-1", "msg-done")] = {"completed"}
    # A send already failed, by a start failure or an earlier sweep.
    log.statuses[("conv-1", "msg-failed")] = {"failed"}
    sweep, publisher = _sweep(log, FakeRuns(with_run={"msg-run"}))

    assert await sweep.sweep() == 0
    assert publisher.events == []


@pytest.mark.asyncio
async def test_window_skips_recent_and_day_old_sends():
    log = FakeSendLog(
        [
            _accepted("msg-recent", age=timedelta(minutes=5)),
            _accepted("msg-old", age=timedelta(days=1, minutes=1)),
            _accepted("msg-due", age=timedelta(hours=3)),
        ]
    )
    sweep, publisher = _sweep(log)

    assert await sweep.sweep() == 1
    assert [event.message_id for event in publisher.events] == ["msg-due"]


@pytest.mark.asyncio
async def test_send_still_orchestrating_in_this_process_is_skipped():
    log = FakeSendLog([_accepted("msg-1", age=timedelta(minutes=30))])
    sweep, publisher = _sweep(log, pending={"msg-1"})

    assert await sweep.sweep() == 0
    assert publisher.events == []


@pytest.mark.asyncio
async def test_sweep_pages_through_every_send_in_the_window():
    log = FakeSendLog(
        [_accepted(f"msg-{i}", age=timedelta(minutes=20 + i)) for i in range(5)]
    )
    sweep, publisher = _sweep(log, page_size=2)

    assert await sweep.sweep() == 5
    assert len(publisher.events) == 5
    # Pages of 2, 2 and 1; each later page starts after the last record.
    assert len(log.pages) == 3
    assert log.pages[0] is None


@pytest.mark.asyncio
async def test_failed_check_does_not_stop_the_sweep():
    log = FakeSendLog(
        [
            _accepted("msg-broken", age=timedelta(minutes=40)),
            _accepted("msg-1", age=timedelta(minutes=30)),
        ]
    )
    runs = FakeRuns()
    runs.error = RuntimeError("run store unavailable")
    sweep, publisher = _sweep(log, runs)

    assert await sweep.sweep() == 1
    assert [event.message_id for event in publisher.events] == ["msg-1"]


class _RecordingCollection:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict, dict]] = []

    async def find(self, query, **kwargs):
        self.calls.append(("find", query, kwargs))
        return self.result or []

    async def find_one(self, query, **kwargs):
        self.calls.append(("find_one", query, kwargs))
        return self.result

    async def create_index(self, keys, **kwargs):
        self.calls.append(("create_index", {"keys": keys}, kwargs))


class _RecordingMongo:
    def __init__(self, result: Any = None) -> None:
        self.collections: dict[str, _RecordingCollection] = {}
        self.result = result

    def collection(self, name: str) -> _RecordingCollection:
        return self.collections.setdefault(name, _RecordingCollection(self.result))


@pytest.mark.asyncio
async def test_store_lists_accepted_sends_by_time_after_the_cursor():
    mongo = _RecordingMongo(
        result=[
            {
                "_id": "evt-1",
                "room_id": "conv-1",
                "ts": "2026-11-18T11:00:00+00:00",
                "payload_public": {"status": "processing", "message_id": "msg-1"},
            }
        ]
    )
    store = MongoRoomEventStore(mongo=mongo)

    records = await store.list_accepted_sends(
        oldest=NOW - timedelta(days=1),
        newest=NOW,
        after=("2026-11-18T10:00:00+00:00", "evt-0"),
        limit=50,
    )

    [(_, query, kwargs)] = mongo.collections["room_events"].calls
    assert query == {
        "kind": "processing_status",
        "payload_public.status": "processing",
        "ts": {
            "$gte": (NOW - timedelta(days=1)).isoformat(),
            "$lte": NOW.isoformat(),
        },
        "$or": [
            {"ts": {"$gt": "2026-11-18T10:00:00+00:00"}},
            {"ts": "2026-11-18T10:00:00+00:00", "_id": {"$gt": "evt-0"}},
        ],
    }
    assert kwargs == {"sort": [("ts", 1), ("_id", 1)], "limit": 50}
    assert records == [
        {
            "room_event_id": "evt-1",
            "conversation_id": "conv-1",
            "ts": "2026-11-18T11:00:00+00:00",
            "payload_public": {"status": "processing", "message_id": "msg-1"},
        }
    ]


@pytest.mark.asyncio
async def test_store_checks_a_message_status_within_its_room():
    mongo = _RecordingMongo(result={"_id": "terminal"})
    store = MongoRoomEventStore(mongo=mongo)

    assert await store.has_status("conv-1", "msg-1", ["failed"]) is True
    [(_, query, _kwargs)] = mongo.collections["room_events"].calls
    assert query == {
        "room_id": "conv-1",
        "kind": "processing_status",
        "payload_public.message_id": "msg-1",
        "payload_public.status": {"$in": ["failed"]},
    }


@pytest.mark.asyncio
async def test_store_indexes_accepted_sends_by_time():
    mongo = _RecordingMongo()
    await MongoRoomEventStore(mongo=mongo).ensure_indexes()

    calls = mongo.collections["room_events"].calls
    assert (
        "create_index",
        {"keys": [("ts", 1), ("_id", 1)]},
        {
            "name": "processing_status_ts",
            "partialFilterExpression": {
                "kind": "processing_status",
                "payload_public.status": "processing",
            },
        },
    ) in calls


@pytest.mark.asyncio
async def test_run_reader_looks_up_by_room_and_user_message():
    mongo = _RecordingMongo(result=None)

    assert await UserMessageRunReader(mongo).has_run("conv-1", "msg-1") is False
    [(_, query, kwargs)] = mongo.collections["orchestrator_runs"].calls
    assert query == {"room_id": "conv-1", "request.user_message_id": "msg-1"}
    assert kwargs == {"projection": {"_id": 1}}


@pytest.mark.asyncio
async def test_checker_runs_the_sweep_even_when_hitl_reconciliation_fails():
    from jobs.stale_task_checker import StaleHITLDeps, StaleTaskChecker

    swept: list[bool] = []

    async def failing_reconcile() -> None:
        raise RuntimeError("hitl store unavailable")

    async def sweep() -> int:
        swept.append(True)
        return 0

    checker = StaleTaskChecker()
    checker.set_hitl_deps(
        StaleHITLDeps(reconcile_lifecycle=failing_reconcile, fail_unstarted_sends=sweep)
    )

    assert await checker.check_stale_tasks() is False
    assert swept == [True]
