"""In-memory room event log for delivery and session tests."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from delivery.room_events import RoomEventAppend, _room_event_doc


class InMemoryRoomEventStore:
    """Deterministic in-process room event log for tests."""

    def __init__(self) -> None:
        self._rooms: dict[str, dict[int, dict[str, Any]]] = {}
        self._counters: dict[str, int] = {}
        self._lock = None

    async def _ensure_lock(self):
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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
        lock = await self._ensure_lock()
        async with lock:
            _id = (
                idempotency_key
                or event_id
                or f"{conversation_id}:{kind}:{uuid.uuid4().hex}"
            )
            room = self._rooms.setdefault(conversation_id, {})
            for existing in room.values():
                if str(existing.get("_id") or "") == str(_id):
                    return RoomEventAppend(
                        room_seq=int(existing["room_seq"]),
                        room_event_id=str(_id),
                    )
            room_seq = self._counters.get(conversation_id, 0) + 1
            self._counters[conversation_id] = room_seq
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
            doc["room_seq"] = room_seq
            room[room_seq] = doc
            return RoomEventAppend(room_seq=room_seq, room_event_id=str(_id))

    async def latest_seq(self, conversation_id: str) -> int:
        return self._counters.get(conversation_id, 0)

    async def read_range(
        self,
        conversation_id: str,
        *,
        after: int = 0,
        limit: int = 500,
        include_skipped: bool = False,
    ) -> list[dict[str, Any]]:
        room = self._rooms.get(conversation_id, {})
        records = []
        for room_seq in sorted(room):
            if room_seq <= after:
                continue
            doc = room[room_seq]
            if doc.get("kind") == "skipped" and not include_skipped:
                continue
            records.append(
                {
                    "room_seq": room_seq,
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
            if len(records) >= limit:
                break
        return records


__all__ = [
    "InMemoryRoomEventStore",
]
