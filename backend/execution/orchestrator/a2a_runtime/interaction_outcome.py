"""Shared typed/untyped interaction parking for execute, continuation, and recovery."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import ValidationError

from common.dto.delivery import (
    DeliveryEmitStatus,
    HITLRequestEvent,
    HITLResolvedEvent,
)
from common.dto.hitl import A2AInteractionSpec

from ..models import TextPart, ToolResult
from .errors import RecoverableCheckpointError
from .hitl_prompt import prompt_type_for_question
from .ledger import apply_observation, transition_call
from .models import AgentCallLedgerRecord, NormalizedA2AObservation
from .ports import HITLApplicationPort

InteractionParkKind = Literal["typed_waiting", "untyped_completed", "invalid_failed"]

CasFn = Callable[[AgentCallLedgerRecord, int], Awaitable[AgentCallLedgerRecord]]
CanonicalHITLControlPublisher = Callable[[str, str, str, list[str]], Awaitable[None]]


def _digest_json(value: object) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode()).hexdigest()


def _canonical_public_call_id(record: AgentCallLedgerRecord, run: Any) -> str:
    public_call_id = next(
        (
            entry.opaque_public_call_id
            for batch in getattr(run, "tool_batches", [])
            for entry in batch.entries
            if entry.call_id == record.invocation_id and entry.opaque_public_call_id
        ),
        None,
    )
    if not public_call_id:
        raise RuntimeError("canonical HITL call has no opaque public identity")
    return str(public_call_id)


def public_activity_message_id(
    record: AgentCallLedgerRecord,
    run: Any | None = None,
) -> str:
    call_id = (
        _canonical_public_call_id(record, run)
        if getattr(run, "lifecycle_family", None) == "canonical"
        else record.invocation_id
    )
    return f"orchestrator:{record.run_id}:{call_id}"


def _canonical_agent_label(record: AgentCallLedgerRecord, run: Any) -> str:
    catalog = getattr(run, "tool_catalog", None)
    entry = next(
        (
            item
            for item in (catalog.entries if catalog is not None else [])
            if item.definition.name == record.tool_name
        ),
        None,
    )
    if entry is None:
        raise RuntimeError("canonical HITL call has no frozen Agent identity")
    label = (entry.agent_display_name or entry.definition.label).strip()
    if not label:
        raise RuntimeError("canonical HITL call has an empty public Agent label")
    return label[:160]


def _public_text(
    value: str,
    *,
    limit: int,
    secret_values: tuple[str, ...],
) -> str:
    from execution.orchestrator.public_text import sanitize_public_text

    return sanitize_public_text(value, secret_values=secret_values)[:limit]


def public_agent_label(
    record: AgentCallLedgerRecord,
    run: Any,
    *,
    secret_values: tuple[str, ...] = (),
) -> str:
    return _public_text(
        _canonical_agent_label(record, run),
        limit=160,
        secret_values=secret_values,
    )


async def _emit_delivery_event(
    delivery: Any,
    event: HITLRequestEvent | HITLResolvedEvent,
    *,
    canonical: bool,
) -> None:
    checked = getattr(delivery, "emit_checked", None)
    if canonical and callable(checked):
        status = checked(event)
        if inspect.isawaitable(status):
            status = await status
        if status not in {
            DeliveryEmitStatus.DELIVERED,
            DeliveryEmitStatus.ALREADY_DELIVERED,
            DeliveryEmitStatus.DEDUPLICATED,
        }:
            raise RuntimeError(
                f"canonical {event.event_type} was not durably delivered"
            )
        return
    result = delivery.emit(event)
    if inspect.isawaitable(result):
        result = await result
    if canonical and result is not True:
        raise RuntimeError(f"canonical {event.event_type} was not durably delivered")


async def park_call_for_interaction(
    *,
    call: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
    hitl: HITLApplicationPort | None,
    cas: CasFn,
) -> tuple[AgentCallLedgerRecord, InteractionParkKind]:
    """Park a call on an interaction observation.

    Typed specs → ``input_required`` / ``auth_required`` with activated HITL.
    Missing spec → silent completed tool result (cyber untyped recovery).
    Invalid spec → fail-closed failed terminal.
    """
    raw_spec = observation.interaction_spec
    if raw_spec is not None:
        if hitl is None:
            raise RuntimeError("HITL port not bound but interaction spec received")
        try:
            interaction = A2AInteractionSpec.model_validate(raw_spec)
            fingerprint = _digest_json(interaction.model_dump(mode="json"))
        except (ValidationError, ValueError):
            failed = await _invalid_failed(call, observation, cas=cas)
            return failed, "invalid_failed"
        waiting = await _typed_waiting(
            call,
            observation,
            hitl=hitl,
            interaction=interaction,
            fingerprint=fingerprint,
            cas=cas,
        )
        return waiting, "typed_waiting"

    if (
        call.answer_applied is not None
        and call.continuation_command is not None
        and observation.event_kind in {"input_required", "auth_required"}
    ):
        # Mid-continuation inspect/send can see input_required with a cleared
        # status.message (no typed spec). Completing that as an untyped tool
        # result ends the call and the kernel narrates the ask as a final
        # answer — breaking multi-round typed HITL.
        raise RecoverableCheckpointError(
            "refusing untyped interaction completion during HITL continuation"
        )

    completed = await _untyped_completed(call, observation, cas=cas)
    return completed, "untyped_completed"


async def _typed_waiting(
    call: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
    *,
    hitl: HITLApplicationPort,
    interaction: A2AInteractionSpec,
    fingerprint: str,
    cas: CasFn,
) -> AgentCallLedgerRecord:
    waiting_state = (
        observation.event_kind
        if observation.event_kind in {"input_required", "auth_required"}
        else "input_required"
    )
    if (
        call.state == waiting_state
        and call.pending_interaction_id == interaction.interaction_id
        and call.interaction_fingerprint == fingerprint
    ):
        activated = await hitl.activate(
            interaction.interaction_id,
            call_record_id=call.call_record_id,
            interaction_fingerprint=fingerprint,
        )
        if activated not in {"accepted", "replayed"}:
            raise RecoverableCheckpointError(
                f"HITL interaction {interaction.interaction_id!r} could not be activated"
            )
        return call

    pending = apply_observation(
        call,
        observation,
        recent_limit=call.runtime_policy.recent_observation_id_limit,
    )
    pending = pending.model_copy(update={"claim_owner": None, "claim_expires_at": None})
    persisted = await cas(pending, call.state_version)
    if persisted != pending:
        return persisted
    call = persisted

    interaction_id = await hitl.create_or_replay(
        call=call,
        interaction=interaction,
        interaction_fingerprint=fingerprint,
    )
    waiting = transition_call(
        call,
        to_state=waiting_state,
        updated_at=datetime.now(UTC),
        pending_interaction_id=interaction_id,
        interaction_revision=1,
        interaction_fingerprint=fingerprint,
        claim_owner=None,
        claim_expires_at=None,
    )
    persisted = await cas(waiting, call.state_version)
    if persisted != waiting:
        return persisted

    activated = await hitl.activate(
        interaction_id,
        call_record_id=persisted.call_record_id,
        interaction_fingerprint=fingerprint,
    )
    if activated not in {"accepted", "replayed"}:
        raise RecoverableCheckpointError(
            f"HITL interaction {interaction_id!r} could not be activated"
        )
    return persisted


async def _untyped_completed(
    call: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
    *,
    cas: CasFn,
) -> AgentCallLedgerRecord:
    content = list(observation.content or [])
    if not content:
        content = [TextPart(text="The Agent requested additional input.")]
    result = ToolResult(
        call_id=call.invocation_id,
        tool_name=call.tool_name,
        status="completed",
        content=content,
        artifact_refs=list(observation.artifact_refs or []),
        error_code=None,
        error_message=None,
    )
    terminal = transition_call(
        call,
        to_state="completed",
        updated_at=datetime.now(UTC),
        terminal_result=result,
        terminal_result_digest=sha256(result.model_dump_json().encode()).hexdigest(),
        claim_owner=None,
        claim_expires_at=None,
    )
    return await cas(terminal, call.state_version)


async def _invalid_failed(
    call: AgentCallLedgerRecord,
    observation: NormalizedA2AObservation,
    *,
    cas: CasFn,
) -> AgentCallLedgerRecord:
    result = ToolResult(
        call_id=call.invocation_id,
        tool_name=call.tool_name,
        status="failed",
        content=[TextPart(text="The Agent returned invalid interaction metadata.")],
        artifact_refs=[],
        error_code="invalid_interaction_metadata",
        error_message="Agent interaction metadata was invalid.",
    )
    terminal = transition_call(
        call,
        to_state="failed",
        updated_at=datetime.now(UTC),
        terminal_result=result,
        terminal_result_digest=sha256(result.model_dump_json().encode()).hexdigest(),
        error_code="invalid_interaction_metadata",
        error_message="Agent interaction metadata was invalid.",
        claim_owner=None,
        claim_expires_at=None,
    )
    return await cas(terminal, call.state_version)


async def _load_hitl_run(record: AgentCallLedgerRecord, run_store: Any | None) -> Any:
    if run_store is None:
        return None
    return await run_store.load(record.run_id)


async def emit_hitl_request_events(
    *,
    record: AgentCallLedgerRecord,
    interaction: A2AInteractionSpec,
    interaction_id: str,
    hitl_delivery: Any | None,
    run_store: Any | None = None,
    canonical_control: CanonicalHITLControlPublisher | None = None,
    public_secret_values: tuple[str, ...] = (),
) -> None:
    run = await _load_hitl_run(record, run_store)
    canonical = getattr(run, "lifecycle_family", None) == "canonical"
    if hitl_delivery is None:
        if canonical:
            raise RuntimeError("canonical HITL delivery is not bound")
        return
    if canonical and canonical_control is None:
        raise RuntimeError("canonical HITL control publisher is not bound")
    related_user_message_id = run.request.user_message_id if run is not None else None
    client_request_id = run.client_request_id if run is not None else None
    if canonical and (not related_user_message_id or not client_request_id):
        raise RuntimeError("canonical HITL request has no exact Turn root")
    message_id = public_activity_message_id(record, run)
    agent_label = (
        public_agent_label(
            record,
            run,
            secret_values=public_secret_values,
        )
        if canonical
        else None
    )
    request_ids: list[str] = []
    for index, question in enumerate(interaction.questions):
        request_ids.append(question.question_id)
        choices = list(question.choices) if question.choices else None
        event = HITLRequestEvent(
            room_id=record.room_id,
            run_id=record.run_id if canonical else None,
            request_id=question.question_id,
            message_id=message_id,
            source="agent",
            prompt=(
                _public_text(
                    question.prompt,
                    limit=4_000,
                    secret_values=public_secret_values,
                )
                if canonical
                else question.prompt
            ),
            prompt_type=prompt_type_for_question(question),
            choices=(
                [
                    _public_text(
                        choice,
                        limit=500,
                        secret_values=public_secret_values,
                    )
                    for choice in choices[:20]
                ]
                if canonical and choices
                else choices
            ),
            agent_id=None if canonical else record.agent_id,
            agent_label=agent_label,
            source_step_id=None if canonical else record.call_record_id,
            interaction_id=interaction_id,
            interaction_status=None if canonical else "pending",
            interaction_version=None if canonical else 1,
            question_count=len(interaction.questions),
            question_index=index,
            related_message_id=(None if canonical else related_user_message_id),
            related_user_message_id=(related_user_message_id if canonical else None),
            client_request_id=client_request_id,
        )
        await _emit_delivery_event(hitl_delivery, event, canonical=canonical)
    if canonical:
        assert canonical_control is not None
        await canonical_control(
            "run_waiting_input",
            record.run_id,
            interaction_id,
            request_ids,
        )


async def emit_hitl_resolved_events(
    *,
    record: AgentCallLedgerRecord,
    interaction: A2AInteractionSpec,
    interaction_id: str,
    status: str,
    hitl_delivery: Any | None,
    run_store: Any | None = None,
    canonical_control: CanonicalHITLControlPublisher | None = None,
    answer_ref: str | None = None,
) -> None:
    run = await _load_hitl_run(record, run_store)
    canonical = getattr(run, "lifecycle_family", None) == "canonical"
    if hitl_delivery is None:
        if canonical:
            raise RuntimeError("canonical HITL delivery is not bound")
        return
    if canonical and status == "responded" and canonical_control is None:
        raise RuntimeError("canonical HITL control publisher is not bound")
    related_user_message_id = run.request.user_message_id if run is not None else None
    client_request_id = run.client_request_id if run is not None else None
    if canonical and (not related_user_message_id or not client_request_id):
        raise RuntimeError("canonical HITL response has no exact Turn root")
    message_id = public_activity_message_id(record, run)
    request_ids: list[str] = []
    for index, question in enumerate(interaction.questions):
        request_ids.append(question.question_id)
        event = HITLResolvedEvent(
            room_id=record.room_id,
            run_id=record.run_id if canonical else None,
            request_id=question.question_id,
            message_id=message_id,
            source="agent",
            status=status,
            interaction_id=interaction_id,
            interaction_status=None if canonical else status,
            interaction_version=None if canonical else 1,
            question_count=len(interaction.questions),
            question_index=index,
            answer_ref=answer_ref if canonical else None,
            related_message_id=(None if canonical else related_user_message_id),
            related_user_message_id=(related_user_message_id if canonical else None),
            client_request_id=client_request_id,
        )
        await _emit_delivery_event(hitl_delivery, event, canonical=canonical)
    if canonical and status == "responded":
        latest = await _load_hitl_run(record, run_store)
        if latest is None or latest.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }:
            return
        assert canonical_control is not None
        await canonical_control(
            "run_resumed",
            record.run_id,
            interaction_id,
            request_ids,
        )


__all__ = [
    "CanonicalHITLControlPublisher",
    "InteractionParkKind",
    "emit_hitl_request_events",
    "emit_hitl_resolved_events",
    "park_call_for_interaction",
    "public_activity_message_id",
    "public_agent_label",
]
