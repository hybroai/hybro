"""Readiness-gated indexes for the room message collections."""

from __future__ import annotations

from typing import Any

from common.idempotency import MAX_CLIENT_REQUEST_ID_LENGTH
from common.observability import events as E
from common.protocols import MongoCollection, MongoDAL
from common.utils.logger import get_logger
from common.utils.mongo_indexes import create_index
from common.utils.time import utcnow
from conversation.timeline import (
    TIMELINE_MIGRATION_MARKER_COLLECTION,
    TIMELINE_MIGRATION_MARKER_ID,
    TIMELINE_MIGRATION_VERSION,
)

logger = get_logger(__name__)


async def ensure_room_timeline_indexes(mongo: MongoDAL) -> None:
    """Require validated identities and a completed historical timeline audit."""

    specifications = (
        ("room_user_messages", "room_user_timeline_desc"),
        ("room_agent_messages", "room_agent_timeline_desc"),
    )
    string_room_id = {"$eq": [{"$type": "$room_id"}, "string"]}
    string_message_id = {"$eq": [{"$type": "$message_id"}, "string"]}
    invalid_identity = {
        "$or": [
            {"$not": [string_room_id]},
            {
                "$eq": [
                    {"$trim": {"input": {"$cond": [string_room_id, "$room_id", ""]}}},
                    "",
                ]
            },
            {"$not": [string_message_id]},
            {
                "$eq": [
                    {
                        "$trim": {
                            "input": {"$cond": [string_message_id, "$message_id", ""]}
                        }
                    },
                    "",
                ]
            },
        ]
    }
    invalid_timeline_key = {
        "$not": [{"$in": [{"$type": "$timeline_sort_us"}, ["int", "long"]]}]
    }
    identity_failures: list[str] = []
    timeline_failures: list[str] = []
    any_messages = False
    for collection_name, _index_name in specifications:
        collection = mongo.collection(collection_name)
        identity_samples = await collection.aggregate(
            [
                {"$match": {"$expr": invalid_identity}},
                {"$project": {"_id": 0, "message_id": 1}},
                {"$limit": 5},
            ]
        )
        if identity_samples:
            sample_ids = [str(sample.get("message_id")) for sample in identity_samples]
            identity_failures.append(
                f"{collection_name} invalid message_ids={sample_ids}"
            )
        timeline_samples = await collection.aggregate(
            [
                {"$match": {"$expr": invalid_timeline_key}},
                {"$project": {"_id": 0, "message_id": 1}},
                {"$limit": 5},
            ]
        )
        if timeline_samples:
            sample_ids = [str(sample.get("message_id")) for sample in timeline_samples]
            timeline_failures.append(
                f"{collection_name} invalid message_ids={sample_ids}"
            )
        any_messages = (
            bool(await collection.find_one({}, projection={"_id": 1})) or any_messages
        )

    if identity_failures:
        details = "; ".join(identity_failures)
        # Sample ids stay in the startup error, out of the log stream.
        logger.emit(
            E.INDEX_READINESS_CHECK_FAILED,
            readiness_check="timeline_identity",
            issue_count=len(identity_failures),
        )
        raise RuntimeError(
            "Room timeline indexes require non-empty string room_id and message_id. "
            "The timeline migration cannot repair identity fields; manually repair "
            f"the key-only samples before restart: {details}"
        )
    migration_command = (
        "cd backend && uv run python -m scripts.migrate_room_timeline_sort_keys --apply"
    )
    if timeline_failures:
        details = "; ".join(timeline_failures)
        logger.emit(
            E.INDEX_READINESS_CHECK_FAILED,
            readiness_check="timeline_key",
            issue_count=len(timeline_failures),
        )
        raise RuntimeError(
            "Room timeline indexes require integer timeline_sort_us on every "
            f"message. Run `{migration_command}`. Key-only samples: {details}"
        )

    bootstrap_empty_marker = not any_messages
    if any_messages:
        marker = await mongo.collection(TIMELINE_MIGRATION_MARKER_COLLECTION).find_one(
            {"_id": TIMELINE_MIGRATION_MARKER_ID}
        )
        evidence = marker.get("collections") if isinstance(marker, dict) else None
        valid_evidence = isinstance(evidence, dict) and all(
            isinstance(item := evidence.get(collection_name), dict)
            and isinstance(item.get("scanned"), int)
            and not isinstance(item.get("scanned"), bool)
            and isinstance(item.get("correct"), int)
            and not isinstance(item.get("correct"), bool)
            and item["scanned"] == item["correct"]
            for collection_name, _index_name in specifications
        )
        if not (
            isinstance(marker, dict)
            and marker.get("_id") == TIMELINE_MIGRATION_MARKER_ID
            and isinstance(marker.get("version"), int)
            and not isinstance(marker.get("version"), bool)
            and marker.get("version") == TIMELINE_MIGRATION_VERSION
            and marker.get("status") == "complete"
            and valid_evidence
        ):
            raise RuntimeError(
                "Room timeline historical consistency has not passed the final "
                f"migration audit. Run `{migration_command}` while writes are "
                "quiesced; startup will not infer consistency from integer type alone."
            )

    for collection_name, index_name in specifications:
        await create_index(
            mongo.collection(collection_name),
            collection_name,
            [
                ("room_id", 1),
                ("timeline_sort_us", -1),
                ("message_id", -1),
            ],
            name=index_name,
            critical=True,
        )

    if bootstrap_empty_marker:
        await mongo.collection(TIMELINE_MIGRATION_MARKER_COLLECTION).update_one(
            {"_id": TIMELINE_MIGRATION_MARKER_ID},
            {
                "$set": {
                    "marker_id": TIMELINE_MIGRATION_MARKER_ID,
                    "version": TIMELINE_MIGRATION_VERSION,
                    "status": "complete",
                    "completed_at": utcnow(),
                    "collections": {
                        collection_name: {"scanned": 0, "correct": 0}
                        for collection_name, _index_name in specifications
                    },
                }
            },
            upsert=True,
        )


async def ensure_user_message_indexes(mongo: MongoDAL) -> None:
    collection = mongo.collection("room_user_messages")
    issues = await user_message_index_readiness_issues(collection)
    if issues:
        details = "; ".join(issues)
        logger.emit(
            E.INDEX_READINESS_CHECK_FAILED,
            collection_name="room_user_messages",
            readiness_check="user_message_unique_indexes",
            issue_count=len(issues),
        )
        raise RuntimeError(
            "room_user_messages cannot enable correctness-critical unique indexes: "
            f"{details}. Repair the historical rows explicitly; startup did not "
            "delete or merge any messages."
        )

    await create_index(
        mongo.collection("room_user_messages"),
        "room_user_messages",
        [("message_id", 1)],
        name="room_user_message_id_unique",
        unique=True,
        critical=True,
    )
    await create_index(
        mongo.collection("room_user_messages"),
        "room_user_messages",
        [("room_id", 1), ("client_request_id", 1)],
        name="room_user_client_request_id_unique",
        unique=True,
        critical=True,
        partialFilterExpression={
            "room_id": {"$type": "string"},
            "client_request_id": {"$type": "string"},
        },
    )


async def user_message_index_readiness_issues(
    collection: MongoCollection,
    *,
    sample_limit: int = 5,
) -> list[str]:
    """Audit historical rows server-side before enabling unique constraints."""

    string_message_id = {"$eq": [{"$type": "$message_id"}, "string"]}
    trimmed_message_id = {
        "$trim": {"input": {"$cond": [string_message_id, "$message_id", ""]}}
    }
    string_room_id = {"$eq": [{"$type": "$room_id"}, "string"]}
    trimmed_room_id = {"$trim": {"input": {"$cond": [string_room_id, "$room_id", ""]}}}
    string_client_request_id = {"$eq": [{"$type": "$client_request_id"}, "string"]}
    normalized_client_request_input = {
        "$cond": [
            string_client_request_id,
            "$client_request_id",
            "",
        ]
    }
    trimmed_client_request_id = {"$trim": {"input": normalized_client_request_input}}
    client_request_id_length = {"$strLenCP": normalized_client_request_input}

    checks: list[tuple[str, list[dict[str, Any]]]] = [
        (
            "duplicate non-empty message_id",
            [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                string_message_id,
                                {"$ne": [trimmed_message_id, ""]},
                            ]
                        }
                    }
                },
                {"$group": {"_id": "$message_id", "occurrences": {"$sum": 1}}},
                {"$match": {"occurrences": {"$gt": 1}}},
                {
                    "$project": {
                        "_id": 0,
                        "message_id": "$_id",
                        "occurrences": 1,
                    }
                },
                {"$limit": sample_limit},
            ],
        ),
        (
            "missing, null, non-string, or empty message_id",
            [
                {
                    "$match": {
                        "$expr": {
                            "$or": [
                                {"$ne": [{"$type": "$message_id"}, "string"]},
                                {"$eq": [trimmed_message_id, ""]},
                            ]
                        }
                    }
                },
                {"$project": {"_id": 1, "message_id": 1}},
                {"$limit": sample_limit},
            ],
        ),
        (
            "duplicate (room_id, normalized client_request_id)",
            [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                string_room_id,
                                string_client_request_id,
                                {"$ne": [trimmed_room_id, ""]},
                                {"$ne": [trimmed_client_request_id, ""]},
                            ]
                        }
                    }
                },
                {
                    "$project": {
                        "room_id": 1,
                        "client_request_id": trimmed_client_request_id,
                    }
                },
                {
                    "$group": {
                        "_id": {
                            "room_id": "$room_id",
                            "client_request_id": "$client_request_id",
                        },
                        "occurrences": {"$sum": 1},
                    }
                },
                {"$match": {"occurrences": {"$gt": 1}}},
                {
                    "$project": {
                        "_id": 0,
                        "room_id": "$_id.room_id",
                        "client_request_id": "$_id.client_request_id",
                        "occurrences": 1,
                    }
                },
                {"$limit": sample_limit},
            ],
        ),
        (
            "invalid or non-normalized client_request_id",
            [
                {
                    "$match": {
                        "$expr": {
                            "$or": [
                                {
                                    "$and": [
                                        string_client_request_id,
                                        {
                                            "$or": [
                                                {
                                                    "$eq": [
                                                        trimmed_client_request_id,
                                                        "",
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        trimmed_client_request_id,
                                                        "$client_request_id",
                                                    ]
                                                },
                                                {
                                                    "$gt": [
                                                        client_request_id_length,
                                                        MAX_CLIENT_REQUEST_ID_LENGTH,
                                                    ]
                                                },
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$not": [
                                        {
                                            "$in": [
                                                {"$type": "$client_request_id"},
                                                ["missing", "null", "string"],
                                            ]
                                        }
                                    ]
                                },
                            ]
                        }
                    }
                },
                {
                    "$project": {
                        "_id": 1,
                        "room_id": 1,
                        "client_request_id": 1,
                    }
                },
                {"$limit": sample_limit},
            ],
        ),
        (
            "missing, null, non-string, or empty room_id",
            [
                {
                    "$match": {
                        "$expr": {
                            "$or": [
                                {"$ne": [{"$type": "$room_id"}, "string"]},
                                {"$eq": [trimmed_room_id, ""]},
                            ]
                        }
                    }
                },
                {"$project": {"_id": 1, "room_id": 1, "message_id": 1}},
                {"$limit": sample_limit},
            ],
        ),
    ]

    issues: list[str] = []
    for label, pipeline in checks:
        samples = await collection.aggregate(pipeline)
        if samples:
            issues.append(
                f"{label}: found at least {len(samples)}; samples={samples!r}"
            )
    return issues


__all__ = [
    "ensure_room_timeline_indexes",
    "ensure_user_message_indexes",
    "user_message_index_readiness_issues",
]
