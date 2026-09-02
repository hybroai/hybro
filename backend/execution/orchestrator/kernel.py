"""Provider- and transport-neutral bounded agent loop."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .budget import BudgetExceeded, BudgetPolicy
from .context import ContextCompiler, UnresolvedToolBatchError
from .models import (
    ArtifactRefPart,
    AssistantMessage,
    DataPart,
    ModelStreamEvent,
    OrchestratorRunState,
    PreparedResourceRef,
    ResolvedTool,
    RunResourceManifestSnapshot,
    SessionNotice,
    TextPart,
    ToolAcceptance,
    ToolBatchEntry,
    ToolBindingRef,
    ToolCall,
    ToolCallBatch,
    ToolDefinition,
    ToolInteractionMessage,
    ToolInteractionQuestion,
    ToolInvocation,
    ToolObservation,
    ToolResult,
    ToolResultMessage,
    ToolSuspension,
    UsageRecord,
)
from .ports import (
    CancellationSignal,
    ContextCompactor,
    IDFactory,
    ModelRuntime,
    OrchestratorRunStore,
    ProjectionDriver,
    ToolCatalog,
    ToolRuntime,
)
from .public_text import (
    DEFAULT_COALESCE_INTERVAL_MS,
    PublicTextCoalescer,
    PublicTextSanitizer,
    enforce_public_label_policy,
)
from .settlement import (
    TerminalCommitRequest,
    TerminalDecisionFacts,
    TerminalStatusCommitRequest,
    commit_terminal_decision,
    commit_terminal_status,
)
from .streaming import ModelStreamAssembler, ModelStreamAssemblyError
from .tools import validate_tool_result_correlation
from .transcript import unresolved_call_ids

KernelLifecycle = Callable[
    [str, OrchestratorRunState, dict[str, object]], Awaitable[None]
]

_kernel_logger = logging.getLogger(__name__)

KernelOutcome = Literal[
    "final_answer",
    "waiting_external",
    "awaiting_user",
    "budget_exhausted",
    "aborted",
    "failed",
]


class KernelConflict(RuntimeError):
    pass


REQUEST_USER_INPUT_TOOL_NAME = "request_user_input"
SURFACE_AGENT_QUESTIONS_TOOL_NAME = "surface_agent_questions"
MAX_CONSECUTIVE_MODEL_JOINS = 4
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


def _task_text(arguments: object) -> str:
    if isinstance(arguments, dict):
        task = arguments.get("task")
        if isinstance(task, str) and task.strip():
            return task.strip()[:4000]
    return ""


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


def _backfill_presentation_ids(
    run: OrchestratorRunState,
) -> OrchestratorRunState:
    """Upgrade checkpointed presented entries/messages without a migration."""

    batches = list(run.tool_batches)
    replacements: dict[tuple[str, str], str] = {}
    changed = False
    for batch_index, batch in enumerate(batches):
        entries = list(batch.entries)
        batch_changed = False
        for entry_index, entry in enumerate(entries):
            if (
                not entry.presented
                or entry.state not in {"input_required", "auth_required"}
                or entry.interaction_fingerprint is None
            ):
                continue
            presentation_id = _presentation_id(run, entry)
            replacements[(entry.call_id, entry.interaction_fingerprint)] = (
                presentation_id
            )
            if entry.presentation_id is None:
                entries[entry_index] = entry.model_copy(
                    update={"presentation_id": presentation_id}
                )
                batch_changed = True
                changed = True
        if batch_changed:
            batches[batch_index] = batch.model_copy(update={"entries": entries})
    transcript = list(run.transcript)
    for index, message in enumerate(transcript):
        if not isinstance(message, ToolInteractionMessage):
            continue
        presentation_id = replacements.get(
            (message.call_id, message.interaction_fingerprint)
        )
        if presentation_id is not None and message.presentation_id is None:
            transcript[index] = message.model_copy(
                update={"presentation_id": presentation_id}
            )
            changed = True
    if not changed:
        return run
    return run.model_copy(update={"tool_batches": batches, "transcript": transcript})


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
            if parked and not (entry.suspended_call_record_id and entry.interaction_id):
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
                or (expected_failure_reason is None and "failure_reason" in payload)
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


def _model_turn_outcome(model_outcome) -> str:
    """Public-safe outcome label for a completed model turn."""

    kind = str(getattr(model_outcome, "kind", "") or "")
    if kind == "assistant":
        return "completed"
    if kind in {"context_overflow", "provider_error", "aborted"}:
        return kind
    return "unknown"


def _elapsed_ms(now: datetime, started_at: datetime) -> int:
    return max(0, int((now - started_at).total_seconds() * 1000))


def _usage_public_fields(usage) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
    }


class OrchestratorKernel:
    def __init__(
        self,
        *,
        run_store: OrchestratorRunStore,
        model_runtime: ModelRuntime,
        tool_runtime: ToolRuntime,
        tool_catalog: ToolCatalog,
        context_compiler: ContextCompiler,
        budget_policy: BudgetPolicy,
        projection_driver: ProjectionDriver,
        clock: SystemClock | None = None,
        id_factory: IDFactory | None = None,
        context_compactor: ContextCompactor | None = None,
        public_secret_values: Iterable[str] = (),
        canonical_event_reader: Callable[[str, str], Awaitable[list[dict[str, object]]]]
        | None = None,
        artifact_metadata_reader: Callable[..., Awaitable[PreparedResourceRef | None]]
        | None = None,
        supervisor_hitl: SupervisorHITLPort | None = None,
    ) -> None:
        self.run_store = run_store
        self.model_runtime = model_runtime
        self.tool_runtime = tool_runtime
        self.tool_catalog = tool_catalog
        self.context_compiler = context_compiler
        self.budget_policy = budget_policy
        self.projection_driver = projection_driver
        self.clock = clock or SystemClock()
        self.id_factory = id_factory or UUIDFactory()
        self.context_compactor = context_compactor
        self.public_secret_values = tuple(
            value for value in public_secret_values if isinstance(value, str) and value
        )
        self.canonical_event_reader = canonical_event_reader
        self.artifact_metadata_reader = artifact_metadata_reader
        self.supervisor_hitl = supervisor_hitl
        self._lifecycle_context: ContextVar[KernelLifecycle | None] = ContextVar(
            f"kernel-lifecycle-{id(self)}", default=None
        )

    async def run(
        self,
        run_id: str,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None = None,
    ) -> KernelRunResult:
        self._lifecycle_context.set(lifecycle)
        invalid_observations = 0
        decision_provider_errors = 0
        recover_initial_state = True
        while True:
            run = await self._load(run_id)
            if run.status in {"completed", "failed", "canceled", "budget_exhausted"}:
                return KernelRunResult(_outcome_for_status(run.status), run)
            if (
                run.status in {"waiting_external", "awaiting_user"}
                and self.clock.now() >= run.budget.deadline_at
            ):
                return await self._terminate(
                    run, status="budget_exhausted", reason="deadline"
                )
            if run.lifecycle_family == "canonical":
                for batch_index, batch in enumerate(list(run.tool_batches)):
                    run = await self._publish_checkpointed_tool_terminals(
                        run,
                        batch_index,
                        lifecycle=lifecycle,
                        internal_turn_id=_batch_internal_turn_id(batch),
                    )
            if (
                recover_initial_state
                and run.lifecycle_family == "canonical"
                and run.active_internal_turn_id
            ):
                recover_initial_state = False
                recovered = await self._recover_active_canonical_attempt(run, lifecycle)
                if isinstance(recovered, KernelRunResult):
                    return recovered
                run, closed_turn_id = recovered
                if closed_turn_id is not None:
                    await self._emit(
                        lifecycle,
                        "model_retry_scheduled",
                        run,
                        {
                            "public_event_id": (
                                f"public:{run.run_id}:{closed_turn_id}:"
                                "retry:process_restart"
                            ),
                            "internal_turn_id": closed_turn_id,
                            "attempt": 2,
                            "error_class": "process_restart",
                            "retry_delay_ms": 0,
                        },
                    )
                continue

            if run.lifecycle_family == "canonical" and _has_presentable_interactions(
                run
            ):
                run = await self._present_interactions(run, lifecycle=lifecycle)
                run = await self._checkpoint(
                    run,
                    updates={"status": "running"},
                    command_id=f"present-interactions:sync:{run.state_version}",
                )
                continue

            recover_initial_state = False
            if run.status == "finalizing":
                assistant = _finalization_candidate(run)
                if assistant is None:
                    return await self._terminate(
                        run, status="failed", reason="finalization candidate missing"
                    )
                return await self._complete(run, assistant)
            if signal.cancelled:
                return await self._terminate(
                    run, status="canceled", reason="cancellation requested"
                )
            if run.status == "waiting_external":
                return KernelRunResult("waiting_external", run)
            if run.status == "awaiting_user":
                return KernelRunResult("awaiting_user", run)
            unflushed = next(
                (
                    batch
                    for batch in run.tool_batches
                    if not batch.results_flushed and not _batch_is_parked(batch)
                ),
                None,
            )
            if unflushed is not None:
                assistant = next(
                    (
                        item
                        for item in run.transcript
                        if isinstance(item, AssistantMessage)
                        and item.message_id == unflushed.assistant_message_id
                    ),
                    None,
                )
                if assistant is None:
                    return await self._terminate(
                        run, status="failed", reason="tool batch assistant missing"
                    )
                recovered = await self._execute_tool_batch(
                    run, assistant, signal, lifecycle=lifecycle
                )
                if recovered == "decide":
                    continue
                if recovered is not None:
                    return recovered
                continue
            torn_assistant = _assistant_missing_tool_batch(run)
            if torn_assistant is not None:
                try:
                    await self._ensure_tool_batch(run, torn_assistant)
                except KernelConflict:
                    return await self._terminate(
                        await self._load(run_id),
                        status="failed",
                        reason="unresolved tool batch could not be recovered",
                    )
                continue

            grace = run.budget.model_turns_used >= run.profile.max_model_turns
            if grace and not run.budget.wrap_up_requested:
                budget = self.budget_policy.request_wrap_up(run.budget)
                notice = SessionNotice(
                    notice_id=self._stable_id(run, "wrap_up", 0),
                    code="wrap_up",
                    content="Tools are disabled. Produce the best final answer now.",
                    created_at=self.clock.now(),
                )
                run = await self._checkpoint(
                    run,
                    updates={
                        "budget": budget,
                        "transcript": [*run.transcript, notice],
                    },
                    command_id=f"wrap-up:{run.run_id}",
                )
            try:
                self.budget_policy.before_model_turn(
                    run.budget,
                    run.profile,
                    now=self.clock.now(),
                )
            except BudgetExceeded as exc:
                return await self._terminate(
                    run, status="budget_exhausted", reason=exc.reason
                )

            run = await self._refresh_resource_manifest(run)
            presentation_backfill = _backfill_presentation_ids(run)
            if presentation_backfill is not run:
                run = await self._checkpoint(
                    run,
                    updates={
                        "tool_batches": presentation_backfill.tool_batches,
                        "transcript": presentation_backfill.transcript,
                    },
                    command_id=f"backfill-presentations:{run.run_id}",
                )
            tools = (
                []
                if run.budget.wrap_up_requested
                else self.tool_catalog.list_tools(run)
            )
            # The structured ask_user action pauses the Run into the unified
            # Execution HITL service. It is only exposed to canonical Runs so
            # the strict run_waiting_input/run_resumed lifecycle owns it.
            if (
                not run.budget.wrap_up_requested
                and run.lifecycle_family == "canonical"
                and self.supervisor_hitl is not None
            ):
                tools = [*tools, REQUEST_USER_INPUT_TOOL_DEFINITION]
                # Forwarding an Agent's questions verbatim is only meaningful
                # while a presented interaction is parked; hide it otherwise.
                if _has_presented_interactions(run):
                    tools = [*tools, _surface_agent_questions_tool_definition(run)]
            try:
                if run.background_context:
                    compiled = self.context_compiler.compile(
                        run,
                        tools=tools,
                        background=run.background_context,
                        summary=run.compaction_summary,
                    )
                else:
                    compiled = self.context_compiler.compile(
                        run, tools=tools, summary=run.compaction_summary
                    )
            except UnresolvedToolBatchError:
                return await self._terminate(
                    run, status="failed", reason="unresolved tool batch"
                )
            if compiled.kind == "context_unfit":
                return await self._terminate(
                    run, status="failed", reason="context_unfit"
                )
            if compiled.kind == "needs_compaction":
                compacted = await self._compact(
                    run,
                    compiled.messages,
                    baseline=compiled.estimated_input_tokens,
                    signal=signal,
                )
                if isinstance(compacted, KernelRunResult):
                    return compacted
                continue
            if (
                run.compaction_baseline_tokens is not None
                and compiled.estimated_input_tokens >= run.compaction_baseline_tokens
            ):
                return await self._terminate(
                    run,
                    status="budget_exhausted",
                    reason="compaction did not reduce context",
                )
            if run.compaction_baseline_tokens is not None:
                run = await self._checkpoint(
                    run,
                    updates={"compaction_baseline_tokens": None},
                    command_id=(
                        f"compaction-validated:{run.run_id}:"
                        f"{run.budget.compactions_used}"
                    ),
                )

            decision_continuation = (
                run.lifecycle_family == "canonical"
                and bool(run.active_internal_turn_id)
                and _has_presented_interactions(run)
            )
            if not decision_continuation:
                # Entering a normal model turn: reset the decision-turn
                # provider-error allowance so a prior unrelated provider error
                # never consumes the single retry of a later decision turn.
                decision_provider_errors = 0
            if decision_continuation:
                request = self._model_request(
                    run,
                    compiled.messages,
                    tools,
                    turn_id=run.active_internal_turn_id,
                )
            else:
                request = self._model_request(run, compiled.messages, tools)
            assistant_message_id = self.id_factory.new_id("assistant")
            if run.lifecycle_family == "canonical":
                run = await self._checkpoint(
                    run,
                    updates={
                        "active_internal_turn_id": request.turn_id,
                        "active_assistant_message_id": assistant_message_id,
                        "active_attempt": 1,
                        "greatest_public_text_offset": 0,
                        "active_public_text": "",
                    },
                    command_id=(
                        f"public-turn-start:{request.turn_id}:{assistant_message_id}"
                        if decision_continuation
                        else f"public-turn-start:{request.turn_id}"
                    ),
                )
            if not decision_continuation:
                await self._emit(
                    lifecycle,
                    "turn_started",
                    run,
                    {"internal_turn_id": request.turn_id, "attempt": 1},
                )
            await self._emit(
                lifecycle,
                "message_started",
                run,
                {
                    "internal_turn_id": request.turn_id,
                    "message_id": assistant_message_id,
                },
            )
            assembler = ModelStreamAssembler()
            public_sanitizer = PublicTextSanitizer(
                secret_values=self.public_secret_values
            )
            public_coalescer = PublicTextCoalescer(
                run_id=run.run_id,
                internal_turn_id=request.turn_id,
                message_id=assistant_message_id,
            )
            turn_started_at = self.clock.now()
            turn_usage: UsageRecord | None = None
            turn_finish_reason: str | None = None
            turn_attempt: int | None = None
            stream = None
            next_event: asyncio.Task[ModelStreamEvent] | None = None
            try:
                stream = self.model_runtime.stream_turn(
                    request, signal=signal
                ).__aiter__()
                next_event = asyncio.create_task(anext(stream))
                while True:
                    done, _ = await asyncio.wait(
                        {next_event},
                        timeout=DEFAULT_COALESCE_INTERVAL_MS / 1000,
                    )
                    if not done:
                        # The provider may stall after a short safe fragment.
                        # Flush the coalescer timer without cancelling/reordering
                        # the outstanding provider ``anext`` operation.
                        run = await self._publish_public_text(
                            lifecycle,
                            run,
                            public_coalescer,
                            "",
                            timer_flush=True,
                        )
                        continue
                    try:
                        event = next_event.result()
                    except StopAsyncIteration:
                        break
                    next_event = asyncio.create_task(anext(stream))
                    assembler.accept(event)
                    if event.kind == "text_delta" and event.delta:
                        decidable = public_sanitizer.feed(event.delta)
                        run = await self._publish_public_text(
                            lifecycle,
                            run,
                            public_coalescer,
                            decidable,
                        )
                    elif event.kind in {"tool_call_start", "finish", "error"}:
                        decidable = public_sanitizer.flush()
                        run = await self._publish_public_text(
                            lifecycle,
                            run,
                            public_coalescer,
                            decidable,
                            semantic_boundary=True,
                        )
                    run = await self._record_model_event(
                        run,
                        request.turn_id,
                        event,
                        message_id=assistant_message_id,
                    )
                    await self._emit_model_event(lifecycle, run, event, request)
                    if event.kind == "usage" and event.usage is not None:
                        turn_usage = event.usage
                    if event.kind == "finish" and event.finish_reason:
                        turn_finish_reason = str(event.finish_reason)
                    if event.attempt is not None:
                        turn_attempt = event.attempt
                final_fragment = public_sanitizer.flush()
                run = await self._publish_public_text(
                    lifecycle,
                    run,
                    public_coalescer,
                    final_fragment,
                    semantic_boundary=True,
                )
                public_text = public_sanitizer.public_text
                model_outcome = assembler.build_outcome(
                    message_id=assistant_message_id,
                    created_at=self.clock.now(),
                )
                if model_outcome.assistant is not None:
                    non_text_parts = [
                        part
                        for part in model_outcome.assistant.content
                        if not isinstance(part, TextPart)
                    ]
                    model_outcome = model_outcome.__class__(
                        kind=model_outcome.kind,
                        assistant=model_outcome.assistant.model_copy(
                            update={
                                "content": (
                                    [TextPart(text=public_text)] if public_text else []
                                )
                                + non_text_parts
                            }
                        ),
                        error_class=model_outcome.error_class,
                        provider_request_id=model_outcome.provider_request_id,
                    )
                await self._emit(
                    lifecycle,
                    "model_turn_completed",
                    run,
                    {
                        "model": request.model.model_id,
                        "provider": request.model.provider,
                        "attempt": turn_attempt,
                        "outcome": _model_turn_outcome(model_outcome),
                        "duration_ms": _elapsed_ms(self.clock.now(), turn_started_at),
                        "usage": _usage_public_fields(turn_usage),
                        "finish_reason": turn_finish_reason,
                    },
                )
            except BudgetExceeded as exc:
                return await self._terminate(
                    run, status="budget_exhausted", reason=exc.reason
                )
            except ModelStreamAssemblyError as exc:
                run, closed_turn_id = await self._close_active_attempt(
                    run,
                    disposition="error",
                    error_summary="The model response could not be assembled.",
                )
                notice = self._assembly_notice(run, exc)
                run = await self._append_notice(run, notice)
                invalid_observations += 1
                if invalid_observations > run.profile.grace_model_turns + 1:
                    return await self._terminate(
                        run, status="failed", reason="invalid model output loop"
                    )
                await self._emit(
                    lifecycle,
                    "model_retry_scheduled",
                    run,
                    {
                        "internal_turn_id": closed_turn_id or request.turn_id,
                        "attempt": 2,
                        "error_class": "assembly_error",
                        "retry_delay_ms": 0,
                    },
                )
                continue
            except ValueError:
                return await self._terminate(
                    run, status="failed", reason="public_text_oversized"
                )
            finally:
                if next_event is not None:
                    if not next_event.done():
                        next_event.cancel()
                    try:
                        await next_event
                    except (asyncio.CancelledError, Exception):
                        pass
                if stream is not None:
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        try:
                            await close_stream()
                        except (asyncio.CancelledError, Exception):
                            pass

            if model_outcome.kind == "aborted":
                return await self._terminate(
                    run, status="canceled", reason="model request aborted"
                )
            if model_outcome.kind == "context_overflow":
                if (
                    self.context_compactor is None
                    or run.budget.compactions_used >= run.profile.max_compactions
                ):
                    return await self._terminate(
                        run, status="budget_exhausted", reason="context overflow"
                    )
                run, closed_turn_id = await self._close_active_attempt(
                    run,
                    disposition="error",
                    error_summary="The model context limit was exceeded.",
                )
                compacted = await self._compact(
                    run,
                    list(compiled.messages),
                    baseline=compiled.estimated_input_tokens,
                    signal=signal,
                )
                if isinstance(compacted, KernelRunResult):
                    return compacted
                run = compacted
                await self._emit(
                    lifecycle,
                    "model_retry_scheduled",
                    run,
                    {
                        "internal_turn_id": closed_turn_id or request.turn_id,
                        "attempt": 2,
                        "error_class": "context_overflow",
                        "retry_delay_ms": 0,
                    },
                )
                continue
            if model_outcome.kind == "provider_error":
                if run.lifecycle_family == "canonical" and _has_presented_interactions(
                    run
                ):
                    # Decision-turn provider failure: keep the presented
                    # interaction open, retry once, then degrade to the user.
                    decision_provider_errors += 1
                    if decision_provider_errors > 1:
                        await self._degrade_presented_interactions(
                            run, lifecycle, reason="provider_error"
                        )
                        run = await self._checkpoint(
                            run,
                            updates={"status": "awaiting_user"},
                            command_id=(
                                f"degrade-to-user:provider-error:{request.turn_id}"
                            ),
                        )
                        return KernelRunResult("awaiting_user", run)
                    await self._emit(
                        lifecycle,
                        "model_retry_scheduled",
                        run,
                        {
                            "internal_turn_id": request.turn_id,
                            "attempt": 2,
                            "error_class": "provider_error",
                            "retry_delay_ms": 0,
                        },
                    )
                    continue
                invalid_observations += 1
                run, closed_turn_id = await self._close_active_attempt(
                    run,
                    disposition="error",
                    error_summary="The model provider did not complete the response.",
                )
                notice = SessionNotice(
                    notice_id=self._stable_id(
                        run,
                        model_outcome.error_class or "provider_error",
                        run.budget.model_turns_used,
                    ),
                    code=model_outcome.error_class or "provider_error",
                    content="The previous model attempt failed; retry within bounds.",
                    created_at=self.clock.now(),
                )
                run = await self._append_notice(run, notice)
                if invalid_observations > run.profile.grace_model_turns + 1:
                    return await self._terminate(
                        run, status="failed", reason="provider error loop"
                    )
                await self._emit(
                    lifecycle,
                    "model_retry_scheduled",
                    run,
                    {
                        "internal_turn_id": closed_turn_id or request.turn_id,
                        "attempt": 2,
                        "error_class": "provider_error",
                        "retry_delay_ms": 0,
                    },
                )
                continue
            assistant = model_outcome.assistant
            if assistant is None:
                return await self._terminate(
                    run, status="failed", reason="missing assistant outcome"
                )
            run = await self._checkpoint(
                run,
                updates={
                    "budget": self.budget_policy.record_assistant_turn(
                        run.budget, grace=grace
                    )
                },
                command_id=(
                    f"assistant-turn:{request.turn_id}:{assistant.message_id}"
                    if decision_continuation
                    else f"assistant-turn:{request.turn_id}"
                ),
            )
            run = await self._append_assistant(run, assistant)
            # Commentary is public only after the complete source-ordered
            # declared Tool batch is durable, so restart recovery never has to
            # guess declarations from public text.
            if assistant.tool_calls:
                run = await self._ensure_tool_batch(run, assistant)
            assistant_public_text = _assistant_text(assistant)
            await self._emit(
                lifecycle,
                "message_completed",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{request.turn_id}:"
                        f"{assistant.message_id}:message_end"
                    ),
                    "internal_turn_id": request.turn_id,
                    "message_id": assistant.message_id,
                    "stop_reason": ("tool_use" if assistant.tool_calls else "stop"),
                    "disposition": ("commentary" if assistant.tool_calls else "final"),
                    "text": assistant_public_text,
                },
            )
            if run.lifecycle_family == "canonical":
                run = await self._checkpoint(
                    run,
                    updates={
                        "active_assistant_message_id": None,
                        "active_public_text": "",
                        "greatest_public_text_offset": 0,
                    },
                    command_id=f"public-message-end:{assistant.message_id}",
                )
            if assistant.tool_calls:
                await self._emit(
                    lifecycle,
                    "orchestrator_decision",
                    run,
                    {
                        "plan_steps": [
                            {
                                "agent": self._tool_label(run, call.tool_name)
                                or call.tool_name,
                                "summary": _task_text(call.arguments),
                            }
                            for call in assistant.tool_calls
                        ],
                        "reason": _assistant_text(assistant),
                    },
                )
            if not assistant.tool_calls:
                if run.lifecycle_family == "canonical" and _has_presented_interactions(
                    run
                ):
                    # F5 degrade: the model produced no effective tool call
                    # while parked interactions remain open. Publish them to the
                    # user instead of failing completion on suspended entries.
                    await self._degrade_presented_interactions(
                        run, lifecycle, reason="decision_turn_inconclusive"
                    )
                    run = await self._checkpoint(
                        run,
                        updates={"status": "awaiting_user"},
                        command_id=(f"degrade-to-user:{assistant.message_id}"),
                    )
                    return KernelRunResult("awaiting_user", run)
                await self._emit(
                    lifecycle,
                    "turn_completed",
                    run,
                    {
                        "internal_turn_id": request.turn_id,
                        "message_id": assistant.message_id,
                        "tool_call_ids": [],
                        "status": "completed",
                    },
                )
                if run.lifecycle_family == "canonical":
                    run = await self._checkpoint(
                        run,
                        updates={
                            "active_internal_turn_id": None,
                            "active_assistant_message_id": None,
                            "active_attempt": None,
                        },
                        command_id=f"public-turn-end:{request.turn_id}",
                    )
                return await self._complete(run, assistant)
            if run.budget.wrap_up_requested:
                run = await self._reject_grace_tools(run, assistant)
                continue
            try:
                result = await self._execute_tool_batch(
                    run, assistant, signal, lifecycle=lifecycle
                )
            except asyncio.CancelledError:
                if signal.cancelled:
                    current = await self._load(run.run_id)
                    return await self._terminate(
                        current, status="canceled", reason="tool execution canceled"
                    )
                raise
            if result == "retry":
                continue
            if result == "decide":
                # Model-first HITL decision turn continues within the same
                # internal turn; the loop re-compiles and calls the model.
                continue
            if result is not None:
                return result
            completed_run = await self._load(run.run_id)
            closing_message_id = assistant.message_id
            if completed_run.lifecycle_family == "canonical":
                closure = _canonical_turn_closure(completed_run, request.turn_id)
                if closure is None:
                    # The active internal turn still has open entries (for
                    # example a presented interaction awaiting a model join
                    # reply). Defer turn_end; the loop continues to the next
                    # decision turn within the same internal turn.
                    continue
                closing_message_id = closure.message_id
                turn_public_ids = list(closure.public_tool_call_ids)
            else:
                turn_public_ids = [
                    entry.opaque_public_call_id
                    for batch in completed_run.tool_batches
                    if batch.assistant_message_id == assistant.message_id
                    for entry in batch.entries
                    if entry.opaque_public_call_id is not None
                ]
            await self._emit(
                lifecycle,
                "turn_completed",
                completed_run,
                {
                    "internal_turn_id": request.turn_id,
                    "message_id": closing_message_id,
                    "tool_call_ids": turn_public_ids,
                    "status": "completed",
                },
            )
            if completed_run.lifecycle_family == "canonical":
                run = await self._checkpoint(
                    completed_run,
                    updates={
                        "active_internal_turn_id": None,
                        "active_assistant_message_id": None,
                        "active_attempt": None,
                    },
                    command_id=f"public-turn-end:{request.turn_id}",
                )
                continue

    async def terminalize(
        self,
        run_id: str,
        *,
        status: Literal["failed", "canceled", "budget_exhausted"],
        reason: str,
        cancellation_cause: (
            Literal["user_requested", "room_closed", "shutdown", "policy"] | None
        ) = None,
        lifecycle: KernelLifecycle | None = None,
    ) -> KernelRunResult:
        """Public family-specific terminalization entrypoint for HITL/recovery."""

        self._lifecycle_context.set(lifecycle)
        return await self._terminate(
            await self._load(run_id),
            status=status,
            reason=reason,
            cancellation_cause=cancellation_cause,
        )

    async def observe_tool(
        self,
        run_id: str,
        observation: ToolObservation,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None = None,
    ) -> KernelRunResult:
        self._lifecycle_context.set(lifecycle)
        run = await self._load(run_id)
        batch_index, entry_index = _find_invocation(run, observation.invocation_id)
        if batch_index is None or entry_index is None:
            raise KeyError(observation.invocation_id)
        entry = run.tool_batches[batch_index].entries[entry_index]
        if entry.invocation is None:
            raise KernelConflict("tool observation target has no invocation")
        if observation.invocation_id != entry.invocation.invocation_id:
            raise ValueError("observation invocation does not correlate")
        if isinstance(observation.outcome, ToolResult):
            if (
                observation.outcome.call_id != entry.call_id
                or observation.outcome.tool_name != entry.tool_name
            ):
                raise ValueError("observation result does not correlate")
        elif observation.outcome.invocation_id != entry.invocation.invocation_id:
            raise ValueError("observation suspension does not correlate")
        if run.status in {"completed", "failed", "canceled", "budget_exhausted"}:
            if observation.observation_id in entry.processed_observation_ids:
                return KernelRunResult(_outcome_for_status(run.status), run)
            raise KernelConflict("terminal Run cannot accept a new tool observation")
        if run.status not in {"waiting_external", "awaiting_user"}:
            raise KernelConflict("tool observations require a suspended Run")
        was_awaiting_user = run.status == "awaiting_user"
        if entry.state not in {"waiting_external", "input_required", "auth_required"}:
            raise KernelConflict("tool observation target is not suspended")
        if observation.observation_id in entry.processed_observation_ids:
            return KernelRunResult(
                "waiting_external"
                if run.status == "waiting_external"
                else "awaiting_user",
                run,
            )
        batches = list(run.tool_batches)
        batch = batches[batch_index]
        entries = list(batch.entries)
        new_interaction = False
        if isinstance(observation.outcome, ToolResult):
            state = "terminal"
            result = observation.outcome
            interaction_update: dict[str, object] = {}
        else:
            state = observation.outcome.status
            result = entry.buffered_terminal_result
            new_interaction = (
                entry.interaction_id != observation.outcome.interaction_id
                or entry.interaction_fingerprint
                != observation.outcome.interaction_fingerprint
                or entry.interaction_questions != observation.outcome.questions
            )
            interaction_update = {
                "suspended_call_record_id": observation.outcome.call_record_id,
                "interaction_id": observation.outcome.interaction_id,
                "interaction_fingerprint": observation.outcome.interaction_fingerprint,
                "interaction_questions": observation.outcome.questions,
                # A distinct continuation round must receive a new private
                # presentation and another model-first decision. Reusing the
                # prior flag/ID would either bypass the model or target the
                # answered questionnaire.
                "presented": False if new_interaction else entry.presented,
                "presentation_id": None if new_interaction else entry.presentation_id,
            }
        entries[entry_index] = entry.model_copy(
            update={
                "state": state,
                "buffered_terminal_result": result,
                "processed_observation_ids": [
                    *entry.processed_observation_ids,
                    observation.observation_id,
                ],
                **interaction_update,
            }
        )
        batch = batch.model_copy(update={"entries": entries})
        batches[batch_index] = batch
        if entry.suspended_call_record_id is not None and (
            isinstance(observation.outcome, ToolResult) or new_interaction
        ):
            # A terminal parent or a distinct follow-up interaction proves the
            # previously surfaced round has been consumed. Close its public
            # surface row before presenting the next private round; otherwise
            # canonical fold retains a phantom open Tool and the Composer sees
            # answered questions as queued.
            for surface_batch_index, surface_batch in enumerate(batches):
                if surface_batch_index == batch_index:
                    continue
                surface_entries = list(surface_batch.entries)
                surface_changed = False
                for surface_entry_index, surface_entry in enumerate(surface_entries):
                    if (
                        surface_entry.surface_for_call_record_id
                        == entry.suspended_call_record_id
                        and surface_entry.state in {"input_required", "auth_required"}
                    ):
                        surface_result = (
                            observation.outcome.model_copy(
                                update={
                                    "call_id": surface_entry.call_id,
                                    "tool_name": SURFACE_AGENT_QUESTIONS_TOOL_NAME,
                                }
                            )
                            if isinstance(observation.outcome, ToolResult)
                            else ToolResult(
                                call_id=surface_entry.call_id,
                                tool_name=SURFACE_AGENT_QUESTIONS_TOOL_NAME,
                                status="completed",
                                content=[
                                    TextPart(
                                        text=(
                                            "The user's answers were applied; the "
                                            "Agent requested follow-up input."
                                        )
                                    )
                                ],
                                artifact_refs=[],
                            )
                        )
                        surface_entries[surface_entry_index] = surface_entry.model_copy(
                            update={
                                "state": "terminal",
                                "buffered_terminal_result": surface_result,
                            }
                        )
                        surface_changed = True
                if surface_changed:
                    batches[surface_batch_index] = surface_batch.model_copy(
                        update={"entries": surface_entries}
                    )
        all_terminal = all(item.state == "terminal" for item in batch.entries)
        updates: dict[str, object] = {"tool_batches": batches}
        transcript = run.transcript
        if isinstance(observation.outcome, ToolResult):
            updates["artifact_refs"] = _merge_artifact_refs(
                run.artifact_refs, [observation.outcome]
            )
            # A user answer or terminal Agent result breaks the auto-reply
            # chain: the run-level join counter resets.
            if run.consecutive_model_joins:
                updates["consecutive_model_joins"] = 0
        if all_terminal:
            transcript, batch = _flush_batch(transcript, batch, self.clock.now())
            batches[batch_index] = batch
            updates.update(
                tool_batches=batches,
                transcript=transcript,
                status="running",
            )
        else:
            updates["status"] = _wait_status(batch)
        # Flush any other batch fully terminalized by the surface-entry closeout
        # so its ToolResultMessage resolves in the model context.
        flushed_other_indices: list[int] = []
        for flush_index, flush_batch in enumerate(list(batches)):
            if flush_index == batch_index or flush_batch.results_flushed:
                continue
            if not all(item.state == "terminal" for item in flush_batch.entries):
                continue
            transcript, flushed = _flush_batch(
                transcript, flush_batch, self.clock.now()
            )
            batches[flush_index] = flushed
            flushed_other_indices.append(flush_index)
            updates.update(
                tool_batches=batches,
                transcript=transcript,
                status="running",
            )
        run = await self._checkpoint(
            run,
            updates=updates,
            command_id=f"tool-observation:{observation.observation_id}",
        )
        if not all_terminal:
            # A follow-up interaction may have terminalized the prior
            # surface_agent_questions batch while leaving the parent suspended.
            # Publish those checkpointed Tool ends before presenting the next
            # round so canonical fold never observes two open surface rows.
            for flush_index in flushed_other_indices:
                run = await self._publish_checkpointed_tool_terminals(
                    run,
                    flush_index,
                    lifecycle=lifecycle,
                    internal_turn_id=(
                        run.active_internal_turn_id or entry.assistant_message_id
                    ),
                )
            if run.lifecycle_family == "canonical" and _has_presentable_interactions(
                run
            ):
                # Model-first re-suspension: the Agent asked a NEW question
                # after a user answer or model join. Present it to the model
                # rather than auto-publishing to the user.
                run = await self._present_interactions(run, lifecycle=lifecycle)
                run = await self._checkpoint(
                    run,
                    updates={
                        "status": "running",
                        "tool_batches": run.tool_batches,
                        "transcript": run.transcript,
                    },
                    command_id=(f"present-interactions:{observation.observation_id}"),
                )
                return await self.run(run_id, signal=signal, lifecycle=lifecycle)
            return KernelRunResult(
                "awaiting_user"
                if run.status == "awaiting_user"
                else "waiting_external",
                run,
            )
        internal_turn_id = run.active_internal_turn_id or entry.assistant_message_id
        if was_awaiting_user:
            resumed_entry = run.tool_batches[batch_index].entries[entry_index]
            update_index = resumed_entry.public_update_index + 1
            await self._emit(
                lifecycle,
                "tool_execution_updated",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{resumed_entry.opaque_public_call_id}:"
                        f"update:{update_index}"
                    ),
                    "call_id": resumed_entry.call_id,
                    "public_call_id": resumed_entry.opaque_public_call_id
                    or _opaque_public_call_id(run.run_id, resumed_entry.call_id),
                    "internal_turn_id": internal_turn_id,
                    "tool_name": resumed_entry.tool_name,
                    "agent_label": self._tool_label(run, resumed_entry.tool_name),
                    "update_index": update_index,
                    "status": "running",
                    "partial_result": "",
                },
            )
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                invocation=resumed_entry.invocation,
                acceptance=resumed_entry.acceptance,
                opaque_public_call_id=resumed_entry.opaque_public_call_id,
                result=resumed_entry.buffered_terminal_result,
                public_update_index=update_index,
                command=(f"public-tool-resumed:{resumed_entry.call_id}:{update_index}"),
            )
        run = await self._publish_checkpointed_tool_terminals(
            run,
            batch_index,
            lifecycle=lifecycle,
            internal_turn_id=internal_turn_id,
        )
        for flush_index in flushed_other_indices:
            run = await self._publish_checkpointed_tool_terminals(
                run,
                flush_index,
                lifecycle=lifecycle,
                internal_turn_id=internal_turn_id,
            )
        batch = run.tool_batches[batch_index]
        for item in batch.entries:
            await self._emit(
                lifecycle,
                "message_completed",
                run,
                {
                    "call_id": item.call_id,
                    "message_kind": "tool_result",
                    "agent_label": self._tool_label(run, result.tool_name)
                    if (result := item.buffered_terminal_result) is not None
                    else None,
                    "binding_id": self._tool_binding_id(run, result.tool_name)
                    if (result := item.buffered_terminal_result) is not None
                    else None,
                    "result_text": _result_text(result)
                    if (result := item.buffered_terminal_result) is not None
                    else None,
                    "result_status": result.status
                    if (result := item.buffered_terminal_result) is not None
                    else None,
                },
            )
        accepted_public_ids = [
            item.opaque_public_call_id
            for item in batch.entries
            if item.opaque_public_call_id is not None
        ]
        closing_message_id = batch.assistant_message_id
        if run.lifecycle_family == "canonical":
            closure = _canonical_turn_closure(run, internal_turn_id)
            if closure is None:
                # The active internal turn still has open entries (presented
                # agent interactions awaiting a model join reply). Defer
                # turn_end and continue the model-first decision loop.
                return await self.run(run_id, signal=signal, lifecycle=lifecycle)
            closing_message_id = closure.message_id
            turn_public_ids = list(closure.public_tool_call_ids)
        else:
            turn_public_ids = accepted_public_ids
        await self._emit(
            lifecycle,
            "turn_completed",
            run,
            {
                "public_event_id": (
                    f"public:{run.run_id}:{internal_turn_id}:turn_end:completed"
                ),
                "internal_turn_id": internal_turn_id,
                "message_id": closing_message_id,
                "tool_call_ids": turn_public_ids,
                "status": "completed",
            },
        )
        if run.lifecycle_family == "canonical":
            run = await self._checkpoint(
                run,
                updates={
                    "active_internal_turn_id": None,
                    "active_assistant_message_id": None,
                    "active_attempt": None,
                    "active_public_text": "",
                    "greatest_public_text_offset": 0,
                },
                command_id=f"public-turn-end:{internal_turn_id}",
            )
        return await self.run(run_id, signal=signal, lifecycle=lifecycle)

    async def _suspend_for_supervisor_input(
        self,
        run: OrchestratorRunState,
        batch_index: int,
        entry_index: int,
        call: ToolCall,
        assistant: AssistantMessage,
    ) -> OrchestratorRunState | None:
        """Create the unified HITL interaction and suspend the ask_user call.

        Returns None after durably rejecting an invalid declaration (the batch
        continues with the preacceptance-failed path). On success the entry is
        ``input_required`` and the batch tail publishes the suspension update
        and the ``awaiting_user`` Run status.
        """
        errors = list(
            Draft202012Validator(
                REQUEST_USER_INPUT_TOOL_DEFINITION.input_schema
            ).iter_errors(call.arguments)
        )
        if errors:
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "invalid_tool_call",
                    "request_user_input arguments failed schema validation",
                ),
                command=f"invalid-ask:{call.call_id}",
            )
            return None
        question = str(call.arguments.get("question") or "").strip()
        choices = _request_user_input_choices(call.arguments)
        if any(_has_ellipsis_placeholder(choice) for choice in choices):
            # A placeholder choice (e.g. "Cloud providers: ...") cannot be
            # selected as a real answer and usually means the model merged
            # several independent questions into one choice list. Reject so
            # the model retries with real answers or omits choices.
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "invalid_tool_call",
                    "request_user_input choices contain placeholder text "
                    "(...). Provide real, mutually exclusive answers or omit "
                    "choices for free-form input.",
                ),
                command=f"invalid-ask-choices:{call.call_id}",
            )
            return None
        if _has_presented_interactions(run):
            # A composed single-question ask is structurally unable to keep an
            # Agent's typed questions separate (the model merges them, e.g.
            # "Please reply with both answers"). While questions are parked for
            # a decision, escalation must go through the verbatim forward tool.
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "invalid_tool_call",
                    _REQUEST_USER_INPUT_PENDING_AGENT_QUESTIONS_ERROR,
                ),
                command=f"invalid-ask-pending-agent:{call.call_id}",
            )
            return None
        interaction_id = self._stable_id(run, "ask", call.call_id)
        assert self.supervisor_hitl is not None
        try:
            await self.supervisor_hitl(
                run=run,
                interaction_id=interaction_id,
                call_id=call.call_id,
                question=question,
                choices=choices,
            )
        except Exception as exc:
            _kernel_logger.error(
                "ask_user supervisor input creation failed",
                extra={
                    "run_id": run.run_id,
                    "call_id": call.call_id,
                    "interaction_id": interaction_id,
                    "ask_error": f"{type(exc).__name__}: {exc}",
                },
                exc_info=True,
            )
            raise
        invocation = ToolInvocation(
            invocation_id=call.call_id,
            run_id=run.run_id,
            expected_run_version=run.state_version,
            assistant_message_id=assistant.message_id,
            source_index=entry_index,
            causation_id=assistant.message_id,
            idempotency_key=self._stable_id(run, "ask-invocation", call.call_id),
            tool=ResolvedTool(
                definition=REQUEST_USER_INPUT_TOOL_DEFINITION,
                binding=ToolBindingRef(
                    binding_id=f"ask:{run.run_id}:{call.call_id}",
                    binding_digest="structured-ask-user",
                ),
            ),
            arguments=call.arguments,
            deadline_at=run.budget.deadline_at,
        )
        return await self._update_entry(
            run,
            batch_index,
            entry_index,
            state="input_required",
            invocation=invocation,
            opaque_public_call_id=_opaque_public_call_id(run.run_id, call.call_id),
            command=f"ask-user:{call.call_id}",
        )

    async def _suspend_for_surface_forward(
        self,
        run: OrchestratorRunState,
        batch_index: int,
        entry_index: int,
        call: ToolCall,
        assistant: AssistantMessage,
        *,
        lifecycle: KernelLifecycle | None,
    ) -> OrchestratorRunState | None:
        """Publish a parked interaction verbatim and suspend into awaiting_user.

        Returns None after durably rejecting an invalid declaration (the batch
        continues with the preacceptance-failed path). On success the surface
        entry is ``input_required`` and points at the parked parent call; the
        user's answer flows through the parent continuation, so
        ``observe_tool`` closes this entry when the parent resolves.
        """
        surface_definition = _surface_agent_questions_tool_definition(run)
        errors = list(
            Draft202012Validator(surface_definition.input_schema).iter_errors(
                call.arguments
            )
        )
        if errors:
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "invalid_tool_call",
                    "surface_agent_questions arguments failed schema validation",
                ),
                command=f"invalid-surface:{call.call_id}",
            )
            return None
        raw_presentation_id = call.arguments.get("presentation_id")
        presentation_id = (
            str(raw_presentation_id).strip()
            if raw_presentation_id is not None
            else None
        )
        located = _find_presented_entry_by_presentation(run, presentation_id)
        if located is None:
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "surface_target_not_presented",
                    "The Agent interaction to forward is not currently "
                    "awaiting a decision.",
                ),
                command=f"surface-target-missing:{call.call_id}",
            )
            return None
        _pb_index, _pe_index, parked_entry = located
        interaction_id = parked_entry.interaction_id
        parent_call_record_id = parked_entry.suspended_call_record_id
        if interaction_id is None:
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "surface_target_not_presented",
                    "The Agent interaction to forward has no durable target.",
                ),
                command=f"surface-target-no-interaction:{call.call_id}",
            )
            return None
        if parent_call_record_id is None:
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "surface_target_not_presented",
                    "The Agent interaction to forward has no parked call.",
                ),
                command=f"surface-target-no-call:{call.call_id}",
            )
            return None

        # Publish before opening the public tool row: a publication failure is
        # a rejection (mirrors schema/target rejection) and must not leave a
        # phantom "running" surface tool that the fold can never close.
        try:
            await self.tool_runtime.publish_parked_interaction(
                call_record_id=parent_call_record_id,
                interaction_id=interaction_id,
            )
        except Exception:
            _kernel_logger.exception(
                "surface publication failed",
                extra={
                    "run_id": run.run_id,
                    "call_record_id": parent_call_record_id,
                    "interaction_id": interaction_id,
                },
            )
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=_tool_error(
                    call,
                    "surface_publication_failed",
                    "The Agent questions could not be published to the user.",
                ),
                command=f"surface-publication-failed:{call.call_id}",
            )
            return None

        public_call_id = _opaque_public_call_id(run.run_id, call.call_id)
        await self._emit(
            lifecycle,
            "tool_execution_started",
            run,
            {
                "public_event_id": f"public:{run.run_id}:{public_call_id}:start",
                "call_id": call.call_id,
                "public_call_id": public_call_id,
                "internal_turn_id": run.active_internal_turn_id or assistant.message_id,
                "tool_name": call.tool_name,
                "agent_label": self._tool_label(run, call.tool_name),
                # presentation_id is private model routing identity and must
                # never enter public lifecycle/SSE/snapshot payloads.
                "arguments": {},
            },
        )
        await self._emit(
            lifecycle,
            "model_decision",
            run,
            {
                "internal_turn_id": run.active_internal_turn_id or assistant.message_id,
                "decision": "forwarded_to_user",
                "agent_label": self._tool_label(run, parked_entry.tool_name)
                or parked_entry.tool_name,
                "question_summary": _interaction_question_summary(
                    parked_entry.interaction_questions
                ),
            },
        )

        batches = list(run.tool_batches)
        batch = batches[batch_index]
        entries = list(batch.entries)
        entries[entry_index] = entries[entry_index].model_copy(
            update={
                "state": "input_required",
                "opaque_public_call_id": public_call_id,
                "suspended_call_record_id": parent_call_record_id,
                "surface_for_call_record_id": parent_call_record_id,
                "presentation_id": _presentation_id(run, parked_entry),
                "interaction_id": interaction_id,
                "interaction_fingerprint": parked_entry.interaction_fingerprint,
                "interaction_questions": parked_entry.interaction_questions,
            }
        )
        batches[batch_index] = batch.model_copy(update={"entries": entries})
        return await self._checkpoint(
            run,
            updates={"tool_batches": batches},
            command_id=f"surface-questions:{call.call_id}",
        )

    @staticmethod
    def _tool_binding_id(run: OrchestratorRunState, tool_name: str) -> str | None:
        """Resolve the frozen binding id for a tool name."""
        if run.tool_catalog is None:
            return None
        for entry in run.tool_catalog.entries:
            if entry.definition.name == tool_name:
                return entry.binding.binding_id
        return None

    @staticmethod
    def _tool_label(run: OrchestratorRunState, tool_name: str) -> str | None:
        """Resolve the user-facing agent label for a tool name."""
        if run.tool_catalog is None:
            return None
        for entry in run.tool_catalog.entries:
            if entry.definition.name == tool_name:
                label = entry.definition.label.strip()
                return label or None
        return None

    def _tool_completed_payload(
        self,
        run: OrchestratorRunState,
        call: ToolCall,
        outcome: ToolResult | ToolSuspension,
        *,
        started_at: datetime,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "call_id": call.call_id,
            "public_call_id": _opaque_public_call_id(run.run_id, call.call_id),
            "internal_turn_id": run.active_internal_turn_id or call.call_id,
            "status": outcome.status,
            "tool_name": call.tool_name,
            "agent_label": self._tool_label(run, call.tool_name),
            "duration_ms": _elapsed_ms(self.clock.now(), started_at),
        }
        if isinstance(outcome, ToolResult):
            payload["result_status"] = outcome.status
            payload["result_error_code"] = outcome.error_code
            payload["result_error_message"] = outcome.error_message
            payload["result_text"] = _result_text(outcome)
        return payload

    async def _present_interactions(
        self,
        run: OrchestratorRunState,
        *,
        lifecycle: KernelLifecycle | None,
    ) -> OrchestratorRunState:
        """Surface parked agent interactions to the model as tool_interaction.

        Idempotent by deterministic ``interaction:<call_id>:<fingerprint>``
        message identity. Only canonical Runs use the model-first flow.
        ``interaction_received`` is emitted only on first presentation.
        """
        if run.lifecycle_family != "canonical":
            return run
        now = self.clock.now()
        transcript = list(run.transcript)
        batches = list(run.tool_batches)
        newly_presented: list[ToolBatchEntry] = []
        for batch_index, batch in enumerate(batches):
            entries = list(batch.entries)
            changed = False
            for entry_index, entry in enumerate(entries):
                if entry.state not in {"input_required", "auth_required"}:
                    continue
                if entry.presented:
                    continue
                # A surface_agent_questions entry is already user-visible;
                # never re-present it back to the model.
                if entry.surface_for_call_record_id is not None:
                    continue
                if (
                    entry.interaction_id is None
                    or entry.interaction_fingerprint is None
                ):
                    continue
                message_id = (
                    f"interaction:{entry.call_id}:{entry.interaction_fingerprint}"
                )
                presentation_id = _presentation_id(run, entry)
                interaction_message = ToolInteractionMessage(
                    message_id=message_id,
                    call_id=entry.call_id,
                    tool_name=entry.tool_name,
                    presentation_id=presentation_id,
                    interaction_id=entry.interaction_id,
                    interaction_fingerprint=entry.interaction_fingerprint,
                    questions=entry.interaction_questions,
                    artifact_refs=[],
                    agent_label=self._tool_label(run, entry.tool_name),
                    created_at=now,
                )
                existing_index = next(
                    (
                        index
                        for index, message in enumerate(transcript)
                        if isinstance(message, ToolInteractionMessage)
                        and message.message_id == message_id
                    ),
                    None,
                )
                if existing_index is None:
                    transcript.append(interaction_message)
                else:
                    # Backfill presentation identity/typed fields for Runs
                    # checkpointed before this contract existed.
                    transcript[existing_index] = interaction_message.model_copy(
                        update={"created_at": transcript[existing_index].created_at}
                    )
                presented_entry = entry.model_copy(
                    update={"presented": True, "presentation_id": presentation_id}
                )
                entries[entry_index] = presented_entry
                newly_presented.append(presented_entry)
                changed = True
            if changed:
                batches[batch_index] = batch.model_copy(update={"entries": entries})
        run = run.model_copy(update={"transcript": transcript, "tool_batches": batches})
        for entry in newly_presented:
            await self._emit(
                lifecycle,
                "model_decision",
                run,
                {
                    "internal_turn_id": run.active_internal_turn_id
                    or entry.assistant_message_id,
                    "decision": "interaction_received",
                    "agent_label": self._tool_label(run, entry.tool_name),
                    "question_summary": _interaction_question_summary(
                        entry.interaction_questions
                    ),
                },
            )
        return run

    async def _degrade_presented_interactions(
        self,
        run: OrchestratorRunState,
        lifecycle: KernelLifecycle | None,
        *,
        reason: str,
    ) -> None:
        """Publish open presented interactions to the user (F5 degrade)."""
        for batch in run.tool_batches:
            for entry in batch.entries:
                if (
                    entry.state not in {"input_required", "auth_required"}
                    or not entry.presented
                    or entry.interaction_id is None
                    or entry.suspended_call_record_id is None
                ):
                    continue
                try:
                    await self.tool_runtime.publish_parked_interaction(
                        call_record_id=entry.suspended_call_record_id,
                        interaction_id=entry.interaction_id,
                    )
                except Exception:
                    _kernel_logger.exception(
                        "degrade publication failed for interaction",
                        extra={
                            "run_id": run.run_id,
                            "call_record_id": entry.suspended_call_record_id,
                            "interaction_id": entry.interaction_id,
                        },
                    )
                await self._emit(
                    lifecycle,
                    "model_decision",
                    run,
                    {
                        "internal_turn_id": run.active_internal_turn_id
                        or entry.assistant_message_id,
                        "decision": "degraded_to_user",
                        "agent_label": self._tool_label(run, entry.tool_name),
                        "question_summary": _interaction_question_summary(
                            entry.interaction_questions
                        ),
                        "reason": reason,
                    },
                )

    async def _publish_checkpointed_tool_terminals(
        self,
        run: OrchestratorRunState,
        batch_index: int,
        *,
        lifecycle: KernelLifecycle | None,
        internal_turn_id: str,
    ) -> OrchestratorRunState:
        for entry_index in range(len(run.tool_batches[batch_index].entries)):
            entry = run.tool_batches[batch_index].entries[entry_index]
            result = entry.buffered_terminal_result
            if (
                entry.state != "terminal"
                or result is None
                or entry.public_terminal_emitted
            ):
                continue
            # ask_user entries never pass through tool_runtime.accept, but they
            # carry an opaque public call id from suspension, so their
            # terminal (the user answer as ToolResult) is publishable too.
            if entry.acceptance is None and entry.opaque_public_call_id is None:
                continue
            await self._emit(
                lifecycle,
                "tool_execution_completed",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{entry.opaque_public_call_id}:end"
                    ),
                    "call_id": entry.call_id,
                    "public_call_id": entry.opaque_public_call_id
                    or _opaque_public_call_id(run.run_id, entry.call_id),
                    "internal_turn_id": internal_turn_id,
                    "status": result.status,
                    "result_status": result.status,
                    "tool_name": entry.tool_name,
                    "agent_label": self._tool_label(run, entry.tool_name),
                    "duration_ms": 0,
                    # The ask_user answer is the user's private input: the
                    # public end event carries no result text.
                    "result_text": (
                        ""
                        if entry.tool_name == REQUEST_USER_INPUT_TOOL_NAME
                        else _result_text(result)
                    ),
                },
            )
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="terminal",
                result=result,
                public_terminal_emitted=True,
                command=f"public-tool-end:{entry.call_id}",
            )
        return run

    async def _publish_checkpointed_suspensions(
        self,
        run: OrchestratorRunState,
        batch_index: int,
        *,
        lifecycle: KernelLifecycle | None,
        internal_turn_id: str,
    ) -> OrchestratorRunState:
        for entry_index in range(len(run.tool_batches[batch_index].entries)):
            entry = run.tool_batches[batch_index].entries[entry_index]
            if entry.state not in {
                "waiting_external",
                "input_required",
                "auth_required",
            }:
                continue
            update_index = entry.public_update_index + 1
            await self._emit(
                lifecycle,
                "tool_execution_updated",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{entry.opaque_public_call_id}:"
                        f"update:{update_index}"
                    ),
                    "call_id": entry.call_id,
                    "public_call_id": entry.opaque_public_call_id
                    or _opaque_public_call_id(run.run_id, entry.call_id),
                    "internal_turn_id": internal_turn_id,
                    "tool_name": entry.tool_name,
                    "agent_label": self._tool_label(run, entry.tool_name),
                    "update_index": update_index,
                    "status": "suspended",
                    "partial_result": "",
                },
            )
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state=entry.state,
                invocation=entry.invocation,
                acceptance=entry.acceptance,
                opaque_public_call_id=entry.opaque_public_call_id,
                result=entry.buffered_terminal_result,
                public_update_index=update_index,
                command=f"public-tool-suspended:{entry.call_id}:{update_index}",
            )
        return run

    async def _ensure_tool_batch(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState:
        existing = next(
            (
                batch
                for batch in run.tool_batches
                if batch.assistant_message_id == assistant.message_id
            ),
            None,
        )
        if existing is not None:
            return run
        call_ids = {call.call_id for call in assistant.tool_calls}
        if not call_ids:
            raise KernelConflict("tool batch reconstruction is inconsistent")
        unresolved = unresolved_call_ids(run.transcript)
        presented_call_ids = {
            message.call_id
            for message in run.transcript
            if isinstance(message, ToolInteractionMessage)
        }
        if not call_ids.issubset(unresolved | presented_call_ids):
            raise KernelConflict("tool batch reconstruction is inconsistent")
        return await self._checkpoint(
            run,
            updates={
                "tool_batches": [
                    *run.tool_batches,
                    _new_tool_batch(
                        assistant, internal_turn_id=run.active_internal_turn_id
                    ),
                ]
            },
            command_id=f"reconstruct-tool-batch:{assistant.message_id}",
        )

    async def _execute_tool_batch(  # noqa: C901
        self,
        run: OrchestratorRunState,
        assistant: AssistantMessage,
        signal: CancellationSignal,
        *,
        lifecycle: KernelLifecycle | None = None,
    ) -> KernelRunResult | Literal["retry", "decide"] | None:
        batch_index = next(
            (
                index
                for index, item in enumerate(run.tool_batches)
                if item.assistant_message_id == assistant.message_id
                and not item.results_flushed
            ),
            None,
        )
        if batch_index is None:
            run = await self._ensure_tool_batch(run, assistant)
            batch_index = next(
                index
                for index, item in enumerate(run.tool_batches)
                if item.assistant_message_id == assistant.message_id
                and not item.results_flushed
            )
        executable: list[tuple[ToolCall, ToolInvocation, ToolAcceptance]] = []
        join_calls: dict[str, tuple[ToolInvocation, str]] = {}
        join_outcomes: dict[str, ToolResult | ToolSuspension] = {}
        preacceptance_failed = False
        recoverable_declaration_failed = False
        fatal_preacceptance_failed = False
        for call in assistant.tool_calls:
            run = await self._load(run.run_id)
            batch = run.tool_batches[batch_index]
            entry_index = next(
                index
                for index, entry in enumerate(batch.entries)
                if entry.call_id == call.call_id
            )
            entry = batch.entries[entry_index]
            if (
                entry.state == "terminal"
                and entry.acceptance is None
                and entry.buffered_terminal_result is not None
            ):
                error_code = entry.buffered_terminal_result.error_code
                if error_code == "invalid_tool_call":
                    recoverable_declaration_failed = True
                    preacceptance_failed = True
                elif error_code == "acceptance_failed":
                    fatal_preacceptance_failed = True
                    preacceptance_failed = True
                elif error_code == "skipped_due_to_prior_rejection":
                    preacceptance_failed = True
            if preacceptance_failed and entry.state == "pending":
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=_tool_error(
                        call,
                        "skipped_due_to_prior_rejection",
                        "Skipped after an earlier declaration was rejected.",
                    ),
                    command=f"skip-tool:{call.call_id}",
                )
                continue
            # Model-first join entries are checkpointed without an acceptance
            # (join dispatch never passes through ToolRuntime.accept). On
            # re-entry — restart mid-join ("accepted") or after a recoverable
            # dispatch suspension ("waiting_external") — re-dispatch the SAME
            # invocation; the runtime's replay dedup (already_joined +
            # deterministic command id) makes this idempotent.
            if (
                entry.state in {"accepted", "waiting_external"}
                and entry.invocation is not None
                and entry.acceptance is None
            ):
                join_target = entry.suspended_call_record_id or (
                    _find_join_target(run, entry.tool_name)
                    if run.lifecycle_family == "canonical"
                    else None
                )
                if join_target is None:
                    run = await self._update_entry(
                        run,
                        batch_index,
                        entry_index,
                        state="terminal",
                        invocation=entry.invocation,
                        result=_tool_error(
                            call,
                            "join_target_missing",
                            "The Agent interaction this reply targeted is no "
                            "longer active.",
                        ),
                        command=f"join-target-missing:{entry.call_id}",
                    )
                    continue
                join_calls[entry.call_id] = (entry.invocation, join_target)
                continue
            if entry.state in {
                "terminal",
                "waiting_external",
                "input_required",
                "auth_required",
            }:
                continue
            if (
                entry.state == "pending"
                and call.tool_name == REQUEST_USER_INPUT_TOOL_NAME
            ):
                if self.supervisor_hitl is None:
                    raise KernelConflict("supervisor HITL port is not bound")
                # Validate before publishing: only valid declarations get a
                # public tool entry, so rejected ask_user calls leave no open
                # tool row behind (the fold must never see a phantom
                # "running" ask that never suspends).
                declaration_errors = list(
                    Draft202012Validator(
                        REQUEST_USER_INPUT_TOOL_DEFINITION.input_schema
                    ).iter_errors(call.arguments)
                )
                has_placeholder_choice = any(
                    _has_ellipsis_placeholder(choice)
                    for choice in _request_user_input_choices(call.arguments)
                )
                has_pending_agent_questions = _has_presented_interactions(run)
                if (
                    not declaration_errors
                    and not has_placeholder_choice
                    and not has_pending_agent_questions
                ):
                    # The ask_user call never enters the tool-runtime dispatch
                    # loop, so publish its tool_execution_started here to
                    # satisfy the public protocol contract
                    # (tool_execution_update must extend an existing tool
                    # entry). The deterministic public_event_id collapses
                    # replays onto the original room event at append time.
                    public_call_id = _opaque_public_call_id(run.run_id, call.call_id)
                    await self._emit(
                        lifecycle,
                        "tool_execution_started",
                        run,
                        {
                            "public_event_id": (
                                f"public:{run.run_id}:{public_call_id}:start"
                            ),
                            "call_id": call.call_id,
                            "public_call_id": public_call_id,
                            "internal_turn_id": (
                                run.active_internal_turn_id or assistant.message_id
                            ),
                            "tool_name": call.tool_name,
                            "agent_label": self._tool_label(run, call.tool_name),
                            "arguments": call.arguments,
                        },
                    )
                suspended = await self._suspend_for_supervisor_input(
                    run,
                    batch_index,
                    entry_index,
                    call,
                    assistant,
                )
                if suspended is None:
                    preacceptance_failed = True
                    recoverable_declaration_failed = True
                    continue
                run = suspended
                if run.consecutive_model_joins:
                    run = await self._checkpoint(
                        run,
                        updates={"consecutive_model_joins": 0},
                        command_id=f"join-reset-ask:{call.call_id}",
                    )
                continue
            if (
                entry.state == "pending"
                and call.tool_name == SURFACE_AGENT_QUESTIONS_TOOL_NAME
            ):
                surface = await self._suspend_for_surface_forward(
                    run,
                    batch_index,
                    entry_index,
                    call,
                    assistant,
                    lifecycle=lifecycle,
                )
                if surface is None:
                    preacceptance_failed = True
                    recoverable_declaration_failed = True
                    continue
                run = surface
                continue
            if entry.state in {"accepted", "executing"}:
                if entry.invocation is None or entry.acceptance is None:
                    raise KernelConflict("accepted tool entry is incomplete")
                if (
                    run.lifecycle_family == "canonical"
                    and entry.opaque_public_call_id is None
                ):
                    run = await self._update_entry(
                        run,
                        batch_index,
                        entry_index,
                        state=entry.state,
                        invocation=entry.invocation,
                        acceptance=entry.acceptance,
                        opaque_public_call_id=_opaque_public_call_id(
                            run.run_id, entry.call_id
                        ),
                        command=f"repair-public-call-id:{entry.call_id}",
                    )
                    entry = run.tool_batches[batch_index].entries[entry_index]
                executable.append((call, entry.invocation, entry.acceptance))
                continue
            if entry.state != "pending":
                raise KernelConflict(
                    f"unsupported recoverable tool state {entry.state}"
                )
            try:
                resolved = self.tool_catalog.resolve(run, call.tool_name)
            except KeyError:
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=_tool_error(
                        call,
                        "invalid_tool_call",
                        "Unknown tool. Choose a tool from the current catalog and retry.",
                    ),
                    command=f"invalid-tool:{call.call_id}",
                )
                preacceptance_failed = True
                recoverable_declaration_failed = True
                continue
            errors = list(
                Draft202012Validator(resolved.definition.input_schema).iter_errors(
                    call.arguments
                )
            )
            if errors:
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=_tool_error(
                        call,
                        "invalid_tool_call",
                        _tool_schema_validation_message(errors[0]),
                    ),
                    command=f"invalid-tool:{call.call_id}",
                )
                preacceptance_failed = True
                recoverable_declaration_failed = True
                continue
            try:
                self.budget_policy.before_tool_call(
                    run.budget, run.profile, now=self.clock.now()
                )
            except BudgetExceeded as exc:
                if exc.reason != "tool_calls":
                    return await self._terminate(
                        run, status="budget_exhausted", reason=exc.reason
                    )
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=_tool_error(call, "tool_call_budget_exhausted", exc.reason),
                    command=f"tool-budget-exhausted:{call.call_id}",
                )
                continue
            run = await self._checkpoint(
                run,
                updates={"budget": self.budget_policy.record_tool_calls(run.budget, 1)},
                command_id=(
                    f"tool-budget-reserve:{assistant.message_id}:{call.call_id}"
                ),
            )
            invocation = ToolInvocation(
                invocation_id=call.call_id,
                run_id=run.run_id,
                expected_run_version=run.state_version,
                assistant_message_id=assistant.message_id,
                source_index=entry_index,
                causation_id=assistant.message_id,
                idempotency_key=self._stable_id(
                    run,
                    "tool",
                    assistant.message_id,
                    call.call_id,
                    entry_index,
                    resolved.binding.binding_digest,
                ),
                tool=resolved,
                arguments=call.arguments,
                deadline_at=run.budget.deadline_at,
            )
            # Model-first join routing: a re-invocation of the same agent+skill
            # while a presented interaction is parked continues the existing
            # task instead of opening a new one.
            join_target = (
                _find_join_target(run, call.tool_name)
                if run.lifecycle_family == "canonical"
                else None
            )
            if join_target is not None:
                if run.consecutive_model_joins >= MAX_CONSECUTIVE_MODEL_JOINS:
                    run = await self._update_entry(
                        run,
                        batch_index,
                        entry_index,
                        state="terminal",
                        invocation=invocation,
                        result=_tool_error(
                            call,
                            "auto_reply_limit_reached",
                            "The platform will not keep auto-replying to "
                            "Agents. Ask the user or conclude from evidence.",
                        ),
                        command=f"join-limit:{call.call_id}",
                    )
                    await self._emit(
                        lifecycle,
                        "model_decision",
                        run,
                        {
                            "internal_turn_id": run.active_internal_turn_id
                            or assistant.message_id,
                            "decision": "no_progress",
                            "agent_label": self._tool_label(run, call.tool_name),
                            "question_summary": _interaction_question_summary(
                                _parked_interaction_questions(run, join_target)
                            ),
                            "reason": "auto_reply_limit_reached",
                        },
                    )
                    recoverable_declaration_failed = True
                    continue
                run = await self._checkpoint(
                    run,
                    updates={
                        "consecutive_model_joins": run.consecutive_model_joins + 1
                    },
                    command_id=f"join-count:{call.call_id}",
                )
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="accepted",
                    invocation=invocation,
                    opaque_public_call_id=_opaque_public_call_id(
                        run.run_id, call.call_id
                    ),
                    command=f"accepted-join:{call.call_id}",
                )
                join_calls[call.call_id] = (invocation, join_target)
                continue
            try:
                acceptance = await self.tool_runtime.accept(invocation)
                if (
                    acceptance.invocation_id != invocation.invocation_id
                    or acceptance.idempotency_key != invocation.idempotency_key
                ):
                    raise ValueError("tool acceptance does not correlate")
            except Exception as exc:
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    invocation=invocation,
                    result=_tool_error(call, "acceptance_failed", str(exc)),
                    command=f"acceptance-failed:{call.call_id}",
                )
                preacceptance_failed = True
                fatal_preacceptance_failed = True
                continue
            run = await self._update_entry(
                run,
                batch_index,
                entry_index,
                state="accepted",
                invocation=invocation,
                acceptance=acceptance,
                opaque_public_call_id=_opaque_public_call_id(run.run_id, call.call_id),
                command=f"accepted-tool:{call.call_id}",
            )
            if run.consecutive_model_joins:
                run = await self._checkpoint(
                    run,
                    updates={"consecutive_model_joins": 0},
                    command_id=f"join-reset:{call.call_id}",
                )
            executable.append((call, invocation, acceptance))

        sequential = run.profile.tool_execution == "sequential" or any(
            item[1].tool.definition.execution_mode == "sequential"
            for item in executable
        )
        outcomes: list[tuple[str, ToolResult | ToolSuspension]] = []
        if sequential:
            for call, invocation, acceptance in executable:
                await self._emit(
                    lifecycle,
                    "tool_execution_started",
                    run,
                    {
                        "call_id": call.call_id,
                        "public_call_id": _opaque_public_call_id(
                            run.run_id, call.call_id
                        ),
                        "internal_turn_id": run.active_internal_turn_id
                        or assistant.message_id,
                        "tool_name": call.tool_name,
                        "agent_label": self._tool_label(run, call.tool_name),
                        "arguments": call.arguments,
                    },
                )
                outcome = await self._execute_one(invocation, acceptance, signal=signal)
                outcomes.append((call.call_id, outcome))
        else:
            semaphore = asyncio.Semaphore(run.profile.max_parallel_calls)

            async def execute_bounded(
                call: ToolCall,
                invocation: ToolInvocation,
                acceptance: ToolAcceptance,
            ) -> ToolResult | ToolSuspension:
                async with semaphore:
                    await self._emit(
                        lifecycle,
                        "tool_execution_started",
                        run,
                        {
                            "call_id": call.call_id,
                            "public_call_id": _opaque_public_call_id(
                                run.run_id, call.call_id
                            ),
                            "internal_turn_id": run.active_internal_turn_id
                            or assistant.message_id,
                            "tool_name": call.tool_name,
                            "agent_label": self._tool_label(run, call.tool_name),
                            "arguments": call.arguments,
                        },
                    )
                    outcome = await self._execute_one(
                        invocation, acceptance, signal=signal
                    )
                    return outcome

            values = await asyncio.gather(
                *(
                    execute_bounded(call, invocation, acceptance)
                    for call, invocation, acceptance in executable
                )
            )
            outcomes = [
                (call.call_id, outcome)
                for (call, _, _), outcome in zip(executable, values, strict=True)
            ]

        # Model-first join continuations execute after ordinary dispatch; each
        # join blocks until the Agent responds on the existing task/context.
        for call in assistant.tool_calls:
            if call.call_id not in join_calls:
                continue
            invocation, parent_call_record_id = join_calls[call.call_id]
            await self._emit(
                lifecycle,
                "tool_execution_started",
                run,
                {
                    "call_id": call.call_id,
                    "public_call_id": _opaque_public_call_id(run.run_id, call.call_id),
                    "internal_turn_id": run.active_internal_turn_id
                    or assistant.message_id,
                    "tool_name": call.tool_name,
                    "agent_label": self._tool_label(run, call.tool_name),
                    "arguments": call.arguments,
                },
            )
            questions = _parked_interaction_questions(run, parent_call_record_id)
            await self._emit(
                lifecycle,
                "model_decision",
                run,
                {
                    "internal_turn_id": run.active_internal_turn_id
                    or assistant.message_id,
                    "decision": "answered_from_context",
                    "agent_label": self._tool_label(run, call.tool_name)
                    or call.tool_name,
                    "question_summary": _interaction_question_summary(questions),
                    "source_summary": "from earlier messages and attachments",
                },
            )
            # Bounded re-dispatch: a recoverable transport suspension carries
            # the parent call identity and parked-interaction metadata. Retry
            # the same idempotent command instead of stalling in
            # waiting_external; exhaust with a diagnostic failure so the model
            # can decide (e.g. request_user_input) on the next turn.
            outcome: ToolResult | ToolSuspension
            retries = 0
            while True:
                try:
                    outcome = await self.tool_runtime.dispatch_model_reply(
                        invocation,
                        parent_call_record_id=parent_call_record_id,
                        interaction_fingerprint=None,
                        signal=signal,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = ToolResult(
                        call_id=invocation.invocation_id,
                        tool_name=invocation.tool.definition.name,
                        status="failed",
                        content=[],
                        artifact_refs=[],
                        error_code="tool_execution_failed",
                        error_message=str(exc)[:500],
                    )
                    break
                if not isinstance(outcome, ToolSuspension):
                    break
                if outcome.status != "waiting_external":
                    break
                if outcome.delivery_state == "accepted":
                    # The Agent acknowledged the reply and is still working. The
                    # response observation arrives asynchronously on the parent
                    # call and is translated to this join invocation. Re-sending
                    # would duplicate a delivered message; keep the entry in
                    # waiting_external and let the observation complete it.
                    break
                # transport_uncertain (or a legacy suspension without the
                # discriminator): the idempotent command is safe to re-dispatch.
                retries += 1
                if retries >= MAX_JOIN_DISPATCH_RETRIES:
                    outcome = ToolResult(
                        call_id=invocation.invocation_id,
                        tool_name=invocation.tool.definition.name,
                        status="failed",
                        content=[],
                        artifact_refs=[],
                        error_code="model_reply_dispatch_failed",
                        error_message=(
                            "The platform could not deliver the reply to the "
                            "Agent after repeated attempts."
                        ),
                    )
                    break
                await asyncio.sleep(JOIN_DISPATCH_RETRY_BACKOFF_SECONDS)
            if (
                isinstance(outcome, ToolResult)
                and outcome.error_code == "auto_reply_limit_reached"
            ):
                await self._emit(
                    lifecycle,
                    "model_decision",
                    run,
                    {
                        "internal_turn_id": run.active_internal_turn_id
                        or assistant.message_id,
                        "decision": "no_progress",
                        "agent_label": self._tool_label(run, call.tool_name),
                        "question_summary": _interaction_question_summary(questions),
                        "reason": "auto_reply_limit_reached",
                    },
                )
            outcomes.append((call.call_id, outcome))
            join_outcomes[parent_call_record_id] = outcome

        run = await self._load(run.run_id)
        batch = run.tool_batches[batch_index]
        entries = list(batch.entries)
        for call_id, outcome in outcomes:
            index = next(i for i, item in enumerate(entries) if item.call_id == call_id)
            entry = entries[index]
            if isinstance(outcome, ToolResult):
                call = next(
                    item for item in assistant.tool_calls if item.call_id == call_id
                )
                validate_tool_result_correlation(call, outcome)
                entries[index] = entry.model_copy(
                    update={"state": "terminal", "buffered_terminal_result": outcome}
                )
            else:
                entries[index] = entry.model_copy(
                    update={
                        "state": outcome.status,
                        "suspended_call_record_id": outcome.call_record_id,
                        "interaction_id": outcome.interaction_id,
                        "interaction_fingerprint": outcome.interaction_fingerprint,
                        "interaction_questions": outcome.questions,
                    }
                )
        batch = batch.model_copy(update={"entries": entries})
        batches = list(run.tool_batches)
        batches[batch_index] = batch
        # Three-way consumption: a join terminal result also closes every other
        # presented entry parked on the same parent call.
        for parent_call_record_id, join_outcome in join_outcomes.items():
            if not isinstance(join_outcome, ToolResult):
                continue
            if join_outcome.error_code in _JOIN_FAILURE_ERROR_CODES:
                # A failed join (dispatch failed, limit reached, invalid target)
                # does not resolve the Agent's question. Leave the parked parent
                # entries eligible for request_user_input / a user answer /
                # abandon so the runtime parent call and the Run stay consistent.
                continue
            for other_index, other_batch in enumerate(list(batches)):
                other_entries = list(other_batch.entries)
                changed = False
                for other_entry_index, other_entry in enumerate(other_entries):
                    if (
                        other_entry.suspended_call_record_id == parent_call_record_id
                        and other_entry.state in {"input_required", "auth_required"}
                    ):
                        other_entries[other_entry_index] = other_entry.model_copy(
                            update={
                                "state": "terminal",
                                "buffered_terminal_result": join_outcome.model_copy(
                                    update={"call_id": other_entry.call_id}
                                ),
                            }
                        )
                        changed = True
                if changed:
                    batches[other_index] = other_batch.model_copy(
                        update={"entries": other_entries}
                    )
        artifact_refs = _merge_artifact_refs(
            run.artifact_refs,
            [outcome for _, outcome in outcomes if isinstance(outcome, ToolResult)],
        )
        # Three-way consumption may have closed a parked entry in the SAME
        # batch; re-read it before flushing so its terminal result is honored.
        batch = batches[batch_index]
        transcript, batch = _flush_batch(run.transcript, batch, self.clock.now())
        batches[batch_index] = batch
        # Flush any other batch fully terminalized by three-way consumption so
        # its call ids resolve in the model context before the next turn.
        for flush_index, flush_batch in enumerate(list(batches)):
            if flush_index == batch_index or flush_batch.results_flushed:
                continue
            if not all(entry.state == "terminal" for entry in flush_batch.entries):
                continue
            transcript, flushed = _flush_batch(
                transcript, flush_batch, self.clock.now()
            )
            batches[flush_index] = flushed
        if fatal_preacceptance_failed and not batch.results_flushed:
            # A fatal local declaration invalidates the whole Run even when a
            # sibling is parked. Checkpoint the mixed batch, then delegate all
            # descendant Tool/interaction closure and the single full-inventory
            # turn_end to the canonical terminalizer.
            run = await self._checkpoint(
                run,
                updates={
                    "tool_batches": batches,
                    "transcript": transcript,
                    "artifact_refs": artifact_refs,
                },
                command_id=f"fatal-mixed-tool-batch:{assistant.message_id}",
            )
            return await self._terminate(run, status="failed", reason="tool_failure")
        if batch.results_flushed:
            run = await self._checkpoint(
                run,
                updates={
                    "tool_batches": batches,
                    "transcript": transcript,
                    "artifact_refs": artifact_refs,
                },
                command_id=f"complete-tool-batch:{assistant.message_id}",
            )
            run = await self._publish_checkpointed_tool_terminals(
                run,
                batch_index,
                lifecycle=lifecycle,
                internal_turn_id=run.active_internal_turn_id or assistant.message_id,
            )
            batch = run.tool_batches[batch_index]
            for entry in batch.entries:
                await self._emit(
                    lifecycle,
                    "message_completed",
                    run,
                    {
                        "call_id": entry.call_id,
                        "message_kind": "tool_result",
                        "agent_label": self._tool_label(
                            run, entry.buffered_terminal_result.tool_name
                        )
                        if entry.buffered_terminal_result is not None
                        else None,
                    },
                )
            if fatal_preacceptance_failed:
                # Terminalization owns descendant closure. Do not manufacture
                # an early turn_end here: _close_active_attempt first closes
                # every accepted/suspended Tool row, then emits one complete
                # ordered turn inventory.
                return await self._terminate(
                    run, status="failed", reason="tool_failure"
                )
            if recoverable_declaration_failed:
                turn_id = run.active_internal_turn_id or assistant.message_id
                closing_message_id = assistant.message_id
                if run.lifecycle_family == "canonical":
                    closure = _canonical_turn_closure(run, turn_id)
                    if closure is None:
                        # A parked parent from an earlier batch still owns this
                        # internal turn. Keep the turn open and return the local
                        # diagnostic to the model for an in-turn retry.
                        return "retry"
                    closing_message_id = closure.message_id
                    public_ids = list(closure.public_tool_call_ids)
                else:
                    public_ids = [
                        entry.opaque_public_call_id
                        for entry in batch.entries
                        if entry.opaque_public_call_id is not None
                    ]
                await self._emit(
                    lifecycle,
                    "turn_completed",
                    run,
                    {
                        "internal_turn_id": turn_id,
                        "message_id": closing_message_id,
                        "tool_call_ids": public_ids,
                        "status": "error",
                    },
                )
                if run.lifecycle_family == "canonical":
                    run = await self._checkpoint(
                        run,
                        updates={
                            "active_internal_turn_id": None,
                            "active_assistant_message_id": None,
                            "active_attempt": None,
                        },
                        command_id=f"public-turn-error:{assistant.message_id}",
                    )
                # A model-authored declaration can be incompatible with the
                # frozen Agent Card schema even though the Agent itself is
                # healthy. Its durable ToolResult becomes a bounded diagnostic
                # observation on the next model turn. Local rejection does not
                # consume Agent-call budget or manufacture a public Tool row.
                return "retry"
            return None
        status = _wait_status(batch)
        run = await self._checkpoint(
            run,
            updates={
                "tool_batches": batches,
                "status": status,
                "artifact_refs": artifact_refs,
                "transcript": transcript,
            },
            command_id=f"suspend-tool-batch:{assistant.message_id}",
        )
        run = await self._publish_checkpointed_tool_terminals(
            run,
            batch_index,
            lifecycle=lifecycle,
            internal_turn_id=run.active_internal_turn_id or assistant.message_id,
        )
        run = await self._publish_checkpointed_suspensions(
            run,
            batch_index,
            lifecycle=lifecycle,
            internal_turn_id=run.active_internal_turn_id or assistant.message_id,
        )
        if run.lifecycle_family == "legacy":
            await self._emit(
                lifecycle,
                "turn_completed",
                run,
                {"message_id": assistant.message_id, "status": status},
            )
            return KernelRunResult(
                "awaiting_user" if status == "awaiting_user" else "waiting_external",
                run,
            )
        if not _has_presentable_interactions(run):
            return KernelRunResult(
                "awaiting_user" if status == "awaiting_user" else "waiting_external",
                run,
            )
        # Canonical model-first HITL: present parked agent interactions to the
        # model as tool_interaction messages instead of suspending the Run into
        # awaiting_user. The same internal turn continues with the decision.
        run = await self._present_interactions(run, lifecycle=lifecycle)
        run = await self._checkpoint(
            run,
            updates={
                "status": "running",
                "tool_batches": run.tool_batches,
                "transcript": run.transcript,
            },
            command_id=f"present-interactions:{assistant.message_id}",
        )
        return "decide"

    async def _execute_one(
        self,
        invocation: ToolInvocation,
        acceptance: ToolAcceptance,
        *,
        signal: CancellationSignal,
    ) -> ToolResult | ToolSuspension:
        if (
            acceptance.invocation_id != invocation.invocation_id
            or acceptance.idempotency_key != invocation.idempotency_key
        ):
            return ToolResult(
                call_id=invocation.invocation_id,
                tool_name=invocation.tool.definition.name,
                status="rejected",
                content=[],
                artifact_refs=[],
                error_code="acceptance_mismatch",
                error_message="tool acceptance does not correlate",
            )
        try:
            return await self.tool_runtime.execute(
                invocation, acceptance, signal=signal
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                call_id=invocation.invocation_id,
                tool_name=invocation.tool.definition.name,
                status="failed",
                content=[],
                artifact_refs=[],
                error_code="tool_execution_failed",
                error_message=str(exc)[:500],
            )

    async def _reject_grace_tools(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState:
        batch_index = next(
            (
                index
                for index, batch in enumerate(run.tool_batches)
                if batch.assistant_message_id == assistant.message_id
                and not batch.results_flushed
            ),
            None,
        )
        if batch_index is None:
            raise KernelConflict("wrap-up tool batch is missing")
        batches = list(run.tool_batches)
        batch = batches[batch_index]
        calls = {call.call_id: call for call in assistant.tool_calls}
        entries = []
        for entry in batch.entries:
            call = calls.get(entry.call_id)
            if call is None:
                raise KernelConflict("wrap-up tool batch does not correlate")
            entries.append(
                entry.model_copy(
                    update={
                        "state": "terminal",
                        "buffered_terminal_result": _tool_error(
                            call,
                            "grace_tools_disabled",
                            "Tools are disabled during wrap-up.",
                        ),
                    }
                )
            )
        batch = batch.model_copy(update={"entries": entries})
        transcript, batch = _flush_batch(run.transcript, batch, self.clock.now())
        batches[batch_index] = batch
        return await self._checkpoint(
            run,
            updates={"transcript": transcript, "tool_batches": batches},
            command_id=f"reject-grace-tools:{assistant.message_id}",
        )

    async def _append_assistant(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState:
        updates: dict[str, object] = {"transcript": [*run.transcript, assistant]}
        if assistant.tool_calls:
            updates["tool_batches"] = [
                *run.tool_batches,
                _new_tool_batch(
                    assistant, internal_turn_id=run.active_internal_turn_id
                ),
            ]
        else:
            updates.update(
                proposed_final_message_id=assistant.message_id,
                status="finalizing",
            )
        return await self._checkpoint(
            run,
            updates=updates,
            command_id=f"assistant:{assistant.message_id}",
        )

    async def _append_notice(
        self, run: OrchestratorRunState, notice: SessionNotice
    ) -> OrchestratorRunState:
        if any(
            isinstance(message, SessionNotice) and message.notice_id == notice.notice_id
            for message in run.transcript
        ):
            return run
        return await self._checkpoint(
            run,
            updates={"transcript": [*run.transcript, notice]},
            command_id=f"notice:{notice.notice_id}",
        )

    async def _record_model_event(
        self,
        run: OrchestratorRunState,
        turn_id: str,
        event,
        *,
        message_id: str | None = None,
    ) -> OrchestratorRunState:
        attempt = event.attempt or 1
        # Include the assistant message identity so a model-first decision
        # continuation (which reuses the public internal turn id) still records
        # a distinct provider attempt instead of colliding with the turn's
        # original model call.
        attempt_key = (
            f"{turn_id}:{message_id}:{attempt}"
            if message_id is not None
            else f"{turn_id}:{attempt}"
        )
        if event.kind == "attempt_started":
            budget = self.budget_policy.record_provider_attempt(
                run.budget,
                run.profile,
                attempt_key=attempt_key,
                retry=attempt > 1,
            )
            return await self._checkpoint(
                run,
                updates={"budget": budget},
                command_id=f"provider-attempt:{attempt_key}",
            )
        if event.kind == "usage" and event.usage is not None:
            budget = self.budget_policy.record_usage_snapshot(
                run.budget,
                attempt_key=attempt_key,
                usage=event.usage,
            )
            usage_key = (
                f"{event.usage.input_tokens}:{event.usage.output_tokens}:"
                f"{event.usage.cache_read_tokens}:{event.usage.cache_write_tokens}"
            )
            run = await self._checkpoint(
                run,
                updates={"budget": budget},
                command_id=f"provider-usage:{attempt_key}:{usage_key}",
            )
            self.budget_policy.before_token_side_effect(run.budget, run.profile)
        return run

    async def _compact(
        self,
        run: OrchestratorRunState,
        messages: list[object],
        *,
        baseline: int,
        signal: CancellationSignal,
    ) -> OrchestratorRunState | KernelRunResult:
        if (
            self.context_compactor is None
            or run.budget.compactions_used >= run.profile.max_compactions
        ):
            return await self._terminate(
                run, status="budget_exhausted", reason="context overflow"
            )
        try:
            self.budget_policy.before_model_turn(
                run.budget,
                run.profile,
                now=self.clock.now(),
                purpose="compaction",
            )
            turn_id = self._stable_id(
                run, "compaction", run.budget.compactions_used + 1
            )

            async def record_event(event) -> None:
                nonlocal run
                run = await self._record_model_event(run, turn_id, event)

            compaction = await self.context_compactor.compact(
                messages,
                turn_id=turn_id,
                remaining_provider_retries=(
                    self.budget_policy.remaining_provider_retries(
                        run.budget, run.profile
                    )
                ),
                deadline_at=run.budget.deadline_at,
                on_event=record_event,
                signal=signal,
            )
            summary = compaction.summary
            budget = self.budget_policy.record_compaction(run.budget, run.profile)
        except BudgetExceeded as exc:
            return await self._terminate(
                run, status="budget_exhausted", reason=exc.reason
            )
        except Exception:
            summary = "Older completed turns omitted; preserve recent context."
            budget = self.budget_policy.record_compaction(run.budget, run.profile)
        run = await self._checkpoint(
            run,
            updates={
                "budget": budget,
                "compaction_summary": summary,
                "compaction_baseline_tokens": baseline,
            },
            command_id=(f"compaction:{run.run_id}:{run.budget.compactions_used + 1}"),
        )
        return run

    async def _emit(
        self,
        lifecycle: KernelLifecycle | None,
        event_type: str,
        run: OrchestratorRunState,
        payload: dict[str, object],
    ) -> None:
        if lifecycle is not None:
            await lifecycle(event_type, run, payload)

    async def _publish_public_text(
        self,
        lifecycle: KernelLifecycle | None,
        run: OrchestratorRunState,
        coalescer: PublicTextCoalescer,
        text: str,
        *,
        semantic_boundary: bool = False,
        timer_flush: bool = False,
    ) -> OrchestratorRunState:
        now = self.clock.now()
        deltas = coalescer.add(text, now=now)
        if timer_flush:
            deltas.extend(coalescer.timed_flush(now=now))
        if semantic_boundary:
            deltas.extend(coalescer.semantic_flush(now=now))
        for delta in deltas:
            await self._emit(
                lifecycle,
                "message_updated",
                run,
                {
                    "public_event_id": delta.event_id,
                    "internal_turn_id": coalescer.internal_turn_id,
                    "message_id": coalescer.message_id,
                    "content_index": delta.content_index,
                    "delta_index": delta.delta_index,
                    "start_offset": delta.start_offset,
                    "end_offset": delta.end_offset,
                    "delta": delta.delta,
                },
            )
            # Public append acknowledgement precedes this advisory checkpoint.
            if run.lifecycle_family == "canonical":
                active_text = f"{run.active_public_text}{delta.delta}"
                run = await self._checkpoint(
                    run,
                    updates={
                        "greatest_public_text_offset": delta.end_offset,
                        "active_public_text": active_text,
                    },
                    command_id=(
                        f"public-text-offset:{coalescer.message_id}:{delta.end_offset}"
                    ),
                )
        return run

    async def _emit_model_event(
        self,
        lifecycle: KernelLifecycle | None,
        run: OrchestratorRunState,
        event,
        request=None,
    ) -> None:
        event_type = {
            "attempt_started": "model_attempt_started",
            "retry_scheduled": "model_retry_scheduled",
            "attempt_failed": "model_attempt_failed",
        }.get(event.kind)
        if event_type is not None:
            # Gateway-internal transport attempts do not own independent public
            # Assistant identities. Canonical retry facts are emitted only by
            # the Kernel after closing the public attempt below.
            if (
                run.lifecycle_family == "canonical"
                and event_type == "model_retry_scheduled"
            ):
                return
            payload: dict[str, object] = {
                "attempt": event.attempt or 0,
                "error_class": event.error_class or "",
            }
            if request is not None:
                payload["internal_turn_id"] = request.turn_id
                payload["model"] = request.model.model_id
                payload["provider"] = request.model.provider
            if event.kind == "retry_scheduled" and event.retry_delay_ms is not None:
                payload["retry_delay_ms"] = event.retry_delay_ms
            await self._emit(lifecycle, event_type, run, payload)

    async def _recover_active_canonical_attempt(
        self,
        run: OrchestratorRunState,
        lifecycle: KernelLifecycle | None,
    ) -> tuple[OrchestratorRunState, str | None] | KernelRunResult:
        """Resume from durable room events, never advisory Run text state."""

        internal_turn_id = run.active_internal_turn_id
        assert internal_turn_id is not None
        records = (
            await self.canonical_event_reader(run.room_id, run.run_id)
            if self.canonical_event_reader is not None
            else []
        )
        text = ""
        terminal: dict[str, object] | None = None
        turn_started = False
        message_started = False
        turn_ended = False
        message_id = run.active_assistant_message_id
        for record in sorted(records, key=lambda item: int(item.get("room_seq") or 0)):
            data = record.get("payload_public")
            if not isinstance(data, dict) or data.get("run_id") != run.run_id:
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("internal_turn_id") != internal_turn_id:
                continue
            kind = data.get("type")
            if kind == "turn_start":
                turn_started = True
            elif kind == "message_start":
                candidate = payload.get("message_id")
                if isinstance(candidate, str):
                    message_id = candidate
                    message_started = True
            elif kind == "message_update" and payload.get("message_id") == message_id:
                nested = payload.get("assistant_message_event")
                if not isinstance(nested, dict):
                    continue
                delta = nested.get("delta")
                start = nested.get("start_offset")
                end = nested.get("end_offset")
                if (
                    isinstance(delta, str)
                    and start == len(text)
                    and end == len(text) + len(delta)
                ):
                    text += delta
            elif kind == "message_end" and payload.get("message_id") == message_id:
                terminal_text = str(payload.get("text") or "")
                if text and terminal_text != text:
                    raise KernelConflict(
                        "durable message_end contradicts assembled public deltas"
                    )
                terminal = payload
                text = terminal_text
            elif kind == "turn_end":
                turn_ended = True

        # The active-state CAS intentionally precedes public starts. Recovery
        # must adopt/restore those missing semantic parents before it emits any
        # message_end/turn_end child. Deterministic public identities make both
        # boundaries exactly-once across repeated recovery.
        if not turn_started:
            await self._emit(
                lifecycle,
                "turn_started",
                run,
                {
                    "internal_turn_id": internal_turn_id,
                    "attempt": run.active_attempt or 1,
                },
            )
        if message_id is not None and not message_started:
            await self._emit(
                lifecycle,
                "message_started",
                run,
                {
                    "internal_turn_id": internal_turn_id,
                    "message_id": message_id,
                    "role": "assistant",
                },
            )

        checkpointed_assistant = next(
            (
                item
                for item in reversed(run.transcript)
                if isinstance(item, AssistantMessage)
                and (message_id is None or item.message_id == message_id)
            ),
            None,
        )
        if (
            terminal is None
            and self.canonical_event_reader is None
            and checkpointed_assistant is not None
        ):
            terminal = {
                "message_id": checkpointed_assistant.message_id,
                "disposition": (
                    "commentary" if checkpointed_assistant.tool_calls else "final"
                ),
                "stop_reason": (
                    "tool_use" if checkpointed_assistant.tool_calls else "stop"
                ),
                "text": _assistant_text(checkpointed_assistant),
            }
            await self._emit(
                lifecycle,
                "message_completed",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{internal_turn_id}:"
                        f"{checkpointed_assistant.message_id}:message_end"
                    ),
                    "internal_turn_id": internal_turn_id,
                    **terminal,
                },
            )

        if terminal is None:
            inferred_message_id = message_id or (
                checkpointed_assistant.message_id if checkpointed_assistant else None
            )
            updates: dict[str, object] = {
                "active_public_text": text,
                "greatest_public_text_offset": len(text),
            }
            if inferred_message_id is not None:
                updates["active_assistant_message_id"] = inferred_message_id
            if updates != {
                "active_public_text": run.active_public_text,
                "greatest_public_text_offset": run.greatest_public_text_offset,
            }:
                run = await self._checkpoint(
                    run,
                    updates=updates,
                    command_id=f"public-repair-offset:{internal_turn_id}:{len(text)}",
                )
            return await self._close_active_attempt(run, disposition="aborted")

        disposition = str(terminal.get("disposition") or "")
        terminal_message_id = str(terminal.get("message_id") or message_id or "")
        if run.active_assistant_message_id is not None or run.active_public_text:
            run = await self._checkpoint(
                run,
                updates={
                    "active_assistant_message_id": None,
                    "active_public_text": "",
                    "greatest_public_text_offset": 0,
                },
                command_id=f"public-adopt-message-end:{terminal_message_id}",
            )

        if disposition == "commentary":
            if not turn_ended:
                closure = _canonical_turn_closure(run, internal_turn_id)
                if closure is None:
                    # The active internal turn still has open entries (for
                    # example presented interactions awaiting a model join
                    # reply). Keep the turn active instead of emitting a
                    # premature turn_end; the decision loop will close it once
                    # every entry is terminal.
                    return run, None
                await self._emit(
                    lifecycle,
                    "turn_completed",
                    run,
                    {
                        "public_event_id": (
                            f"public:{run.run_id}:{internal_turn_id}:turn_end:completed"
                        ),
                        "internal_turn_id": internal_turn_id,
                        "message_id": closure.message_id,
                        "tool_call_ids": list(closure.public_tool_call_ids),
                        "status": "completed",
                    },
                )
            run = await self._checkpoint(
                run,
                updates={
                    "active_internal_turn_id": None,
                    "active_assistant_message_id": None,
                    "active_attempt": None,
                    "active_public_text": "",
                    "greatest_public_text_offset": 0,
                },
                command_id=f"public-adopt-turn-end:{internal_turn_id}",
            )
            return run, None

        status = "completed" if disposition == "final" else disposition
        if not turn_ended:
            await self._emit(
                lifecycle,
                "turn_completed",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{internal_turn_id}:turn_end:{status}"
                    ),
                    "internal_turn_id": internal_turn_id,
                    "message_id": terminal_message_id,
                    "tool_call_ids": [],
                    "status": status,
                },
            )
        run = await self._checkpoint(
            run,
            updates={"active_internal_turn_id": None, "active_attempt": None},
            command_id=f"public-adopt-turn-end:{internal_turn_id}:{status}",
        )
        if disposition == "final":
            if checkpointed_assistant is None:
                return await self._terminate(
                    run, status="failed", reason="finalization candidate missing"
                )
            return await self._complete(run, checkpointed_assistant)
        return run, internal_turn_id

    async def _complete(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> KernelRunResult:
        sequence = (
            max((item.event_sequence for item in run.projection_outbox), default=0) + 1
        )
        request = TerminalCommitRequest(
            expected_state_version=run.state_version,
            command_id=f"complete:{run.run_id}:{assistant.message_id}",
            event_id=self.id_factory.new_id("event"),
            event_sequence=sequence,
            event_intent_id=self.id_factory.new_id("intent-event"),
            final_message_intent_id=self.id_factory.new_id("intent-message"),
            public_run_intent_id=self.id_factory.new_id("intent-run"),
            final_message_target=run.room_id,
            public_run_target=run.run_id,
            created_at=self.clock.now(),
        )
        committed = commit_terminal_decision(
            run,
            facts=TerminalDecisionFacts(final_message_id=assistant.message_id),
            request=request,
        )
        if committed.outcome != "accepted":
            return await self._terminate(
                run, status="failed", reason=committed.evaluation.reason
            )
        stored = await self.run_store.cas_mutate(
            committed.run,
            expected_state_version=run.state_version,
            command_id=request.command_id,
        )
        if stored.run is None:
            raise KernelConflict("terminal completion CAS failed")
        settled = await self.projection_driver.settle(run.run_id)
        return KernelRunResult("final_answer", settled)

    async def _close_active_attempt(
        self,
        run: OrchestratorRunState,
        *,
        disposition: Literal["error", "aborted"],
        error_summary: str | None = None,
    ) -> tuple[OrchestratorRunState, str | None]:
        """Close every incomplete canonical descendant before root termination.

        Suspended multi-turn Runs can lose their active-Turn pointer after a
        crash while an accepted parent Tool remains parked in an older batch.
        Closing only ``active_internal_turn_id`` both missed that row and, when
        a newer Turn was active, attributed its public Tool end to the wrong
        owner.  Recovery instead sweeps all durable batches, preserves each
        batch's canonical owner, and proves the full terminal inventory.
        """

        if run.lifecycle_family != "canonical":
            return run, None
        lifecycle = self._lifecycle_context.get()
        active_turn_id = run.active_internal_turn_id
        canonical_records = (
            await self.canonical_event_reader(run.room_id, run.run_id)
            if self.canonical_event_reader is not None
            else []
        )
        # Pure, immutable preflight for every historical and active owner. No
        # HITL/store/lifecycle effect is allowed before this succeeds.
        plan = _terminal_closure_plan(
            run,
            canonical_records,
            canonical_reader_available=self.canonical_event_reader is not None,
            public_secret_values=self.public_secret_values,
        )

        # Exact HITL ownership must converge before the Run or any public child
        # can be closed. The finalizer is idempotent, so a retry after an
        # ambiguous acknowledgement safely repeats these exact identities.
        for call_record_id, interaction_id in plan.interactions:
            await self.tool_runtime.abandon_parked_interaction(
                call_record_id=call_record_id,
                interaction_id=interaction_id,
                terminal_state=("failed" if disposition == "error" else "canceled"),
            )

        # A canonical Tool end can win immediately before the aggregate flag
        # checkpoint. Reconcile that exact opaque public identity first so the
        # normal checkpointed-terminal publisher does not emit it again.
        for public_id in plan.replayed_public_tool_call_ids:
            matched = False
            for batch_index, batch in enumerate(run.tool_batches):
                for entry_index, entry in enumerate(batch.entries):
                    if entry.opaque_public_call_id != public_id:
                        continue
                    assert entry.buffered_terminal_result is not None
                    run = await self._update_entry(
                        run,
                        batch_index,
                        entry_index,
                        state="terminal",
                        result=entry.buffered_terminal_result,
                        public_terminal_emitted=True,
                        command=f"recover-public-tool-end:{public_id}",
                    )
                    matched = True
                    break
                if matched:
                    break
            if not matched:  # pragma: no cover - immutable preflight proof
                raise KernelConflict("canonical Tool end child disappeared")

        if plan.active_message_end is not None:
            message_owner, message_id = plan.active_message_end
            payload: dict[str, object] = {
                "public_event_id": (
                    f"public:{run.run_id}:{message_owner}:{message_id}:message_end"
                ),
                "internal_turn_id": message_owner,
                "message_id": message_id,
                "stop_reason": "error" if disposition == "error" else "aborted",
                "disposition": disposition,
                "text": run.active_public_text,
            }
            if disposition == "error":
                payload["error_summary"] = (
                    error_summary or "The response could not be completed."
                )
            await self._emit(lifecycle, "message_completed", run, payload)

        # Publish crash-checkpointed terminals after preflight and HITL
        # convergence. Their durable batch owns the public child event.
        for batch_index in range(len(run.tool_batches)):
            run = await self._publish_checkpointed_tool_terminals(
                run,
                batch_index,
                lifecycle=lifecycle,
                internal_turn_id=_batch_internal_turn_id(run.tool_batches[batch_index]),
            )

        for batch_index in range(len(run.tool_batches)):
            owner = _batch_internal_turn_id(run.tool_batches[batch_index])
            for entry_index in range(len(run.tool_batches[batch_index].entries)):
                entry = run.tool_batches[batch_index].entries[entry_index]
                if entry.state == "terminal":
                    continue
                if entry.acceptance is not None and entry.opaque_public_call_id is None:
                    raise KernelConflict(
                        "accepted Tool child has no canonical public identity"
                    )
                public_child = entry.opaque_public_call_id is not None
                is_parked = (
                    entry.state in {"input_required", "auth_required"}
                    and entry.suspended_call_record_id is not None
                    and entry.interaction_id is not None
                )
                result = ToolResult(
                    call_id=entry.call_id,
                    tool_name=entry.tool_name,
                    status="canceled" if disposition == "aborted" else "failed",
                    content=[],
                    artifact_refs=[],
                    error_code=(
                        "interaction_abandoned"
                        if is_parked
                        else (
                            "run_canceled"
                            if disposition == "aborted"
                            else (
                                "run_failed"
                                if public_child
                                else "skipped_due_to_run_terminal"
                            )
                        )
                    ),
                    error_message=None,
                )
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=result,
                    command=f"public-close-tool:{entry.call_id}:{disposition}",
                )
                if not public_child:
                    continue
                await self._emit(
                    lifecycle,
                    "tool_execution_completed",
                    run,
                    {
                        "public_event_id": (
                            f"public:{run.run_id}:{entry.opaque_public_call_id}:end"
                        ),
                        "call_id": entry.call_id,
                        "public_call_id": entry.opaque_public_call_id
                        or _opaque_public_call_id(run.run_id, entry.call_id),
                        "internal_turn_id": owner,
                        "status": result.status,
                        "result_status": result.status,
                        "tool_name": entry.tool_name,
                        "agent_label": self._tool_label(run, entry.tool_name),
                        "duration_ms": 0,
                        "result_text": "",
                    },
                )
                run = await self._update_entry(
                    run,
                    batch_index,
                    entry_index,
                    state="terminal",
                    result=result,
                    public_terminal_emitted=True,
                    command=f"public-close-tool-emitted:{entry.call_id}",
                )

        for batch_index in range(len(run.tool_batches)):
            batch = run.tool_batches[batch_index]
            if batch.results_flushed:
                continue
            if not all(entry.state == "terminal" for entry in batch.entries):
                raise KernelConflict("terminal Tool sweep left an open entry")
            transcript, flushed = _flush_batch(run.transcript, batch, self.clock.now())
            batches = list(run.tool_batches)
            batches[batch_index] = flushed
            run = await self._checkpoint(
                run,
                updates={"tool_batches": batches, "transcript": transcript},
                command_id=f"public-flush-closed-batch:{batch.assistant_message_id}",
            )

        if not _terminal_closure_is_complete(run):
            raise KernelConflict("terminal Tool closure invariant is incomplete")

        for turn_plan in plan.turns:
            closure = _canonical_turn_closure(run, turn_plan.internal_turn_id)
            expected = _TurnClosureFacts(
                message_id=turn_plan.message_id,
                public_tool_call_ids=turn_plan.public_tool_call_ids,
            )
            if closure != expected:
                raise KernelConflict("canonical turn closure inventory changed")
            if not turn_plan.emit_turn_end:
                continue
            await self._emit(
                lifecycle,
                "turn_completed",
                run,
                {
                    "public_event_id": (
                        f"public:{run.run_id}:{turn_plan.internal_turn_id}:"
                        f"turn_end:{disposition}"
                    ),
                    "internal_turn_id": turn_plan.internal_turn_id,
                    "message_id": turn_plan.message_id,
                    "tool_call_ids": list(turn_plan.public_tool_call_ids),
                    "status": disposition,
                },
            )

        has_active_lifecycle_state = any(
            (
                run.active_internal_turn_id,
                run.active_assistant_message_id,
                run.active_attempt,
                run.active_public_text,
                run.greatest_public_text_offset,
            )
        )
        if has_active_lifecycle_state:
            closure_owner = active_turn_id or "orphaned"
            run = await self._checkpoint(
                run,
                updates={
                    "active_internal_turn_id": None,
                    "active_assistant_message_id": None,
                    "active_attempt": None,
                    "active_public_text": "",
                    "greatest_public_text_offset": 0,
                },
                command_id=(f"public-close-attempt:{closure_owner}:{disposition}"),
            )
        return run, active_turn_id or (
            plan.turns[-1].internal_turn_id if plan.turns else None
        )

    async def _terminate(
        self,
        run: OrchestratorRunState,
        *,
        status: Literal["failed", "canceled", "budget_exhausted"],
        reason: str,
        cancellation_cause: (
            Literal["user_requested", "room_closed", "shutdown", "policy"] | None
        ) = None,
    ) -> KernelRunResult:
        current = await self._load(run.run_id)
        if current.status in {"completed", "failed", "canceled", "budget_exhausted"}:
            return KernelRunResult(_outcome_for_status(current.status), current)
        run = current
        try:
            run, _ = await self._close_active_attempt(
                run,
                disposition="aborted" if status == "canceled" else "error",
                error_summary=(
                    "The response exceeded its safe public output limit."
                    if reason == "public_text_oversized"
                    else "The request could not be completed."
                ),
            )
        except ValueError:
            # Canonical projection/fold validation is fail-closed. Persistent
            # legacy history rejection is a bounded recovery invariant failure,
            # not a reason to relax the public contract.
            raise KernelConflict(
                "canonical terminal lifecycle publication rejected"
            ) from None
        if not _terminal_closure_is_complete(run):
            raise KernelConflict("terminal Tool closure invariant is incomplete")
        sequence = (
            max((item.event_sequence for item in run.projection_outbox), default=0) + 1
        )
        request = TerminalStatusCommitRequest(
            expected_state_version=run.state_version,
            command_id=f"terminate:{status}:{run.run_id}:{run.state_version}",
            event_id=self.id_factory.new_id("event"),
            event_sequence=sequence,
            event_intent_id=self.id_factory.new_id("intent-event"),
            public_run_intent_id=self.id_factory.new_id("intent-run"),
            public_run_target=run.run_id,
            status=status,
            terminal_reason=reason,
            cancellation_cause=(
                cancellation_cause or "user_requested" if status == "canceled" else None
            ),
            created_at=self.clock.now(),
        )
        committed = commit_terminal_status(run, request=request)
        if committed.outcome != "accepted":
            raise KernelConflict("terminal status CAS rejected")
        stored = await self.run_store.cas_mutate(
            committed.run,
            expected_state_version=run.state_version,
            command_id=request.command_id,
        )
        if stored.run is None:
            raise KernelConflict("terminal status store CAS failed")
        settled = await self.projection_driver.settle(run.run_id)
        return KernelRunResult(_outcome_for_status(status), settled)

    async def _update_entry(
        self,
        run: OrchestratorRunState,
        batch_index: int,
        entry_index: int,
        *,
        state: str,
        command: str,
        invocation: ToolInvocation | None = None,
        acceptance: ToolAcceptance | None = None,
        opaque_public_call_id: str | None = None,
        result: ToolResult | None = None,
        public_update_index: int | None = None,
        public_terminal_emitted: bool | None = None,
    ) -> OrchestratorRunState:
        batches = list(run.tool_batches)
        batch = batches[batch_index]
        entries = list(batch.entries)
        original_entry = entries[entry_index]
        update: dict[str, object] = {
            "state": state,
            "buffered_terminal_result": result,
        }
        if invocation is not None:
            update["invocation"] = invocation
        if acceptance is not None:
            update["acceptance"] = acceptance
        if opaque_public_call_id is not None:
            update["opaque_public_call_id"] = opaque_public_call_id
        if public_update_index is not None:
            update["public_update_index"] = public_update_index
        if public_terminal_emitted is not None:
            update["public_terminal_emitted"] = public_terminal_emitted
        desired_entry = original_entry.model_copy(update=update)
        entries[entry_index] = desired_entry
        batches[batch_index] = batch.model_copy(update={"entries": entries})
        try:
            return await self._checkpoint(
                run, updates={"tool_batches": batches}, command_id=command
            )
        except KernelConflict:
            current = await self._load(run.run_id)
            current_batch_index, current_entry_index = _find_entry(
                current,
                assistant_message_id=original_entry.assistant_message_id,
                call_id=original_entry.call_id,
            )
            if current_batch_index is None or current_entry_index is None:
                raise KernelConflict(
                    "tool entry disappeared during CAS retry"
                ) from None
            current_entry = current.tool_batches[current_batch_index].entries[
                current_entry_index
            ]
            if current_entry == desired_entry:
                return current
            if current_entry != original_entry:
                raise KernelConflict("tool entry changed during CAS retry") from None
            current_batches = list(current.tool_batches)
            current_batch = current_batches[current_batch_index]
            current_entries = list(current_batch.entries)
            current_entries[current_entry_index] = desired_entry
            current_batches[current_batch_index] = current_batch.model_copy(
                update={"entries": current_entries}
            )
            return await self._checkpoint(
                current,
                updates={"tool_batches": current_batches},
                command_id=command,
            )

    async def _refresh_resource_manifest(
        self, run: OrchestratorRunState
    ) -> OrchestratorRunState:
        """Fold produced Agent artifacts into the live resource manifest.

        Inline DataParts are already durable in the accepted A2A observation.
        The metadata reader promotes their refs into real MIME/digest-bearing
        resources so the next model turn can select them from strict Tool
        schemas and the A2A runtime can rematerialize the original DataPart.
        """
        if run.resource_manifest is None:
            return run
        current = run
        for _attempt in range(3):
            existing = {ref.ref_id for ref in current.resource_manifest.refs}
            pending = [
                ref_id
                for ref_id in current.artifact_refs
                if ref_id and ref_id not in existing
            ]
            if not pending:
                return current
            new_refs: list[PreparedResourceRef] = []
            for ref_id in pending:
                described = (
                    await self.artifact_metadata_reader(
                        ref_id,
                        room_id=current.room_id,
                        room_epoch=current.request.room_epoch,
                    )
                    if self.artifact_metadata_reader is not None
                    else None
                )
                if described is None:
                    if ref_id.startswith("art_"):
                        raise KernelConflict(
                            f"inline artifact metadata is unavailable: {ref_id}"
                        )
                    described = PreparedResourceRef(
                        ref_id=ref_id,
                        kind="artifact",
                        source_message_id=current.request.user_message_id,
                        mime_type=None,
                        size_bytes=0,
                        content_digest="",
                    )
                new_refs.append(described)
            manifest = _resource_manifest_from_refs(
                [*current.resource_manifest.refs, *new_refs]
            )
            try:
                return await self._checkpoint(
                    current,
                    updates={"resource_manifest": manifest},
                    command_id=(
                        f"resource-manifest:{current.run_id}:{manifest.content_digest}"
                    ),
                )
            except KernelConflict:
                current = await self._load(current.run_id)
        raise KernelConflict("resource manifest refresh did not converge")

    async def _checkpoint(
        self,
        run: OrchestratorRunState,
        *,
        updates: dict[str, object],
        command_id: str,
    ) -> OrchestratorRunState:
        now = self.clock.now()
        candidate = run.model_copy(
            update={
                **updates,
                "state_version": run.state_version + 1,
                "updated_at": now,
            }
        )
        result = await self.run_store.cas_mutate(
            candidate,
            expected_state_version=run.state_version,
            command_id=command_id,
        )
        if result.outcome in {"accepted", "replayed"} and result.run is not None:
            return result.run
        if result.outcome == "conflict":
            current = await self._load(run.run_id)
            if command_id in current.processed_command_ids:
                return current
        raise KernelConflict(f"checkpoint failed: {command_id}:{result.outcome}")

    async def _load(self, run_id: str) -> OrchestratorRunState:
        run = await self.run_store.load(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _model_request(
        self,
        run: OrchestratorRunState,
        messages: list[object],
        tools: list[object],
        *,
        turn_id: str | None = None,
    ):
        from .models import ModelMessage, ModelTurnRequest, ToolDefinition

        return ModelTurnRequest(
            turn_id=turn_id or self._stable_id(run, "model-turn", run.state_version),
            model=run.profile.model,
            system_prompt=run.profile.prompt.rendered_system_prompt,
            messages=[item for item in messages if isinstance(item, ModelMessage)],
            tools=[item for item in tools if isinstance(item, ToolDefinition)],
            tool_choice="none" if run.budget.wrap_up_requested else "auto",
            purpose="agent_turn",
            thinking_level=run.profile.thinking_level,
            remaining_provider_retries=self.budget_policy.remaining_provider_retries(
                run.budget, run.profile
            ),
            absolute_deadline_at=run.budget.deadline_at,
        )

    def _assembly_notice(
        self, run: OrchestratorRunState, exc: ModelStreamAssemblyError
    ) -> SessionNotice:
        notice_id = self._stable_id(
            run,
            exc.code,
            len(run.budget.provider_attempt_keys),
            exc.provider_call_id or "",
            exc.tool_index if exc.tool_index is not None else "",
            exc.raw_arguments_digest or "",
        )
        return SessionNotice(
            notice_id=notice_id,
            code=exc.code,
            content="The prior tool call was malformed or incomplete; retry safely.",
            related_call_id=exc.provider_call_id,
            created_at=self.clock.now(),
        )

    def _stable_id(self, run: OrchestratorRunState, *parts: object) -> str:
        raw = ":".join([run.run_id, *(str(part) for part in parts)])
        return sha256(raw.encode()).hexdigest()


def _merge_artifact_refs(existing: list[str], results: list[ToolResult]) -> list[str]:
    return list(
        dict.fromkeys(
            [*existing, *(ref for result in results for ref in result.artifact_refs)]
        )
    )


def _resource_manifest_from_refs(
    refs: list[PreparedResourceRef],
) -> RunResourceManifestSnapshot:
    canonical = json.dumps(
        [ref.model_dump(mode="json") for ref in refs],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = sha256(canonical.encode()).hexdigest()
    return RunResourceManifestSnapshot(
        manifest_id=f"manifest-{digest}",
        refs=refs,
        content_digest=digest,
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


__all__ = [
    "KernelConflict",
    "KernelLifecycle",
    "KernelRunResult",
    "OrchestratorKernel",
    "SystemClock",
    "UUIDFactory",
]
