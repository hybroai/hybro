"""StoreSink: queue log entries and flush them to the capped collections.

``accept`` runs on whichever thread emitted the record, so it only appends
to bounded lists under a lock: no asyncio object and no I/O. The flush task
writes batches on the event loop. A failed write puts its batch back with the
same ``_id`` values (a timed-out insert may have written them; duplicates
count as written). A batch the encoder rejects, or the documents the server
rejects, are dropped and counted instead, so they cannot block the
collection. Failures are
visible only in ``stats()``: the store never logs, because anything it logged
would come back to it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from bson.errors import InvalidDocument
from pymongo.errors import BulkWriteError, DocumentTooLarge

from common.observability import traced_create_task
from common.observability.sinks import LogEntry, RingBufferSink, SinkHandler
from diagnostics.repository.mongo import (
    DUPLICATE_KEY,
    EVENTS_COLLECTION,
    LOGS_COLLECTION,
    DiagnosticsMongoRepository,
    to_document,
)

# Records caused by the store itself are never queued.
_OWN_LOGGERS = ("pymongo", "motor", "diagnostics")
_MAX_BACKOFF_SECONDS = 30.0
_STOP_FLUSH_SECONDS = 2.0


def _is_own(logger_name: str) -> bool:
    return any(
        logger_name == prefix or logger_name.startswith(f"{prefix}.")
        for prefix in _OWN_LOGGERS
    )


# Encoder failures: the batch can never be sent as it is.
_UNENCODABLE = (InvalidDocument, DocumentTooLarge, OverflowError, UnicodeEncodeError)
# Server codes that reject a document for good: BadValue, DocumentValidation-
# Failure, BSONObjectTooLarge. Any other write error (a stepdown, a shutdown)
# is retried; the documents already stored come back as duplicates.
_PERMANENT_CODES = frozenset({2, 121, 10334})


def _rejected_count(exc: BaseException, size: int) -> int | None:
    """Documents permanently rejected by this failure; None to retry."""
    if isinstance(exc, _UNENCODABLE):
        return size
    if isinstance(exc, BulkWriteError):
        codes = [
            error.get("code") for error in (exc.details or {}).get("writeErrors") or []
        ]
        failed = [code for code in codes if code != DUPLICATE_KEY]
        if failed and all(code in _PERMANENT_CODES for code in failed):
            return len(failed)
    return None


class StoreSink:
    def __init__(
        self,
        repository: DiagnosticsMongoRepository,
        *,
        queue_max: int = 5000,
        batch_max: int = 500,
        flush_seconds: float = 1.0,
        write_timeout: float = 5.0,
    ) -> None:
        self._repository = repository
        self._queue_max = queue_max
        self._batch_max = batch_max
        self._flush_seconds = flush_seconds
        self._write_timeout = write_timeout
        self._lock = threading.Lock()
        self._queues: dict[str, deque[dict[str, Any]]] = {
            EVENTS_COLLECTION: deque(),
            LOGS_COLLECTION: deque(),
        }
        self._handler: SinkHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = False
        self._dropped = 0
        self._written = 0
        self._write_failures = 0
        self._consecutive_failures = 0
        self._last_error_type: str | None = None
        self._last_write_at: datetime | None = None

    def accept(self, entry: LogEntry) -> None:
        if _is_own(entry.logger):
            return
        to_events = entry.system and entry.level_no >= logging.INFO
        to_logs = entry.level_no >= logging.WARNING
        if not (to_events or to_logs):
            return
        document = to_document(entry)
        with self._lock:
            if to_events:
                self._push(self._queues[EVENTS_COLLECTION], document)
            if to_logs:
                self._push(self._queues[LOGS_COLLECTION], document)

    def _push(self, queue: deque[dict[str, Any]], document: dict[str, Any]) -> None:
        if len(queue) >= self._queue_max:
            queue.popleft()
            self._dropped += 1
        queue.append(document)

    def start(self, handler: SinkHandler, ring: RingBufferSink) -> None:
        """Backfill the ring buffer, register, and start the flush task.

        Must run inside the event loop that owns the Mongo client. The
        handler lock is held throughout, so a record emitted meanwhile waits
        and lands after the backlog instead of falling between the two.
        """
        handler.acquire()
        try:
            backlog = ring.entries()
            handler.add_sink(self.accept)
            self._handler = handler
            for entry in backlog:
                self.accept(entry)
        finally:
            handler.release()
        self._ready = True
        self._task = traced_create_task(self._run(), name="diagnostics-store")

    async def stop(self) -> None:
        if self._handler is not None:
            self._handler.remove_sink(self.accept)
            self._handler = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.wait_for(self._drain(), _STOP_FLUSH_SECONDS)
        except Exception:
            pass
        self._ready = False

    async def _drain(self) -> None:
        while self._queued() and await self.flush_once():
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._delay())
            await self.flush_once()

    def _delay(self) -> float:
        if not self._consecutive_failures:
            return self._flush_seconds
        return min(2.0**self._consecutive_failures, _MAX_BACKOFF_SECONDS)

    async def flush_once(self) -> bool:
        """Write one batch per collection; False when a write must be retried."""
        results = [
            await self._flush_collection(collection)
            for collection in (EVENTS_COLLECTION, LOGS_COLLECTION)
        ]
        # One step of backoff per tick, whichever collection failed.
        if all(results):
            self._consecutive_failures = 0
            return True
        self._consecutive_failures += 1
        return False

    async def _flush_collection(self, collection: str) -> bool:
        queue = self._queues[collection]
        with self._lock:
            batch = [queue.popleft() for _ in range(min(self._batch_max, len(queue)))]
        if not batch:
            return True
        try:
            await asyncio.wait_for(
                self._repository.insert(collection, batch), self._write_timeout
            )
        except BaseException as exc:
            rejected = _rejected_count(exc, len(batch))
            if rejected is None:
                self._requeue(queue, batch)
            else:
                with self._lock:
                    self._dropped += rejected
                self._written += len(batch) - rejected
            if not isinstance(exc, Exception):
                raise
            self._write_failures += 1
            self._last_error_type = type(exc).__name__
            return rejected is not None
        self._written += len(batch)
        self._last_write_at = datetime.now(UTC)
        return True

    def _requeue(
        self, queue: deque[dict[str, Any]], batch: list[dict[str, Any]]
    ) -> None:
        with self._lock:
            queue.extendleft(reversed(batch))
            while len(queue) > self._queue_max:
                queue.popleft()
                self._dropped += 1

    def _queued(self) -> int:
        with self._lock:
            return sum(len(queue) for queue in self._queues.values())

    def queued_ids(self, collection: str) -> list[Any]:
        with self._lock:
            return [document["_id"] for document in self._queues[collection]]

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self._ready,
            "queued": self._queued(),
            "dropped": self._dropped,
            "written": self._written,
            "write_failures": self._write_failures,
            "last_error_type": self._last_error_type,
            "last_write_at": self._last_write_at,
        }


__all__ = ["StoreSink"]
