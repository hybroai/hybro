"""Run-state types, tool definitions and pure helpers shared by the kernel."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from jsonschema.exceptions import ValidationError

from .models import (
    ArtifactRefPart,
    AssistantMessage,
    DataPart,
    HeldCallAnswer,
    OrchestratorRunState,
    TextPart,
    ToolBatchEntry,
    ToolCall,
    ToolCallBatch,
    ToolDefinition,
    ToolInteractionAnswers,
    ToolInteractionQuestion,
    ToolObservation,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from .ports import (
    ParkedInteractionCloseout,
)
from .public_text import (
    enforce_public_label_policy,
)
from .transcript import unresolved_call_ids

KernelLifecycle = Callable[
    [str, OrchestratorRunState, dict[str, object]], Awaitable[None]
]


KernelOutcome = Literal[
    "final_answer",
    "waiting_external",
    "awaiting_user",
    "cancellation_pending",
    "budget_exhausted",
    "aborted",
    "failed",
]


class KernelConflict(RuntimeError):
    pass


class KernelCancellationPending(RuntimeError):
    """Normal execution lost its CAS to durable cancellation."""


REQUEST_USER_INPUT_TOOL_NAME = "request_user_input"
SURFACE_AGENT_QUESTIONS_TOOL_NAME = "surface_agent_questions"
MAX_CONSECUTIVE_MODEL_JOINS = 4
# A check rule refused a new Agent call in ``accept``. The text is fixed: no
# rule text and no exception detail reaches the model.
AGENT_CALL_DENIED = "agent_call_denied"
_AGENT_CALL_DENIED_TEXT = "A check rule set by the owner denies this agent call."
# The owner refused a call a check rule held for an answer. Fixed text, as for
# a deny: neither the rule nor the owner's answer reaches the model.
AGENT_CALL_REFUSED = "agent_call_refused"
_AGENT_CALL_REFUSED_TEXT = "The owner refused this agent call."
# The only two answers to a check rule's question. Exactly the first approves.
RULE_ASK_APPROVE = "Approve"
RULE_ASK_REJECT = "Reject"
# The longest call task text a check rule's question shows the owner.
RULE_ASK_TASK_TEXT_LIMIT = 2_000
# Bounded in-kernel re-dispatch of a model-reply join after a recoverable
# transport suspension. Exhausting the budget terminalizes the join with a
# diagnostic failure instead of stalling the Run in waiting_external.
MAX_JOIN_DISPATCH_RETRIES = 3
JOIN_DISPATCH_RETRY_BACKOFF_SECONDS = 1.0
# Join ToolResults carrying one of these codes mean the JOIN ITSELF failed
# (dispatch failed, limit reached, invalid target, etc.) and the Agent's
# question is still unanswered. Three-way consumption must NOT terminalize the
# parked parent entries with such a failure — they must remain eligible for
# request_user_input / a user answer / abandon closeout.
_JOIN_FAILURE_ERROR_CODES = frozenset(
    {
        "model_reply_dispatch_failed",
        "tool_execution_failed",
        "auto_reply_limit_reached",
        "join_target_not_interactive",
        "continuation_target_missing",
        "call_ledger_missing",
        "invalid_tool_call",
    }
)
_REQUEST_USER_INPUT_PENDING_AGENT_QUESTIONS_ERROR = (
    "An Agent's typed questions are awaiting a decision. Forward them "
    "unchanged with surface_agent_questions (one question at a time), or "
    "answer them from available context by calling the Agent tool again."
)
REQUEST_USER_INPUT_TOOL_DEFINITION = ToolDefinition(
    name=REQUEST_USER_INPUT_TOOL_NAME,
    label="Ask the user",
    description=(
        "Ask the user a clarifying question when required information is "
        "missing, the request is ambiguous, or confirmation is needed before "
        "proceeding. Execution pauses until the user answers; include short "
        "options when the answer space is small."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4_000,
                "pattern": r"[\s\S]*\S[\s\S]*",
            },
            "choices": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "pattern": r"[\s\S]*\S[\s\S]*",
                },
                "uniqueItems": True,
                "maxItems": 12,
            },
        },
    },
    execution_mode="sequential",
    side_effect_level="read",
)

SURFACE_AGENT_QUESTIONS_TOOL_DEFINITION = ToolDefinition(
    name=SURFACE_AGENT_QUESTIONS_TOOL_NAME,
    label="Forward the agent's questions",
    description=(
        "Forward an Agent's typed questions to the user unchanged, one "
        "question at a time, preserving each question's answer kind and "
        "options. The runtime supplies an invocation-specific target schema."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    execution_mode="sequential",
    side_effect_level="read",
)


SupervisorHITLPort = Callable[..., Awaitable[None]]


def supervisor_answer_observation(
    run_id: str,
    call_id: str,
    answers: str,
    observed_at: datetime,
) -> ToolObservation:
    """Build the deterministic ToolObservation that resumes an ask_user call.

    The observation identity is a pure function of the durable answer so HITL
    application replays collapse into the already-processed observation path.
    """
    observation_id = sha256(
        f"{run_id}:ask-answer:{call_id}:{answers}".encode()
    ).hexdigest()
    return ToolObservation(
        observation_id=observation_id,
        invocation_id=call_id,
        outcome=ToolResult(
            call_id=call_id,
            tool_name=REQUEST_USER_INPUT_TOOL_NAME,
            status="completed",
            content=[TextPart(text=answers)],
            artifact_refs=[],
        ),
        observed_at=observed_at,
    )


def held_call_answer_observation(
    run_id: str,
    call_id: str,
    answer: str,
    observed_at: datetime,
) -> ToolObservation:
    """Build the observation that answers a call a check rule held.

    It fails closed: only exactly ``Approve`` approves. The identity is a pure
    function of the Run, the call and the decision, so a replayed answer is
    recognized as the one already applied.
    """
    approved = answer == RULE_ASK_APPROVE
    decision = "approve" if approved else "refuse"
    observation_id = sha256(
        f"{run_id}:rule-answer:{call_id}:{decision}".encode()
    ).hexdigest()
    return ToolObservation(
        observation_id=observation_id,
        invocation_id=call_id,
        outcome=HeldCallAnswer(invocation_id=call_id, approved=approved),
        observed_at=observed_at,
    )


def run_level_answer_disposition(
    run: OrchestratorRunState, call_id: str
) -> Literal["deliver", "busy", "applied"]:
    """What the resume port does with the owner's answer to a run-level call.

    Only a call a check rule held, on a Run that is running, differs: while
    it is still asked another call's approved pass is running, so the answer
    waits (``busy``); once answered, the answer is already durable
    (``applied``) and the running pass, or recovery, carries it on.
    """
    if run.status != "running":
        return "deliver"
    hold = next(
        (
            entry.rule_hold
            for batch in run.tool_batches
            for entry in batch.entries
            if entry.call_id == call_id and entry.rule_hold is not None
        ),
        None,
    )
    if hold is None:
        return "deliver"
    return "busy" if hold.state == "asked" else "applied"


def run_level_answer_observation(
    run: OrchestratorRunState,
    call_id: str,
    answers: str,
    observed_at: datetime,
) -> ToolObservation:
    """The observation for the owner's answer to a run-level Question.

    The entry decides: a call a check rule held takes a held-call answer,
    anything else takes the answer text as ``request_user_input`` does.
    """
    held = any(
        entry.call_id == call_id and entry.rule_hold is not None
        for batch in run.tool_batches
        for entry in batch.entries
    )
    if held:
        return held_call_answer_observation(run.run_id, call_id, answers, observed_at)
    return supervisor_answer_observation(run.run_id, call_id, answers, observed_at)


@dataclass(frozen=True, slots=True)
class KernelRunResult:
    outcome: KernelOutcome
    run: OrchestratorRunState


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UUIDFactory:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"


def _opaque_public_call_id(run_id: str, private_call_id: str) -> str:
    digest = sha256(f"{run_id}:{private_call_id}".encode()).hexdigest()[:24]
    return f"inv_{digest}"


def _result_text(result: ToolResult) -> str:
    parts: list[str] = []
    for part in result.content:
        if isinstance(part, TextPart):
            if part.text:
                parts.append(part.text)
        elif isinstance(part, DataPart):
            parts.append(
                json.dumps(part.data, ensure_ascii=False, separators=(",", ":"))
            )
        elif isinstance(part, ArtifactRefPart):
            parts.append(f"[artifact reference: {part.artifact_ref}]")
    return "\n".join(parts)[:8000]


def _interaction_question_summary(
    questions: list[ToolInteractionQuestion],
) -> str:
    if not questions:
        return ""
    return " | ".join(question.prompt for question in questions)[:1_000]


def _has_presentable_interactions(run: OrchestratorRunState) -> bool:
    return any(
        entry.state in {"input_required", "auth_required"}
        and entry.interaction_id is not None
        and not entry.presented
        and entry.surface_for_call_record_id is None
        for batch in run.tool_batches
        for entry in batch.entries
    )


def _has_visible_input(run: OrchestratorRunState) -> bool:
    return any(
        entry.state in {"input_required", "auth_required"}
        and (
            entry.surface_for_call_record_id is not None
            or (not entry.presented and entry.interaction_id is None)
        )
        for batch in run.tool_batches
        for entry in batch.entries
    )


def _has_presented_interactions(run: OrchestratorRunState) -> bool:
    return any(
        entry.state in {"input_required", "auth_required"} and entry.presented
        for batch in run.tool_batches
        for entry in batch.entries
    )


def _presentation_id(run: OrchestratorRunState, entry: ToolBatchEntry) -> str:
    if entry.presentation_id:
        return entry.presentation_id
    digest = sha256(
        (
            f"{run.run_id}:presentation:{entry.call_id}:"
            f"{entry.interaction_fingerprint or ''}"
        ).encode()
    ).hexdigest()[:24]
    return f"prs_{digest}"


def _presented_targets(
    run: OrchestratorRunState,
) -> list[tuple[str, int, int, ToolBatchEntry]]:
    targets: list[tuple[str, int, int, ToolBatchEntry]] = []
    for batch_index, batch in enumerate(run.tool_batches):
        for entry_index, entry in enumerate(batch.entries):
            if (
                entry.state in {"input_required", "auth_required"}
                and entry.presented
                and entry.surface_for_call_record_id is None
            ):
                targets.append(
                    (_presentation_id(run, entry), batch_index, entry_index, entry)
                )
    return targets


def _forwarded_presentation_ids(run: OrchestratorRunState) -> set[str]:
    """Presentations whose surface_agent_questions call already resolved."""
    return {
        entry.presentation_id
        for batch in run.tool_batches
        for entry in batch.entries
        if entry.tool_name == SURFACE_AGENT_QUESTIONS_TOOL_NAME
        and entry.state == "terminal"
        and entry.presentation_id
    }


def _surface_agent_questions_tool_definition(
    run: OrchestratorRunState,
) -> ToolDefinition:
    targets = _presented_targets(run)
    if len(targets) <= 1:
        return SURFACE_AGENT_QUESTIONS_TOOL_DEFINITION.model_copy(
            update={
                "description": (
                    "Forward the only pending Agent interaction's typed "
                    "questions unchanged. This tool takes no arguments."
                )
            }
        )
    presentation_ids = [target[0] for target in targets]
    return SURFACE_AGENT_QUESTIONS_TOOL_DEFINITION.model_copy(
        update={
            "description": (
                "Forward one pending Agent interaction's typed questions "
                "unchanged. Use the presentation_id from the private Agent "
                "input observation."
            ),
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["presentation_id"],
                "properties": {
                    "presentation_id": {
                        "type": "string",
                        "enum": presentation_ids,
                    }
                },
            },
        }
    )


def _batch_is_parked(batch: ToolCallBatch) -> bool:
    """A batch whose entries are all settled (terminal) or presented-for-decision.

    Such a batch must not be re-entered by ``_execute_tool_batch`` even though
    its ``results_flushed`` is still False: its terminal results are already
    materialized per-entry and its suspended entries have been surfaced to the
    model as ``tool_interaction`` messages.
    """
    return bool(batch.entries) and all(
        (entry.state == "terminal" and entry.result_flushed)
        or (entry.state in {"input_required", "auth_required"} and entry.presented)
        for entry in batch.entries
    )


@dataclass(frozen=True, slots=True)
class _TurnClosureFacts:
    message_id: str
    public_tool_call_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TerminalTurnClosurePlan:
    internal_turn_id: str
    message_id: str
    public_tool_call_ids: tuple[str, ...]
    emit_turn_end: bool


@dataclass(frozen=True, slots=True)
class _TerminalClosurePlan:
    turns: tuple[_TerminalTurnClosurePlan, ...]
    interactions: tuple[tuple[str, str], ...]
    replayed_public_tool_call_ids: tuple[str, ...]
    active_message_end: tuple[str, str] | None


def _parked_closeout_result(
    entry: ToolBatchEntry,
    closeout: ParkedInteractionCloseout,
) -> ToolResult:
    """Correlate a durable child winner to its exact Kernel entry."""

    direct_owner = (
        closeout.result.call_id == entry.call_id
        and closeout.result.tool_name == entry.tool_name
    )
    projected_surface = (
        entry.surface_for_call_record_id is not None
        and entry.surface_for_call_record_id == entry.suspended_call_record_id
    )
    if not direct_owner and not projected_surface:
        raise KernelConflict("parked Agent-call terminal winner does not correlate")
    if closeout.local_cancellation_won:
        return ToolResult(
            call_id=entry.call_id,
            tool_name=entry.tool_name,
            status="canceled",
            content=[],
            artifact_refs=[],
            error_code="interaction_abandoned",
            error_message=None,
        )
    if direct_owner:
        return closeout.result
    return closeout.result.model_copy(
        update={"call_id": entry.call_id, "tool_name": entry.tool_name}
    )


def _expected_public_tool_terminal(
    result: ToolResult,
) -> tuple[str, bool, str | None]:
    """Mirror the public projection's exact private-status mapping."""

    if result.status == "completed":
        return "completed", False, None
    if result.status == "canceled":
        return "canceled", False, None
    if result.status in {"rejected", "expired"}:
        return "failed", True, result.status
    return "failed", True, "execution"


def _batch_internal_turn_id(batch: ToolCallBatch) -> str:
    """Return the canonical owner for a durable Tool batch.

    Early canonical batches predate the explicit ``internal_turn_id`` field and
    used the AssistantMessage identity as their Turn identity.  Recovery must
    retain that compatibility mapping without assigning historical children to
    whichever Turn happens to be active now.
    """

    return batch.internal_turn_id or batch.assistant_message_id


def _canonical_final_turn_closure(
    run: OrchestratorRunState,
    internal_turn_id: str,
    final_message_id: str,
) -> _TurnClosureFacts | None:
    """Build a final Turn inventory from fully terminal durable batches."""

    public_ids: list[str] = []
    for batch in run.tool_batches:
        if _batch_internal_turn_id(batch) != internal_turn_id:
            continue
        for entry in sorted(batch.entries, key=lambda item: item.source_index):
            if entry.state != "terminal":
                return None
            if entry.opaque_public_call_id is not None:
                public_ids.append(entry.opaque_public_call_id)
    return _TurnClosureFacts(
        message_id=final_message_id,
        public_tool_call_ids=tuple(public_ids),
    )


def _canonical_turn_closure(
    run: OrchestratorRunState, internal_turn_id: str
) -> _TurnClosureFacts | None:
    """Atomic message and Tool inventory for a closed canonical turn.

    Tool batches are durably appended in AssistantMessage order.  A later
    model-first decision batch therefore owns the closing message identity even
    when an earlier suspended parent batch happens to terminalize last.
    ``active_assistant_message_id`` is newer still when recovery is closing an
    interrupted message.  An open entry or a turn with no durable message
    identity defers ``turn_end``.
    """
    batches = [
        batch
        for batch in run.tool_batches
        if _batch_internal_turn_id(batch) == internal_turn_id
    ]
    ids: list[str] = []
    for batch in batches:
        for entry in sorted(batch.entries, key=lambda item: item.source_index):
            if entry.state != "terminal":
                return None
            if entry.opaque_public_call_id is not None:
                ids.append(entry.opaque_public_call_id)
    message_id = (
        run.active_assistant_message_id
        if run.active_internal_turn_id == internal_turn_id
        else None
    ) or (batches[-1].assistant_message_id if batches else None)
    if message_id is None:
        return None
    return _TurnClosureFacts(
        message_id=message_id,
        public_tool_call_ids=tuple(ids),
    )


def _terminal_closure_is_complete(run: OrchestratorRunState) -> bool:
    """Prove the fail-closed descendant invariants required by termination."""

    return all(
        batch.results_flushed
        and all(
            entry.state == "terminal"
            and (
                (entry.acceptance is None and entry.opaque_public_call_id is None)
                or (
                    entry.opaque_public_call_id is not None
                    and entry.public_terminal_emitted
                )
            )
            for entry in batch.entries
        )
        for batch in run.tool_batches
    )


def _terminal_closure_plan(
    run: OrchestratorRunState,
    canonical_records: list[dict[str, object]],
    *,
    canonical_reader_available: bool,
    public_secret_values: tuple[str, ...] = (),
) -> _TerminalClosurePlan:
    """Validate the complete canonical closeout before any external effect.

    This is intentionally a pure function. Recovery can discover an
    irreparable historical shape only here, before abandoning HITL ownership,
    publishing lifecycle events, or mutating the Run aggregate.
    """

    active_turn_id = run.active_internal_turn_id
    active_remnants = any(
        (
            run.active_assistant_message_id,
            run.active_attempt,
            run.active_public_text,
            run.greatest_public_text_offset,
        )
    )
    if active_turn_id is None and active_remnants:
        raise KernelConflict("canonical active lifecycle has no owning turn")

    batches_by_turn: dict[str, list[ToolCallBatch]] = {}
    public_owner: dict[str, tuple[str, str, ToolBatchEntry]] = {}
    interactions: list[tuple[str, str]] = []
    interaction_routes: dict[str, str] = {}
    incomplete_turns: set[str] = set()
    for batch in run.tool_batches:
        owner = _batch_internal_turn_id(batch)
        batches_by_turn.setdefault(owner, []).append(batch)
        if not batch.results_flushed:
            incomplete_turns.add(owner)
        for entry in sorted(batch.entries, key=lambda item: item.source_index):
            if entry.acceptance is not None and entry.opaque_public_call_id is None:
                raise KernelConflict(
                    "accepted Tool child has no canonical public identity"
                )
            if entry.opaque_public_call_id is not None:
                if entry.opaque_public_call_id in public_owner:
                    raise KernelConflict("canonical public Tool identity is duplicated")
                public_label = entry.tool_name
                if run.tool_catalog is not None:
                    catalog_entry = next(
                        (
                            item
                            for item in run.tool_catalog.entries
                            if item.definition.name == entry.tool_name
                        ),
                        None,
                    )
                    if catalog_entry is not None:
                        public_label = (
                            catalog_entry.definition.label.strip() or entry.tool_name
                        )
                public_owner[entry.opaque_public_call_id] = (
                    owner,
                    enforce_public_label_policy(
                        public_label,
                        secret_values=public_secret_values,
                    ),
                    entry,
                )
            if entry.state != "terminal" or (
                entry.opaque_public_call_id is not None
                and not entry.public_terminal_emitted
            ):
                incomplete_turns.add(owner)
            parked = entry.state in {"input_required", "auth_required"}
            parked_identity = (
                entry.suspended_call_record_id is not None
                or entry.interaction_id is not None
            )
            # A parked entry with no identity at all owns no Agent interaction
            # (a run-level question); it closes like any open entry.
            if (
                parked
                and parked_identity
                and not (entry.suspended_call_record_id and entry.interaction_id)
            ):
                raise KernelConflict(
                    "parked Tool child has incomplete interaction identity"
                )
            if parked and parked_identity:
                assert entry.suspended_call_record_id is not None
                assert entry.interaction_id is not None
                previous_route = interaction_routes.setdefault(
                    entry.interaction_id, entry.suspended_call_record_id
                )
                if previous_route != entry.suspended_call_record_id:
                    raise KernelConflict("parked interaction has conflicting ownership")
                identity = (entry.suspended_call_record_id, entry.interaction_id)
                if identity not in interactions:
                    interactions.append(identity)

    if active_turn_id is not None and (
        active_turn_id not in batches_by_turn
        and run.active_assistant_message_id is None
    ):
        raise KernelConflict("canonical active turn has no durable assistant message")

    closed_turns: dict[str, _TurnClosureFacts] = {}
    ended_messages: set[tuple[str, str]] = set()
    ended_public_tools: set[str] = set()
    replayed_public_tools: list[str] = []
    for record in canonical_records:
        data = record.get("payload_public")
        if not isinstance(data, dict) or data.get("run_id") != run.run_id:
            continue
        event_type = data.get("type")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            if event_type == "tool_execution_end":
                raise KernelConflict("canonical Tool end payload is malformed")
            continue
        owner = payload.get("internal_turn_id")
        if event_type == "tool_execution_end":
            if not isinstance(owner, str) or not owner or owner not in batches_by_turn:
                raise KernelConflict("canonical Tool end has no durable turn owner")
            # PublicProjectionTranslator deliberately exposes only the opaque
            # Tool identity. Private provider/model call ids must never be
            # required from (or copied into) canonical room history.
            public_id = payload.get("tool_call_id")
            if not isinstance(public_id, str) or not public_id:
                raise KernelConflict("canonical Tool end has no public identity")
            if public_id in ended_public_tools:
                raise KernelConflict("canonical Tool end identity is duplicated")
            ended_public_tools.add(public_id)
            expected = public_owner.get(public_id)
            if expected is None:
                raise KernelConflict("canonical Tool end has no durable child")
            expected_owner, expected_tool_name, entry = expected
            public_tool_name = payload.get("tool_name")
            if (
                expected_owner != owner
                or not isinstance(public_tool_name, str)
                or public_tool_name != expected_tool_name
            ):
                raise KernelConflict("canonical Tool end ownership conflicts")
            if entry.state != "terminal" or entry.buffered_terminal_result is None:
                raise KernelConflict("canonical Tool end conflicts with durable child")
            expected_outcome, expected_is_error, expected_failure_reason = (
                _expected_public_tool_terminal(entry.buffered_terminal_result)
            )
            if (
                not isinstance(payload.get("outcome"), str)
                or payload.get("outcome") != expected_outcome
                or type(payload.get("is_error")) is not bool
                or payload.get("is_error") is not expected_is_error
                or (
                    expected_failure_reason is None
                    and payload.get("failure_reason") is not None
                )
                or (
                    expected_failure_reason is not None
                    and (
                        not isinstance(payload.get("failure_reason"), str)
                        or payload.get("failure_reason") != expected_failure_reason
                    )
                )
            ):
                raise KernelConflict("canonical Tool end outcome conflicts")
            if not entry.public_terminal_emitted:
                replayed_public_tools.append(public_id)
        elif not isinstance(owner, str) or not owner:
            continue
        elif event_type == "message_end":
            message_id = payload.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                raise KernelConflict("canonical message end has no identity")
            ended_messages.add((owner, message_id))
        elif event_type == "turn_end":
            message_id = payload.get("message_id")
            public_ids = payload.get("tool_call_ids")
            if (
                not isinstance(message_id, str)
                or not isinstance(public_ids, list)
                or any(not isinstance(item, str) for item in public_ids)
            ):
                raise KernelConflict("canonical turn end inventory is malformed")
            facts = _TurnClosureFacts(
                message_id=message_id,
                public_tool_call_ids=tuple(public_ids),
            )
            existing = closed_turns.setdefault(owner, facts)
            if existing != facts:
                raise KernelConflict(
                    "canonical turn has conflicting terminal inventory"
                )

    for owner in incomplete_turns:
        if owner in closed_turns:
            raise KernelConflict(
                "canonical closed turn retains an incomplete Tool child"
            )

    ordered_turns: list[str] = []
    for batch in run.tool_batches:
        owner = _batch_internal_turn_id(batch)
        if owner not in ordered_turns:
            ordered_turns.append(owner)
    if active_turn_id is not None and active_turn_id not in ordered_turns:
        ordered_turns.append(active_turn_id)

    plans: list[_TerminalTurnClosurePlan] = []
    for owner in ordered_turns:
        affected = (
            owner in incomplete_turns
            or owner == active_turn_id
            or (canonical_reader_available and owner not in closed_turns)
        )
        if not affected:
            continue
        batches = batches_by_turn.get(owner, [])
        public_ids = tuple(
            entry.opaque_public_call_id
            for batch in batches
            for entry in sorted(batch.entries, key=lambda item: item.source_index)
            if entry.opaque_public_call_id is not None
        )
        message_id = (
            run.active_assistant_message_id if active_turn_id == owner else None
        ) or (batches[-1].assistant_message_id if batches else None)
        if message_id is None:
            raise KernelConflict("canonical turn has no durable assistant message")
        facts = _TurnClosureFacts(
            message_id=message_id,
            public_tool_call_ids=public_ids,
        )
        existing = closed_turns.get(owner)
        if existing is not None and existing != facts:
            raise KernelConflict("canonical turn terminal inventory conflicts")
        plans.append(
            _TerminalTurnClosurePlan(
                internal_turn_id=owner,
                message_id=message_id,
                public_tool_call_ids=public_ids,
                emit_turn_end=existing is None,
            )
        )

    active_message_end = None
    if active_turn_id is not None and run.active_assistant_message_id is not None:
        identity = (active_turn_id, run.active_assistant_message_id)
        if identity not in ended_messages:
            active_message_end = identity

    return _TerminalClosurePlan(
        turns=tuple(plans),
        interactions=tuple(interactions),
        replayed_public_tool_call_ids=tuple(replayed_public_tools),
        active_message_end=active_message_end,
    )


def _find_presented_entry_by_presentation(
    run: OrchestratorRunState, presentation_id: str | None
) -> tuple[int, int, ToolBatchEntry] | None:
    """Resolve a private presentation target with strict run ownership."""

    targets = _presented_targets(run)
    if presentation_id is None:
        if len(targets) != 1:
            return None
        _target, batch_index, entry_index, entry = targets[0]
        return batch_index, entry_index, entry
    for target, batch_index, entry_index, entry in targets:
        if target == presentation_id:
            return batch_index, entry_index, entry
    return None


def _has_ellipsis_placeholder(text: str) -> bool:
    return "..." in text or "…" in text


def _request_user_input_choices(arguments: dict[str, object]) -> list[str]:
    """Normalized (stripped, non-blank) choices for request_user_input."""
    return [
        str(choice).strip()
        for choice in (arguments.get("choices") or [])
        if str(choice).strip()
    ]


def _passthrough_refused(run: OrchestratorRunState) -> bool:
    """Whether the owner refused the passthrough Run's one held call."""
    return any(
        entry.rule_hold is not None and entry.rule_hold.state == "refused"
        for batch in run.tool_batches
        for entry in batch.entries
    )


def _answered_question_count(batch: ToolCallBatch) -> int:
    """Owner Questions of a batch already answered.

    Held calls the owner approved or refused, and ask_user or forwarded
    Agent questions that ended. Each answer can lead to another suspension
    of the same batch, which needs its own checkpoint command id.
    """
    return sum(
        1
        for entry in batch.entries
        if (entry.rule_hold is not None and entry.rule_hold.state != "asked")
        or (
            entry.state == "terminal"
            and entry.tool_name
            in {REQUEST_USER_INPUT_TOOL_NAME, SURFACE_AGENT_QUESTIONS_TOOL_NAME}
        )
    )


def _rule_hold_asked(batch: ToolCallBatch, *, other_than: str) -> bool:
    """Whether another call of the batch waits on the owner's rule answer."""
    return any(
        entry.call_id != other_than
        and entry.rule_hold is not None
        and entry.rule_hold.state == "asked"
        for entry in batch.entries
    )


def _has_waiting_call(batches: list[ToolCallBatch]) -> bool:
    """Whether a call waits, unasked, for an earlier owner Question to end.

    A pending ``request_user_input`` or ``surface_agent_questions`` call
    counts too: it waits the same way while a held call is asked.
    """
    return any(
        entry.state == "pending"
        and (
            entry.rule_deferred
            or entry.tool_name
            in {REQUEST_USER_INPUT_TOOL_NAME, SURFACE_AGENT_QUESTIONS_TOOL_NAME}
        )
        for batch in batches
        if not batch.results_flushed
        for entry in batch.entries
    )


def _has_working_call(batches: list[ToolCallBatch]) -> bool:
    """Whether an Agent call of the Run is still at work remotely."""
    return any(
        entry.state in {"executing", "waiting_external"}
        for batch in batches
        for entry in batch.entries
    )


def _waiting_call_runs_next(batches: list[ToolCallBatch]) -> bool:
    """Whether the batch the run loop takes next holds a waiting call.

    The loop runs the first unflushed batch that is not parked. A waiting call
    in a later batch is reached only once the earlier batch ends, so until
    then the Run keeps waiting on that batch's Agents.
    """
    next_batch = next(
        (
            batch
            for batch in batches
            if not batch.results_flushed and not _batch_is_parked(batch)
        ),
        None,
    )
    return next_batch is not None and _has_waiting_call([next_batch])


def _owner_question_open(batch: ToolCallBatch, *, other_than: str) -> bool:
    """Whether another call of the batch has an owner Question open.

    The Room shows one open Question per turn, so a call a check rule asks
    about waits unasked while a held call, an ask_user call or a forwarded
    Agent question of the same batch waits on the owner.
    """
    return _rule_hold_asked(batch, other_than=other_than) or any(
        entry.call_id != other_than
        and entry.state == "input_required"
        and entry.tool_name
        in {REQUEST_USER_INPUT_TOOL_NAME, SURFACE_AGENT_QUESTIONS_TOOL_NAME}
        for entry in batch.entries
    )


def _passthrough_denied(run: OrchestratorRunState) -> bool:
    """Whether a check rule refused the passthrough Run's one Agent call."""
    return any(
        entry.denied_by_rule for batch in run.tool_batches for entry in batch.entries
    )


def _find_join_target(run: OrchestratorRunState, tool_name: str) -> str | None:
    """Parent call of the most recently presented interaction for a tool.

    A model re-invocation of the same agent+skill while a presented interaction
    is parked routes as a continuation join on that call rather than opening a
    new A2A task.
    """
    target: str | None = None
    for batch in run.tool_batches:
        for entry in batch.entries:
            if (
                entry.tool_name == tool_name
                and entry.state in {"input_required", "auth_required"}
                and entry.presented
                and entry.suspended_call_record_id is not None
            ):
                target = entry.suspended_call_record_id
    return target


def _parked_interaction_questions(
    run: OrchestratorRunState, call_record_id: str
) -> list[ToolInteractionQuestion]:
    """Typed questions of a parked interaction on a parent call."""
    for batch in run.tool_batches:
        for entry in batch.entries:
            if entry.suspended_call_record_id == call_record_id and entry.state in {
                "input_required",
                "auth_required",
            }:
                return entry.interaction_questions
    return []


def _assistant_text(assistant: AssistantMessage) -> str:
    parts: list[str] = []
    for part in assistant.content:
        if isinstance(part, TextPart) and part.text:
            parts.append(part.text)
    return "\n".join(parts)[:32_000]


def _merge_artifact_refs(existing: list[str], results: list[ToolResult]) -> list[str]:
    return list(
        dict.fromkeys(
            [*existing, *(ref for result in results for ref in result.artifact_refs)]
        )
    )


def _new_tool_batch(
    assistant: AssistantMessage, *, internal_turn_id: str | None = None
) -> ToolCallBatch:
    return ToolCallBatch(
        assistant_message_id=assistant.message_id,
        internal_turn_id=internal_turn_id,
        entries=[
            ToolBatchEntry(
                call_id=call.call_id,
                assistant_message_id=assistant.message_id,
                source_index=index,
                tool_name=call.tool_name,
            )
            for index, call in enumerate(assistant.tool_calls)
        ],
    )


def _user_request_text(run: OrchestratorRunState) -> str:
    """The text of the user message that started the Run."""
    message = next(
        (item for item in run.transcript if isinstance(item, UserMessage)), None
    )
    if message is None:
        return ""
    return "\n".join(
        part.text for part in message.content if isinstance(part, TextPart)
    ).strip()


def _passthrough_answer_text(result: ToolResult) -> str:
    """The Agent's complete answer, as the passthrough final message text."""
    parts: list[str] = []
    for part in result.content:
        if isinstance(part, TextPart):
            if part.text:
                parts.append(part.text)
        elif isinstance(part, DataPart):
            parts.append(
                json.dumps(part.data, ensure_ascii=False, separators=(",", ":"))
            )
        elif isinstance(part, ArtifactRefPart):
            parts.append(f"[artifact reference: {part.artifact_ref}]")
    return "\n".join(parts)


def _finalization_candidate(run: OrchestratorRunState) -> AssistantMessage | None:
    if run.proposed_final_message_id is None:
        return None
    candidates = [
        item
        for item in run.transcript
        if isinstance(item, AssistantMessage)
        and item.message_id == run.proposed_final_message_id
        and not item.tool_calls
    ]
    return candidates[0] if len(candidates) == 1 else None


def _assistant_missing_tool_batch(
    run: OrchestratorRunState,
) -> AssistantMessage | None:
    unresolved = unresolved_call_ids(run.transcript)
    batch_message_ids = {batch.assistant_message_id for batch in run.tool_batches}
    for item in run.transcript:
        if not isinstance(item, AssistantMessage) or not item.tool_calls:
            continue
        if item.message_id in batch_message_ids:
            continue
        if {call.call_id for call in item.tool_calls} & unresolved:
            return item
    return None


def _find_entry(
    run: OrchestratorRunState,
    *,
    assistant_message_id: str,
    call_id: str,
) -> tuple[int | None, int | None]:
    for batch_index, batch in enumerate(run.tool_batches):
        if batch.assistant_message_id != assistant_message_id:
            continue
        for entry_index, entry in enumerate(batch.entries):
            if entry.call_id == call_id:
                return batch_index, entry_index
    return None, None


def _tool_schema_validation_message(error: ValidationError) -> str:
    """Return an actionable diagnostic without echoing argument values."""

    path = "$"
    if error.absolute_path:
        path += "".join(f"[{item}]" for item in error.absolute_path)
    detail = "arguments do not match the declared schema"
    if error.validator == "additionalProperties":
        instance = error.instance
        properties = error.schema.get("properties")
        allowed = set(properties) if isinstance(properties, dict) else set()
        unexpected = (
            sorted(str(key) for key in instance if key not in allowed)
            if isinstance(instance, dict)
            else []
        )
        if unexpected:
            detail = f"unsupported properties: {', '.join(unexpected[:5])}"
    elif error.validator == "required":
        required = error.validator_value
        instance = error.instance
        missing = (
            sorted(str(key) for key in required if key not in instance)
            if isinstance(required, list) and isinstance(instance, dict)
            else []
        )
        if missing:
            detail = f"missing required properties: {', '.join(missing[:5])}"
    elif error.validator == "type":
        detail = f"value at {path} has the wrong type"
    elif error.validator == "enum":
        detail = f"value at {path} is not an allowed resource reference"

    return (
        f"Tool arguments failed schema validation: {detail}. "
        "Consult the current Tool schema and retry with compatible arguments."
    )


def _tool_error(call: ToolCall, code: str, message: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="rejected",
        content=[TextPart(text=message[:500])],
        artifact_refs=[],
        error_code=code,
        error_message=message[:500],
    )


def _with_interaction_answers(
    known: list[ToolInteractionAnswers],
    observed: ToolInteractionAnswers | None,
) -> list[ToolInteractionAnswers]:
    """Record one round's answers once; observations replay the latest round."""
    if observed is None or any(
        item.interaction_id == observed.interaction_id for item in known
    ):
        return known
    return [*known, observed]


def _flush_batch(
    transcript: list[object], batch: ToolCallBatch, created_at: datetime
) -> tuple[list[object], ToolCallBatch]:
    if batch.results_flushed:
        return transcript, batch
    results: list[ToolResultMessage] = []
    for entry in sorted(batch.entries, key=lambda item: item.source_index):
        if entry.result_flushed:
            continue
        if entry.state != "terminal":
            continue
        result = entry.buffered_terminal_result
        if result is None:
            raise ValueError("terminal tool entry has no buffered result")
        results.append(
            ToolResultMessage(
                message_id=f"tool-result:{entry.call_id}",
                call_id=result.call_id,
                tool_name=result.tool_name,
                status=result.status,
                content=result.content,
                artifact_refs=result.artifact_refs,
                is_error=result.status != "completed",
                error_code=result.error_code,
                error_message=result.error_message,
                interaction_answers=entry.interaction_answers,
                created_at=created_at,
            )
        )
    entries = [
        entry.model_copy(update={"result_flushed": True})
        if entry.state == "terminal"
        else entry
        for entry in batch.entries
    ]
    # A mixed batch keeps results_flushed False until every entry terminalizes;
    # its terminal entries are already materialized as ToolResultMessages so the
    # model context never observes an unresolved call id.
    results_flushed = all(entry.state == "terminal" for entry in batch.entries)
    return [*transcript, *results], batch.model_copy(
        update={"entries": entries, "results_flushed": results_flushed}
    )


def _wait_status(batch: ToolCallBatch) -> str:
    states = {entry.state for entry in batch.entries}
    if states & {"input_required", "auth_required"}:
        return "awaiting_user"
    return "waiting_external"


def _find_invocation(
    run: OrchestratorRunState, invocation_id: str
) -> tuple[int | None, int | None]:
    for batch_index, batch in enumerate(run.tool_batches):
        for entry_index, entry in enumerate(batch.entries):
            if entry.call_id == invocation_id:
                return batch_index, entry_index
    return None, None


def _outcome_for_status(status: str) -> KernelOutcome:
    return {
        "completed": "final_answer",
        "failed": "failed",
        "canceled": "aborted",
        "budget_exhausted": "budget_exhausted",
    }.get(status, "failed")  # type: ignore[return-value]
