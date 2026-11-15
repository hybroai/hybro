"""SDK-free A2A task state and processing-status constants."""

from __future__ import annotations

from enum import Enum
from typing import Any

HYBRO_A2A_INTERACTION_METADATA_KEY = "hybro.ai/a2a/interaction"
HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY = "hybro.ai/a2a/interaction-answer"
HYBRO_A2A_PART_PROVENANCE_METADATA_KEY = "hybro.ai/a2a/part-provenance"
HYBRO_A2A_SELECTED_SKILL_METADATA_KEY = "hybro.ai/a2a/selected-skill"
# Agent-reported failure on a failed/rejected task's status message:
# {"code": "<identifier>", "message": "<short human-readable text>"}.
HYBRO_A2A_ERROR_METADATA_KEY = "hybro.ai/a2a/error"
HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE = "orchestrator_instruction"
HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE = "durable_user_context"

# Error reported when an agent asks for an interaction Hybro cannot route.


class CommonTaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    POLICY_REQUIRED = "policy-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


PENDING_STATES = {CommonTaskState.SUBMITTED, CommonTaskState.WORKING}
INTERACTIVE_STATES = {
    CommonTaskState.INPUT_REQUIRED,
    CommonTaskState.AUTH_REQUIRED,
    CommonTaskState.POLICY_REQUIRED,
}
TERMINAL_STATES = {
    CommonTaskState.COMPLETED,
    CommonTaskState.FAILED,
    CommonTaskState.CANCELED,
    CommonTaskState.REJECTED,
    CommonTaskState.EXPIRED,
}
FAILURE_STATES = {
    CommonTaskState.FAILED,
    CommonTaskState.CANCELED,
    CommonTaskState.REJECTED,
    CommonTaskState.EXPIRED,
}
NON_TERMINAL_STATES = PENDING_STATES | INTERACTIVE_STATES


class SSEProcessingStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    AWAITING_INPUT = "awaiting_input"


def normalize_task_state_value(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    return str(raw)


__all__ = [
    "CommonTaskState",
    "FAILURE_STATES",
    "HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE",
    "HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY",
    "HYBRO_A2A_INTERACTION_METADATA_KEY",
    "HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE",
    "HYBRO_A2A_PART_PROVENANCE_METADATA_KEY",
    "HYBRO_A2A_SELECTED_SKILL_METADATA_KEY",
    "INTERACTIVE_STATES",
    "NON_TERMINAL_STATES",
    "PENDING_STATES",
    "SSEProcessingStatus",
    "TERMINAL_STATES",
    "normalize_task_state_value",
]
