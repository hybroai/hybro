"""Room event log: sequencing, idempotency, range reads, hole healing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tests.fakes.room_events import InMemoryRoomEventStore

NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


def _payload(marker: str) -> dict:
    return {"marker": marker}


@pytest.mark.asyncio
async def test_append_allocates_contiguous_per_room_sequences():
    store = InMemoryRoomEventStore()
    first = await store.append(
        conversation_id="room-1",
        kind="task_update",
        payload_public=_payload("a"),
        event_id="evt-a",
    )
    second = await store.append(
        conversation_id="room-1",
        kind="task_update",
        payload_public=_payload("b"),
        event_id="evt-b",
    )
    other_room = await store.append(
        conversation_id="room-2",
        kind="task_update",
        payload_public=_payload("c"),
        event_id="evt-c",
    )

    assert (first.room_seq, first.room_event_id) == (1, "evt-a")
    assert (second.room_seq, second.room_event_id) == (2, "evt-b")
    assert (other_room.room_seq, other_room.room_event_id) == (1, "evt-c")
    assert await store.latest_seq("room-1") == 2
    assert await store.latest_seq("room-2") == 1


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_reuses_persisted_sequence():
    store = InMemoryRoomEventStore()
    first = await store.append(
        conversation_id="room-1",
        kind="processing_status",
        payload_public=_payload("a"),
        event_id="logical-1",
        idempotency_key="terminal:room-1:msg-1:completed",
    )
    # A delivery retry re-appends with the same deterministic key.
    retry = await store.append(
        conversation_id="room-1",
        kind="processing_status",
        payload_public=_payload("a"),
        event_id="logical-1",
        idempotency_key="terminal:room-1:msg-1:completed",
    )

    assert first == retry
    assert await store.latest_seq("room-1") == 1
    records = await store.read_range("room-1")
    assert len(records) == 1


@pytest.mark.asyncio
async def test_read_range_orders_by_seq_and_respects_after_and_limit():
    store = InMemoryRoomEventStore()
    for marker in ("a", "b", "c", "d"):
        await store.append(
            conversation_id="room-1",
            kind="run_event",
            payload_public=_payload(marker),
            event_id=f"evt-{marker}",
        )

    records = await store.read_range("room-1", after=1, limit=2)
    assert [record["room_seq"] for record in records] == [2, 3]
    assert [record["event_id"] for record in records] == ["evt-b", "evt-c"]
    records = await store.read_range("room-1")
    assert [record["room_seq"] for record in records] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_append_records_parent_and_run_links():
    store = InMemoryRoomEventStore()
    appended = await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_payload("decision"),
        event_id="public:run-1:orchestrator_decision:1",
        run_id="run-1",
    )
    child = await store.append(
        conversation_id="room-1",
        kind="task_submitted",
        payload_public=_payload("dispatch"),
        event_id="task-1",
        parent_event_id=appended.room_event_id,
        run_id="run-1",
    )

    records = await store.read_range("room-1")
    assert records[1]["parent_event_id"] == appended.room_event_id
    assert records[0]["run_id"] == "run-1"
    assert child.room_seq == 2


@pytest.mark.asyncio
async def test_skipped_tombstones_are_hidden_from_replay_but_foldable():
    store = InMemoryRoomEventStore()
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_payload("a"),
        event_id="evt-a",
    )
    await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_payload("b"),
        event_id="evt-b",
    )
    await store.append(
        conversation_id="room-1",
        kind="skipped",
        payload_public={},
        event_id=None,
        idempotency_key="skip:room-1:2",
    )

    replayed = await store.read_range("room-1", include_skipped=False)
    assert [record["room_seq"] for record in replayed] == [1, 2]

    folded = await store.read_range("room-1", include_skipped=True)
    assert [record["room_seq"] for record in folded] == [1, 2, 3]
    assert folded[2]["kind"] == "skipped"


class _FakeMongoDAL:
    """Faithful stand-in for MongoDALImpl: ``start_session`` returns an async
    context manager OBJECT (not an awaitable), mirroring the real adapter's
    ``asynccontextmanager`` contract. Guards against ``await`` misuse in the
    transactional append path.
    """

    def __init__(self) -> None:
        self.collections: dict[str, _FakeSeqCollection | _FakeEventsCollection] = {}
        self.counter = 0

    def collection(self, name: str):
        if name not in self.collections:
            if name == "room_event_seq":
                self.collections[name] = _FakeCollection(_FakeSeqCollection(self))
            else:
                self.collections[name] = _FakeCollection(_FakeEventsCollection())
        return self.collections[name]

    def start_session(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def session_context():
            @asynccontextmanager
            async def start_transaction():
                yield None

            yield _FakeSession(start_transaction=start_transaction)

        return session_context()


class _FakeSession:
    """Mirrors the Motor session surface used by the transactional append:
    ``start_transaction()`` returns an async context manager."""

    def __init__(self, *, start_transaction) -> None:
        self.start_transaction = start_transaction


class _FakeCollection:
    """Mirrors MongoCollectionAdapter: exposes ``raw_collection`` and the
    awaitable collection operations used by MongoRoomEventStore."""

    def __init__(self, impl) -> None:
        self.raw_collection = impl
        self._impl = impl
        self.created_indexes: list[tuple[list[tuple[str, int]], dict]] = []

    async def create_index(self, keys, **kwargs):
        self.created_indexes.append((keys, kwargs))
        return kwargs.get("name", "index")

    async def find_one(self, query, **kwargs):
        return await self._impl.find_one(query, **kwargs)

    async def find(self, query, **kwargs):
        return await self._impl.find(query, **kwargs)


class _FakeSeqCollection:
    def __init__(self, dal: _FakeMongoDAL) -> None:
        self._dal = dal
        self._counters: dict[str, int] = {}

    async def find_one(self, query):
        conversation_id = query.get("_id")
        return {
            "seq": self._counters.get(conversation_id, 0),
            "healed_through": getattr(self, "_healed", {}).get(conversation_id, 0),
        }

    async def find_one_and_update(self, filter_, update, **kwargs):
        conversation_id = filter_["_id"]
        current = self._counters.get(conversation_id, 0) + update.get("$inc", {}).get(
            "seq", 0
        )
        self._counters[conversation_id] = current
        if "$max" in update:
            if not hasattr(self, "_healed"):
                self._healed = {}
            self._healed[conversation_id] = max(
                self._healed.get(conversation_id, 0),
                update["$max"].get("healed_through", 0),
            )
        return {
            "seq": current,
            "healed_through": getattr(self, "_healed", {}).get(conversation_id, 0),
        }


class _FakeEventsCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.find_queries: list[dict] = []

    async def find(self, query, **kwargs):  # noqa: C901
        self.find_queries.append(query)
        limit = kwargs.get("limit")

        def matches(doc: dict) -> bool:
            for key, expected in query.items():
                value = doc.get(key)
                if isinstance(expected, dict):
                    for op, operand in expected.items():
                        if op == "$gt" and not (value is not None and value > operand):
                            return False
                        if op == "$gte" and not (
                            value is not None and value >= operand
                        ):
                            return False
                        if op == "$lte" and not (
                            value is not None and value <= operand
                        ):
                            return False
                        if op == "$ne" and value == operand:
                            return False
                elif value != expected:
                    return False
            return True

        results = [d for d in self.docs if matches(d)]
        sort = kwargs.get("sort")
        if sort:
            for key, direction in reversed(sort):
                results.sort(key=lambda item: item.get(key), reverse=direction < 0)
        if limit is not None:
            results = results[:limit]
        return results

    async def insert_one(self, doc, **kwargs):
        from pymongo.errors import DuplicateKeyError

        if any(
            existing.get("_id") == doc.get("_id")
            or (
                existing.get("room_id") == doc.get("room_id")
                and existing.get("room_seq") == doc.get("room_seq")
            )
            for existing in self.docs
        ):
            raise DuplicateKeyError("duplicate")
        self.docs.append(doc)
        return object()

    async def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None


class _DelayedEventsCollection(_FakeEventsCollection):
    def __init__(self) -> None:
        super().__init__()
        self.slow_insert_entered = asyncio.Event()
        self.release_slow_insert = asyncio.Event()

    async def insert_one(self, doc, **kwargs):
        if doc.get("_id") == "slow-event" and doc.get("room_seq") == 1:
            self.slow_insert_entered.set()
            await self.release_slow_insert.wait()
        return await super().insert_one(doc, **kwargs)


@pytest.mark.asyncio
async def test_mongo_store_transactional_append_with_real_session_contract():
    """The transactional append must consume the async context manager from
    ``MongoDALImpl.start_session`` directly (no ``await``), exactly as the
    real adapter returns it. Regression: ``async with await ...`` crashed
    every production append with TypeError and silently dead-lettered every
    emit, leaving room_events empty (found via docker E2E)."""
    from delivery.room_events import MongoRoomEventStore

    store = MongoRoomEventStore(mongo=_FakeMongoDAL())

    first = await store.append(
        conversation_id="room-1",
        kind="processing_status",
        payload_public=_payload("a"),
        event_id="evt-a",
    )
    second = await store.append(
        conversation_id="room-1",
        kind="run_event",
        payload_public=_payload("b"),
        event_id="evt-b",
    )

    assert (first.room_seq, second.room_seq) == (1, 2)
    assert await store.latest_seq("room-1") == 2
    records = await store.read_range("room-1")
    assert [record["room_seq"] for record in records] == [1, 2]
    assert records[0]["kind"] == "processing_status"


@pytest.mark.asyncio
async def test_mongo_fallback_idempotent_retry_does_not_burn_sequence():
    from delivery.room_events import MongoRoomEventStore

    mongo = _FakeMongoDAL()
    mongo.start_session = None
    store = MongoRoomEventStore(mongo=mongo, skip_grace=5)

    first = await store.append(
        conversation_id="room-retry",
        kind="processing_status",
        payload_public={"message_id": "message-1", "status": "processing"},
        idempotency_key="same-event",
    )
    replay = await store.append(
        conversation_id="room-retry",
        kind="processing_status",
        payload_public={"message_id": "message-1", "status": "processing"},
        idempotency_key="same-event",
    )

    assert replay == first
    assert await store.latest_seq("room-retry") == 1
    assert [
        item["room_seq"]
        for item in await store.read_range("room-retry", after=0, include_skipped=True)
    ] == [1]


@pytest.mark.asyncio
async def test_mongo_fallback_reallocates_after_slow_writer_is_tombstoned():
    from delivery.room_events import MongoRoomEventStore

    mongo = _FakeMongoDAL()
    mongo.start_session = None
    delayed = _DelayedEventsCollection()
    mongo.collections["room_events"] = _FakeCollection(delayed)
    store = MongoRoomEventStore(mongo=mongo, skip_grace=1)

    slow_task = asyncio.create_task(
        store.append(
            conversation_id="room-race",
            kind="agent_response",
            payload_public={"message_id": "slow"},
            idempotency_key="slow-event",
        )
    )
    await delayed.slow_insert_entered.wait()

    fast = await store.append(
        conversation_id="room-race",
        kind="agent_response",
        payload_public={"message_id": "fast"},
        idempotency_key="fast-event",
    )
    assert fast.room_seq == 2
    assert any(
        row.get("kind") == "skipped" and row.get("room_seq") == 1
        for row in delayed.docs
    )

    delayed.release_slow_insert.set()
    slow = await slow_task
    assert slow.persisted is True
    assert slow.room_seq == 3

    rows = await store.read_range("room-race", include_skipped=True)
    assert [(row["room_seq"], row["kind"]) for row in rows] == [
        (1, "skipped"),
        (2, "agent_response"),
        (3, "agent_response"),
    ]
    assert [row["room_event_id"] for row in rows if row["kind"] != "skipped"] == [
        "fast-event",
        "slow-event",
    ]


@pytest.mark.asyncio
async def test_mongo_fallback_heals_genuine_burned_counter_for_forced_snapshot():
    from delivery.room_events import MongoRoomEventStore
    from delivery.snapshot import SnapshotService

    mongo = _FakeMongoDAL()
    # Development fallback: no transaction-capable session surface.
    mongo.start_session = None
    store = MongoRoomEventStore(mongo=mongo, skip_grace=2)

    first = await store.append(
        conversation_id="room-gap",
        kind="agent_response",
        payload_public={"message_id": "m1", "content": "one"},
        event_id="m1-final",
    )
    assert first.room_seq == 1

    # Burn allocation 2 exactly as a crash between counter increment and insert.
    await store._seq.raw_collection.find_one_and_update(
        {"_id": "room-gap"}, {"$inc": {"seq": 1}}, upsert=True
    )
    for index in range(3, 6):
        appended = await store.append(
            conversation_id="room-gap",
            kind="agent_response",
            payload_public={"message_id": f"m{index}", "content": str(index)},
            event_id=f"m{index}-final",
        )
        assert appended.room_seq == index

    folded = await store.read_range("room-gap", include_skipped=True)
    assert [(row["room_seq"], row["kind"]) for row in folded] == [
        (1, "agent_response"),
        (2, "skipped"),
        (3, "agent_response"),
        (4, "agent_response"),
        (5, "agent_response"),
    ]
    snapshot = await SnapshotService(store=store).snapshot("room-gap", force=True)
    assert snapshot["room_seq"] == 5
    assert [row["message_id"] for row in snapshot["messages"]] == [
        "m1",
        "m3",
        "m4",
        "m5",
    ]

    # Healing persists a contiguous cursor: later appends scan only the newly
    # confirmed range rather than repeatedly querying the full room prefix.
    events = mongo.collections["room_events"].raw_collection
    heal_queries = [
        query
        for query in events.find_queries
        if isinstance(query.get("room_seq"), dict) and "$lte" in query["room_seq"]
    ]
    assert [query["room_seq"].get("$gte") for query in heal_queries] == [1, 2, 3]


@pytest.mark.asyncio
async def test_room_event_startup_fails_closed_when_unique_index_creation_fails(
    monkeypatch,
):
    from pymongo.errors import DuplicateKeyError

    from container import _create_ready_room_event_store
    from delivery.room_events import MongoRoomEventStore

    async def fail_indexes(_self):
        raise DuplicateKeyError("duplicate room sequence rows")

    monkeypatch.setattr(MongoRoomEventStore, "ensure_indexes", fail_indexes)

    with pytest.raises(DuplicateKeyError, match="duplicate room sequence rows"):
        await _create_ready_room_event_store(mongo=_FakeMongoDAL())


@pytest.mark.asyncio
async def test_room_sequence_index_is_unique_and_migration_safe():
    from delivery.room_events import MongoRoomEventStore

    mongo = _FakeMongoDAL()
    store = MongoRoomEventStore(mongo=mongo)
    await store.ensure_indexes()

    events = mongo.collections["room_events"]
    assert (
        [("room_id", 1), ("room_seq", 1)],
        {"name": "room_id_seq_unique", "unique": True},
    ) in events.created_indexes
