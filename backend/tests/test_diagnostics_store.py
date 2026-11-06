"""StoreSink: bounded queues and the flush loop of the diagnostics store."""

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from bson.errors import InvalidDocument
from pymongo.errors import BulkWriteError, DocumentTooLarge

from common.observability.logging import StructuredFormatter
from common.observability.sinks import LogEntry, RingBufferSink, SinkHandler
from diagnostics.repository.mongo import (
    EVENTS_COLLECTION,
    LOGS_COLLECTION,
    MAX_FIELDS_JSON_BYTES,
)
from diagnostics.store_sink import StoreSink

_sequence = iter(range(1, 10**9))


def _entry(
    *,
    level_no: int = logging.INFO,
    system: bool = True,
    logger: str = "jobs.compaction_sweep",
    event: str = "job_run_completed",
    fields: dict | None = None,
) -> LogEntry:
    return LogEntry(
        sequence=next(_sequence),
        timestamp=datetime.now(UTC),
        level=logging.getLevelName(level_no).lower(),
        level_no=level_no,
        service="hybro-backend",
        event=event,
        logger=logger,
        system=system,
        fields=MappingProxyType(
            fields
            if fields is not None
            else {"instance_id": "ab12cd34", "version": "0.4.4", "job": "j"}
        ),
    )


class _Repository:
    def __init__(self) -> None:
        self.inserted: dict[str, list[dict]] = {
            EVENTS_COLLECTION: [],
            LOGS_COLLECTION: [],
        }
        self.calls = 0
        self.fail = False
        self.block: asyncio.Event | None = None

    async def insert(self, collection: str, documents: list[dict]) -> None:
        self.calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise ConnectionError("mongo down")
        self.inserted[collection].extend(documents)

    def ids(self, collection: str) -> list:
        return [doc["_id"] for doc in self.inserted[collection]]


def _sink(repository: _Repository, **kwargs) -> StoreSink:
    kwargs.setdefault("flush_seconds", 0.01)
    return StoreSink(repository, **kwargs)


async def _flush(sink: StoreSink) -> bool:
    return await sink.flush_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level_no", "system", "events", "logs"),
    [
        (logging.INFO, True, 1, 0),
        (logging.WARNING, False, 0, 1),
        (logging.WARNING, True, 1, 1),
        (logging.INFO, False, 0, 0),
        (logging.DEBUG, True, 0, 0),
        (logging.ERROR, False, 0, 1),
    ],
)
async def test_entries_route_to_their_collections(level_no, system, events, logs):
    repository = _Repository()
    sink = _sink(repository)

    sink.accept(_entry(level_no=level_no, system=system))
    await _flush(sink)

    assert len(repository.inserted[EVENTS_COLLECTION]) == events
    assert len(repository.inserted[LOGS_COLLECTION]) == logs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "logger",
    ["pymongo.topology", "motor", "diagnostics.repository.mongo", "diagnostics"],
)
async def test_records_from_the_store_path_are_never_queued(logger):
    repository = _Repository()
    sink = _sink(repository)

    sink.accept(_entry(level_no=logging.ERROR, logger=logger))
    await _flush(sink)

    assert sink.stats()["queued"] == 0
    assert repository.calls == 0


@pytest.mark.asyncio
async def test_document_shape_and_fields_json():
    repository = _Repository()
    sink = _sink(repository)
    entry = _entry(
        level_no=logging.WARNING,
        fields={
            "instance_id": "ab12cd34",
            "version": "0.4.4",
            "trace_id": "t-1",
            "request_id": "r-1",
            "error.type": "X",
            "big": 2**70,
        },
    )

    sink.accept(entry)
    await _flush(sink)

    [doc] = repository.inserted[EVENTS_COLLECTION]
    assert doc["ts"] == entry.timestamp
    assert doc["level"] == "warning"
    assert doc["level_no"] == logging.WARNING
    assert doc["service"] == "hybro-backend"
    assert doc["event"] == "job_run_completed"
    assert doc["logger"] == "jobs.compaction_sweep"
    assert doc["system"] is True
    assert doc["instance_id"] == "ab12cd34"
    assert doc["version"] == "0.4.4"
    assert doc["trace_id"] == "t-1"
    assert doc["request_id"] == "r-1"
    assert json.loads(doc["fields_json"]) == {"error.type": "X", "big": 2**70}


@pytest.mark.asyncio
async def test_oversized_fields_become_a_truncation_marker():
    repository = _Repository()
    sink = _sink(repository)

    sink.accept(_entry(fields={"blob": "x" * (MAX_FIELDS_JSON_BYTES + 10)}))
    await _flush(sink)

    [doc] = repository.inserted[EVENTS_COLLECTION]
    marker = json.loads(doc["fields_json"])
    assert marker["truncated"] is True
    assert marker["size"] > MAX_FIELDS_JSON_BYTES


@pytest.mark.asyncio
async def test_failed_write_keeps_documents_and_logs_nothing(caplog):
    repository = _Repository()
    repository.fail = True
    sink = _sink(repository)
    sink.accept(_entry())
    sink.accept(_entry())

    with caplog.at_level(logging.DEBUG):
        assert await _flush(sink) is False

    assert caplog.records == []
    stats = sink.stats()
    assert stats["queued"] == 2
    assert stats["write_failures"] == 1
    assert stats["last_error_type"] == "ConnectionError"
    queued_ids = sink.queued_ids(EVENTS_COLLECTION)

    repository.fail = False
    assert await _flush(sink) is True

    assert repository.ids(EVENTS_COLLECTION) == queued_ids
    assert sink.stats()["queued"] == 0
    assert sink.stats()["written"] == 2


@pytest.mark.asyncio
async def test_slow_insert_is_retried_with_the_same_ids():
    repository = _Repository()
    repository.block = asyncio.Event()
    sink = _sink(repository, write_timeout=0.02)
    sink.accept(_entry())
    queued_ids = sink.queued_ids(EVENTS_COLLECTION)

    assert await _flush(sink) is False
    assert sink.stats()["last_error_type"] == "TimeoutError"

    repository.block = None
    assert await _flush(sink) is True
    assert repository.ids(EVENTS_COLLECTION) == queued_ids


@pytest.mark.asyncio
async def test_queue_overflow_drops_the_oldest():
    repository = _Repository()
    sink = _sink(repository, queue_max=3)
    entries = [_entry(event=f"e{n}") for n in range(5)]
    for entry in entries:
        sink.accept(entry)

    assert sink.stats()["dropped"] == 2
    await _flush(sink)
    assert [d["event"] for d in repository.inserted[EVENTS_COLLECTION]] == [
        "e2",
        "e3",
        "e4",
    ]


@pytest.mark.asyncio
async def test_batches_are_bounded():
    repository = _Repository()
    sink = _sink(repository, batch_max=2)
    for _ in range(5):
        sink.accept(_entry())

    await _flush(sink)

    assert len(repository.inserted[EVENTS_COLLECTION]) == 2
    assert sink.stats()["queued"] == 3


def _handler() -> SinkHandler:
    return SinkHandler(StructuredFormatter(output_format="json", environment="test"))


@pytest.mark.asyncio
async def test_start_backfills_the_ring_once_and_stop_flushes():
    repository = _Repository()
    ring = RingBufferSink()
    ring.accept(_entry(event="before_ready"))
    ring.accept(_entry(event="debug_noise", level_no=logging.DEBUG))
    handler = _handler()
    sink = _sink(repository, flush_seconds=60)

    sink.start(handler, ring)
    try:
        sink.accept(_entry(event="after_ready"))
        assert sink.stats()["ready"] is True
    finally:
        await sink.stop()

    assert [d["event"] for d in repository.inserted[EVENTS_COLLECTION]] == [
        "before_ready",
        "after_ready",
    ]
    assert sink.accept not in handler._sinks


@pytest.mark.asyncio
async def test_flush_loop_writes_periodically():
    repository = _Repository()
    sink = _sink(repository)
    sink.start(_handler(), RingBufferSink())
    try:
        sink.accept(_entry())
        for _ in range(100):
            if repository.inserted[EVENTS_COLLECTION]:
                break
            await asyncio.sleep(0.01)
    finally:
        await sink.stop()

    assert len(repository.inserted[EVENTS_COLLECTION]) == 1


@pytest.mark.asyncio
async def test_stop_during_a_blocked_insert_writes_the_batch_in_the_final_flush():
    repository = _Repository()
    repository.block = asyncio.Event()
    sink = _sink(repository, write_timeout=30)
    sink.start(_handler(), RingBufferSink())
    sink.accept(_entry())
    queued_ids = sink.queued_ids(EVENTS_COLLECTION)
    for _ in range(100):
        if repository.calls:
            break
        await asyncio.sleep(0.01)
    assert repository.calls == 1

    repository.block = None
    await sink.stop()

    assert repository.ids(EVENTS_COLLECTION) == queued_ids


@pytest.mark.asyncio
async def test_accept_from_a_thread_while_flushing_loses_nothing():
    repository = _Repository()
    sink = _sink(repository, batch_max=7)
    total = 500

    def produce() -> None:
        for _ in range(total):
            sink.accept(_entry())

    thread = threading.Thread(target=produce)
    thread.start()
    while thread.is_alive():
        await _flush(sink)
        await asyncio.sleep(0)
    thread.join()
    while sink.stats()["queued"]:
        await _flush(sink)

    assert len(repository.inserted[EVENTS_COLLECTION]) == total
    assert sink.stats()["dropped"] == 0


@pytest.mark.asyncio
async def test_backoff_doubles_to_the_cap_and_resets_after_a_success():
    repository = _Repository()
    repository.fail = True
    sink = _sink(repository, flush_seconds=0.5)
    sink.accept(_entry())

    assert sink._delay() == 0.5
    delays = []
    for _ in range(6):
        await _flush(sink)
        delays.append(sink._delay())
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    repository.fail = False
    assert await _flush(sink) is True
    assert sink._delay() == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [InvalidDocument("cannot encode"), DocumentTooLarge("too large")],
)
async def test_unencodable_batch_is_dropped_not_retried(error):
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry())
    sink.accept(_entry())

    async def reject(collection, documents):
        raise error

    repository.insert = reject
    assert await _flush(sink) is True

    stats = sink.stats()
    assert stats["queued"] == 0
    assert stats["dropped"] == 2
    assert stats["last_error_type"] == type(error).__name__
    assert sink._delay() == sink._flush_seconds


@pytest.mark.asyncio
async def test_rejected_documents_are_dropped_and_the_rest_count_as_written():
    repository = _Repository()
    sink = _sink(repository)
    for _ in range(3):
        sink.accept(_entry())

    async def reject(collection, documents):
        raise BulkWriteError(
            {"writeErrors": [{"index": 1, "code": 121}], "writeConcernErrors": []}
        )

    repository.insert = reject
    assert await _flush(sink) is True

    stats = sink.stats()
    assert stats["queued"] == 0
    assert stats["dropped"] == 1
    assert stats["written"] == 2


@pytest.mark.asyncio
async def test_write_concern_failure_is_retried():
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry())

    async def reject(collection, documents):
        raise BulkWriteError({"writeErrors": [], "writeConcernErrors": [{"code": 64}]})

    repository.insert = reject
    assert await _flush(sink) is False
    assert sink.stats()["queued"] == 1
    assert sink.stats()["dropped"] == 0


def _record(message: str) -> logging.LogRecord:
    record = logging.LogRecord(
        "jobs.x", logging.WARNING, __file__, 1, message, None, None
    )
    return record


@pytest.mark.asyncio
async def test_record_logged_during_start_is_neither_lost_nor_ahead_of_the_backlog():
    repository = _Repository()
    handler = _handler()
    ring = RingBufferSink()
    ring.accept(_entry(event="before_ready", level_no=logging.WARNING, system=False))
    snapshot = ring.entries
    racer: list[threading.Thread] = []

    def entries():
        thread = threading.Thread(target=handler.handle, args=(_record("racing"),))
        racer.append(thread)
        thread.start()
        thread.join(timeout=0.2)
        return snapshot()

    ring.entries = entries
    sink = _sink(repository, flush_seconds=60)
    sink.start(handler, ring)
    racer[0].join(timeout=5)
    try:
        assert repository.inserted[LOGS_COLLECTION] == []
    finally:
        await sink.stop()

    assert [d["logger"] for d in repository.inserted[LOGS_COLLECTION]] == [
        "jobs.compaction_sweep",
        "jobs.x",
    ]


@pytest.mark.asyncio
async def test_lifted_fields_are_stored_as_strings():
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry(fields={"trace_id": 2**70, "request_id": None}))
    await _flush(sink)

    [doc] = repository.inserted[EVENTS_COLLECTION]
    assert doc["trace_id"] == str(2**70)
    assert doc["request_id"] is None


@pytest.mark.asyncio
async def test_a_failing_collection_does_not_block_the_other():
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry(level_no=logging.WARNING, system=False))
    sink.accept(_entry())
    original = repository.insert

    async def events_down(collection, documents):
        if collection == EVENTS_COLLECTION:
            raise ConnectionError("events down")
        await original(collection, documents)

    repository.insert = events_down
    assert await _flush(sink) is False

    assert len(repository.inserted[LOGS_COLLECTION]) == 1
    assert sink.queued_ids(EVENTS_COLLECTION) != []


@pytest.mark.asyncio
async def test_duplicates_in_a_mixed_bulk_error_count_as_written():
    repository = _Repository()
    sink = _sink(repository)
    for _ in range(3):
        sink.accept(_entry())

    async def reject(collection, documents):
        raise BulkWriteError(
            {
                "writeErrors": [{"index": 0, "code": 11000}, {"index": 1, "code": 121}],
                "writeConcernErrors": [],
            }
        )

    repository.insert = reject
    await _flush(sink)

    assert sink.stats()["dropped"] == 1
    assert sink.stats()["written"] == 2


@pytest.mark.asyncio
async def test_a_rejected_batch_does_not_end_the_shutdown_drain():
    repository = _Repository()
    sink = _sink(repository, batch_max=1, flush_seconds=60)
    sink.start(_handler(), RingBufferSink())
    sink.accept(_entry(event="bad"))
    sink.accept(_entry(event="good"))
    original = repository.insert

    async def reject_bad(collection, documents):
        if documents[0]["event"] == "bad":
            raise InvalidDocument("cannot encode")
        await original(collection, documents)

    repository.insert = reject_bad
    await sink.stop()

    assert [d["event"] for d in repository.inserted[EVENTS_COLLECTION]] == ["good"]
    assert sink.stats()["dropped"] == 1


def _bulk_error(*codes: int) -> BulkWriteError:
    return BulkWriteError(
        {
            "writeErrors": [{"index": n, "code": c} for n, c in enumerate(codes)],
            "writeConcernErrors": [],
        }
    )


@pytest.mark.asyncio
async def test_transient_write_errors_requeue_the_batch():
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry())
    sink.accept(_entry())

    async def reject(collection, documents):
        raise _bulk_error(11602, 11000)

    repository.insert = reject
    assert await _flush(sink) is False
    assert sink.stats()["queued"] == 2
    assert sink.stats()["dropped"] == 0


@pytest.mark.asyncio
async def test_unencodable_logger_name_drops_the_batch():
    repository = _Repository()
    sink = _sink(repository)
    sink.accept(_entry())

    async def reject(collection, documents):
        raise UnicodeEncodeError("utf-8", "a\udcff", 1, 2, "surrogates not allowed")

    repository.insert = reject
    assert await _flush(sink) is True
    assert sink.stats()["dropped"] == 1


@pytest.mark.asyncio
async def test_backoff_counts_one_failure_per_tick_when_both_collections_fail():
    repository = _Repository()
    repository.fail = True
    sink = _sink(repository)
    sink.accept(_entry(level_no=logging.WARNING))

    await _flush(sink)
    assert sink._delay() == 2.0
    await _flush(sink)
    assert sink._delay() == 4.0


@pytest.mark.asyncio
async def test_backoff_grows_while_one_collection_keeps_failing():
    repository = _Repository()
    sink = _sink(repository)
    original = repository.insert

    async def events_down(collection, documents):
        if collection == EVENTS_COLLECTION:
            raise ConnectionError("events down")
        await original(collection, documents)

    repository.insert = events_down
    sink.accept(_entry())
    delays = []
    for _ in range(3):
        sink.accept(_entry(level_no=logging.WARNING, system=False))
        await _flush(sink)
        delays.append(sink._delay())
    assert delays == [2.0, 4.0, 8.0]
