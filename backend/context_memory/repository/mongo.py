from __future__ import annotations

from datetime import datetime
from typing import Any

from common.observability import events as E
from common.protocols import MongoCollection, MongoDAL
from common.utils.logger import get_logger
from common.utils.mongo_indexes import create_index
from common.utils.room_state import delete_room_rows

logger = get_logger(__name__)

# Memory search was removed; its text index is no longer read.
OBSOLETE_CONTENT_INDEXES = ("turn_notes_text",)


class MemoryMongoRepository:
    def __init__(
        self,
        *,
        mongo: MongoDAL,
        collection_name: str = "room_memories",
    ) -> None:
        self._memories = mongo.collection(collection_name)

    async def delete_room_state(self, conversation_id: str) -> bool:
        return await delete_room_rows((self._memories,), conversation_id)

    async def ensure_indexes(self) -> None:
        await create_index(
            self._memories,
            "room_memories",
            [("room_id", 1)],
            name="room_id_unique",
            unique=True,
            critical=True,
        )

    async def get_room_memory(self, conversation_id: str) -> dict | None:
        return await self._memories.find_one({"room_id": conversation_id})

    async def upsert_room_memory(self, conversation_id: str, memory: dict) -> None:
        memory = _canonicalize_memory_document(memory)
        set_fields = _sanitize_update(memory)
        insert_doc = dict(memory)
        insert_doc["room_id"] = conversation_id
        set_on_insert = {
            key: value for key, value in insert_doc.items() if key not in set_fields
        }
        await self._memories.update_one(
            {"room_id": conversation_id},
            {
                "$set": set_fields,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    async def delete_room_memory(self, conversation_id: str) -> bool:
        return await self._memories.delete_one({"room_id": conversation_id})

    async def create_room_memory(self, memory: dict) -> str:
        memory_id = memory.get("memory_id")
        if not memory_id:
            raise ValueError("memory_id is required")
        await self._memories.insert_one(_canonicalize_memory_document(memory))
        return str(memory_id)

    async def ensure_room_memory(self, conversation_id: str, defaults: dict) -> dict:
        defaults = _canonicalize_memory_document(defaults)
        defaults["room_id"] = conversation_id
        doc = await self._memories.find_one_and_update(
            {"room_id": conversation_id},
            {"$setOnInsert": defaults},
            upsert=True,
            return_document=True,
        )
        if doc is None:
            doc = await self.get_room_memory(conversation_id)
        return doc or defaults

    async def push_and_trim_conversation_turn_if_absent(
        self,
        conversation_id: str,
        turn: dict,
        *,
        turn_id: str,
        max_turns: int,
        summary_stub: str,
        max_summary_chars: int,
    ) -> tuple[bool, bool, bool]:
        if max_turns <= 0:
            return False, False, False
        max_summary_chars = max(max_summary_chars, 10)
        query = {
            "room_id": conversation_id,
            "conversation_history.turn_id": {"$ne": turn_id},
        }
        doc = await self._push_turn(
            conversation_id,
            turn,
            max_turns,
            summary_stub,
            max_summary_chars,
            query=query,
        )
        if doc is not None:
            return True, True, False
        existing = await self.get_room_memory(conversation_id)
        if not existing:
            return False, False, False
        if _history_contains_turn(existing, turn_id):
            return False, True, True
        retry = await self._push_turn(
            conversation_id,
            turn,
            max_turns,
            summary_stub,
            max_summary_chars,
            query=query,
        )
        if retry is not None:
            return True, True, False
        existing = await self.get_room_memory(conversation_id)
        if not existing:
            return False, False, False
        if _history_contains_turn(existing, turn_id):
            return False, True, True
        raise RuntimeError(f"Could not determine projection state for turn {turn_id}")

    async def compact_turns_bulk(
        self, conversation_id: str, compacted_turns: list[dict]
    ) -> bool:
        if not compacted_turns:
            return True
        turn_ids = [entry["turn_id"] for entry in compacted_turns]
        full_turn_match = {
            "$elemMatch": {
                "turn_id": {"$in": turn_ids},
                "representation": "full",
            }
        }
        try:
            return await self._memories.update_one(
                {
                    "room_id": conversation_id,
                    "conversation_history": full_turn_match,
                },
                _compact_turns_pipeline(compacted_turns),
            )
        except Exception as exc:
            if isinstance(exc, TypeError | AttributeError | ValueError):
                raise
            logger.emit(
                E.TURN_COMPACTION_WRITE_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                compacted_turn_count=len(compacted_turns),
            )
            return False

    async def list_room_ids_with_memory(self, limit: int | None = None) -> list[str]:
        room_ids: list[str] = []
        skip = 0
        batch_size = 500
        while True:
            remaining = None if limit is None else max(0, limit - len(room_ids))
            if remaining == 0:
                break
            current_limit = min(batch_size, remaining) if remaining else batch_size
            docs = await self._memories.find(
                {},
                projection={"room_id": 1},
                limit=current_limit,
                skip=skip,
                sort=[("room_id", 1)],
            )
            if not docs:
                break
            room_ids.extend(doc["room_id"] for doc in docs if doc.get("room_id"))
            skip += len(docs)
            if len(docs) < current_limit:
                break
        return room_ids

    async def _push_turn(
        self,
        conversation_id: str,
        turn: dict,
        max_turns: int,
        summary_stub: str,
        max_summary_chars: int,
        *,
        query: dict | None = None,
    ) -> dict | None:
        query = query or {"room_id": conversation_id}
        update = _push_turn_update(turn, max_turns, max_summary_chars)
        return await self._memories.find_one_and_update(
            query,
            update,
            upsert=False,
            return_document=True,
            projection={"room_id": 1, "memory_id": 1},
        )


class ContentStorageMongoRepository:
    def __init__(
        self,
        *,
        mongo: MongoDAL,
        collection_name: str = "conversation_content",
    ) -> None:
        self._content: MongoCollection = mongo.collection(collection_name)

    async def delete_room_state(self, conversation_id: str) -> bool:
        return await delete_room_rows((self._content,), conversation_id)

    async def ensure_indexes(self) -> None:
        await self._drop_obsolete_indexes()
        await create_index(
            self._content,
            "conversation_content",
            [("room_id", 1), ("turn_id", 1)],
            name="room_turn_unique",
            unique=True,
            critical=True,
        )
        await create_index(
            self._content,
            "conversation_content",
            [("document_id", 1)],
            name="document_id_unique",
            unique=True,
            partialFilterExpression={"document_id": {"$exists": True}},
        )
        await create_index(
            self._content,
            "conversation_content",
            [("room_id", 1), ("stored_at", -1)],
            name="room_stored_at",
        )
        await create_index(
            self._content,
            "conversation_content",
            [("expires_at", 1)],
            name="content_ttl",
            expireAfterSeconds=0,
            sparse=True,
        )

    async def _drop_obsolete_indexes(self) -> None:
        """Drop indexes whose readers were removed; failure never blocks startup."""
        existing = await self._content.index_information()
        for name in OBSOLETE_CONTENT_INDEXES:
            if name not in existing:
                continue
            try:
                await self._content.drop_index(name)
            except Exception as exc:
                logger.emit(
                    E.OBSOLETE_INDEX_DROP_FAILED,
                    exc=exc,
                    collection_name="conversation_content",
                    index_name=name,
                )

    async def upsert_full_content(
        self,
        *,
        document_id: str,
        conversation_id: str,
        turn_id: str,
        content: str,
        content_type: str,
        content_hash: str,
        stored_at: datetime,
        turn_timestamp: datetime | str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        fields = {
            "document_id": document_id,
            "room_id": conversation_id,
            "turn_id": turn_id,
            "content": content,
            "content_type": content_type,
            "content_hash": content_hash,
            "stored_at": stored_at,
            "turn_timestamp": turn_timestamp,
            "expires_at": expires_at,
        }
        doc = await self._content.find_one_and_update(
            {"room_id": conversation_id, "turn_id": turn_id},
            {"$set": fields},
            upsert=True,
            return_document=True,
        )
        if doc and doc.get("document_id"):
            return doc["document_id"]
        return document_id

    async def get_content_by_turn_id(
        self, conversation_id: str, turn_id: str
    ) -> dict | None:
        return await self._content.find_one(
            {"room_id": conversation_id, "turn_id": turn_id}
        )

    async def delete_content_by_turn_id(
        self, conversation_id: str, turn_id: str
    ) -> bool:
        return await self._content.delete_one(
            {"room_id": conversation_id, "turn_id": turn_id}
        )

    async def delete_content_by_room_id(self, conversation_id: str) -> int:
        return await self._content.delete_many({"room_id": conversation_id})


def _canonicalize_memory_document(memory: dict) -> dict:
    doc = dict(memory)
    memory_content = doc.get("memory_content")
    if isinstance(memory_content, dict):
        memory_content = dict(memory_content)
        memory_content.pop("conversation_history", None)
        doc["memory_content"] = memory_content
    doc.pop("memory_content.conversation_history", None)
    return doc


def _sanitize_update(updates: dict) -> dict:
    immutable = {"_id", "room_id", "memory_id"}
    canonical = _canonicalize_memory_document(updates)
    return {key: value for key, value in canonical.items() if key not in immutable}


def _history_contains_turn(doc: dict, turn_id: str) -> bool:
    history = doc.get("conversation_history") or []
    return any(
        isinstance(turn, dict) and turn.get("turn_id") == turn_id for turn in history
    )


def _push_turn_update(
    turn: dict,
    max_turns: int,
    max_summary_chars: int,
) -> list[dict]:
    canonical_history = _canonical_history_expression()
    return [
        {
            "$set": {
                "conversation_history": {
                    "$concatArrays": [canonical_history, _literal([turn])]
                },
                "memory_content.summary": _summary_append_evicted_turn_expression(
                    max_turns=max_turns,
                    max_summary_chars=max_summary_chars,
                ),
                "last_activity_at": "$$NOW",
                "total_messages": {"$add": [{"$ifNull": ["$total_messages", 0]}, 1]},
            }
        }
    ]


def _canonical_history_expression() -> dict:
    return {
        "$cond": [
            {"$isArray": "$conversation_history"},
            "$conversation_history",
            [],
        ]
    }


def _summary_append_evicted_turn_expression(
    *,
    max_turns: int,
    max_summary_chars: int,
) -> dict:
    history = _canonical_history_expression()
    addition = {
        "$cond": [
            {"$gte": [{"$size": history}, max_turns]},
            {
                "$let": {
                    "vars": {
                        "evicted": {
                            "$arrayElemAt": [
                                history,
                                {"$subtract": [{"$size": history}, max_turns]},
                            ]
                        }
                    },
                    "in": _turn_summary_preview_expression("$$evicted"),
                }
            },
            "",
        ]
    }
    return {
        "$let": {
            "vars": {
                "existing": {"$ifNull": ["$memory_content.summary", ""]},
                "addition": addition,
            },
            "in": {
                "$let": {
                    "vars": {
                        "concatenated": {
                            "$cond": [
                                {"$eq": ["$$addition", ""]},
                                "$$existing",
                                {
                                    "$cond": [
                                        {"$eq": ["$$existing", ""]},
                                        "$$addition",
                                        {
                                            "$concat": [
                                                "$$existing",
                                                "\n",
                                                "$$addition",
                                            ]
                                        },
                                    ]
                                },
                            ]
                        }
                    },
                    "in": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$strLenCP": "$$concatenated"},
                                    max_summary_chars,
                                ]
                            },
                            {
                                "$concat": [
                                    "...",
                                    {
                                        "$substrCP": [
                                            "$$concatenated",
                                            {
                                                "$subtract": [
                                                    {"$strLenCP": "$$concatenated"},
                                                    max_summary_chars - 3,
                                                ]
                                            },
                                            max_summary_chars - 3,
                                        ]
                                    },
                                ]
                            },
                            "$$concatenated",
                        ]
                    },
                }
            },
        }
    }


def _turn_summary_preview_expression(turn: str) -> dict:
    role_label = {
        "$switch": {
            "branches": [
                {"case": {"$eq": [f"{turn}.role", "user"]}, "then": "User"},
                {
                    "case": {"$eq": [f"{turn}.role", "agent"]},
                    "then": {"$ifNull": [f"{turn}.agent_name", "Agent"]},
                },
                {
                    "case": {"$eq": [f"{turn}.role", "supervisor"]},
                    "then": "Supervisor",
                },
            ],
            "default": {"$ifNull": [f"{turn}.role", "Unknown"]},
        }
    }
    content = {
        "$cond": [
            {"$eq": [{"$type": f"{turn}.content"}, "string"]},
            f"{turn}.content",
            {
                "$cond": [
                    {"$eq": [{"$type": f"{turn}.brief_summary"}, "string"]},
                    f"{turn}.brief_summary",
                    "[compact turn]",
                ]
            },
        ]
    }
    return {
        "$concat": [
            "[",
            role_label,
            "] ",
            {"$substrCP": [content, 0, 200]},
            "...",
        ]
    }


def _compact_turns_pipeline(compacted_turns: list[dict]) -> list[dict]:
    turn_ids = [entry["turn_id"] for entry in compacted_turns]
    compacted_history = _compact_history_expression(
        "$conversation_history", compacted_turns, turn_ids
    )
    return [
        {
            "$set": {
                "conversation_history": compacted_history,
                "last_activity_at": "$$NOW",
                "total_compactions": {
                    "$add": [{"$ifNull": ["$total_compactions", 0]}, 1]
                },
            }
        }
    ]


def _compact_history_expression(
    history_path: str, compacted_turns: list[dict], turn_ids: list[str]
) -> dict:
    mapped = {
        "$map": {
            "input": history_path,
            "as": "turn",
            "in": {
                "$cond": [
                    {
                        "$and": [
                            {"$in": ["$$turn.turn_id", _literal(turn_ids)]},
                            {"$eq": ["$$turn.representation", "full"]},
                        ]
                    },
                    {
                        "$mergeObjects": [
                            "$$turn",
                            {
                                "representation": "compact",
                                "content": None,
                                "content_ref": _switch_for_field(
                                    compacted_turns, "content_ref"
                                ),
                                "estimated_tokens_compact": _switch_for_field(
                                    compacted_turns,
                                    "estimated_tokens_compact",
                                    default=20,
                                ),
                                "brief_summary": _switch_for_field(
                                    compacted_turns,
                                    "brief_summary",
                                ),
                            },
                        ]
                    },
                    "$$turn",
                ]
            },
        }
    }
    return {
        "$cond": [
            {"$isArray": history_path},
            mapped,
            "$$REMOVE",
        ]
    }


def _switch_for_field(
    compacted_turns: list[dict], field: str, default: Any | None = None
) -> dict:
    return {
        "$switch": {
            "branches": [
                {
                    "case": {"$eq": ["$$turn.turn_id", _literal(entry["turn_id"])]},
                    "then": _literal(entry.get(field, default)),
                }
                for entry in compacted_turns
            ],
            # This is an aggregation variable reference, not caller-provided data.
            "default": f"$$turn.{field}",
        }
    }


def _literal(value: Any) -> dict[str, Any]:
    return {"$literal": value}
