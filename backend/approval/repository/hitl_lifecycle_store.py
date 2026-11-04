from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.room_state import delete_room_rows
from common.utils.time import ensure_utc, utcnow
from models.hitl import HITLInteractionStatus, HITLResumeCommandStatus

logger = get_logger(__name__)

_INTERACTION_TERMINAL = {
    HITLInteractionStatus.APPLIED.value,
    HITLInteractionStatus.CANCELED.value,
    HITLInteractionStatus.EXPIRED.value,
    HITLInteractionStatus.FAILED.value,
}
_COMMAND_CLAIMABLE = {
    HITLResumeCommandStatus.PENDING.value,
    HITLResumeCommandStatus.RETRYABLE_ERROR.value,
}


class HITLLifecycleStore:
    """CAS/fenced persistence for HITL aggregates and remote commands."""

    def __init__(self, *, interactions, resume_commands, hitl_requests) -> None:
        self._interactions = interactions
        self._resume_commands = resume_commands
        self._hitl_requests = hitl_requests

    async def delete_room_state(self, conversation_id: str) -> bool:
        return await delete_room_rows(
            (self._interactions, self._resume_commands), conversation_id
        )

    async def materialize_interaction(
        self, interaction_data: dict[str, Any]
    ) -> dict[str, Any]:
        doc = dict(interaction_data)
        try:
            await self._interactions.insert_one(doc)
            return doc
        except DuplicateKeyError:
            existing = await self.get_interaction_strict(doc["interaction_id"])
            if existing is None:
                raise
            immutable = (
                "room_id",
                "user_message_id",
                "orchestration_run_id",
                "application_route",
                "public_source",
                "evidence_origin",
                "route_snapshot",
                "route_fingerprint",
                "creation_inventory",
                "expected_request_count",
            )
            if any(existing.get(key) != doc.get(key) for key in immutable):
                raise ValueError("conflicting HITL interaction metadata") from None
            return existing

    async def attach_interaction_request(  # noqa: C901
        self,
        interaction_id: str,
        *,
        request_id: str,
        required: bool,
        expires_at: datetime | None,
        question_index: int,
    ) -> dict[str, Any] | None:
        for _attempt in range(8):
            current = await self.get_interaction_strict(interaction_id)
            if current is None:
                return None
            request_ids = list(current.get("request_ids") or [])
            request_order = list(current.get("request_order") or [])
            if not request_order:
                request_order = [
                    {"request_id": existing, "index": index}
                    for index, existing in enumerate(request_ids)
                ]
            expected = int(current["expected_request_count"])
            inventory = list(current.get("creation_inventory") or [])
            if len(inventory) != expected:
                raise ValueError("interaction creation inventory is incomplete")
            if not 0 <= question_index < expected:
                raise ValueError("question_index is outside the interaction inventory")
            if inventory[question_index].get("request_id") != request_id:
                raise ValueError("request does not match creation inventory")
            existing_order = next(
                (item for item in request_order if item["request_id"] == request_id),
                None,
            )
            if existing_order is not None and existing_order["index"] != question_index:
                raise ValueError("request was attached with a different question_index")
            if request_id not in request_ids:
                if any(item["index"] == question_index for item in request_order):
                    raise ValueError("question_index is already attached")
                request_order.append(
                    {
                        "request_id": request_id,
                        "index": question_index,
                    }
                )
            request_order.sort(key=lambda item: item["index"])
            request_ids = [item["request_id"] for item in request_order]
            required_ids = list(current.get("required_request_ids") or [])
            if required and request_id not in required_ids:
                required_ids.append(request_id)
            current_expiry = current.get("expires_at")
            expiry_candidates = [
                ensure_utc(value)
                for value in (current_expiry, expires_at)
                if value is not None
            ]
            shared_expiry = min(expiry_candidates) if expiry_candidates else None
            status = current.get("status")
            if status == HITLInteractionStatus.MATERIALIZING.value:
                if len(request_ids) == expected:
                    if [item["index"] for item in request_order] != list(
                        range(expected)
                    ):
                        raise ValueError("interaction question inventory is incomplete")
                    status = HITLInteractionStatus.OPEN.value
            updated = await self._interactions.find_one_and_update(
                {
                    "interaction_id": interaction_id,
                    "version": current.get("version", 1),
                    "status": {"$nin": list(_INTERACTION_TERMINAL)},
                },
                {
                    "$set": {
                        "request_ids": request_ids,
                        "request_order": request_order,
                        "required_request_ids": [
                            request_id
                            for request_id in request_ids
                            if request_id in required_ids
                        ],
                        "expires_at": shared_expiry,
                        "status": status,
                        "updated_at": utcnow(),
                    },
                    "$inc": {"version": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                return updated
        return None

    async def get_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        try:
            return await self.get_interaction_strict(interaction_id)
        except Exception as exc:
            logger.emit(
                E.HITL_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_interaction",
                interaction_id=interaction_id,
            )
            return None

    async def get_interaction_strict(
        self, interaction_id: str
    ) -> dict[str, Any] | None:
        return await self._interactions.find_one({"interaction_id": interaction_id})

    async def get_interaction_for_request_strict(
        self, request_id: str
    ) -> dict[str, Any] | None:
        return await self._interactions.find_one({"request_ids": request_id})

    async def record_interaction_answer(
        self,
        interaction_id: str,
        *,
        request_id: str,
        answer_digest: str,
    ) -> dict[str, Any] | None:
        for _attempt in range(8):
            current = await self.get_interaction_strict(interaction_id)
            if current is None or request_id not in (current.get("request_ids") or []):
                return None
            if request_id in (current.get("answer_request_ids") or []):
                return current
            if current.get("status") not in {
                HITLInteractionStatus.OPEN.value,
                HITLInteractionStatus.PARTIALLY_ANSWERED.value,
            }:
                return None
            answer_ids = [*(current.get("answer_request_ids") or []), request_id]
            required = set(current.get("required_request_ids") or [])
            complete = required.issubset(answer_ids)
            status = (
                HITLInteractionStatus.ANSWERS_RECORDED.value
                if complete
                else HITLInteractionStatus.PARTIALLY_ANSWERED.value
            )
            updated = await self._interactions.find_one_and_update(
                {
                    "interaction_id": interaction_id,
                    "version": current.get("version", 1),
                    "status": current.get("status"),
                    "$or": [
                        {"expires_at": {"$gt": utcnow()}},
                        {"expires_at": None},
                        {"expires_at": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "answer_request_ids": answer_ids,
                        "status": status,
                        "updated_at": utcnow(),
                    },
                    "$push": {
                        "answer_refs": {
                            "request_id": request_id,
                            "digest": answer_digest,
                        }
                    },
                    "$inc": {"version": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                return updated
        return None

    async def claim_interaction_application(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        now = utcnow()
        lease = now + timedelta(seconds=lease_seconds)
        claimed = await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "status": HITLInteractionStatus.ANSWERS_RECORDED.value,
            },
            {
                "$set": {
                    "status": HITLInteractionStatus.APPLYING.value,
                    "application_claim_id": claim_id,
                    "application_lease_expires_at": lease,
                    "application_started_at": now,
                    "application_error": None,
                    "updated_at": now,
                },
                "$inc": {
                    "application_revision": 1,
                    "application_attempts": 1,
                    "version": 1,
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if claimed is not None:
            return claimed
        return await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "status": HITLInteractionStatus.APPLYING.value,
                "$or": [
                    {"application_lease_expires_at": {"$lte": now}},
                    {"application_lease_expires_at": None},
                    {"application_lease_expires_at": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "application_claim_id": claim_id,
                    "application_lease_expires_at": lease,
                    "application_error": None,
                    "updated_at": now,
                },
                "$inc": {"application_attempts": 1, "version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def renew_interaction_application(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._interactions.update_one(
            {
                "interaction_id": interaction_id,
                "status": HITLInteractionStatus.APPLYING.value,
                "application_claim_id": claim_id,
            },
            {
                "$set": {
                    "application_lease_expires_at": utcnow()
                    + timedelta(seconds=lease_seconds),
                    "updated_at": utcnow(),
                },
                "$inc": {"version": 1},
            },
        )
        return bool(getattr(result, "matched_count", result))

    async def resume_uncertain_interaction(
        self,
        interaction_id: str,
        *,
        claim_id: str,
    ) -> dict[str, Any] | None:
        now = utcnow()
        return await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "status": HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
            },
            {
                "$set": {
                    "status": HITLInteractionStatus.APPLYING.value,
                    "application_claim_id": claim_id,
                    "application_lease_expires_at": now,
                    "application_error": None,
                    "updated_at": now,
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def claim_run_answer_projection(
        self,
        interaction_id: str,
        *,
        application_revision: int,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        now = utcnow()
        return await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "application_revision": application_revision,
                "$or": [
                    {"run_projection_status": "pending"},
                    {"run_projection_status": "failed"},
                    {
                        "run_projection_status": "applying",
                        "run_projection_lease_expires_at": {"$lte": now},
                    },
                    {"run_projection_status": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "run_projection_status": "applying",
                    "run_projection_claim_id": claim_id,
                    "run_projection_lease_expires_at": now
                    + timedelta(seconds=lease_seconds),
                    "run_projection_error": None,
                    "updated_at": now,
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def renew_run_answer_projection(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._interactions.update_one(
            {
                "interaction_id": interaction_id,
                "run_projection_status": "applying",
                "run_projection_claim_id": claim_id,
            },
            {
                "$set": {
                    "run_projection_lease_expires_at": utcnow()
                    + timedelta(seconds=lease_seconds),
                    "updated_at": utcnow(),
                },
                "$inc": {"version": 1},
            },
        )
        return bool(getattr(result, "matched_count", result))

    async def mark_run_answer_projection(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"applied", "failed"}:
            raise ValueError("invalid run projection status")
        return await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "run_projection_status": "applying",
                "run_projection_claim_id": claim_id,
            },
            {
                "$set": {
                    "run_projection_status": status,
                    "run_projection_claim_id": None,
                    "run_projection_lease_expires_at": None,
                    "run_projection_error": error,
                    "updated_at": utcnow(),
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def mark_interaction_application_state(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        allowed = {
            HITLInteractionStatus.ANSWERS_RECORDED.value,
            HITLInteractionStatus.APPLYING.value,
            HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
            HITLInteractionStatus.APPLIED.value,
            HITLInteractionStatus.FAILED.value,
        }
        if status not in allowed:
            raise ValueError(f"invalid application target status {status!r}")
        now = utcnow()
        updates: dict[str, Any] = {
            "status": status,
            "application_error": error,
            "updated_at": now,
        }
        if status == HITLInteractionStatus.APPLYING.value:
            # Preserve the fencing token; an immediate retry is enabled by an
            # expired lease, never by dropping ownership while still APPLYING.
            updates["application_lease_expires_at"] = now
        else:
            updates["application_claim_id"] = None
            updates["application_lease_expires_at"] = None
        if status == HITLInteractionStatus.APPLIED.value:
            updates["applied_at"] = now
        if status == HITLInteractionStatus.FAILED.value:
            updates.update(
                {
                    "terminal_reason": error,
                    "member_terminal_status": "canceled",
                    "owning_run_terminal_status": "failed",
                    "terminal_reconciled": False,
                }
            )
        query: dict[str, Any] = {
            "interaction_id": interaction_id,
            "status": HITLInteractionStatus.APPLYING.value,
            "application_claim_id": claim_id,
        }
        if status == HITLInteractionStatus.APPLIED.value:
            query["run_projection_status"] = "applied"
        return await self._interactions.find_one_and_update(
            query,
            {"$set": updates, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )

    async def terminalize_interaction(
        self,
        interaction_id: str,
        *,
        expected_statuses: list[str],
        status: str,
        reason: str,
        member_status: str | None = None,
        owning_run_terminal_status: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {
            HITLInteractionStatus.CANCELED.value,
            HITLInteractionStatus.EXPIRED.value,
            HITLInteractionStatus.FAILED.value,
        }:
            raise ValueError("invalid interaction terminal status")
        return await self._interactions.find_one_and_update(
            {
                "interaction_id": interaction_id,
                "status": {"$in": expected_statuses},
            },
            {
                "$set": {
                    "status": status,
                    "terminal_reason": reason,
                    "member_terminal_status": member_status,
                    "owning_run_terminal_status": owning_run_terminal_status,
                    "terminal_reconciled": False,
                    "application_claim_id": None,
                    "application_lease_expires_at": None,
                    "updated_at": utcnow(),
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def mark_interaction_terminal_reconciled(
        self, interaction_id: str, *, version: int
    ) -> bool:
        return await self._interactions.update_one(
            {"interaction_id": interaction_id, "version": version},
            {
                "$set": {"terminal_reconciled": True, "updated_at": utcnow()},
                "$inc": {"version": 1},
            },
        )

    async def iter_materializing_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._interactions.find(
            {"status": HITLInteractionStatus.MATERIALIZING.value},
            sort=[("updated_at", 1), ("interaction_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def iter_due_interactions(
        self, now: datetime, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._interactions.find(
            {
                "status": {
                    "$in": [
                        HITLInteractionStatus.OPEN.value,
                        HITLInteractionStatus.PARTIALLY_ANSWERED.value,
                    ]
                },
                "expires_at": {"$ne": None, "$lte": now},
            },
            sort=[("expires_at", 1), ("interaction_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def iter_stale_applications(
        self, now: datetime, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._interactions.find(
            {
                "$or": [
                    {"status": HITLInteractionStatus.ANSWERS_RECORDED.value},
                    {
                        "status": HITLInteractionStatus.APPLYING.value,
                        "$or": [
                            {"application_lease_expires_at": {"$lte": now}},
                            {"application_lease_expires_at": None},
                            {"application_lease_expires_at": {"$exists": False}},
                        ],
                    },
                    {"status": HITLInteractionStatus.DELIVERY_UNCERTAIN.value},
                    {
                        "status": HITLInteractionStatus.APPLIED.value,
                        "terminal_reconciled": {"$ne": True},
                    },
                ]
            },
            sort=[("updated_at", 1), ("interaction_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def iter_active_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._interactions.find(
            {
                "status": {
                    "$in": [
                        HITLInteractionStatus.OPEN.value,
                        HITLInteractionStatus.PARTIALLY_ANSWERED.value,
                        HITLInteractionStatus.ANSWERS_RECORDED.value,
                        HITLInteractionStatus.APPLYING.value,
                        HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
                    ]
                }
            },
            sort=[("updated_at", 1), ("interaction_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def iter_unreconciled_terminal_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._interactions.find(
            {
                "status": {
                    "$in": [
                        HITLInteractionStatus.CANCELED.value,
                        HITLInteractionStatus.EXPIRED.value,
                        HITLInteractionStatus.FAILED.value,
                    ]
                },
                "terminal_reconciled": {"$ne": True},
            },
            sort=[("updated_at", 1), ("interaction_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def iter_unreconciled_terminal_requests(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._hitl_requests.find(
            {
                "status": {"$in": ["canceled", "expired"]},
                "cancellation_reconciled": {"$ne": True},
            },
            sort=[("created_at", 1), ("request_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def create_resume_command(
        self, command_data: dict[str, Any]
    ) -> dict[str, Any]:
        doc = dict(command_data)
        try:
            await self._resume_commands.insert_one(doc)
            return doc
        except DuplicateKeyError:
            existing = await self.get_resume_command_strict(doc["command_id"])
            if existing is None:
                raise
            immutable = (
                "kind",
                "interaction_id",
                "application_revision",
                "task_id",
                "context_id",
                "continuation_message_id",
                "agent_id",
                "outbound_message_id",
                "orchestration_run_id",
                "answer_request_ids",
                "answer_digest",
            )
            if any(existing.get(key) != doc.get(key) for key in immutable):
                raise ValueError("conflicting HITL resume command metadata") from None
            return existing

    async def get_resume_command_strict(self, command_id: str) -> dict[str, Any] | None:
        return await self._resume_commands.find_one({"command_id": command_id})

    async def get_resume_command_for_interaction_strict(
        self, interaction_id: str, application_revision: int
    ) -> dict[str, Any] | None:
        return await self._resume_commands.find_one(
            {
                "interaction_id": interaction_id,
                "application_revision": application_revision,
            }
        )

    async def claim_resume_command(
        self,
        command_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        now = utcnow()
        return await self._resume_commands.find_one_and_update(
            {
                "command_id": command_id,
                "status": {"$in": sorted(_COMMAND_CLAIMABLE)},
                "$or": [
                    {"next_attempt_at": None},
                    {"next_attempt_at": {"$exists": False}},
                    {"next_attempt_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": HITLResumeCommandStatus.DELIVERING.value,
                    "claim_id": claim_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1, "version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def renew_resume_command(
        self,
        command_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._resume_commands.update_one(
            {
                "command_id": command_id,
                "status": HITLResumeCommandStatus.DELIVERING.value,
                "claim_id": claim_id,
            },
            {
                "$set": {
                    "lease_expires_at": utcnow() + timedelta(seconds=lease_seconds),
                    "updated_at": utcnow(),
                },
                "$inc": {"version": 1},
            },
        )
        return bool(getattr(result, "matched_count", result))

    async def reclaim_stale_resume_command(
        self,
        command_id: str,
        *,
        observed_claim_id: str | None,
        observed_version: int,
        observed_lease_expires_at: datetime,
        now: datetime,
        status: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """CAS a stale DELIVERING scan result without clobbering renewal."""
        updates: dict[str, Any] = {
            "status": status,
            "claim_id": None,
            "lease_expires_at": None,
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": now,
        }
        if status == HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value:
            updates["uncertain_since"] = now
        if retry_after_seconds is not None:
            updates["next_attempt_at"] = now + timedelta(seconds=retry_after_seconds)
        return await self._resume_commands.find_one_and_update(
            {
                "command_id": command_id,
                "status": HITLResumeCommandStatus.DELIVERING.value,
                "claim_id": observed_claim_id,
                "version": observed_version,
                "lease_expires_at": observed_lease_expires_at,
                "$and": [{"lease_expires_at": {"$lte": now}}],
            },
            {"$set": updates, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )

    async def mark_resume_command_state(
        self,
        command_id: str,
        *,
        claim_id: str | None,
        expected_statuses: list[str],
        status: str,
        response_snapshot: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {
            "command_id": command_id,
            "status": {"$in": expected_statuses},
        }
        if claim_id is not None:
            query["claim_id"] = claim_id
        now = utcnow()
        updates: dict[str, Any] = {
            "status": status,
            "claim_id": None,
            "lease_expires_at": None,
            "response_snapshot": response_snapshot,
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": now,
        }
        if status == HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value:
            updates["uncertain_since"] = now
        if retry_after_seconds is not None:
            updates["next_attempt_at"] = now + timedelta(seconds=retry_after_seconds)
        return await self._resume_commands.find_one_and_update(
            query,
            {"$set": updates, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )

    async def mark_resume_command_aggregate_applied(self, command_id: str) -> bool:
        return await self._resume_commands.update_one(
            {
                "command_id": command_id,
                "status": HITLResumeCommandStatus.PROJECTED.value,
                "aggregate_applied_at": {"$exists": False},
            },
            {"$set": {"aggregate_applied_at": utcnow(), "updated_at": utcnow()}},
        )

    async def iter_due_resume_commands(
        self, now: datetime, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        rows = await self._resume_commands.find(
            {
                "$or": [
                    {
                        "status": HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                        "next_attempt_at": {"$lte": now},
                    },
                    {"status": HITLResumeCommandStatus.ACKNOWLEDGED.value},
                    {"status": HITLResumeCommandStatus.PERMANENT_FAILURE.value},
                    {
                        "status": HITLResumeCommandStatus.PROJECTED.value,
                        "aggregate_applied_at": {"$exists": False},
                    },
                    {"status": HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value},
                    {
                        "status": HITLResumeCommandStatus.DELIVERING.value,
                        "lease_expires_at": {"$lte": now},
                    },
                ]
            },
            sort=[("updated_at", 1), ("command_id", 1)],
            limit=limit,
        )
        for row in rows:
            yield row

    async def ensure_indexes(self) -> None:
        interaction_indexes = [
            ([("interaction_id", 1)], {"unique": True}),
            ([("room_id", 1), ("status", 1), ("expires_at", 1)], {}),
            ([("status", 1), ("application_lease_expires_at", 1)], {}),
            ([("orchestration_run_id", 1), ("status", 1)], {}),
        ]
        command_indexes = [
            ([("command_id", 1)], {"unique": True}),
            (
                [("interaction_id", 1), ("application_revision", 1), ("kind", 1)],
                {"unique": True},
            ),
            ([("status", 1), ("next_attempt_at", 1)], {}),
            ([("status", 1), ("lease_expires_at", 1)], {}),
        ]
        for collection, indexes in (
            (self._interactions, interaction_indexes),
            (self._resume_commands, command_indexes),
        ):
            for keys, options in indexes:
                await collection.create_index(keys, **options)
