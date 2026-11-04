"""Snapshot materialization for the room stream (Room Stream Snapshot plan §5).

Snapshots are produced by folding ``room_events[0..N]`` through the same fold
logic the client uses (P1/P4). The fold is incrementally materialized: an
in-memory checkpoint at ``room_seq`` M plus a fold of M+1..N on demand keeps
long rooms cheap. ``force=True`` refolds from the authoritative log (used by
the ``?snapshot=1`` recovery path to rule out checkpoint staleness).
"""

from __future__ import annotations

import asyncio
import copy
import json
import weakref
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from common.dto.delivery import (
    HITLRequestEvent,
    HITLResolvedEvent,
    RunEventNotification,
    TaskSubmittedEvent,
    TaskUpdateEvent,
)
from common.dto.turn_lifecycle import CANONICAL_RUN_EVENT_KINDS
from common.observability import events as E
from common.utils.logger import get_logger
from delivery.room_events import RoomEventStore

logger = get_logger(__name__)

_CHECKPOINT_CAPACITY = 512
_SNAPSHOT_READ_LIMIT = 2000
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "canceled", "rejected", "expired"}
)
_TERMINAL_PROCESSING_STATUSES = frozenset(
    {"completed", "failed", "canceled", "rejected", "rate_limited", "error"}
)


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _specific_agent_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.casefold() in {"agent", "unknown", "unknown agent"}:
        return None
    if name.startswith(("agent_", "binding-", "inv_", "orchestrator:")):
        return None
    return name[:160]


def _patched_agent_name(current: Any, incoming: Any) -> str | None:
    return _specific_agent_name(incoming) or _specific_agent_name(current)


def _hitl_request_key(data: dict[str, Any]) -> str:
    request_id = str(data.get("request_id") or "")
    interaction_id = str(data.get("interaction_id") or request_id)
    return f"{interaction_id}:{request_id}"


class RoomEventFold:
    """Pure fold: room event records → snapshot state (mirrors client folds)."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.hitl_requests: dict[str, dict[str, Any]] = {}
        self.hitl_resolved: list[dict[str, Any]] = []
        self.streaming: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}

    def apply(self, record: dict[str, Any]) -> bool:
        kind = record.get("kind")
        data = record.get("payload_public") or {}
        handler = {
            "processing_status": self._processing_status,
            "agent_response_partial": self._agent_response_partial,
            "agent_response": self._agent_response,
            "task_submitted": self._task_submitted,
            "task_update": self._task_update,
            "artifact_update": self._artifact_update,
            "error": self._error,
            "cancellation": self._cancellation,
            "run_event": self._run_event,
            "hitl_request": self._hitl_request,
            "hitl_response": self._hitl_response,
        }.get(kind)
        if handler is None:
            return True
        accepted = handler(data, record)
        return accepted is not False

    # ── message-level folds ─────────────────────────────────────────────

    def _message(self, message_id: Any) -> dict[str, Any]:
        if not message_id:
            return {}
        key = str(message_id)
        return self.messages.setdefault(
            key,
            {
                "message_id": key,
                "agent_id": None,
                "agent_name": None,
                "content": None,
                "parts": None,
                "related_message_id": None,
                "client_request_id": None,
                "status": None,
                "task_status": None,
                "task_content": None,
                "task_error": None,
                "requires_input": False,
                "requires_auth": False,
                "step_number": None,
                "total_steps": None,
                "created_at": None,
                "ts": None,
                "artifacts": None,
                "status_logs": [],
            },
        )

    def _processing_status(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        message = self._message(data.get("message_id"))
        status = data.get("status")
        if status in _TERMINAL_PROCESSING_STATUSES:
            message["status"] = status
        details = data.get("details")
        if isinstance(details, dict):
            log_message = _first_present(
                details, "message", "status_message", "stage", "description"
            )
            if not isinstance(log_message, str) or not log_message.strip():
                log_message = json.dumps(
                    details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                if log_message == "{}":
                    log_message = None
            if log_message:
                entry = {
                    "message": str(log_message).strip(),
                    "timestamp": str(record.get("ts") or ""),
                }
                turn_phase = details.get("turn_phase")
                if turn_phase in {"collecting", "synthesizing", "terminal"}:
                    entry["turn_phase"] = turn_phase
                message["status_logs"].append(entry)
        self._carry_correlation(message, data)

    def _agent_response_partial(
        self, data: dict[str, Any], record: dict[str, Any]
    ) -> None:
        message_id = str(data.get("message_id") or "")
        if not message_id:
            return
        buffer = self.streaming.setdefault(
            message_id,
            {
                "message_id": message_id,
                "agent_id": None,
                "text": "",
                "artifacts": [],
                "is_complete": False,
                "client_request_id": None,
                "last_chunk": False,
            },
        )
        buffer["agent_id"] = data.get("agent_id") or buffer["agent_id"]
        delta = data.get("content_delta")
        if isinstance(delta, str):
            buffer["text"] = f"{buffer['text']}{delta}"
        buffer["client_request_id"] = (
            _first_present(data, "client_request_id", "correlation_id")
            or buffer["client_request_id"]
        )

    def _agent_response(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        message_id = str(data.get("message_id") or "")
        if not message_id:
            return
        message = self._message(message_id)
        message["agent_id"] = data.get("agent_id")
        message["content"] = data.get("content")
        if data.get("parts") is not None:
            message["parts"] = data["parts"]
        message["ts"] = str(record.get("ts") or "") or message["ts"]
        self._carry_correlation(message, data)
        message["related_message_id"] = (
            data.get("related_message_id") or message["related_message_id"]
        )
        # The terminal commit supersedes any partial buffer.
        self.streaming.pop(message_id, None)
        for turn in self.turns.values():
            final = turn.get("final_answer")
            if not isinstance(final, dict) or final.get("message_id") != message_id:
                continue
            if data.get("client_request_id") == turn.get(
                "client_request_id"
            ) and data.get("related_message_id") == turn.get("user_message_id"):
                durable_text = str(data.get("content") or "")
                if turn.get("final_committed"):
                    # First exact final commit is absorbing. Contradictory
                    # duplicates are protocol violations and never mutate it.
                    if final.get("text") != durable_text:
                        logger.emit(
                            E.CANONICAL_FINAL_CONFLICT_IGNORED,
                            message_id=message_id,
                            run_id=turn.get("run_id"),
                        )
                    continue
                final["text"] = durable_text
                turn["final_committed"] = True

    def _task_submitted(self, data: dict[str, Any], record: dict[str, Any]) -> bool:
        if data.get("run_id") is not None:
            try:
                validated = TaskSubmittedEvent(
                    conversation_id=str(record.get("room_id") or "snapshot"),
                    **{
                        key: value
                        for key, value in data.items()
                        if key
                        not in {
                            "room_seq",
                            "room_event_id",
                            "parent_event_id",
                            "trace_id",
                        }
                    },
                )
            except (TypeError, ValueError):
                return False
            data = validated.model_dump(mode="json", exclude_none=True)
        task_id = str(data.get("task_id") or "")
        message_id = str(data.get("message_id") or "")
        task = self.tasks.setdefault(
            task_id,
            {
                "task_id": task_id,
                "message_id": message_id,
                "agent_name": None,
                "agent_id": None,
                "status": None,
                "requires_input": False,
                "requires_auth": False,
                "content": None,
                "status_message": None,
                "step_number": None,
                "total_steps": None,
                "task_content": None,
                "created_at": None,
                "error": None,
            },
        )
        task["message_id"] = message_id or task["message_id"]
        task["agent_name"] = _patched_agent_name(
            task["agent_name"], data.get("agent_name")
        )
        task["agent_id"] = data.get("agent_id")
        task["status"] = data.get("status")
        task["created_at"] = data.get("created_at") or task["created_at"]
        task["step_number"] = data.get("step_number")
        task["total_steps"] = data.get("total_steps")
        task["task_content"] = data.get("task_content") or task["task_content"]
        message = self._message(message_id)
        message["agent_id"] = data.get("agent_id") or message["agent_id"]
        message["agent_name"] = _patched_agent_name(
            message["agent_name"], data.get("agent_name")
        )
        message["task_status"] = data.get("status")
        message["task_content"] = data.get("task_content")
        message["related_message_id"] = (
            data.get("related_message_id") or message["related_message_id"]
        )
        message["created_at"] = data.get("created_at") or message["created_at"]
        message["step_number"] = data.get("step_number")
        message["total_steps"] = data.get("total_steps")
        self._carry_correlation(message, data)
        run_id = str(data.get("run_id") or "")
        turn = self.turns.get(run_id)
        if run_id and (
            turn is None
            or data.get("client_request_id") != turn.get("client_request_id")
            or data.get("related_message_id") != turn.get("user_message_id")
        ):
            return False
        if turn is not None and message_id not in turn["agent_call_message_ids"]:
            turn["agent_call_message_ids"].append(message_id)
        return True

    def _task_update(self, data: dict[str, Any], record: dict[str, Any]) -> bool:
        if data.get("run_id") is not None:
            try:
                validated = TaskUpdateEvent(
                    conversation_id=str(record.get("room_id") or "snapshot"),
                    **{
                        key: value
                        for key, value in data.items()
                        if key
                        not in {
                            "room_seq",
                            "room_event_id",
                            "parent_event_id",
                            "trace_id",
                        }
                    },
                )
            except (TypeError, ValueError):
                return False
            data = validated.model_dump(mode="json", exclude_none=True)
        message_id = str(data.get("message_id") or "")
        run_id = str(data.get("run_id") or "")
        turn = self.turns.get(run_id)
        if run_id and (
            turn is None
            or data.get("client_request_id") != turn.get("client_request_id")
            or data.get("related_message_id") != turn.get("user_message_id")
        ):
            return False
        status = data.get("status")
        for task in self.tasks.values():
            if message_id and task.get("message_id") == message_id:
                task["status"] = status
                task["agent_name"] = _patched_agent_name(
                    task["agent_name"], data.get("agent_name")
                )
                task["content"] = data.get("content") or task["content"]
                task["error"] = data.get("error") or task["error"]
                task["status_message"] = (
                    data.get("status_message") or task["status_message"]
                )
                task["requires_input"] = bool(
                    data.get("requires_input", task.get("requires_input"))
                )
                task["requires_auth"] = bool(
                    data.get("requires_auth", task.get("requires_auth"))
                )
        message = self._message(message_id)
        message["task_status"] = status
        message["agent_name"] = _patched_agent_name(
            message["agent_name"], data.get("agent_name")
        )
        message["task_error"] = data.get("error") or message["task_error"]
        message["requires_input"] = bool(data.get("requires_input", False))
        message["requires_auth"] = bool(data.get("requires_auth", False))
        if data.get("content") is not None:
            message["content"] = data["content"]
        if data.get("parts") is not None:
            message["parts"] = data["parts"]
        message["related_message_id"] = (
            data.get("related_message_id") or message["related_message_id"]
        )
        message["created_at"] = data.get("created_at") or message["created_at"]
        message["step_number"] = data.get("step_number")
        message["total_steps"] = data.get("total_steps")
        self._carry_correlation(message, data)
        if status in _TERMINAL_TASK_STATUSES:
            self.streaming.pop(message_id, None)
        return True

    def _artifact_update(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        message_id = str(data.get("message_id") or "")
        if not message_id or data.get("last_chunk") is not True:
            return
        artifact = data.get("artifact")
        if not isinstance(artifact, dict):
            return
        buffer = self.streaming.setdefault(
            message_id,
            {
                "message_id": message_id,
                "agent_id": None,
                "text": "",
                "artifacts": [],
                "is_complete": False,
                "client_request_id": None,
                "last_chunk": False,
            },
        )
        buffer["agent_id"] = data.get("agent_id") or buffer["agent_id"]
        is_append = bool(data.get("append"))
        chunks = buffer["artifacts"]
        if not is_append or not chunks:
            chunks.append(artifact)
        else:
            existing = chunks[-1]
            if isinstance(existing, dict):
                merged = dict(existing)
                for part in artifact.get("parts") or []:
                    merged.setdefault("parts", []).append(part)
                chunks[-1] = merged
            else:
                chunks.append(artifact)
        buffer["last_chunk"] = True
        buffer["is_complete"] = True
        buffer["client_request_id"] = (
            _first_present(data, "client_request_id", "correlation_id")
            or buffer["client_request_id"]
        )

    def _error(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        message_id = data.get("message_id")
        if not message_id:
            return
        message = self._message(message_id)
        message["task_error"] = str(data.get("error") or "")
        self._carry_correlation(message, data)

    def _cancellation(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        message_id = str(data.get("message_id") or "")
        if not message_id:
            return
        message = self._message(message_id)
        message["status"] = "canceled"
        message["task_status"] = "canceled"

    def _run_event(self, data: dict[str, Any], record: dict[str, Any]) -> bool:
        # Only canonical kinds carry a projection; any other stored kind is
        # retired history without a snapshot section.
        if data.get("type") not in CANONICAL_RUN_EVENT_KINDS:
            return True
        allowed = {
            "event_id",
            "run_id",
            "seq",
            "type",
            "payload",
            "correlation_id",
            "room_seq",
            "room_event_id",
            "parent_event_id",
            "delivery_id",
            "trace_id",
        }
        if set(data) - allowed:
            raise ValueError("canonical run event contains unknown public fields")
        try:
            validated = RunEventNotification(
                conversation_id=str(record.get("room_id") or "snapshot"),
                event_id=data.get("event_id"),
                delivery_id=data.get("delivery_id"),
                trace_id=data.get("trace_id"),
                run_id=data.get("run_id"),
                seq=data.get("seq"),
                run_event_type=data.get("type"),
                payload=data.get("payload"),
                correlation_id=data.get("correlation_id"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid canonical run event payload") from exc
        data = {**data, "payload": validated.payload.model_dump(mode="json")}
        return self._fold_canonical_turn(
            validated.run_id, validated.run_event_type, data, record
        )

    def _fold_canonical_turn(  # noqa: C901
        self,
        run_id: str,
        sub_type: str,
        data: dict[str, Any],
        record: dict[str, Any],
    ) -> bool:
        payload = data.get("payload") or {}
        correlation_id = data.get("correlation_id")
        room_seq = int(record.get("room_seq") or 0)
        if sub_type == "run_started":
            if run_id in self.turns:
                return False
            if not correlation_id or payload.get("hybro_turn_id") != run_id:
                return False
            self.turns[run_id] = {
                "hybro_turn_id": run_id,
                "run_id": run_id,
                "user_message_id": payload.get("user_message_id"),
                "client_request_id": correlation_id,
                "state": "active",
                "started_at": payload.get("started_at"),
                "settled_at": None,
                "duration_ms": None,
                "terminal_code": None,
                "terminal_summary": None,
                "internal_turns": [],
                "activity": [],
                "current_assistant": None,
                "final_answer": None,
                "final_committed": False,
                "hitl_interactions": [],
                "active_interaction_id": None,
                "agent_call_message_ids": [],
            }
            return True
        turn = self.turns.get(run_id)
        if turn is None or correlation_id != turn.get("client_request_id"):
            return False
        if turn.get("state") in {"completed", "failed", "canceled"}:
            # Final commit/settlement is absorbing. Replayed children and
            # contradictory duplicate settlement records never mutate it.
            return False
        final_answer = turn.get("final_answer")
        if isinstance(final_answer, dict):
            closes_final = sub_type == "turn_end" and payload.get(
                "internal_turn_id"
            ) == final_answer.get("internal_turn_id")
            if not closes_final and sub_type != "run_settled":
                return False
        internal_turn_id = payload.get("internal_turn_id")
        if sub_type == "turn_start":
            if any(item.get("status") == "active" for item in turn["internal_turns"]):
                return False
            turn["internal_turns"].append(
                {
                    "internal_turn_id": internal_turn_id,
                    "attempt": payload.get("attempt"),
                    "message_ids": [],
                    "tool_call_ids": [],
                    "status": "active",
                }
            )
            return True
        internal = next(
            (
                item
                for item in turn["internal_turns"]
                if item.get("internal_turn_id") == internal_turn_id
            ),
            None,
        )
        if (
            sub_type
            in {
                "message_start",
                "message_update",
                "message_end",
                "tool_execution_start",
                "tool_execution_update",
                "tool_execution_end",
                "turn_end",
            }
            and internal is None
        ):
            return False
        if sub_type == "message_start":
            if turn["current_assistant"] is not None:
                return False
            message_id = payload.get("message_id")
            internal["message_ids"].append(message_id)
            turn["current_assistant"] = {
                "message_id": message_id,
                "internal_turn_id": internal_turn_id,
                "text": "",
                "status": "streaming",
                "content_index": 0,
                "next_delta_index": 0,
                "end_offset": 0,
                "order": room_seq,
            }
        elif sub_type == "message_update":
            current = turn.get("current_assistant")
            nested = payload.get("assistant_message_event") or {}
            if not isinstance(current, dict) or current.get(
                "message_id"
            ) != payload.get("message_id"):
                return False
            if (
                nested.get("type") != "text_delta"
                or nested.get("content_index") != current["content_index"]
                or nested.get("delta_index") != current["next_delta_index"]
                or nested.get("start_offset") != current["end_offset"]
            ):
                return False
            delta = str(nested.get("delta") or "")
            if nested.get("end_offset") != current["end_offset"] + len(delta):
                return False
            current["text"] += delta
            current["end_offset"] = nested["end_offset"]
            current["next_delta_index"] += 1
        elif sub_type == "message_end":
            current = turn.get("current_assistant")
            if not isinstance(current, dict) or current.get(
                "message_id"
            ) != payload.get("message_id"):
                return False
            terminal_text = str(payload.get("text") or "")
            if current.get("next_delta_index", 0) > 0 and terminal_text != current.get(
                "text", ""
            ):
                raise ValueError(
                    "canonical message_end contradicts assembled durable deltas"
                )
            current["text"] = terminal_text
            current["status"] = (
                "completed"
                if payload.get("disposition") in {"commentary", "final"}
                else payload.get("disposition")
            )
            disposition = payload.get("disposition")
            if disposition == "commentary":
                if current["text"]:
                    turn["activity"].append(
                        {
                            "kind": "assistant",
                            **{
                                key: value
                                for key, value in current.items()
                                if key
                                not in {
                                    "content_index",
                                    "next_delta_index",
                                    "end_offset",
                                }
                            },
                            "order": room_seq,
                        }
                    )
                turn["current_assistant"] = None
            elif disposition == "final":
                turn["final_answer"] = {
                    "message_id": current["message_id"],
                    "internal_turn_id": internal_turn_id,
                    "text": current["text"],
                    "status": "completed",
                    "order": room_seq,
                }
                turn["current_assistant"] = None
            else:
                if current["text"]:
                    turn["activity"].append(
                        {
                            "kind": "assistant",
                            "message_id": current["message_id"],
                            "internal_turn_id": internal_turn_id,
                            "text": current["text"],
                            "status": disposition,
                            "order": room_seq,
                        }
                    )
                turn["current_assistant"] = None
        elif sub_type == "tool_execution_start":
            call_id = payload.get("tool_call_id")
            if any(
                item.get("kind") == "tool" and item.get("tool_call_id") == call_id
                for item in turn["activity"]
            ):
                return False
            internal["tool_call_ids"].append(call_id)
            execution_kind = payload.get("execution_kind") or "tool"
            target = payload.get("target")
            turn["activity"].append(
                {
                    "kind": "tool",
                    "id": call_id,
                    "internal_turn_id": internal_turn_id,
                    "tool_call_id": call_id,
                    "label": payload.get("tool_name"),
                    "input": payload.get("input") or {},
                    "partial_result": "",
                    "result": None,
                    "is_error": None,
                    "duration_ms": None,
                    "status": "running",
                    "update_index": 0,
                    "execution_kind": execution_kind,
                    "target_name": (
                        target.get("name")
                        if execution_kind == "agent"
                        and isinstance(target, dict)
                        and target.get("name")
                        else None
                    ),
                    "request_summary": payload.get("request_summary") or "",
                    "detail_available": False,
                    "order": room_seq,
                }
            )
        elif sub_type in {"tool_execution_update", "tool_execution_end"}:
            tool = next(
                (
                    item
                    for item in turn["activity"]
                    if item.get("kind") == "tool"
                    and item.get("tool_call_id") == payload.get("tool_call_id")
                ),
                None,
            )
            if tool is None:
                return False
            if sub_type == "tool_execution_update":
                index = int(payload.get("update_index") or 0)
                if index <= int(tool.get("update_index") or 0):
                    return False
                if payload.get("execution_kind") is not None:
                    if payload.get("execution_kind") != tool.get("execution_kind"):
                        return False
                tool["update_index"] = index
                tool["status"] = payload.get("status")
                tool["partial_result"] = payload.get("partial_result") or ""
            else:
                if tool.get("status") in {"completed", "failed", "canceled"}:
                    return False
                if payload.get("execution_kind") is not None:
                    if payload.get("execution_kind") != tool.get("execution_kind"):
                        return False
                tool["status"] = payload.get("outcome")
                tool["result"] = payload.get("result") or ""
                tool["is_error"] = payload.get("is_error")
                tool["duration_ms"] = payload.get("duration_ms")
                tool["detail_available"] = payload.get("detail_available") is True
                for key in ("error_code", "error_message"):
                    if isinstance(payload.get(key), str):
                        tool[key] = payload[key]
        elif sub_type == "model_decision":
            if internal is None:
                return False
            decision_id = str(data.get("event_id") or "")
            if not decision_id:
                return False
            if any(
                item.get("kind") == "decision" and item.get("id") == decision_id
                for item in turn["activity"]
            ):
                return False
            turn["activity"].append(
                {
                    "kind": "decision",
                    "id": decision_id,
                    "internal_turn_id": internal_turn_id,
                    "decision": payload.get("decision"),
                    "agent_label": payload.get("agent_label"),
                    "question_summary": payload.get("question_summary"),
                    "source_summary": payload.get("source_summary"),
                    "reason": payload.get("reason"),
                    "order": room_seq,
                }
            )
        elif sub_type == "turn_end":
            expected_calls = [
                item.get("tool_call_id")
                for item in turn["activity"]
                if item.get("kind") == "tool"
                and item.get("internal_turn_id") == internal_turn_id
            ]
            open_calls = [
                item
                for item in turn["activity"]
                if item.get("kind") == "tool"
                and item.get("internal_turn_id") == internal_turn_id
                and item.get("status") in {"running", "suspended"}
            ]
            expected_message_id = (
                internal.get("message_ids", [])[-1]
                if internal.get("message_ids")
                else None
            )
            if (
                payload.get("tool_call_ids") != expected_calls
                or open_calls
                or payload.get("message_id") != expected_message_id
            ):
                return False
            internal["status"] = payload.get("status")
        elif sub_type == "retry_scheduled":
            if internal is None or internal.get("status") not in {"error", "aborted"}:
                return False
            if any(
                item.get("kind") == "retry" and item.get("id") == data.get("event_id")
                for item in turn["activity"]
            ):
                return False
            turn["activity"].append(
                {
                    "kind": "retry",
                    "id": data.get("event_id"),
                    "internal_turn_id": internal_turn_id,
                    "attempt": payload.get("attempt"),
                    "delay_ms": payload.get("delay_ms"),
                    "error_class": payload.get("error_class"),
                    "order": room_seq,
                }
            )
        elif sub_type == "run_waiting_input":
            interaction = next(
                (
                    item
                    for item in turn["hitl_interactions"]
                    if item.get("interaction_id") == payload.get("interaction_id")
                ),
                None,
            )
            if interaction is None or interaction.get("request_ids") != payload.get(
                "request_ids"
            ):
                return False
            requests = interaction.get("requests") or []
            expected_count = len(requests)
            if sorted(item.get("question_index") for item in requests) != list(
                range(expected_count)
            ) or any(item.get("question_count") != expected_count for item in requests):
                return False
            interaction["requested_at"] = payload.get("requested_at")
            turn["state"] = "awaiting_input"
            turn["active_interaction_id"] = payload.get("interaction_id")
        elif sub_type == "run_resumed":
            if turn.get("active_interaction_id") != payload.get("interaction_id"):
                return False
            interaction = next(
                (
                    item
                    for item in turn["hitl_interactions"]
                    if item.get("interaction_id") == payload.get("interaction_id")
                ),
                None,
            )
            if (
                interaction is None
                or interaction.get("request_ids") != payload.get("resolved_request_ids")
                or any(
                    item.get("status") != "responded"
                    for item in interaction.get("requests") or []
                )
            ):
                return False
            interaction["state"] = "resumed"
            interaction["resumed_at"] = payload.get("resumed_at")
            turn["state"] = "active"
            turn["active_interaction_id"] = None
        elif sub_type == "run_settled":
            open_children = (
                turn.get("current_assistant") is not None
                or turn.get("active_interaction_id") is not None
                or any(
                    item.get("status") == "active" for item in turn["internal_turns"]
                )
                or any(
                    item.get("kind") == "tool"
                    and item.get("status") in {"running", "suspended"}
                    for item in turn["activity"]
                )
            )
            if open_children:
                return False
            internal_turns = turn.get("internal_turns") or []
            settlement_status = payload.get("status")
            if settlement_status == "completed":
                last_internal = internal_turns[-1] if internal_turns else None
                earlier_closed = all(
                    item.get("status") == "completed"
                    or (
                        item.get("status") in {"error", "aborted"}
                        and any(
                            activity.get("kind") == "retry"
                            and activity.get("internal_turn_id")
                            == item.get("internal_turn_id")
                            for activity in turn.get("activity") or []
                        )
                    )
                    for item in internal_turns[:-1]
                )
                if (
                    not turn.get("final_committed")
                    or not isinstance(turn.get("final_answer"), dict)
                    or turn["final_answer"].get("message_id")
                    != payload.get("final_message_id")
                    or last_internal is None
                    or last_internal.get("status") != "completed"
                    or turn["final_answer"].get("internal_turn_id")
                    != last_internal.get("internal_turn_id")
                    or not last_internal.get("message_ids")
                    or last_internal["message_ids"][-1]
                    != turn["final_answer"].get("message_id")
                    or not earlier_closed
                ):
                    return False
            elif internal_turns and internal_turns[-1].get("status") not in {
                "error",
                "aborted",
            }:
                return False
            turn["state"] = settlement_status
            turn["settled_at"] = payload.get("settled_at")
            turn["duration_ms"] = payload.get("duration_ms")
            turn["terminal_code"] = payload.get("failure_code") or payload.get(
                "cancellation_code"
            )
            turn["terminal_summary"] = payload.get("error_summary")
        return True

    def _hitl_request(self, data: dict[str, Any], record: dict[str, Any]) -> bool:
        if data.get("run_id") is not None:
            try:
                HITLRequestEvent(
                    conversation_id=str(record.get("room_id") or "snapshot"),
                    **{
                        key: value
                        for key, value in data.items()
                        if key
                        not in {
                            "room_seq",
                            "room_event_id",
                            "parent_event_id",
                            "trace_id",
                        }
                    },
                )
            except (TypeError, ValueError):
                return False
        request_id = str(data.get("request_id") or "")
        if not request_id:
            return data.get("run_id") is None
        request_key = _hitl_request_key(data)
        run_id = str(data.get("run_id") or "")
        turn = self.turns.get(run_id)
        if turn is None or (
            data.get("client_request_id") != turn.get("client_request_id")
            or data.get("related_user_message_id") != turn.get("user_message_id")
        ):
            if not run_id:
                self.hitl_requests[request_key] = dict(data)
                self.hitl_requests[request_key]["room_seq"] = int(
                    record.get("room_seq") or 0
                )
                self.hitl_requests[request_key]["ts"] = str(record.get("ts") or "")
            return not run_id
        interaction_id = str(data.get("interaction_id") or "")
        if not interaction_id:
            return False
        interaction = next(
            (
                item
                for item in turn["hitl_interactions"]
                if item.get("interaction_id") == interaction_id
            ),
            None,
        )
        if interaction is not None and interaction.get("state") != "awaiting_input":
            # A late or CAS-losing observation can incorrectly re-emit a new
            # questionnaire under an already resumed interaction identity.
            # Without a matching new interaction/control boundary it is not
            # canonical state, so retain the settled inventory and ignore it.
            return True
        if interaction is None:
            interaction = {
                "interaction_id": interaction_id,
                "state": "awaiting_input",
                "request_ids": [],
                "requests": [],
                "requested_at": str(record.get("ts") or ""),
                "resumed_at": None,
            }
            turn["hitl_interactions"].append(interaction)
        if request_id in interaction["request_ids"]:
            return False
        self.hitl_requests[request_key] = dict(data)
        self.hitl_requests[request_key]["room_seq"] = int(record.get("room_seq") or 0)
        self.hitl_requests[request_key]["ts"] = str(record.get("ts") or "")
        interaction["request_ids"].append(request_id)
        interaction["requests"].append(
            {
                "request_id": request_id,
                "message_id": data.get("message_id"),
                "question_index": data.get("question_index"),
                "question_count": data.get("question_count"),
                "prompt": data.get("prompt"),
                "prompt_type": data.get("prompt_type"),
                "choices": data.get("choices") or [],
                "source": data.get("source"),
                "agent_label": data.get("agent_label"),
                "status": "requested",
                "answer_ref": None,
            }
        )
        return True

    def _stored_hitl_request(self, data: dict[str, Any]) -> dict[str, Any] | None:
        request_key = _hitl_request_key(data)
        existing = self.hitl_requests.get(request_key)
        if existing is not None:
            return existing
        request_id = str(data.get("request_id") or "")
        return self.hitl_requests.get(f"{request_id}:{request_id}")

    def _hitl_response(self, data: dict[str, Any], record: dict[str, Any]) -> bool:
        if data.get("run_id") is not None:
            try:
                HITLResolvedEvent(
                    conversation_id=str(record.get("room_id") or "snapshot"),
                    **{
                        key: value
                        for key, value in data.items()
                        if key
                        not in {
                            "room_seq",
                            "room_event_id",
                            "parent_event_id",
                            "trace_id",
                        }
                    },
                )
            except (TypeError, ValueError):
                return False
        request_id = str(data.get("request_id") or "")
        entry = dict(data)
        entry["ts"] = str(record.get("ts") or "")
        self.hitl_resolved.append(entry)
        existing = self._stored_hitl_request(data)
        if request_id and existing is not None:
            existing["status"] = data.get("status")
            existing["interaction_id"] = data.get("interaction_id")
            existing["interaction_status"] = data.get("interaction_status")
            existing["interaction_version"] = data.get("interaction_version")
            existing["application_status"] = data.get("application_status")
        run_id = str(data.get("run_id") or "")
        turn = self.turns.get(run_id)
        if turn is None or (
            data.get("client_request_id") != turn.get("client_request_id")
            or data.get("related_user_message_id") != turn.get("user_message_id")
        ):
            return not run_id
        interaction = next(
            (
                item
                for item in turn["hitl_interactions"]
                if item.get("interaction_id") == data.get("interaction_id")
            ),
            None,
        )
        if interaction is None:
            return False
        request = next(
            (
                item
                for item in interaction["requests"]
                if item.get("request_id") == request_id
            ),
            None,
        )
        if request is None:
            return False
        request["status"] = data.get("status")
        request["answer_ref"] = data.get("answer_ref")
        statuses = {item.get("status") for item in interaction["requests"]}
        if len(interaction["requests"]) == len(interaction["request_ids"]) and all(
            status in {"responded", "expired", "canceled", "error"}
            for status in statuses
        ):
            non_resumable = [
                status
                for status in ("error", "canceled", "expired")
                if status in statuses
            ]
            if non_resumable:
                interaction["state"] = non_resumable[0]
                if turn.get("active_interaction_id") == interaction["interaction_id"]:
                    turn["active_interaction_id"] = None
        return True

    @staticmethod
    def _carry_correlation(message: dict[str, Any], data: dict[str, Any]) -> None:
        client_request_id = _first_present(data, "client_request_id", "correlation_id")
        if client_request_id:
            message["client_request_id"] = client_request_id

    def state(self, *, room_seq: int) -> dict[str, Any]:
        state = {
            "room_seq": room_seq,
            "messages": list(self.messages.values()),
            "tasks": list(self.tasks.values()),
            "hitl": {
                "requests": list(self.hitl_requests.values()),
                "resolved": list(self.hitl_resolved),
            },
            "streaming": self.streaming,
        }
        # Canonical authority is explicit even before the first Run. The
        # frontend never falls back to a second lifecycle/card projection.
        state["turn_lifecycle_schema"] = 1
        state["turns"] = list(self.turns.values())
        return state


class SnapshotService:
    """Incremental materialized snapshot producer over the room event log."""

    def __init__(
        self,
        *,
        store: RoomEventStore,
        capacity: int = _CHECKPOINT_CAPACITY,
        read_limit: int = _SNAPSHOT_READ_LIMIT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._capacity = capacity
        self._read_limit = read_limit
        self._now = now or (lambda: datetime.now())
        # conversation_id → (watermark room_seq, folded state dict)
        self._checkpoints: OrderedDict[str, tuple[int, RoomEventFold]] = OrderedDict()
        self._room_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def snapshot(  # noqa: C901
        self, conversation_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        lock = self._room_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            return await self._snapshot_locked(conversation_id, force=force)

    async def _snapshot_locked(  # noqa: C901
        self, conversation_id: str, *, force: bool
    ) -> dict[str, Any]:
        watermark = 0
        fold = RoomEventFold()
        cached = self._checkpoints.get(conversation_id)
        if not force and cached is not None:
            watermark, cached_fold = cached
            # Never mutate the authoritative checkpoint until every new event
            # has folded successfully. A rejected canonical event therefore
            # leaves recovery anchored at the last valid watermark.
            fold = copy.deepcopy(cached_fold)

        expected = watermark + 1
        folded_any = False
        reached_gap = False
        while True:
            records = await self._store.read_range(
                conversation_id,
                after=expected - 1,
                limit=self._read_limit,
                include_skipped=True,
            )
            if not records:
                break
            for record in records:
                room_seq = int(record.get("room_seq") or 0)
                if room_seq < expected:
                    continue
                if room_seq > expected:
                    # Contiguous-prefix rule: never include an event above a
                    # missing sequence. A later request can advance after the
                    # store heals the hole with a skipped tombstone.
                    reached_gap = True
                    break
                if record.get("kind") != "skipped":
                    # ``apply`` deliberately surfaces canonical contract/fold
                    # violations; expected is advanced only after success.
                    if not fold.apply(record):
                        raise ValueError(f"canonical fold rejected room_seq={room_seq}")
                    folded_any = True
                expected += 1
            if reached_gap or len(records) < self._read_limit:
                break
        watermark = expected - 1

        # A force-refold can race external log visibility but must never
        # regress an already-authoritative checkpoint or returned snapshot.
        if cached is not None and watermark < cached[0]:
            cached_watermark, cached_fold = cached
            return cached_fold.state(room_seq=cached_watermark)
        if force or folded_any or watermark > 0:
            self._remember(conversation_id, watermark, fold)
        return fold.state(room_seq=watermark)

    async def latest_seq(self, conversation_id: str) -> int:
        return await self._store.latest_seq(conversation_id)

    def _remember(
        self, conversation_id: str, watermark: int, fold: RoomEventFold
    ) -> None:
        self._checkpoints[conversation_id] = (watermark, fold)
        self._checkpoints.move_to_end(conversation_id)
        while len(self._checkpoints) > self._capacity:
            self._checkpoints.popitem(last=False)


__all__ = ["RoomEventFold", "SnapshotService"]
