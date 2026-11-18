"""Append-only public room event log with per-room monotonic sequencing.

Phase 2 of the Room Stream Snapshot plan (§5): the ``room_events`` collection
is the source of truth for the realtime UI. The sequence is allocated
ATOMICALLY with the insert (Mongo counter document advanced in the same
transaction as the event write), so a ``room_seq`` exists iff its document
exists — no permanent sequence holes are possible on the transactional path.

For environments without replica-set transactions (rare development setups)
the store falls back to counter-then-insert allocation and heals permanent
holes with idempotent ``skipped`` tombstones once the counter has advanced
past the gap by ``skip_grace`` allocations (§11 risk 10 fallback).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pymongo.errors import DuplicateKeyError

from common.utils.logger import get_logger

logger = get_logger(__name__)

_GRACE_DEFAULT = 5
_ACCEPTED_SEND = {"kind": "processing_status", "payload_public.status": "processing"}
_FALLBACK_INSERT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class RoomEventAppend:
    room_seq: int
    room_event_id: str
    persisted: bool = True


class RoomEventStore(Protocol):
    """Single writer surface for the public room event log."""

    async def append(
        self,
        *,
        conversation_id: str,
        kind: str,
        payload_public: dict[str, Any],
        event_id: str | None = None,
        idempotency_key: str | None = None,
        parent_event_id: str | None = None,
        run_id: str | None = None,
        persist_state: str = "settled",
        ts: datetime | None = None,
    ) -> RoomEventAppend: ...

    async def latest_seq(self, conversation_id: str) -> int: ...

    async def read_range(
        self,
        conversation_id: str,
        *,
        after: int = 0,
        limit: int = 500,
        include_skipped: bool = False,
    ) -> list[dict[str, Any]]: ...


class MongoRoomEventStore:
    """Transactional room_events writer over Motor/MongoDB."""

    def __init__(
        self,
        *,
        mongo: Any,
        events_collection: str = "room_events",
        seq_collection: str = "room_event_seq",
        skip_grace: int = _GRACE_DEFAULT,
    ) -> None:
        self._events = mongo.collection(events_collection)
        self._seq = mongo.collection(seq_collection)
        self._mongo = mongo
        self._skip_grace = skip_grace

    async def ensure_indexes(self) -> None:
        # Establish the unique room sequence index before accepting writes.
        # Existing ambiguous rows make startup fail instead of allowing a
        # tombstone and delayed original to coexist at one room sequence.
        await self._events.create_index(
            [("room_id", 1), ("room_seq", 1)],
            name="room_id_seq_unique",
            unique=True,
        )
        await self._events.create_index([("room_id", 1), ("ts", 1)], name="room_id_ts")
        # Accepted sends by time, across rooms, for the unstarted-send sweep.
        await self._events.create_index(
            [("ts", 1), ("_id", 1)],
            name="processing_status_ts",
            partialFilterExpression=_ACCEPTED_SEND,
        )

    async def append(
        self,
        *,
        conversation_id: str,
        kind: str,
        payload_public: dict[str, Any],
        event_id: str | None = None,
        idempotency_key: str | None = None,
        parent_event_id: str | None = None,
        run_id: str | None = None,
        persist_state: str = "settled",
        ts: datetime | None = None,
    ) -> RoomEventAppend:
        _id = (
            idempotency_key
            or event_id
            or f"{conversation_id}:{kind}:{uuid.uuid4().hex}"
        )
        doc = _room_event_doc(
            _id=_id,
            conversation_id=conversation_id,
            kind=kind,
            payload_public=payload_public,
            event_id=event_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            persist_state=persist_state,
            ts=ts,
        )
        try:
            return await self._append_transactional(conversation_id, doc)
        except DuplicateKeyError:
            return await self._read_back(conversation_id, _id)
        except _TransactionUnavailable:
            return await self._append_fallback(conversation_id, doc)

    async def _append_transactional(
        self, conversation_id: str, doc: dict[str, Any]
    ) -> RoomEventAppend:
        session_factory = getattr(self._mongo, "start_session", None)
        if not callable(session_factory):
            raise _TransactionUnavailable()
        from pymongo.errors import (
            ConfigurationError,
            OperationFailure,
        )

        async with session_factory() as session:
            try:
                async with session.start_transaction():
                    counter = await self._seq.raw_collection.find_one_and_update(
                        {"_id": conversation_id},
                        {"$inc": {"seq": 1}},
                        upsert=True,
                        return_document=True,
                        session=session,
                    )
                    room_seq = int(counter["seq"])
                    doc["room_seq"] = room_seq
                    await self._events.raw_collection.insert_one(doc, session=session)
            except DuplicateKeyError:
                raise
            except (OperationFailure, ConfigurationError) as exc:
                raise _TransactionUnavailable() from exc
        return RoomEventAppend(room_seq=room_seq, room_event_id=str(doc["_id"]))

    async def _append_fallback(
        self, conversation_id: str, doc: dict[str, Any]
    ) -> RoomEventAppend:
        """Counter-then-insert allocation for non-transactional deployments.

        Deterministic retries are read before allocation. A concurrent retry
        can still lose after allocating; that exact burned slot is immediately
        filled with a skipped tombstone so a quiescent room never retains a
        permanent tail hole.
        """

        from pymongo.errors import DuplicateKeyError

        existing = await self._events.find_one(
            {"_id": str(doc["_id"]), "room_id": conversation_id}
        )
        if existing is not None:
            return RoomEventAppend(
                room_seq=int(existing.get("room_seq") or 0),
                room_event_id=str(doc["_id"]),
            )

        for _attempt in range(_FALLBACK_INSERT_ATTEMPTS):
            counter = await self._seq.raw_collection.find_one_and_update(
                {"_id": conversation_id},
                {"$inc": {"seq": 1}},
                upsert=True,
                return_document=True,
            )
            room_seq = int(counter["seq"])
            doc["room_seq"] = room_seq
            try:
                await self._events.raw_collection.insert_one(doc)
            except DuplicateKeyError:
                existing = await self._events.find_one(
                    {"_id": str(doc["_id"]), "room_id": conversation_id}
                )
                if existing is not None:
                    await self._fill_skipped(conversation_id, room_seq)
                    return RoomEventAppend(
                        room_seq=int(existing.get("room_seq") or 0),
                        room_event_id=str(doc["_id"]),
                    )
                # A delayed writer can find its allocated sequence occupied by
                # a healer tombstone. Its logical id is still absent, so reuse
                # the same idempotency key at a fresh allocation rather than
                # reporting a false persisted=false/seq=0 result.
                continue
            await self._heal_skipped(conversation_id, room_seq)
            return RoomEventAppend(room_seq=room_seq, room_event_id=str(doc["_id"]))
        raise DuplicateKeyError(
            "room event fallback allocation repeatedly collided before persistence"
        )

    async def _fill_skipped(self, conversation_id: str, room_seq: int) -> None:
        tombstone = {
            "_id": f"skip:{conversation_id}:{room_seq}",
            "room_id": conversation_id,
            "room_seq": room_seq,
            "kind": "skipped",
            "event_id": None,
            "parent_event_id": None,
            "run_id": None,
            "ts": None,
            "payload_public": {},
            "persist_state": "settled",
        }
        try:
            await self._events.raw_collection.insert_one(tombstone)
        except DuplicateKeyError:
            pass

    async def _read_back(self, conversation_id: str, _id: str) -> RoomEventAppend:
        existing = await self._events.find_one({"_id": _id, "room_id": conversation_id})
        if existing is None:
            # Extremely unlikely: the deterministic key collided but the doc is
            # gone. Treat as a failed append with no sequence.
            return RoomEventAppend(room_seq=0, room_event_id=str(_id), persisted=False)
        return RoomEventAppend(
            room_seq=int(existing.get("room_seq") or 0),
            room_event_id=str(_id),
        )

    async def _heal_skipped(self, conversation_id: str, allocated_seq: int) -> None:
        if self._skip_grace <= 0:
            return
        heal_through = allocated_seq - self._skip_grace
        if heal_through <= 0:
            return
        counter = await self._seq.find_one({"_id": conversation_id})
        healed_through = int((counter or {}).get("healed_through") or 0)
        if heal_through <= healed_through:
            return
        scan_from = healed_through + 1
        try:
            existing_seqs = {
                int(doc.get("room_seq") or 0)
                for doc in await self._events.find(
                    {
                        "room_id": conversation_id,
                        "room_seq": {"$gte": scan_from, "$lte": heal_through},
                    },
                    projection={"room_seq": 1},
                    limit=heal_through - healed_through,
                )
            }
        except Exception:
            return
        for missing in range(scan_from, heal_through + 1):
            if missing in existing_seqs:
                continue
            tombstone = {
                "_id": f"skip:{conversation_id}:{missing}",
                "room_id": conversation_id,
                "room_seq": missing,
                "kind": "skipped",
                "event_id": None,
                "parent_event_id": None,
                "run_id": None,
                "ts": None,
                "payload_public": {},
                "persist_state": "settled",
            }
            try:
                await self._events.raw_collection.insert_one(tombstone)
            except DuplicateKeyError:
                continue
        # ``$max`` makes concurrent fallback healers monotonic and persists the
        # contiguous scan cursor, bounding each allocation to newly confirmed
        # sequence positions rather than rescanning the full room prefix.
        await self._seq.raw_collection.find_one_and_update(
            {"_id": conversation_id},
            {"$max": {"healed_through": heal_through}},
            upsert=True,
            return_document=True,
        )

    async def list_accepted_sends(
        self,
        *,
        oldest: datetime,
        newest: datetime,
        after: tuple[str, str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """``processing`` status records with ``oldest <= ts <= newest``.

        Ordered by ``(ts, room_event_id)``; ``after`` is the last pair of the
        previous page.
        """
        query: dict[str, Any] = {
            **_ACCEPTED_SEND,
            "ts": {"$gte": oldest.isoformat(), "$lte": newest.isoformat()},
        }
        if after is not None:
            ts, event_id = after
            query["$or"] = [
                {"ts": {"$gt": ts}},
                {"ts": ts, "_id": {"$gt": event_id}},
            ]
        docs = await self._events.find(
            query,
            sort=[("ts", 1), ("_id", 1)],
            limit=limit,
        )
        return [
            {
                "room_event_id": str(doc.get("_id") or ""),
                "conversation_id": doc.get("room_id"),
                "ts": doc.get("ts"),
                "payload_public": doc.get("payload_public") or {},
            }
            for doc in docs
        ]

    async def has_status(
        self, conversation_id: str, message_id: str, statuses: Iterable[str]
    ) -> bool:
        """Whether the room logged one of ``statuses`` for ``message_id``."""
        doc = await self._events.find_one(
            {
                "room_id": conversation_id,
                "kind": "processing_status",
                "payload_public.message_id": message_id,
                "payload_public.status": {"$in": list(statuses)},
            }
        )
        return doc is not None

    async def latest_seq(self, conversation_id: str) -> int:
        counter = await self._seq.find_one({"_id": conversation_id})
        if not counter:
            return 0
        return int(counter.get("seq") or 0)

    async def read_range(
        self,
        conversation_id: str,
        *,
        after: int = 0,
        limit: int = 500,
        include_skipped: bool = False,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"room_id": conversation_id, "room_seq": {"$gt": after}}
        if not include_skipped:
            query["kind"] = {"$ne": "skipped"}
        docs = await self._events.find(
            query,
            sort=[("room_seq", 1)],
            limit=limit,
        )
        records = []
        for doc in docs:
            records.append(
                {
                    "room_seq": int(doc.get("room_seq") or 0),
                    "room_event_id": str(doc.get("_id") or ""),
                    "event_id": doc.get("event_id"),
                    "parent_event_id": doc.get("parent_event_id"),
                    "run_id": doc.get("run_id"),
                    "kind": doc.get("kind"),
                    "ts": doc.get("ts"),
                    "payload_public": doc.get("payload_public") or {},
                    "persist_state": doc.get("persist_state"),
                }
            )
        return records


def _room_event_doc(
    *,
    _id: str,
    conversation_id: str,
    kind: str,
    payload_public: dict[str, Any],
    event_id: str | None,
    parent_event_id: str | None,
    run_id: str | None,
    persist_state: str,
    ts: datetime | None,
) -> dict[str, Any]:
    return {
        "_id": _id,
        "room_id": conversation_id,
        "kind": kind,
        "event_id": event_id,
        "parent_event_id": parent_event_id,
        "run_id": run_id,
        "ts": ts.isoformat() if ts is not None else None,
        "payload_public": payload_public,
        "persist_state": persist_state,
    }


class _TransactionUnavailable(Exception):
    pass


__all__ = [
    "MongoRoomEventStore",
    "RoomEventAppend",
    "RoomEventStore",
]
