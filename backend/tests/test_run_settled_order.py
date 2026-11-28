"""``run_settled`` is appended after every lifecycle event of its Run.

A lifecycle listener loads the Run, drops the event if the Run has ended, and
appends it. ``run_settled`` comes from the projection worker. Without a shared
lock, a listener that loaded the Run before a Stop ended it can append its
event after ``run_settled`` (a Stop before the first turn emits nothing, so it
never waits on the listener), and the Room then shows a Turn that never
settles. ``_RunEventOrder`` takes the Run's lock for both appends.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest

import container
from delivery.snapshot import RoomEventFold
from tests.test_canonical_delivery_snapshot import NOW, _run_event

STARTED = {
    "hybro_turn_id": "run-1",
    "user_message_id": "user-1",
    "started_at": NOW.isoformat(),
    "mode": "fast",
}
SETTLED = {
    "status": "canceled",
    "started_at": NOW.isoformat(),
    "settled_at": (NOW + timedelta(seconds=1)).isoformat(),
    "duration_ms": 1000,
    "cancellation_code": "user_requested",
}


class Room:
    """The Run's status and the Room log, as the listener and worker see them."""

    def __init__(self) -> None:
        self.run_status = "running"
        self.log: list[dict] = []

    def append(self, kind: str, payload: dict) -> None:
        self.log.append(_run_event(len(self.log) + 1, kind, payload))

    def folds(self) -> bool:
        # The Room is right when the snapshot build accepts every record and
        # every Turn ends settled. The fold skips a run_settled that comes
        # before its run_started, so that order leaves a Turn active forever.
        fold = RoomEventFold()
        try:
            if not all(fold.apply(record) for record in self.log):
                return False
        except ValueError:
            return False
        return all(
            turn["state"] in {"completed", "failed", "canceled"}
            for turn in fold.state(room_seq=len(self.log))["turns"]
        )


def _listener_append(room: Room, loaded: asyncio.Event, release: asyncio.Event):
    async def append() -> None:
        # The listener loads the Run before the Stop ends it.
        if room.run_status in {"completed", "failed", "canceled"}:
            return
        loaded.set()
        await release.wait()
        room.append("run_started", STARTED)

    return append


async def _settle(room: Room) -> None:
    room.append("run_settled", SETTLED)


async def _nothing() -> None:
    return None


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_without_the_lock_run_settled_lands_first_and_breaks_the_room():
    # An illustration of the race the lock closes, on the Room model alone:
    # the old code appended run_settled without the listener's lock.
    room = Room()
    loaded, release = asyncio.Event(), asyncio.Event()
    listener = asyncio.create_task(_listener_append(room, loaded, release)())
    await loaded.wait()
    room.run_status = "canceled"

    await _settle(room)
    release.set()
    await listener

    assert [r["payload_public"]["type"] for r in room.log] == [
        "run_settled",
        "run_started",
    ]
    assert room.folds() is False
    # The fold takes both records and leaves the Turn active for good.
    fold = RoomEventFold()
    assert all(fold.apply(record) for record in room.log)
    (turn,) = fold.state(room_seq=len(room.log))["turns"]
    assert turn["state"] == "active"


async def test_run_settled_waits_for_a_listener_that_loaded_the_run_before_the_end():
    order = container._RunEventOrder()
    room = Room()
    loaded, release = asyncio.Event(), asyncio.Event()
    listener = asyncio.create_task(
        order.lifecycle(
            "run-1", _listener_append(room, loaded, release), after=_nothing
        )
    )
    await loaded.wait()
    # A Stop ends the Run, and settlement publishes run_settled.
    room.run_status = "canceled"
    settlement = asyncio.create_task(order.settlement("run-1", lambda: _settle(room)))
    await _drain()
    assert room.log == []

    release.set()
    await asyncio.gather(listener, settlement)

    assert [r["payload_public"]["type"] for r in room.log] == [
        "run_started",
        "run_settled",
    ]
    assert room.folds() is True


async def test_a_listener_that_takes_the_lock_after_settlement_drops_its_event():
    order = container._RunEventOrder()
    room = Room()
    room.append("run_started", STARTED)
    room.run_status = "canceled"
    await order.settlement("run-1", lambda: _settle(room))
    loaded, release = asyncio.Event(), asyncio.Event()
    release.set()

    # A late event of a pass that kept running past the Stop.
    await order.lifecycle(
        "run-1", _listener_append(room, loaded, release), after=_nothing
    )

    assert not loaded.is_set()
    assert [r["payload_public"]["type"] for r in room.log] == [
        "run_started",
        "run_settled",
    ]
    assert room.folds() is True


async def test_work_after_a_lifecycle_append_may_settle_the_same_run():
    # The final-answer nudge runs the projection worker, which may publish
    # this Run's run_settled. It runs after the lock is released, so it does
    # not wait on itself.
    order = container._RunEventOrder()
    room = Room()

    async def append() -> None:
        room.append("run_started", STARTED)

    async def nudge() -> None:
        room.run_status = "canceled"
        await order.settlement("run-1", lambda: _settle(room))

    await asyncio.wait_for(order.lifecycle("run-1", append, after=nudge), 1)

    assert [r["payload_public"]["type"] for r in room.log] == [
        "run_started",
        "run_settled",
    ]


async def test_other_runs_do_not_wait():
    order = container._RunEventOrder()
    release = asyncio.Event()

    async def held() -> None:
        await release.wait()

    listener = asyncio.create_task(order.lifecycle("run-1", held, after=_nothing))
    await _drain()

    await asyncio.wait_for(order.settlement("run-2", _nothing), 1)

    release.set()
    await listener


@pytest.mark.parametrize(
    "pattern",
    [
        # The listener and run_settled share the order, and each runs its
        # whole append under it.
        "await _run_event_order.lifecycle(",
        "lambda: _orchestrator_session_event(event),",
        "await _run_event_order.settlement(",
        "run.run_id, lambda: _publish_run_settled(run)",
        # The nudge runs after the lifecycle append, outside the lock.
        "after=lambda: _after_orchestrator_session_event(event)",
    ],
)
def test_the_lifespan_orders_both_appends(pattern):
    source = inspect.getsource(container._runtime_lifespan)
    assert source.count(pattern) == 1


def _closure(source: str, name: str) -> str:
    start = source.index(f"async def {name}(")
    end = source.index("\n        async def ", start + 1)
    return source[start:end]


def test_the_listener_loads_the_run_inside_the_locked_append():
    source = inspect.getsource(container._runtime_lifespan)
    listener = _closure(source, "_orchestrator_session_listener")
    append = _closure(source, "_orchestrator_session_event")
    assert "run_store.load" not in listener
    assert "_run_event_order.lifecycle(" in listener
    assert append.count("runtime.run_store.load(event.run_id)") == 1
    assert "_drop_event_for_ended_run(" in append


def test_settlement_holds_the_lock_across_the_whole_run_settled_append():
    source = inspect.getsource(container._runtime_lifespan)
    publish = _closure(source, "publish_orchestrator_projection_status")
    settled = _closure(source, "_publish_run_settled")
    # The entry point does nothing but take the lock around the append.
    assert "emit_checked_identified" not in publish
    assert "_read_canonical_run_events" not in publish
    for step in (
        "_read_canonical_run_events(",
        'run_event_type="run_settled"',
        "_emit_canonical_processing_adapter(",
    ):
        assert step in settled


def test_the_lifecycle_append_does_not_run_the_projection_worker():
    source = inspect.getsource(container._runtime_lifespan)
    start = source.index("async def _orchestrator_session_event(")
    end = source.index("async def _after_orchestrator_session_event(")
    assert "projection_worker" not in source[start:end]
    # The only direct call is the nudge, after the append.
    assert source.count("projection_worker.run_once(") == 1
    nudge = source.index("projection_worker.run_once(")
    assert nudge > source.index("async def _after_orchestrator_session_event(")
