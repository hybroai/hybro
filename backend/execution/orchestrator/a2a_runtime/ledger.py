"""Pure lifecycle and correlation rules for the external A2A call ledger."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from ..models import ToolResult
from .models import (
    ACTIVE_AGENT_CALL_STATES,
    AGENT_CALL_STATES,
    TERMINAL_AGENT_CALL_STATES,
    A2AOwnershipAlias,
    AgentCallLedgerRecord,
    AgentCallState,
    NormalizedA2AObservation,
)

AGENT_CALL_TRANSITIONS: dict[AgentCallState, frozenset[AgentCallState]] = {
    "accepted": frozenset(
        {"ready_to_dispatch", "cancel_pending", "rejected", "expired"}
    ),
    "ready_to_dispatch": frozenset(
        {"dispatching", "cancel_pending", "rejected", "expired"}
    ),
    "dispatching": frozenset(
        {
            "ready_to_dispatch",
            "delivery_uncertain",
            "working",
            "continuation_pending",
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "cancel_pending",
        }
    ),
    "delivery_uncertain": frozenset(
        {
            "dispatching",
            "working",
            "continuation_pending",
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "cancel_pending",
        }
    ),
    "working": frozenset(
        {
            "continuation_pending",
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "cancel_pending",
        }
    ),
    "continuation_pending": frozenset(
        {
            "input_required",
            "auth_required",
            "resuming",
            "failed",
            "canceled",
            "expired",
            "cancel_pending",
        }
    ),
    "input_required": frozenset(
        {"resuming", "failed", "canceled", "expired", "cancel_pending"}
    ),
    "auth_required": frozenset(
        {"resuming", "failed", "canceled", "rejected", "expired", "cancel_pending"}
    ),
    "resuming": frozenset(
        {
            "delivery_uncertain",
            "working",
            "continuation_pending",
            "completed",
            "failed",
            "canceled",
            "rejected",
            "expired",
            "cancel_pending",
        }
    ),
    "cancel_pending": frozenset({"canceled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}


class IllegalAgentCallTransition(ValueError):
    pass


class ConflictingTerminalObservation(ValueError):
    def __init__(self, *, persisted_digest: str, conflicting_digest: str) -> None:
        super().__init__("terminal observation conflicts with persisted winner")
        self.persisted_digest = persisted_digest
        self.conflicting_digest = conflicting_digest


def is_legal_agent_call_transition(
    from_state: AgentCallState, to_state: AgentCallState
) -> bool:
    return to_state in AGENT_CALL_TRANSITIONS[from_state]


def validate_agent_call_transition(
    from_state: AgentCallState, to_state: AgentCallState
) -> None:
    if not is_legal_agent_call_transition(from_state, to_state):
        raise IllegalAgentCallTransition(
            f"illegal AgentCall transition: {from_state} -> {to_state}"
        )


def transition_call(
    record: AgentCallLedgerRecord,
    *,
    to_state: AgentCallState,
    updated_at: datetime,
    **updates: object,
) -> AgentCallLedgerRecord:
    validate_agent_call_transition(record.state, to_state)
    terminal = to_state in TERMINAL_AGENT_CALL_STATES
    if terminal and "terminal_at" not in updates:
        updates["terminal_at"] = updated_at
    if terminal:
        updates.update(
            claim_owner=None,
            claim_expires_at=None,
            next_attempt_at=None,
        )
    if not terminal:
        updates["terminal_at"] = None
    return record.model_copy(
        update={
            **updates,
            "state": to_state,
            "state_version": record.state_version + 1,
            "updated_at": updated_at,
        }
    )


def bind_authoritative_aliases(
    record: AgentCallLedgerRecord,
    *,
    task_id: str | None,
    context_id: str | None,
) -> list[A2AOwnershipAlias]:
    aliases = list(record.ownership_aliases)
    scope = record.endpoint_scope_digest
    for kind, value in (("task", task_id), ("context", context_id)):
        if not value or value.startswith(
            ("relay-pending-", "pending-", "provisional-")
        ):
            continue
        alias = A2AOwnershipAlias(kind=kind, value=value, binding_scope=scope)
        same_kind = next((item for item in aliases if item.kind == kind), None)
        if same_kind is not None and same_kind != alias:
            raise ValueError(f"authoritative {kind} alias already bound")
        if same_kind is None:
            aliases.append(alias)
    return aliases


def apply_observation(
    record: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
    *,
    recent_limit: int,
) -> AgentCallLedgerRecord:
    if observation.observation_id in record.recent_observation_ids:
        return record
    if record.state in TERMINAL_AGENT_CALL_STATES:
        if observation.event_kind != "terminal":
            return record
        conflicting = _observation_result_digest(record, observation)
        if conflicting == record.terminal_result_digest:
            return record
        raise ConflictingTerminalObservation(
            persisted_digest=record.terminal_result_digest or "missing",
            conflicting_digest=conflicting,
        )
    if record.state == "cancel_pending" and not (
        observation.event_kind == "terminal" and observation.status == "canceled"
    ):
        return record
    if observation.event_kind in {"working", "artifact"} and record.state in {
        "accepted",
        "ready_to_dispatch",
    }:
        return record

    aliases = bind_authoritative_aliases(
        record, task_id=observation.task_id, context_id=observation.context_id
    )
    recent = [*record.recent_observation_ids, observation.observation_id][
        -recent_limit:
    ]
    common: dict[str, object] = {
        "ownership_aliases": aliases,
        "ownership_alias_keys": ownership_alias_keys(aliases),
        "a2a_task_id": observation.task_id or record.a2a_task_id,
        "a2a_context_id": observation.context_id or record.a2a_context_id,
        "recent_observation_ids": recent,
        "latest_observation_cursor": observation.cursor
        or record.latest_observation_cursor,
    }
    if observation.event_kind in {"input_required", "auth_required"}:
        return transition_call(
            record,
            to_state="continuation_pending",
            updated_at=observation.observed_at,
            pending_interaction_id=None,
            interaction_revision=None,
            interaction_fingerprint=None,
            answer_applied=None,
            continuation_command=None,
            continuation_state=None,
            continuation_attempts=0,
            authorization_refresh_attempts=0,
            next_attempt_at=None,
            **common,
        )
    if observation.event_kind in {"working", "artifact"}:
        if record.state in {"dispatching", "delivery_uncertain", "resuming"}:
            return transition_call(
                record,
                to_state="working",
                updated_at=observation.observed_at,
                artifact_refs=list(
                    dict.fromkeys([*record.artifact_refs, *observation.artifact_refs])
                ),
                **common,
            )
        return record.model_copy(
            update={
                **common,
                "artifact_refs": list(
                    dict.fromkeys([*record.artifact_refs, *observation.artifact_refs])
                ),
                "state_version": record.state_version + 1,
                "updated_at": observation.observed_at,
            }
        )

    assert observation.status is not None
    result = _observation_result(record, observation)
    digest = sha256(result.model_dump_json().encode()).hexdigest()
    return transition_call(
        record,
        to_state=observation.status,
        updated_at=observation.observed_at,
        artifact_refs=list(
            dict.fromkeys([*record.artifact_refs, *observation.artifact_refs])
        ),
        terminal_result=result,
        terminal_result_digest=digest,
        error_code=observation.error_code,
        error_message=observation.error_message,
        **common,
    )


def ownership_alias_keys(aliases: list[A2AOwnershipAlias]) -> list[str]:
    return sorted(
        f"{alias.binding_scope}|{alias.kind}|{alias.value}"
        for alias in aliases
        if alias.authoritative
    )


def _observation_result(
    record: AgentCallLedgerRecord, observation: NormalizedA2AObservation
) -> ToolResult:
    assert observation.status is not None
    return ToolResult(
        call_id=record.invocation_id,
        tool_name=record.tool_name,
        status=observation.status,
        content=observation.content,
        artifact_refs=list(
            dict.fromkeys([*record.artifact_refs, *observation.artifact_refs])
        ),
        error_code=observation.error_code,
        error_message=observation.error_message,
    )


def _observation_result_digest(
    record: AgentCallLedgerRecord, observation: NormalizedA2AObservation
) -> str:
    return sha256(
        _observation_result(record, observation).model_dump_json().encode()
    ).hexdigest()


__all__ = [
    "ACTIVE_AGENT_CALL_STATES",
    "AGENT_CALL_STATES",
    "AGENT_CALL_TRANSITIONS",
    "ConflictingTerminalObservation",
    "IllegalAgentCallTransition",
    "TERMINAL_AGENT_CALL_STATES",
    "apply_observation",
    "bind_authoritative_aliases",
    "is_legal_agent_call_transition",
    "ownership_alias_keys",
    "transition_call",
    "validate_agent_call_transition",
]
