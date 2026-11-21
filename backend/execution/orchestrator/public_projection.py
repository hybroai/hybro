"""Public decision-visibility projection for the room SSE stream.

Decision-visibility projection that translates private kernel lifecycle
``SessionEvent`` values into public ``run_event`` payload types carried over the
existing SSE ``run_event`` frame.

The payloads produced here are the only public surface for decisions, LLM
calls, retries, and tool calls. Raw system/user prompts, full tool arguments,
and reasoning text never leave this module: public payloads carry backend-
generated short summaries and metadata only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from common.dto.turn_lifecycle import CANONICAL_RUN_EVENT_KINDS

from .lifecycle import SessionEvent
from .models import FrozenToolCatalogSnapshot, OrchestratorRunState
from .public_summaries import PublicSummaryRegistry
from .public_text import enforce_public_label_policy, sanitize_public_text

PUBLIC_RUN_EVENT_KINDS = CANONICAL_RUN_EVENT_KINDS

_DEFAULT_SUMMARY_LIMIT = 240


@dataclass(frozen=True, slots=True)
class PublicRunEvent:
    """A ready-to-emit public decision-visibility event.

    ``kind`` is one of :data:`PUBLIC_RUN_EVENT_KINDS` and becomes the
    ``RunEventNotification.run_event_type`` over the wire.
    """

    kind: str
    event_id: str
    run_id: str
    seq: int
    conversation_id: str
    user_message_id: str | None
    client_request_id: str | None
    payload: dict[str, Any]


def canonical_settlement_payload(run: OrchestratorRunState) -> dict[str, Any]:
    """Map a settled aggregate to the closed public settlement payload."""
    settled_at = run.updated_at
    payload: dict[str, Any] = {
        "status": "failed" if run.status == "budget_exhausted" else run.status,
        "started_at": run.created_at,
        "settled_at": settled_at,
        "duration_ms": max(
            0, int((settled_at - run.created_at).total_seconds() * 1000)
        ),
    }
    if run.status == "completed":
        payload["final_message_id"] = run.proposed_final_message_id
    elif run.status == "canceled":
        payload["cancellation_code"] = run.cancellation_cause
        if run.cancellation_cause == "policy" and run.denied_rule_id:
            payload["rule_id"] = run.denied_rule_id
    else:
        payload["failure_code"] = (
            "budget_exhausted" if run.status == "budget_exhausted" else "internal_error"
        )
        payload["error_summary"] = (
            "The run exhausted its execution budget."
            if run.status == "budget_exhausted"
            else "The run could not be completed."
        )
    return payload


class PublicProjectionTranslator:
    """SessionEvent → public decision-visibility payloads (sole public writer)."""

    def __init__(
        self,
        *,
        summary_limit: int = _DEFAULT_SUMMARY_LIMIT,
        summary_registry: PublicSummaryRegistry | None = None,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self._summary_limit = summary_limit
        self._summary_registry = summary_registry or PublicSummaryRegistry()
        self._secret_values = secret_values

    def _public_label(self, value: Any) -> str:
        return enforce_public_label_policy(value, secret_values=self._secret_values)

    def _execution_identity(
        self,
        raw: dict[str, Any],
        *,
        catalog: FrozenToolCatalogSnapshot | None,
    ) -> tuple[str, dict[str, str | None] | None, str]:
        """Resolve execution kind, safe target, and public trace label.

        A frozen catalog entry identifies an A2A Agent Execution; every other
        invocation (e.g. structured system actions) is a plain Tool Execution.
        Registry ids never enter public payloads; only the base Agent name and
        the opaque public call id are exposed.
        """
        raw_tool_name = str(raw.get("tool_name") or "")
        entry = next(
            (
                item
                for item in (catalog.entries if catalog is not None else [])
                if item.definition.name == raw_tool_name
            ),
            None,
        )
        trace_label = self._public_label(
            raw.get("agent_label") or raw_tool_name or "tool"
        )
        if entry is None:
            return "tool", None, trace_label
        base_name = self._public_label(
            entry.agent_display_name or entry.definition.label.strip() or trace_label
        )
        return "agent", {"name": base_name, "source": None}, trace_label

    def translate(
        self,
        event: SessionEvent,
        *,
        catalog: FrozenToolCatalogSnapshot | None = None,
    ) -> PublicRunEvent | None:
        raw = event.payload or {}
        kind = {
            "run_started": "run_started",
            "turn_started": "turn_start",
            "message_started": "message_start",
            "message_updated": "message_update",
            "message_completed": "message_end",
            "tool_execution_started": "tool_execution_start",
            "tool_execution_updated": "tool_execution_update",
            "tool_execution_completed": "tool_execution_end",
            "turn_completed": "turn_end",
            "model_retry_scheduled": "retry_scheduled",
            "model_decision": "model_decision",
        }.get(event.event_type)
        if kind is None:
            return None
        payload = self._canonical_payload(kind, event, raw, catalog=catalog)
        if payload is None:
            return None
        semantic = str(
            _canonical_semantic_id(kind, event, raw)
            if kind == "run_started"
            else raw.get("public_event_id") or _canonical_semantic_id(kind, event, raw)
        )
        return PublicRunEvent(
            kind=kind,
            event_id=f"public:{event.run_id}:{kind}:{semantic}",
            run_id=event.run_id,
            seq=event.sequence,
            conversation_id=event.conversation_id or "",
            user_message_id=event.user_message_id or event.causation_id,
            client_request_id=event.client_request_id,
            payload=payload,
        )

    def _canonical_payload(  # noqa: C901
        self,
        kind: str,
        event: SessionEvent,
        raw: dict[str, Any],
        *,
        catalog: FrozenToolCatalogSnapshot | None,
    ) -> dict[str, Any] | None:
        internal_turn_id = str(raw.get("internal_turn_id") or "")
        message_id = str(raw.get("message_id") or "")
        if kind == "run_started":
            return {
                "hybro_turn_id": event.run_id,
                "user_message_id": event.user_message_id or event.causation_id,
                "started_at": raw.get("started_at") or event.timestamp,
                "mode": str(raw.get("mode") or "supervisor"),
            }
        if kind == "turn_start":
            return {
                "internal_turn_id": internal_turn_id,
                "attempt": _as_int(raw.get("attempt")) or 1,
            }
        if kind == "message_start":
            return {
                "internal_turn_id": internal_turn_id,
                "message_id": message_id,
                "role": "assistant",
            }
        if kind == "message_update":
            return {
                "internal_turn_id": internal_turn_id,
                "message_id": message_id,
                "assistant_message_event": {
                    "type": "text_delta",
                    "content_index": _as_int(raw.get("content_index")) or 0,
                    "delta_index": _as_int(raw.get("delta_index")) or 0,
                    "start_offset": _as_int(raw.get("start_offset")) or 0,
                    "end_offset": _as_int(raw.get("end_offset")) or 0,
                    "delta": str(raw.get("delta") or ""),
                },
            }
        if kind == "message_end":
            if raw.get("message_kind") == "tool_result":
                return None
            payload = {
                "internal_turn_id": internal_turn_id,
                "message_id": message_id,
                "stop_reason": raw.get("stop_reason"),
                "disposition": raw.get("disposition"),
                "text": str(raw.get("text") or ""),
            }
            if raw.get("error_summary") is not None:
                payload["error_summary"] = str(raw["error_summary"])
            return payload
        if kind == "tool_execution_start":
            execution_kind, target, tool_name = self._execution_identity(
                raw, catalog=catalog
            )
            return {
                "internal_turn_id": internal_turn_id,
                "tool_call_id": str(raw.get("public_call_id") or ""),
                "tool_name": tool_name,
                "input": self._summary_registry.input_summary(
                    str(raw.get("tool_name") or ""),
                    raw.get("arguments"),
                    catalog=catalog,
                ),
                "execution_kind": execution_kind,
                **({} if target is None else {"target": target}),
                "request_summary": _public_request_summary(
                    raw.get("arguments"), self._summary_limit
                ),
            }
        if kind == "tool_execution_update":
            execution_kind, target, tool_name = self._execution_identity(
                raw, catalog=catalog
            )
            return {
                "internal_turn_id": internal_turn_id,
                "tool_call_id": str(raw.get("public_call_id") or ""),
                "tool_name": tool_name,
                "update_index": _as_int(raw.get("update_index")) or 1,
                "status": raw.get("status"),
                "partial_result": self._summary_registry.result_summary(
                    str(raw.get("tool_name") or ""),
                    raw.get("partial_result"),
                    catalog=catalog,
                ),
                "execution_kind": execution_kind,
                **({} if target is None else {"target": target}),
            }
        if kind == "tool_execution_end":
            private_status = str(raw.get("result_status") or raw.get("status") or "")
            outcome = {
                "completed": "completed",
                "canceled": "canceled",
                "rejected": "failed",
                "expired": "failed",
            }.get(private_status, "failed")
            failure_reason = (
                private_status
                if private_status in {"rejected", "expired"}
                else ("execution" if outcome == "failed" else None)
            )
            execution_kind, target, tool_name = self._execution_identity(
                raw, catalog=catalog
            )
            payload = {
                "internal_turn_id": internal_turn_id,
                "tool_call_id": str(raw.get("public_call_id") or ""),
                "tool_name": tool_name,
                "outcome": outcome,
                "result": (
                    ""
                    if outcome == "canceled" or execution_kind == "agent"
                    else self._summary_registry.result_summary(
                        str(raw.get("tool_name") or ""),
                        raw.get("result_text"),
                        catalog=catalog,
                    )
                ),
                "is_error": outcome == "failed",
                "duration_ms": _as_int(raw.get("duration_ms")) or 0,
                "execution_kind": execution_kind,
                **({} if target is None else {"target": target}),
                "detail_available": (
                    execution_kind == "agent" and outcome == "completed"
                ),
            }
            if failure_reason is not None:
                payload["failure_reason"] = failure_reason
            if outcome == "failed":
                payload.update(_public_agent_error(raw))
            return payload
        if kind == "turn_end":
            return {
                "internal_turn_id": internal_turn_id,
                "message_id": message_id or None,
                "tool_call_ids": list(raw.get("tool_call_ids") or []),
                "status": str(raw.get("status") or "completed"),
            }
        if kind == "retry_scheduled":
            private_error = str(raw.get("error_class") or "provider_error")
            error_class = {
                "timeout": "provider_timeout",
                "content_filter": "content_filter",
                "assembly_error": "assembly_error",
                "tool_failure": "tool_failure",
                "process_restart": "process_restart",
            }.get(private_error, "provider_error")
            return {
                "internal_turn_id": internal_turn_id,
                "attempt": max(2, _as_int(raw.get("attempt")) or 2),
                "delay_ms": _as_int(raw.get("retry_delay_ms")) or 0,
                "error_class": error_class,
            }
        if kind == "model_decision":
            payload = {
                "internal_turn_id": internal_turn_id,
                "decision": raw.get("decision"),
            }
            agent_label = raw.get("agent_label")
            if agent_label is not None:
                payload["agent_label"] = self._public_label(agent_label)
            question_summary = raw.get("question_summary")
            if question_summary is not None:
                payload["question_summary"] = sanitize_public_text(
                    question_summary, secret_values=self._secret_values
                )[:1_000]
            source_summary = raw.get("source_summary")
            if source_summary is not None:
                payload["source_summary"] = sanitize_public_text(
                    source_summary, secret_values=self._secret_values
                )[:1_000]
            reason = raw.get("reason")
            if reason is not None:
                payload["reason"] = sanitize_public_text(
                    reason, secret_values=self._secret_values
                )[:1_000]
            return payload
        return None


def _canonical_semantic_id(  # noqa: C901
    kind: str, event: SessionEvent, raw: dict[str, Any]
) -> str:
    """Restart-stable identity for every canonical lifecycle boundary."""

    turn_id = str(raw.get("internal_turn_id") or "root")
    message_id = str(raw.get("message_id") or "none")
    call_id = str(raw.get("public_call_id") or raw.get("call_id") or "none")
    if kind == "run_started":
        return f"{event.run_id}:run_started"
    if kind == "turn_start":
        return f"{turn_id}:turn_start:{_as_int(raw.get('attempt')) or 1}"
    if kind == "message_start":
        return f"{turn_id}:{message_id}:message_start"
    if kind == "message_update":
        return str(
            raw.get("public_event_id")
            or f"{turn_id}:{message_id}:update:{raw.get('start_offset')}:{raw.get('end_offset')}"
        )
    if kind == "message_end":
        return f"{turn_id}:{message_id}:message_end"
    if kind == "tool_execution_start":
        return f"{turn_id}:{call_id}:tool_start"
    if kind == "tool_execution_update":
        return (
            f"{turn_id}:{call_id}:tool_update:{_as_int(raw.get('update_index')) or 0}"
        )
    if kind == "tool_execution_end":
        return f"{turn_id}:{call_id}:tool_end"
    if kind == "turn_end":
        return f"{turn_id}:turn_end:{raw.get('status') or 'completed'}"
    if kind == "retry_scheduled":
        return f"{turn_id}:retry:{_as_int(raw.get('attempt')) or 2}"
    if kind == "model_decision":
        content = (
            f"{raw.get('decision')}:"
            f"{raw.get('agent_label') or ''}:"
            f"{raw.get('question_summary') or ''}"
        )
        digest = hashlib.sha256(sanitize_public_text(content).encode()).hexdigest()[:16]
        return f"{turn_id}:decision:{digest}"
    raise ValueError(f"unsupported canonical kind {kind}")


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_PUBLIC_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")


def _public_agent_error(raw: dict[str, Any]) -> dict[str, str]:
    """Expose a failed call's own error code and sanitized short message."""
    code = raw.get("result_error_code")
    if not isinstance(code, str) or not _PUBLIC_ERROR_CODE.fullmatch(code):
        return {}
    details = {"error_code": code}
    message = raw.get("result_error_message")
    if isinstance(message, str) and message.strip():
        details["error_message"] = sanitize_public_text(message.strip())[:500]
    return details


def _public_request_summary(arguments: Any, limit: int) -> str:
    """Extract the safe Agent Card request description from tool arguments.

    Agent calls carry the model-authored ``task`` question. Anything that is
    not a short, plain string closes to an empty summary rather than exposing
    raw argument material.
    """
    if not isinstance(arguments, dict):
        return ""
    task = arguments.get("task")
    if not isinstance(task, str):
        return ""
    text = task.strip()
    if not text:
        return ""
    return sanitize_public_text(text)[:limit]


__all__ = [
    "PUBLIC_RUN_EVENT_KINDS",
    "PublicProjectionTranslator",
    "PublicRunEvent",
]
