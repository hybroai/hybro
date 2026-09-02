"""Concrete Mongo projection side effects for the orchestrator outbox.

These projectors are bound into ``ProjectionOutboxWorker`` by the production
composition root. Each ``project`` call is idempotent: exact-winner re-reads and
unique indexes make crash replay harmless.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from execution.orchestrator.models import (
    AssistantMessage,
    DataPart,
    OrchestratorEvent,
    OrchestratorRunState,
    ProjectionIntent,
    TextPart,
)
from execution.orchestrator.ports import StoreOutcome
from models.room import CoordinatorAgentId
from models.run import TERMINAL_RUN_STATES, RunState
from room.timeline import normalize_timeline_document

_TERMINAL_RUN_STATE_VALUES = [state.value for state in TERMINAL_RUN_STATES]
FinalMessageDelivery = Callable[
    [OrchestratorRunState, AssistantMessage, str], Awaitable[bool]
]
FinalMessageMemoryProjection = Callable[[str, str], Awaitable[dict[str, Any] | None]]


class MongoAppendEventProjector:
    """Project an ``append_orchestrator_event`` intent into the event store."""

    def __init__(self, event_store: Any) -> None:
        self.event_store = event_store

    async def project(
        self, intent: ProjectionIntent, run: OrchestratorRunState
    ) -> StoreOutcome:
        del run
        event = OrchestratorEvent.model_validate(intent.payload)
        return await self.event_store.append(event)


class MongoFinalMessageProjector:
    """Deliver the terminal assistant message into ``room_agent_messages``."""

    def __init__(
        self,
        messages: Any,
        delivery: FinalMessageDelivery | None = None,
        memory_projection: FinalMessageMemoryProjection | None = None,
    ) -> None:
        self.messages = messages
        self.delivery = delivery
        self.memory_projection = memory_projection

    async def project(
        self, intent: ProjectionIntent, run: OrchestratorRunState
    ) -> StoreOutcome:
        message_id = intent.payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return "conflict"
        final = _final_assistant_message(run, message_id)
        if final is None:
            return "conflict"
        # The projected document is a minimal pass-through shape
        # (message_text + run correlation). Step 7's E2E must verify the
        # frontend/terminal-state consumers read this shape; enrich with the
        # full message_task surface only if a consumer requires it.
        document = _final_message_document(run, final)
        outcome: StoreOutcome = "accepted"
        existing = await self.messages.find_one({"message_id": message_id})
        if existing is not None:
            if existing.get("room_id") != run.room_id:
                return "conflict"
            outcome = "replayed"
        else:
            try:
                await self.messages.insert_one(document)
            except DuplicateKeyError:
                existing = await self.messages.find_one({"message_id": message_id})
                if existing is None or existing.get("room_id") != run.room_id:
                    return "conflict"
                outcome = "replayed"
        if self.delivery is not None:
            delivered = await self.delivery(run, final, _assistant_text(final))
            if not delivered:
                return "error"
        if not await self._project_memory(run.room_id, final.message_id):
            return "error"
        return outcome

    async def _project_memory(self, room_id: str, message_id: str) -> bool:
        if self.memory_projection is None:
            return True
        projected = await self.memory_projection(room_id, message_id)
        return not isinstance(projected, dict) or bool(
            projected.get("projected") or projected.get("reason") == "duplicate"
        )


class MongoTerminalRunStatusProjector:
    """Project a terminal status into the public ``runs`` collection.

    The public collection is the legacy UI surface keyed by the originating
    user message id (``run_id`` == user message id). The preflight already
    created that row in ``processing``; converging it here keeps the room's
    active-run gate and the UI timeline on one row per turn instead of
    leaking a second, orchestrator-keyed row that leaves the room stuck in
    ``processing``.
    """

    def __init__(self, runs: Any, messages: Any | None = None) -> None:
        self.runs = runs
        self.messages = messages

    async def project(
        self, intent: ProjectionIntent, run: OrchestratorRunState
    ) -> StoreOutcome:
        status = intent.payload.get("status")
        state = _run_state_for_status(status)
        if state is None:
            return "conflict"
        public_run_id = run.request.user_message_id or run.run_id
        now = datetime.now(UTC)
        document: dict[str, Any] = {
            "run_id": public_run_id,
            "room_id": run.room_id,
            "state": state.value,
            "trigger_message_id": public_run_id,
            "client_request_id": run.client_request_id,
            "seq": 0,
            "created_at": run.created_at,
            "updated_at": now,
            "ended_at": now,
        }
        if state == RunState.FAILED:
            document["error_code"] = (
                "BUDGET_EXHAUSTED" if status == "budget_exhausted" else "FAILED"
            )
            document["error_message"] = run.terminal_reason
        already_terminal = False
        try:
            await self.runs.update_one(
                {
                    "run_id": public_run_id,
                    "state": {"$nin": _TERMINAL_RUN_STATE_VALUES},
                },
                {"$set": document},
                upsert=True,
            )
        except DuplicateKeyError:
            already_terminal = True
        existing = await self.runs.find_one({"run_id": public_run_id})
        if existing is None:
            return "conflict"
        if existing.get("state") != state.value:
            return "conflict"
        if self.messages is not None:
            await _repair_terminal_agent_cards(self.messages, run)
        return "replayed" if already_terminal else "accepted"


async def _repair_terminal_agent_cards(
    messages: Any,
    run: OrchestratorRunState,
) -> None:
    """Converge live compatibility cards from the durable terminal Run.

    The real-time lifecycle projection is only a latency optimization. This
    outbox-owned repair makes a checkpoint/crash or transient listener failure
    replay-safe and prevents a durable ``working`` card after the Run settles.
    """
    state_map = {
        "completed": "completed",
        "failed": "failed",
        "canceled": "canceled",
        "rejected": "rejected",
        "expired": "expired",
    }
    terminal_at = run.updated_at
    lifecycle_family = getattr(run, "lifecycle_family", "legacy")
    unresolved_state = "canceled" if run.status == "canceled" else "failed"
    for batch in run.tool_batches:
        for entry in batch.entries:
            result = entry.buffered_terminal_result
            state = (
                state_map.get(result.status, "failed")
                if result is not None
                else unresolved_state
            )
            updates: dict[str, Any] = {
                "message_content.message_task.status.state": state,
                "message_content.message_task.status.timestamp": (
                    terminal_at.isoformat()
                ),
                "task_updated_at": terminal_at,
            }
            if result is not None and lifecycle_family == "legacy":
                rendered_parts = [
                    part.text
                    if isinstance(part, TextPart)
                    else json.dumps(
                        part.data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                    for part in result.content
                    if isinstance(part, (TextPart, DataPart))
                ]
                text = "\n".join(part for part in rendered_parts if part).strip()
                if text:
                    updates["message_content.message_text"] = text
            public_call_id = (
                entry.opaque_public_call_id
                if lifecycle_family == "canonical"
                else entry.call_id
            )
            if not public_call_id:
                continue
            await messages.update_one(
                {
                    "room_id": run.room_id,
                    "message_id": f"orchestrator:{run.run_id}:{public_call_id}",
                    "extend_info.orchestrator_run_id": run.run_id,
                },
                {"$set": updates},
                upsert=False,
            )


def _final_assistant_message(
    run: OrchestratorRunState, message_id: str
) -> AssistantMessage | None:
    return next(
        (
            message
            for message in run.transcript
            if isinstance(message, AssistantMessage)
            and message.message_id == message_id
        ),
        None,
    )


def _assistant_text(message: AssistantMessage) -> str:
    parts = [
        part.text
        for part in message.content
        if isinstance(part, TextPart) and part.text
    ]
    return "".join(parts).strip()


def _final_message_document(
    run: OrchestratorRunState, final: AssistantMessage
) -> dict[str, Any]:
    return normalize_timeline_document(
        {
            "room_id": run.room_id,
            "message_id": final.message_id,
            "message_type": "agent",
            "agent_id": CoordinatorAgentId.SYSTEM_HYBRO.value,
            "message_content": {"message_text": _assistant_text(final)},
            "message_created_at": final.created_at,
            "client_request_id": run.client_request_id,
            "related_message_id": run.request.user_message_id,
            "run_id": run.run_id,
            "extend_info": {
                "orchestrator_run_id": run.run_id,
                "terminal_reason": run.terminal_reason,
            },
        }
    )


def _run_state_for_status(status: object) -> RunState | None:
    if status == "completed":
        return RunState.COMPLETED
    if status == "canceled":
        return RunState.CANCELED
    if status in {"failed", "budget_exhausted"}:
        return RunState.FAILED
    return None


__all__ = [
    "MongoAppendEventProjector",
    "MongoFinalMessageProjector",
    "MongoTerminalRunStatusProjector",
]
