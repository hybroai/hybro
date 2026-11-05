"""The two capped collections of the diagnostics store.

``diagnostic_system_events`` holds system events at INFO and above;
``diagnostic_log_records`` holds every record at WARNING and above. Both are
capped, so MongoDB evicts the oldest documents by size: no TTL and no
secondary index (queries sort by ``_id`` over at most the cap).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import BulkWriteError, CollectionInvalid, OperationFailure

from common.observability import events as E
from common.observability import get_logger
from common.observability.sinks import LogEntry
from common.protocols import MongoDAL

logger = get_logger(__name__)

EVENTS_COLLECTION = "diagnostic_system_events"
LOGS_COLLECTION = "diagnostic_log_records"
EVENTS_CAP_BYTES = 16 * 1024 * 1024
LOGS_CAP_BYTES = 32 * 1024 * 1024
MAX_FIELDS_JSON_BYTES = 16 * 1024
_CAPS = ((EVENTS_COLLECTION, EVENTS_CAP_BYTES), (LOGS_COLLECTION, LOGS_CAP_BYTES))
# Lifted out of ``fields`` into top-level document keys.
_LIFTED_FIELDS = ("instance_id", "version", "trace_id", "request_id")
_NAMESPACE_EXISTS = 48
DUPLICATE_KEY = 11000


@dataclass(frozen=True)
class RecordFilter:
    service: str | None = None
    events: tuple[str, ...] = ()
    min_level_no: int | None = None
    trace_id: str | None = None
    instance_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    before_id: ObjectId | None = None
    limit: int = 100


def _fields_json(fields: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        dict(fields), ensure_ascii=False, separators=(",", ":"), default=str
    )
    size = len(rendered.encode("utf-8"))
    if size > MAX_FIELDS_JSON_BYTES:
        return json.dumps({"truncated": True, "size": size})
    return rendered


def _as_text(value: Any) -> str | None:
    return value if value is None or isinstance(value, str) else str(value)


def to_document(entry: LogEntry) -> dict[str, Any]:
    """The stored shape of an entry, with a fresh ``_id`` fixed for retries."""
    fields = dict(entry.fields)
    # Lifted values are queried as strings and must always encode.
    lifted = {key: _as_text(fields.pop(key, None)) for key in _LIFTED_FIELDS}
    return {
        "_id": ObjectId(),
        "ts": entry.timestamp,
        "level": entry.level,
        "level_no": entry.level_no,
        "service": entry.service,
        "event": entry.event,
        "logger": entry.logger,
        "system": entry.system,
        **lifted,
        "fields_json": _fields_json(fields),
    }


def _query(record_filter: RecordFilter) -> dict[str, Any]:
    query: dict[str, Any] = {}
    equal = (
        ("service", record_filter.service),
        ("trace_id", record_filter.trace_id),
        ("instance_id", record_filter.instance_id),
    )
    query.update({key: value for key, value in equal if value is not None})
    if record_filter.events:
        query["event"] = {"$in": list(record_filter.events)}
    if record_filter.min_level_no is not None:
        query["level_no"] = {"$gte": record_filter.min_level_no}
    time_range = {
        op: value
        for op, value in (("$gte", record_filter.since), ("$lt", record_filter.until))
        if value is not None
    }
    if time_range:
        query["ts"] = time_range
    if record_filter.before_id is not None:
        query["_id"] = {"$lt": record_filter.before_id}
    return query


class DiagnosticsMongoRepository:
    """Owner of the two capped diagnostics collections."""

    def __init__(self, mongo: MongoDAL) -> None:
        self._mongo = mongo

    def _raw(self, name: str) -> Any:
        return self._mongo.collection(name).raw_collection

    async def ensure_indexes(self) -> bool:
        """Create both collections capped; report, never raise, a failure."""
        try:
            for name, size in _CAPS:
                await self._ensure_capped(name, size)
        except Exception as exc:
            logger.emit(E.DIAGNOSTICS_STORE_UNAVAILABLE, reason=type(exc).__name__)
            return False
        return True

    async def _ensure_capped(self, name: str, size: int) -> None:
        raw = self._raw(name)
        database = raw.database
        try:
            await database.create_collection(name, capped=True, size=size)
            return
        except CollectionInvalid:
            pass
        except OperationFailure as exc:
            # Another worker created it between pymongo's check and the create.
            if exc.code != _NAMESPACE_EXISTS:
                raise
        options = await raw.options()
        if not options.get("capped"):
            await database.command("convertToCapped", name, size=size)

    async def insert(self, collection: str, documents: list[dict[str, Any]]) -> None:
        try:
            await self._raw(collection).insert_many(documents, ordered=False)
        except BulkWriteError as exc:
            details = exc.details or {}
            errors = details.get("writeErrors") or []
            # Only duplicates: an earlier attempt that timed out wrote them.
            if (
                not errors
                or details.get("writeConcernErrors")
                or any(error.get("code") != DUPLICATE_KEY for error in errors)
            ):
                raise

    async def find(
        self, collection: str, record_filter: RecordFilter
    ) -> list[dict[str, Any]]:
        cursor = (
            self._raw(collection)
            .find(_query(record_filter))
            .sort("_id", -1)
            .limit(record_filter.limit)
        )
        documents = await cursor.to_list(length=record_filter.limit)
        for document in documents:
            stamp = document.get("ts")
            # Motor returns naive datetimes (the client is not tz_aware).
            if isinstance(stamp, datetime) and stamp.tzinfo is None:
                document["ts"] = stamp.replace(tzinfo=UTC)
        return documents


__all__ = [
    "EVENTS_CAP_BYTES",
    "EVENTS_COLLECTION",
    "LOGS_CAP_BYTES",
    "LOGS_COLLECTION",
    "MAX_FIELDS_JSON_BYTES",
    "DiagnosticsMongoRepository",
    "RecordFilter",
    "to_document",
]
