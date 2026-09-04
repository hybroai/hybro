"""Pure terminal-decision and projection-settlement state transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .events import canonicalize_orchestrator_event
from .models import (
    ArtifactRefPart,
    AssistantMessage,
    ContractModel,
    OrchestratorEvent,
    OrchestratorRunState,
    ProjectionIntent,
    ProjectionIntentStatus,
)

TerminalDecision = Literal[
    "ready",
    "waiting_external",
    "awaiting_user",
    "operational_rejection",
    "terminal_conflict",
]
StoreOutcome = Literal["accepted", "replayed", "conflict", "error"]


class ArtifactDeliveryCheck(ContractModel):
    artifact_ref: str
    exists: bool
    belongs_to_run: bool
    belongs_to_room: bool
    deliverable: bool


class TerminalDecisionFacts(ContractModel):
    final_message_id: str
    terminal_observations_persisted: bool = True
    cancellation_won: bool = False
    artifact_checks: list[ArtifactDeliveryCheck] = Field(default_factory=list)


class TerminalDecisionEvaluation(ContractModel):
    decision: TerminalDecision
    reason: str


class TerminalCommitRequest(ContractModel):
    expected_state_version: int = Field(ge=0)
    command_id: str
    event_id: str
    event_sequence: int = Field(gt=0)
    event_intent_id: str
    final_message_intent_id: str
    public_run_intent_id: str
    final_message_target: str
    public_run_target: str
    terminal_reason: str = "completed"
    correlation_id: str | None = None
    created_at: datetime


class TerminalCommitResult(ContractModel):
    outcome: StoreOutcome
    evaluation: TerminalDecisionEvaluation
    run: OrchestratorRunState
    event: OrchestratorEvent | None = None


def _find_final_message(
    run: OrchestratorRunState, final_message_id: str
) -> AssistantMessage | None:
    for message in run.transcript:
        if (
            isinstance(message, AssistantMessage)
            and message.message_id == final_message_id
        ):
            return message
    return None


def _accepted_public_tool_terminals_complete(run: OrchestratorRunState) -> bool:
    return all(
        entry.acceptance is None or entry.public_terminal_emitted
        for batch in run.tool_batches
        for entry in batch.entries
    )


def evaluate_terminal_decision(
    run: OrchestratorRunState,
    facts: TerminalDecisionFacts,
) -> TerminalDecisionEvaluation:
    """Evaluate only the machine-verifiable Section 15.1 completion gates."""

    if facts.cancellation_won or run.status in {"canceling", "canceled"}:
        return TerminalDecisionEvaluation(
            decision="terminal_conflict", reason="cancellation already won"
        )
    if run.status in {"completed", "failed", "budget_exhausted"}:
        return TerminalDecisionEvaluation(
            decision="terminal_conflict", reason="Run is already terminal"
        )

    final_message = _find_final_message(run, facts.final_message_id)
    if final_message is None or run.proposed_final_message_id != facts.final_message_id:
        return TerminalDecisionEvaluation(
            decision="operational_rejection",
            reason="stable final message is not part of the pending aggregate",
        )

    batch_states = [
        entry.state for batch in run.tool_batches for entry in batch.entries
    ]
    if any(state in {"input_required", "auth_required"} for state in batch_states):
        return TerminalDecisionEvaluation(
            decision="awaiting_user", reason="user interaction remains pending"
        )

    if any(
        state in {"pending", "accepted", "executing", "waiting_external"}
        for state in batch_states
    ):
        return TerminalDecisionEvaluation(
            decision="waiting_external", reason="ToolBatch entry remains active"
        )

    if not _accepted_public_tool_terminals_complete(run):
        return TerminalDecisionEvaluation(
            decision="waiting_external",
            reason="an accepted Tool public terminal is not durable",
        )

    if not facts.terminal_observations_persisted:
        return TerminalDecisionEvaluation(
            decision="waiting_external",
            reason="a required terminal call observation is not persisted",
        )

    referenced = {
        part.artifact_ref
        for part in final_message.content
        if isinstance(part, ArtifactRefPart)
    }
    checks = {check.artifact_ref: check for check in facts.artifact_checks}
    for artifact_ref in referenced:
        check = checks.get(artifact_ref)
        if (
            check is None
            or not check.exists
            or not check.belongs_to_run
            or not check.belongs_to_room
            or not check.deliverable
            or artifact_ref not in run.artifact_refs
        ):
            return TerminalDecisionEvaluation(
                decision="operational_rejection",
                reason=f"artifact {artifact_ref!r} is missing, foreign, or undeliverable",
            )

    return TerminalDecisionEvaluation(decision="ready", reason="completion gates pass")


def commit_terminal_decision(
    run: OrchestratorRunState,
    *,
    facts: TerminalDecisionFacts,
    request: TerminalCommitRequest,
) -> TerminalCommitResult:
    """Apply the authoritative completion CAS and create its atomic outbox facts."""

    if request.command_id in run.processed_command_ids:
        return TerminalCommitResult(
            outcome="replayed",
            evaluation=TerminalDecisionEvaluation(
                decision="terminal_conflict", reason="command already applied"
            ),
            run=run,
        )
    if request.expected_state_version != run.state_version:
        return TerminalCommitResult(
            outcome="conflict",
            evaluation=TerminalDecisionEvaluation(
                decision="terminal_conflict", reason="state version changed"
            ),
            run=run,
        )

    evaluation = evaluate_terminal_decision(run, facts)
    if evaluation.decision != "ready":
        return TerminalCommitResult(outcome="conflict", evaluation=evaluation, run=run)

    intent_ids = {
        request.event_intent_id,
        request.final_message_intent_id,
        request.public_run_intent_id,
    }
    existing_intent_ids = {item.intent_id for item in run.projection_outbox}
    if (
        len(intent_ids) != 3
        or intent_ids & existing_intent_ids
        or any(item.event_id == request.event_id for item in run.projection_outbox)
    ):
        return TerminalCommitResult(
            outcome="conflict",
            evaluation=TerminalDecisionEvaluation(
                decision="terminal_conflict",
                reason="terminal event or intent identity is already allocated",
            ),
            run=run,
        )

    allocated_sequences: dict[str, int] = {}
    for item in run.projection_outbox:
        allocated = allocated_sequences.setdefault(item.event_id, item.event_sequence)
        if allocated != item.event_sequence:
            return TerminalCommitResult(
                outcome="conflict",
                evaluation=TerminalDecisionEvaluation(
                    decision="terminal_conflict",
                    reason="existing event identity has inconsistent sequences",
                ),
                run=run,
            )
    occupied_sequences: dict[int, str] = {}
    for event_id, sequence in allocated_sequences.items():
        occupied_by = occupied_sequences.setdefault(sequence, event_id)
        if occupied_by != event_id:
            return TerminalCommitResult(
                outcome="conflict",
                evaluation=TerminalDecisionEvaluation(
                    decision="terminal_conflict",
                    reason="existing event sequence is allocated more than once",
                ),
                run=run,
            )
    expected_sequence = max(occupied_sequences, default=0) + 1
    if request.event_sequence != expected_sequence:
        return TerminalCommitResult(
            outcome="conflict",
            evaluation=TerminalDecisionEvaluation(
                decision="terminal_conflict",
                reason=f"expected terminal event sequence {expected_sequence}",
            ),
            run=run,
        )

    next_version = run.state_version + 1
    event_payload: dict[str, object] = {
        "final_message_id": facts.final_message_id,
        "terminal_reason": request.terminal_reason,
    }
    event = canonicalize_orchestrator_event(
        OrchestratorEvent(
            event_id=request.event_id,
            event_type="run_completed",
            session_id=run.session_id,
            run_id=run.run_id,
            room_id=run.room_id,
            room_epoch=run.request.room_epoch,
            sequence=request.event_sequence,
            state_version=next_version,
            causation_id=request.command_id,
            correlation_id=request.correlation_id,
            payload=event_payload,
            created_at=request.created_at,
        )
    )
    common = {
        "event_id": event.event_id,
        "event_sequence": event.sequence,
        "causation_id": request.command_id,
        "status": "pending",
    }
    intents = [
        ProjectionIntent(
            intent_id=request.event_intent_id,
            kind="append_orchestrator_event",
            target="orchestrator_run_events",
            dedupe_key=event.event_id,
            required=True,
            payload=event.model_dump(mode="json"),
            **common,
        ),
        ProjectionIntent(
            intent_id=request.final_message_intent_id,
            kind="deliver_final_message",
            target=request.final_message_target,
            dedupe_key=f"final-message:{facts.final_message_id}",
            required=True,
            payload={
                "run_id": run.run_id,
                "room_id": run.room_id,
                "message_id": facts.final_message_id,
            },
            **common,
        ),
        ProjectionIntent(
            intent_id=request.public_run_intent_id,
            kind="project_terminal_run_status",
            target=request.public_run_target,
            dedupe_key=f"run-completed:{run.run_id}",
            required=True,
            payload={"run_id": run.run_id, "status": "completed"},
            **common,
        ),
    ]
    committed = run.model_copy(
        update={
            "status": "completed",
            "terminal_reason": request.terminal_reason,
            "projection_state": "pending",
            "projection_outbox": [*run.projection_outbox, *intents],
            "processed_command_ids": [
                *run.processed_command_ids,
                request.command_id,
            ],
            "state_version": next_version,
            "updated_at": request.created_at,
        }
    )
    return TerminalCommitResult(
        outcome="accepted", evaluation=evaluation, run=committed, event=event
    )


class TerminalStatusCommitRequest(ContractModel):
    expected_state_version: int = Field(ge=0)
    command_id: str
    event_id: str
    event_sequence: int = Field(gt=0)
    event_intent_id: str
    public_run_intent_id: str
    public_run_target: str
    status: Literal["failed", "canceled", "budget_exhausted"]
    terminal_reason: str
    cancellation_cause: (
        Literal["user_requested", "room_closed", "shutdown", "policy"] | None
    ) = None
    correlation_id: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _cancellation_cause_matches_status(self) -> TerminalStatusCommitRequest:
        if self.status == "canceled" and self.cancellation_cause is None:
            raise ValueError("canceled terminal status requires cancellation_cause")
        if self.status != "canceled" and self.cancellation_cause is not None:
            raise ValueError("cancellation_cause is valid only for canceled status")
        return self


class TerminalStatusCommitResult(ContractModel):
    outcome: StoreOutcome
    run: OrchestratorRunState
    event: OrchestratorEvent | None = None


def commit_terminal_status(
    run: OrchestratorRunState,
    *,
    request: TerminalStatusCommitRequest,
) -> TerminalStatusCommitResult:
    """Commit a non-success terminal winner and its mandatory projection facts."""

    if request.command_id in run.processed_command_ids:
        return TerminalStatusCommitResult(outcome="replayed", run=run)
    if request.expected_state_version != run.state_version:
        return TerminalStatusCommitResult(outcome="conflict", run=run)
    if run.status in {"completed", "failed", "canceled", "budget_exhausted"}:
        return TerminalStatusCommitResult(outcome="conflict", run=run)
    if run.status == "canceling" and (
        request.status != "canceled"
        or request.cancellation_cause != run.cancellation_cause
    ):
        return TerminalStatusCommitResult(outcome="conflict", run=run)
    if not _accepted_public_tool_terminals_complete(run):
        return TerminalStatusCommitResult(outcome="conflict", run=run)
    expected_sequence = (
        max((item.event_sequence for item in run.projection_outbox), default=0) + 1
    )
    if request.event_sequence != expected_sequence:
        return TerminalStatusCommitResult(outcome="conflict", run=run)
    identities = {
        request.event_id,
        request.event_intent_id,
        request.public_run_intent_id,
    }
    if len(identities) != 3 or any(
        item.event_id == request.event_id
        or item.intent_id in {request.event_intent_id, request.public_run_intent_id}
        for item in run.projection_outbox
    ):
        return TerminalStatusCommitResult(outcome="conflict", run=run)

    next_version = run.state_version + 1
    event = canonicalize_orchestrator_event(
        OrchestratorEvent(
            event_id=request.event_id,
            event_type=f"run_{request.status}",  # type: ignore[arg-type]
            session_id=run.session_id,
            run_id=run.run_id,
            room_id=run.room_id,
            room_epoch=run.request.room_epoch,
            sequence=request.event_sequence,
            state_version=next_version,
            causation_id=request.command_id,
            correlation_id=request.correlation_id,
            payload={"terminal_reason": request.terminal_reason},
            created_at=request.created_at,
        )
    )
    common = {
        "event_id": event.event_id,
        "event_sequence": event.sequence,
        "causation_id": request.command_id,
        "status": "pending",
        "required": True,
    }
    intents = [
        ProjectionIntent(
            intent_id=request.event_intent_id,
            kind="append_orchestrator_event",
            target="orchestrator_run_events",
            dedupe_key=event.event_id,
            payload=event.model_dump(mode="json"),
            **common,
        ),
        ProjectionIntent(
            intent_id=request.public_run_intent_id,
            kind="project_terminal_run_status",
            target=request.public_run_target,
            dedupe_key=f"run-{request.status}:{run.run_id}",
            payload={"run_id": run.run_id, "status": request.status},
            **common,
        ),
    ]
    committed = run.model_copy(
        update={
            "status": request.status,
            "terminal_reason": request.terminal_reason,
            "cancellation_cause": request.cancellation_cause,
            "projection_state": "pending",
            "projection_outbox": [*run.projection_outbox, *intents],
            "processed_command_ids": [
                *run.processed_command_ids,
                request.command_id,
            ],
            "state_version": next_version,
            "updated_at": request.created_at,
        }
    )
    return TerminalStatusCommitResult(outcome="accepted", run=committed, event=event)


PROJECTION_INTENT_TRANSITIONS: dict[
    ProjectionIntentStatus, frozenset[ProjectionIntentStatus]
] = {
    "pending": frozenset({"claimed", "blocked"}),
    "claimed": frozenset({"pending", "completed", "blocked"}),
    "completed": frozenset(),
    "blocked": frozenset(),
}


class IllegalProjectionIntentTransition(ValueError):
    """Raised when an outbox intent is reopened or skips its claim."""


def transition_projection_intent(
    intent: ProjectionIntent,
    *,
    to_status: ProjectionIntentStatus,
    claim_owner: str | None = None,
    claim_expires_at: datetime | None = None,
    blocked_reason: str | None = None,
    next_attempt_at: datetime | None = None,
) -> ProjectionIntent:
    """Return a new intent after a legal claim/complete/block/release transition."""

    if to_status not in PROJECTION_INTENT_TRANSITIONS[intent.status]:
        raise IllegalProjectionIntentTransition(
            f"illegal projection transition: {intent.status} -> {to_status}"
        )
    if to_status == "claimed" and (claim_owner is None or claim_expires_at is None):
        raise ValueError("claimed intents require an owner and lease expiry")
    if to_status == "blocked" and not blocked_reason:
        raise ValueError("blocked intents require a reason")
    return intent.model_copy(
        update={
            "status": to_status,
            "blocked_reason": blocked_reason if to_status == "blocked" else None,
            "claim_owner": claim_owner if to_status == "claimed" else None,
            "claim_expires_at": (claim_expires_at if to_status == "claimed" else None),
            "attempt_count": intent.attempt_count
            + (1 if to_status == "claimed" else 0),
            "next_attempt_at": next_attempt_at,
        }
    )


def evaluate_projection_settlement(
    intents: list[ProjectionIntent],
) -> Literal["pending", "settled", "blocked"]:
    """Evaluate required delivery only; an empty inventory is never settled."""

    required = [intent for intent in intents if intent.required]
    if not required:
        return "pending"
    if any(intent.status == "blocked" for intent in required):
        return "blocked"
    if any(intent.status != "completed" for intent in required):
        return "pending"
    return "settled"


def _has_mandatory_terminal_intents(run: OrchestratorRunState) -> bool:
    groups: dict[tuple[str, int, str], list[ProjectionIntent]] = {}
    for item in run.projection_outbox:
        key = (item.event_id, item.event_sequence, item.causation_id)
        groups.setdefault(key, []).append(item)

    mandatory = {"append_orchestrator_event", "project_terminal_run_status"}
    if run.status == "completed":
        mandatory.add("deliver_final_message")

    matching_groups: list[list[ProjectionIntent]] = []
    for items in groups.values():
        required = [item for item in items if item.required]
        if not mandatory <= {item.kind for item in required}:
            continue
        public_status = next(
            (
                item
                for item in required
                if item.kind == "project_terminal_run_status"
                and item.payload.get("status") == run.status
            ),
            None,
        )
        event = next(
            (
                item
                for item in required
                if item.kind == "append_orchestrator_event"
                and item.payload.get("event_type") == f"run_{run.status}"
            ),
            None,
        )
        if public_status is None or event is None:
            continue
        if run.status == "completed" and not any(
            item.kind == "deliver_final_message"
            and item.payload.get("message_id") == run.proposed_final_message_id
            for item in required
        ):
            continue
        matching_groups.append(required)

    return len(matching_groups) == 1


class TerminalEvaluationTransitionResult(ContractModel):
    outcome: StoreOutcome
    run: OrchestratorRunState


def transition_after_terminal_evaluation(
    run: OrchestratorRunState,
    *,
    evaluation: TerminalDecisionEvaluation,
    expected_state_version: int,
    updated_at: datetime,
) -> TerminalEvaluationTransitionResult:
    """Persist the non-terminal wait selected by the operational truth table."""

    if expected_state_version != run.state_version:
        return TerminalEvaluationTransitionResult(outcome="conflict", run=run)
    if run.status in {
        "canceling",
        "completed",
        "failed",
        "canceled",
        "budget_exhausted",
    }:
        return TerminalEvaluationTransitionResult(outcome="conflict", run=run)
    target = {
        "waiting_external": "waiting_external",
        "awaiting_user": "awaiting_user",
    }.get(evaluation.decision)
    if target is None:
        return TerminalEvaluationTransitionResult(outcome="conflict", run=run)
    if run.status == target:
        return TerminalEvaluationTransitionResult(outcome="replayed", run=run)
    transitioned = run.model_copy(
        update={
            "status": target,
            "state_version": run.state_version + 1,
            "updated_at": updated_at,
        }
    )
    return TerminalEvaluationTransitionResult(outcome="accepted", run=transitioned)


class ProjectionSettlementResult(ContractModel):
    outcome: StoreOutcome
    run: OrchestratorRunState


def transition_projection_settlement(
    run: OrchestratorRunState,
    *,
    expected_state_version: int,
    updated_at: datetime,
) -> ProjectionSettlementResult:
    """CAS the derived projection state without changing the terminal winner."""

    if expected_state_version != run.state_version:
        return ProjectionSettlementResult(outcome="conflict", run=run)
    if run.status not in {"completed", "failed", "canceled", "budget_exhausted"}:
        return ProjectionSettlementResult(outcome="conflict", run=run)

    projection_state = (
        evaluate_projection_settlement(run.projection_outbox)
        if _has_mandatory_terminal_intents(run)
        else "pending"
    )
    if projection_state == run.projection_state:
        return ProjectionSettlementResult(outcome="replayed", run=run)
    settled = run.model_copy(
        update={
            "projection_state": projection_state,
            "state_version": run.state_version + 1,
            "updated_at": updated_at,
        }
    )
    return ProjectionSettlementResult(outcome="accepted", run=settled)
