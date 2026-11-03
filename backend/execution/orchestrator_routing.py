"""Orchestrator ingress adapter for the single execution path.

This module is the *only* orchestrator surface the product entry points
(``execution/facade.py`` and ``api_gateway/routes``) are allowed to import. It
owns the ingress concerns between ``ExecutionRequest`` and the orchestrator
runtime:

* **Run-creation** — ``process_room_user_message`` prepares a Run and drives
  the orchestrator ``RoomSessionHost`` prompt for every new user message.
* **Ingress routing** — HITL answers and cancellation
  are dispatched to the orchestrator runtime (the only runtime).

Building the orchestrator's ``RoomSessionHost`` inputs from the request and
the persisted user message also lives here, so ``execution/facade.py`` stays
orchestrator-import-free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from approval import (
    HITLConflictError,
    HITLDeliveryUncertainError,
    HITLRoomMismatchError,
)
from common.dto.execution import CancellationAck, ExecutionRequest, HITLRequest
from common.dto.hitl import (
    A2AInteractionSpec,
    HITLAnswerKind,
    HITLConfirmationAnswer,
    HITLMultiChoiceAnswer,
    HITLPolicyDecision,
    HITLPolicyDecisionAnswer,
    HITLQuestionAnswer,
    HITLQuestionSpec,
    HITLSingleChoiceAnswer,
    HITLTextAnswer,
)
from common.observability import events as E
from common.utils.context_utils import split_agent_mentions
from common.utils.logger import get_logger
from context_memory.translators import normalize_room_memory
from execution.orchestrator.a2a_runtime.hitl_prompt import (
    prompt_type_for_question,
    public_question_fields,
)
from execution.orchestrator.a2a_runtime.interaction_outcome import (
    emit_hitl_resolved_events,
    public_activity_message_id,
    public_agent_label,
)
from execution.orchestrator.a2a_runtime.ledger import TERMINAL_AGENT_CALL_STATES
from execution.orchestrator.models import (
    AuthorizationBasis,
    CandidateScopeSnapshot,
    ModelMessage,
    ModelTextPart,
    PreparedResourceRef,
    RunResourceManifestSnapshot,
    TextPart,
    UserMessage,
)
from execution.orchestrator.persistence import TERMINAL_RUN_STATUSES
from execution.orchestrator.public_text import sanitize_public_text
from execution.orchestrator.session import (
    DefaultRunFactory,
    RunFactory,
    SessionConflict,
)
from execution.orchestrator.transcript import resolved_user_answers
from models.response import OrchestrationResponse

logger = get_logger(__name__)

MODE_PROFILE_MAP = {
    "direct": "fast",
    "supervisor": "ultimate",
    "passthrough": "passthrough",
}

_PROFILE_PINNED_INITIAL_ROUTING = "explicit_agent_first"
_PROFILE_PINNED_FINALIZATION = "pass_through"
_MAX_ROOM_HISTORY_MESSAGES = 10
_MAX_ROOM_HISTORY_CHARS_PER_MESSAGE = 4_000
# Earlier attachments and Agent artifacts a follow-up Run may select by ref.
_MAX_EARLIER_RESOURCES = 20
_MAX_EARLIER_RESOURCE_TURNS = 50

RoomMemoryReader = Callable[[str], Awaitable[dict[str, Any] | None]]


async def request_run_cancellation(
    run_store: Any,
    run: Any,
    *,
    cause: str = "user_requested",
) -> Any:
    """Claim the Run cancellation winner through the shared durable CAS."""

    command_id = f"cancel:{run.run_id}:{cause}"
    current = run
    for _attempt in range(4):
        result = await run_store.request_cancellation(
            current.run_id,
            expected_state_version=current.state_version,
            command_id=command_id,
            cause=cause,
            requested_at=datetime.now(UTC),
        )
        if result.run is None:
            raise OrchestratorRoutingError("Run cancellation CAS failed")
        current = result.run
        if result.outcome in {"accepted", "replayed"}:
            return current
        if current.status not in {
            "queued",
            "running",
            "waiting_external",
            "awaiting_user",
        }:
            return current
    raise OrchestratorRoutingError("Run cancellation CAS did not converge")


class UnsupportedEnvelopeError(ValueError):
    """The request envelope asks for something the orchestrator cannot serve."""


class OrchestratorRoutingError(RuntimeError):
    """The routing seam is misconfigured or a required binding is missing."""


class OrchestratorHITLNotOwnedError(OrchestratorRoutingError):
    """The interaction is not owned by the orchestrator A2A ingress.

    Supervisor ``ask_user`` interactions materialize through the unified
    Execution HITL service and must fall back to its HITL manager instead
    of the orchestrator call-ledger ingress.
    """


@dataclass(frozen=True, slots=True)
class AttachmentEnvelope:
    file_id: str
    mime_type: str | None = None
    size_bytes: int = 0
    content_digest: str = ""


@dataclass(frozen=True, slots=True)
class RoomMessageEnvelope:
    message_text: str
    mode: str
    candidate_agent_ids: list[str]
    attachments: list[AttachmentEnvelope] | None = None
    # Extracted attachment text (PDF/text projections) rendered into the
    # kernel's user message so the LLM can carry attachment facts into agent
    # tasks instead of losing them during task decomposition.
    attachment_texts: list[str] = field(default_factory=list)
    requesting_subject_id: str | None = None
    # The canonical scope source the candidates were resolved from; it feeds
    # the Run's frozen AuthorizationBasis (membership vs all-active-agents).
    scope_source: str = "explicit_selection"
    group_id: str | None = None
    # Model-facing projection of ``message_text``. The stored message keeps the
    # editor's ``<@agent-id|AgentName>`` token so the UI can render a chip, but
    # that token carries an internal id and a syntax the model cannot interpret,
    # so it is removed here. ``message_text`` stays the durable record.
    model_text: str | None = None
    # Display names the user explicitly addressed, in the order they appeared.
    # Naming an Agent is a user instruction to use it, so it reaches the model
    # as a separate, platform-authored note rather than as prose.
    addressed_agents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoomContext:
    history: tuple[ModelMessage, ...] = ()
    earlier_refs: tuple[PreparedResourceRef, ...] = ()


class RoomEnvelopeSource(Protocol):
    async def load_envelope(
        self, request: ExecutionRequest, *, message_id: str
    ) -> RoomMessageEnvelope: ...


class RoomMessageEnvelopeResolver:
    """Resolve the orchestrator inputs for one persisted user message.

    The route-validated ``ExecutionRequest`` supplies the mode and candidate
    scope; the persisted message supplies the text and attachments.
    """

    def __init__(
        self,
        *,
        get_user_message: Callable[[str], Awaitable[Any | None]],
        list_room_agent_ids: Callable[[str], Awaitable[list[str]]],
        list_group_agent_ids: Callable[[str], Awaitable[list[str]]] | None = None,
        list_all_active_agent_ids: Callable[[str | None], Awaitable[list[str]]]
        | None = None,
        attachment_text_reader: (
            Callable[[AttachmentEnvelope], Awaitable[str | None]] | None
        ) = None,
    ) -> None:
        self._get_user_message = get_user_message
        self._list_room_agent_ids = list_room_agent_ids
        self._list_group_agent_ids = list_group_agent_ids
        self._list_all_active_agent_ids = list_all_active_agent_ids
        self._attachment_text_reader = attachment_text_reader

    async def load_envelope(
        self, request: ExecutionRequest, *, message_id: str
    ) -> RoomMessageEnvelope:
        message = await self._get_user_message(message_id)
        if message is None:
            raise UnsupportedEnvelopeError(
                f"orchestrator cannot resolve user message {message_id!r}"
            )
        content = getattr(message, "message_content", None)
        message_text = getattr(content, "message_text", None)
        if not isinstance(message_text, str) or not message_text.strip():
            raise UnsupportedEnvelopeError(
                "orchestrator requires a non-empty user message"
            )

        mode = request.mode
        scope = request.agent_scope.model_dump(mode="json")
        candidate_agent_ids = await self._resolve_candidate_agent_ids(
            request.conversation_id, scope, user_id=request.sender_id
        )
        scope_source = str(scope.get("source") or "explicit_selection")
        group_id = (
            scope.get("group_id") if isinstance(scope.get("group_id"), str) else None
        )

        attachments = _attachments_from_message(content)
        attachment_texts = await self._resolve_attachment_texts(attachments)
        requesting_subject_id = request.sender_id
        model_text, mentions = split_agent_mentions(message_text)
        # Only a mention that survived scope resolution names an Agent this Run
        # can actually call; a mention outside the candidate scope is dropped
        # with the token but not reported as addressed.
        in_scope = {str(agent_id) for agent_id in candidate_agent_ids}
        addressed_agents = tuple(
            dict.fromkeys(
                name for agent_id, name in mentions if str(agent_id) in in_scope
            )
        )
        return RoomMessageEnvelope(
            message_text=message_text,
            mode=mode,
            candidate_agent_ids=candidate_agent_ids,
            attachments=attachments,
            attachment_texts=attachment_texts,
            requesting_subject_id=requesting_subject_id,
            scope_source=scope_source,
            group_id=group_id,
            model_text=model_text,
            addressed_agents=addressed_agents,
        )

    async def _resolve_attachment_texts(
        self, attachments: list[AttachmentEnvelope]
    ) -> list[str]:
        """Project attachment contents into the kernel's user message."""
        if self._attachment_text_reader is None:
            return []
        blocks: list[str] = []
        for attachment in attachments:
            text = await self._attachment_text_reader(attachment)
            if text:
                blocks.append(
                    f"[attachment {attachment.file_id}"
                    f" ({attachment.mime_type or 'unknown'})]:\n{text}"
                )
        return blocks

    async def _resolve_candidate_agent_ids(
        self, conversation_id: str | None, scope: Any, *, user_id: str | None = None
    ) -> list[str]:
        if not isinstance(scope, dict):
            raise UnsupportedEnvelopeError(
                "orchestrator envelope is missing agent_scope"
            )
        source = scope.get("source")
        if source == "mention":
            agent_ids = scope.get("agent_ids") or []
            return [str(agent_id) for agent_id in agent_ids if agent_id]
        if source == "room_default":
            if conversation_id is None:
                raise UnsupportedEnvelopeError(
                    "orchestrator room scope requires conversation_id"
                )
            return await self._list_room_agent_ids(conversation_id)
        if source == "all_agents":
            if self._list_all_active_agent_ids is None:
                raise UnsupportedEnvelopeError(
                    "orchestrator all_agents scope is not bound"
                )
            return await self._list_all_active_agent_ids(user_id)
        if source == "saved_group":
            if self._list_group_agent_ids is None:
                raise UnsupportedEnvelopeError(
                    "orchestrator saved_group scope is not bound"
                )
            group_id = scope.get("group_id")
            if not isinstance(group_id, str) or not group_id.strip():
                raise UnsupportedEnvelopeError(
                    "orchestrator saved_group scope requires group_id"
                )
            return await self._list_group_agent_ids(group_id)
        raise UnsupportedEnvelopeError(
            f"orchestrator cannot serve agent scope {source!r}"
        )


def _attachments_from_message(content: Any) -> list[AttachmentEnvelope]:
    raw_attachments = getattr(content, "attachments", None)
    if not isinstance(raw_attachments, list):
        return []
    attachments: list[AttachmentEnvelope] = []
    for item in raw_attachments:
        file_id = (
            item.get("file_id")
            if isinstance(item, dict)
            else getattr(item, "file_id", None)
        )
        if not isinstance(file_id, str) or not file_id.strip():
            continue
        attachments.append(
            AttachmentEnvelope(
                file_id=file_id,
                mime_type=(
                    item.get("mime_type")
                    if isinstance(item, dict)
                    else getattr(item, "mime_type", None)
                ),
                size_bytes=(
                    item.get("size_bytes", 0)
                    if isinstance(item, dict)
                    else getattr(item, "size_bytes", 0)
                )
                or 0,
                content_digest=(
                    item.get("sha256", "")
                    if isinstance(item, dict)
                    else getattr(item, "sha256", "")
                )
                or "",
            )
        )
    return attachments


def map_mode_to_profile(mode: str) -> str:
    profile_id = MODE_PROFILE_MAP.get(mode)
    if profile_id is None:
        raise UnsupportedEnvelopeError(f"unsupported execution mode {mode!r}")
    return profile_id


# Scope source → AuthorizationBasis.kind. Scopes outside this map fall back to
# explicit_selection (per-turn user selection; skips room-membership refresh).
# Roster-derived kinds (room_member / saved_group_member) still require
# membership at authorization refresh.
_SCOPE_AUTHORIZATION_KINDS = {
    "mention": "mention",
    "room_default": "room_member",
    "saved_group": "saved_group_member",
    "all_agents": "all_active_agents",
}


def _build_candidate_scope(
    *,
    conversation_id: str,
    agent_ids: list[str],
    scope_source: str = "explicit_selection",
    group_id: str | None = None,
    requesting_subject_id: str | None = None,
) -> CandidateScopeSnapshot:
    basis = AuthorizationBasis(
        kind=_SCOPE_AUTHORIZATION_KINDS.get(scope_source, "explicit_selection"),
        conversation_id=conversation_id,
        group_id=group_id,
        selected_by_user_id=requesting_subject_id or None,
    )
    return CandidateScopeSnapshot(
        snapshot_id=f"scope-{_sha256_hex(json.dumps([conversation_id, sorted(agent_ids), scope_source, group_id or '']))}",
        source=scope_source,
        conversation_id=conversation_id,
        group_id=group_id,
        agent_ids=list(dict.fromkeys(agent_ids)),
        authorization_basis=basis,
    )


def _addressed_agents_note(names: tuple[str, ...]) -> str:
    """Platform-authored line naming the Agents the user explicitly addressed.

    It is rendered as its own part so the model can tell it apart from the
    user's prose, and it is the only signal that an explicit mention happened —
    the stored ``<@agent-id|AgentName>`` token never reaches the model.
    """
    return f"[Explicitly addressed by the user: {', '.join(names)}]"


def _build_resource_manifest(
    *,
    source_message_id: str,
    user_text: str | None,
    attachments: list[AttachmentEnvelope] | None,
    earlier_refs: tuple[PreparedResourceRef, ...] = (),
) -> RunResourceManifestSnapshot:
    from context_memory.resources import (
        AttachmentResource,
        ResourceCatalogSource,
        assemble_resource_catalog,
    )

    entries = assemble_resource_catalog(
        ResourceCatalogSource(
            user_message_id=source_message_id,
            user_text=user_text,
            attachments=[
                AttachmentResource(
                    file_id=attachment.file_id,
                    mime_type=attachment.mime_type,
                    size_bytes=attachment.size_bytes,
                    content_digest=attachment.content_digest,
                )
                for attachment in (attachments or [])
            ],
        )
    )
    refs = [
        PreparedResourceRef(
            ref_id=entry.ref_id,
            kind=entry.kind,
            source_message_id=entry.source_message_id,
            mime_type=entry.mime_type,
            size_bytes=entry.size_bytes,
            content_digest=entry.content_digest,
        )
        for entry in entries
    ]
    current = {ref.ref_id for ref in refs}
    refs.extend(ref for ref in earlier_refs if ref.ref_id not in current)
    return RunResourceManifestSnapshot(
        manifest_id=f"manifest-{_sha256_hex(json.dumps([ref.model_dump(mode='json') for ref in refs]))}",
        refs=refs,
        content_digest=_sha256_hex(
            json.dumps([ref.model_dump(mode="json") for ref in refs])
        ),
    )


def _attach_turn_records(messages: list[ModelMessage], records: dict[int, str]) -> None:
    """Append each earlier turn's record after that turn's answer.

    Keyed by the user message index; attaching to the following assistant
    message keeps the history alternating.
    """
    for index, record in records.items():
        target = (
            index + 1
            if index + 1 < len(messages) and messages[index + 1].role == "assistant"
            else index
        )
        messages[target] = ModelMessage(
            role=messages[target].role,
            content=[*messages[target].content, ModelTextPart(text=record)],
        )


def _history_entries(
    state: Any, current_turn_id: str
) -> tuple[list[tuple[str | None, ModelMessage]], list[str]]:
    """Recent history as ``(user turn id or None, message)`` plus all user turns.

    The window keeps the most recent messages and starts with a user turn.
    """
    user_turn_ids: list[str] = []
    # (user turn id or None for an answer, message) in history order.
    entries: list[tuple[str | None, ModelMessage]] = []
    for turn in state.conversation_history:
        if turn.turn_id == current_turn_id:
            continue
        content = turn.content or turn.brief_summary
        if not isinstance(content, str) or not content.strip():
            continue
        content = content.strip()[:_MAX_ROOM_HISTORY_CHARS_PER_MESSAGE]
        if turn.role == "user":
            user_turn_ids.append(turn.turn_id)
            entries.append(
                (
                    turn.turn_id,
                    ModelMessage(role="user", content=[ModelTextPart(text=content)]),
                )
            )
        elif turn.role == "supervisor" or (
            turn.role == "agent" and turn.agent_id == "system:hybro"
        ):
            entries.append(
                (
                    None,
                    ModelMessage(
                        role="assistant", content=[ModelTextPart(text=content)]
                    ),
                )
            )
    entries = entries[-_MAX_ROOM_HISTORY_MESSAGES:]
    while entries and entries[0][1].role != "user":
        entries.pop(0)
    return entries, user_turn_ids


def _run_resources(run: Any) -> list[tuple[PreparedResourceRef, str]]:
    """A finished Run's attachments and Agent artifacts, with short labels."""
    manifest = run.resource_manifest
    if manifest is None:
        return []
    agent_names = {
        entry.definition.name: entry.agent_display_name or entry.definition.label
        for entry in (run.tool_catalog.entries if run.tool_catalog else [])
    }
    producers: dict[str, str] = {}
    for message in run.transcript:
        name = agent_names.get(getattr(message, "tool_name", ""))
        if name is None or getattr(message, "kind", None) != "tool_result":
            continue
        for ref_id in message.artifact_refs:
            producers.setdefault(ref_id, name)
    resources: list[tuple[PreparedResourceRef, str]] = []
    for ref in manifest.refs:
        mime = ref.mime_type or "unknown type"
        if ref.kind == "attachment":
            resources.append((ref, f"attachment the user uploaded ({mime})"))
        elif ref.kind == "artifact":
            producer = producers.get(ref.ref_id)
            label = f"Artifact ({mime})"
            resources.append(
                (ref, f"{label} produced by {producer}" if producer else label)
            )
    return resources


def _earlier_turn_record(
    answers: list[tuple[str, str]],
    resources: list[tuple[str, PreparedResourceRef, str]],
    *,
    header: str,
) -> str | None:
    """Platform note with a turn's user answers and still-selectable files."""
    if not answers and not resources:
        return None
    lines = [header]
    if answers:
        lines.append("The user gave these answers; treat them as user-supplied facts:")
        lines.extend(
            f"- Question: {question}\n  Answer: {answer}"
            for question, answer in answers
        )
    if resources:
        lines.append(
            "Files that Agents can still receive by reference "
            "(pass the id through attachment_refs or artifact_refs):"
        )
        lines.extend(
            f"- {ref.ref_id}: {label}" for _turn, ref, label in reversed(resources)
        )
    return "\n".join(lines)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _map_interaction_answers(
    spec: A2AInteractionSpec, answers: list[dict[str, str]]
) -> list[HITLQuestionAnswer]:
    questions = {question.question_id: question for question in spec.questions}
    mapped: list[HITLQuestionAnswer] = []
    for raw in answers:
        question_id = raw.get("request_id")
        user_input = raw.get("user_input")
        if question_id not in questions:
            raise HITLConflictError(
                "The input request changed before submission. Refresh and try again."
            )
        question = questions[question_id]
        mapped.append(
            HITLQuestionAnswer(
                question_id=question_id,
                answer=_answer_for_question(question, user_input or ""),
            )
        )
    required = {
        question.question_id for question in spec.questions if question.required
    }
    if not required.issubset(answer.question_id for answer in mapped):
        raise HITLConflictError(
            "The input request changed before submission. Refresh and try again."
        )
    return mapped


def _answer_for_question(question: HITLQuestionSpec, user_input: str) -> Any:
    """Typed answer for the UI's text: a listed choice, or Other when allowed."""

    kind = question.answer_kind
    if kind == HITLAnswerKind.TEXT:
        return HITLTextAnswer(text=user_input)
    choices = set(question.choices or ())
    if kind == HITLAnswerKind.SINGLE_CHOICE:
        value = user_input.strip()
        if value in choices or not question.allow_other:
            return HITLSingleChoiceAnswer(choice=value)
        return HITLSingleChoiceAnswer(other=value)
    if kind == HITLAnswerKind.MULTI_CHOICE:
        tokens = [token.strip() for token in user_input.split(",") if token.strip()]
        if not question.allow_other:
            return HITLMultiChoiceAnswer(choices=tokens)
        other = ", ".join(token for token in tokens if token not in choices)
        return HITLMultiChoiceAnswer(
            choices=[token for token in tokens if token in choices],
            other=other or None,
        )
    normalized = user_input.strip().lower()
    if kind == HITLAnswerKind.CONFIRMATION:
        return HITLConfirmationAnswer(
            confirmed=normalized
            in {"true", "yes", "1", "confirm", "confirmed", "approve"}
        )
    if kind == HITLAnswerKind.POLICY_DECISION:
        # The UI offers Approve and Reject for policy decisions.
        if normalized in {"approve", "approved"}:
            return HITLPolicyDecisionAnswer(decision=HITLPolicyDecision.APPROVE)
        if normalized in {"reject", "rejected", "deny", "denied"}:
            return HITLPolicyDecisionAnswer(decision=HITLPolicyDecision.DENY)
        raise HITLConflictError("Answer this request with Approve or Reject.")
    raise UnsupportedEnvelopeError(
        f"orchestrator cannot serve HITL answer kind {kind.value!r} from legacy text"
    )


class _PreparedRunFactory:
    """Run factory that pins the Run id the catalog was prepared against."""

    def __init__(self, run_id: str, base: RunFactory) -> None:
        self._run_id = run_id
        self._base = base

    def create_run(
        self,
        *,
        config: Any,
        message: UserMessage,
        client_request_id: str | None,
    ) -> Any:
        run = self._base.create_run(
            config=config, message=message, client_request_id=client_request_id
        )
        return run.model_copy(update={"run_id": self._run_id})


class DualRuntimeRouter:
    """Thin orchestrator ingress adapter for the single execution path.

    Construction is cheap and non-IO. ``runtime`` is the composed orchestrator
    runtime; every ingress (message, cancellation, HITL answer) is served by
    the orchestrator directly.
    """

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        envelope_source: RoomEnvelopeSource | None = None,
        run_factory: RunFactory | None = None,
        room_memory_reader: RoomMemoryReader | None = None,
    ) -> None:
        self._runtime = runtime
        self._envelope_source = envelope_source
        self._run_factory = run_factory or DefaultRunFactory()
        self._room_memory_reader = room_memory_reader

    # -- Ingress routing -------------------------------------------------

    async def route_cancellation_by_user_message(
        self,
        user_message_id: str,
        *,
        reason: str,
        deletion_id: str | None = None,
        post_claim_cleanup: Callable[[], Awaitable[object]] | None = None,
    ) -> CancellationAck:
        """Claim durable Run cancellation before interrupting descendants."""
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator cancellation is not bound")
        run = await self._runtime.run_store.load_by_user_message_id(user_message_id)
        if run is None:
            raise KeyError(user_message_id)
        run = await self._request_run_cancellation(run)
        command_id = f"cancel:{run.run_id}:user_requested"
        if run.status == "canceled" and run.cancellation_command_id == command_id:
            return CancellationAck(
                status="canceled", cancellation_applied=True, reconciled=True
            )
        if run.status != "canceling" or run.cancellation_command_id != command_id:
            return CancellationAck(
                status=run.status,
                cancellation_applied=False,
                reconciled=run.status != "canceling",
            )
        try:
            results = await self._runtime.cancellation_coordinator.cancel_run(
                run.run_id,
                reason=reason,
                deletion_id=deletion_id,
                notify_remote=True,
            )
            if any(
                state not in TERMINAL_AGENT_CALL_STATES for state in results.values()
            ):
                return self._pending_cancellation_ack()
            await self._cancel_owned_hitl_for_run(run, reason=reason)
            if post_claim_cleanup is not None:
                await post_claim_cleanup()
            settled = await self._runtime.session_host.reconcile_cancellation(run)
        except Exception as exc:
            logger.emit(
                E.RUN_CANCELLATION_RECONCILIATION_DEFERRED,
                exc=exc,
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                client_request_id=run.client_request_id,
            )
            return self._pending_cancellation_ack()
        if settled.run.status != "canceled":
            return self._pending_cancellation_ack()
        try:
            await self._runtime.session_host.signal_run_cancellation(run, command_id)
        except Exception as exc:
            logger.emit(
                E.RUN_CANCELLATION_CLEANUP_FAILED,
                exc=exc,
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                client_request_id=run.client_request_id,
            )
        return CancellationAck(
            status="canceled", cancellation_applied=True, reconciled=True
        )

    async def _request_run_cancellation(self, run: Any) -> Any:
        return await request_run_cancellation(self._runtime.run_store, run)

    @staticmethod
    def _pending_cancellation_ack() -> CancellationAck:
        return CancellationAck(
            status="cancellation_pending",
            cancellation_applied=True,
            reconciled=False,
        )

    async def route_hitl_answer(
        self,
        *,
        interaction_id: str,
        answers: list[dict[str, str]],
        responder_id: str,
        conversation_id: str,
    ) -> str:
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator HITL ingress is not bound")
        read = await self._runtime.hitl_port.read_interaction(interaction_id)
        if read is None:
            raise OrchestratorHITLNotOwnedError(interaction_id)
        spec, route, _fingerprint = read
        if route.conversation_id != conversation_id:
            raise HITLRoomMismatchError("Room mismatch")
        run = await self._runtime.run_store.load(route.orchestration_run_id)
        if run is None or run.status in {"canceling", *TERMINAL_RUN_STATUSES}:
            return "canceled"
        mapped = _map_interaction_answers(spec, answers)
        try:
            state = await self._runtime.continuation.resume(
                call_record_id=route.call_record_id,
                interaction_id=interaction_id,
                interaction_revision=route.interaction_revision,
                route_fingerprint=route.fingerprint,
                answers=mapped,
                authenticated_answerer_id=responder_id,
            )
        except (PermissionError, ValueError) as exc:
            # Durable answer identity/inventory/owner conflicts are stale
            # client state, not internal server failures.
            raise HITLConflictError(
                "HITL interaction changed before the answer was applied"
            ) from exc
        # The continuation coordinator durably publishes hitl_response and
        # run_resumed before dispatch, so an immediate follow-up challenge
        # cannot overtake the interaction it replaces. Then process the new
        # observation so the parked Run can leave suspension.
        await self._wake_after_hitl_resume(
            route.call_record_id,
            answered_interaction_id=interaction_id,
        )
        if state == "delivery_uncertain":
            # A continuation that returns terminal artifacts is routed through
            # the observation inbox before it can be applied to the call. The
            # synchronous wake above may prove that exact answered call
            # terminal even though the transport receipt was initially
            # classified as uncertain. Prefer that durable proof over a stale
            # 503; only an actually nonterminal call remains uncertain.
            latest = await self._runtime.call_ledger.load_by_record_id(
                route.call_record_id
            )
            marker = getattr(latest, "answer_applied", None)
            if (
                latest is not None
                and latest.state in TERMINAL_AGENT_CALL_STATES
                and marker is not None
                and marker.interaction_id == interaction_id
                and marker.interaction_revision == route.interaction_revision
            ):
                return latest.state
            # A different parked round (a new interaction or revision) can only
            # follow the Agent receiving this answer: delivery is proven.
            if (
                latest is not None
                and latest.state in {"input_required", "auth_required"}
                and (
                    latest.pending_interaction_id != interaction_id
                    or latest.interaction_revision != route.interaction_revision
                )
            ):
                return latest.state
            raise HITLDeliveryUncertainError(
                "HITL answer was recorded but continuation delivery is uncertain"
            )
        return state

    async def cancel_hitl_interaction(
        self,
        *,
        conversation_id: str,
        interaction_id: str,
        expected_version: int,
    ) -> int:
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator HITL ingress is not bound")
        read = await self._runtime.hitl_port.read_interaction(interaction_id)
        if read is None:
            raise KeyError(interaction_id)
        spec, route, _fingerprint = read
        if route.conversation_id != conversation_id:
            raise HITLRoomMismatchError("Room mismatch")
        if route.interaction_revision != expected_version:
            raise HITLConflictError("HITL interaction changed before cancellation")
        run = await self._runtime.run_store.load(route.orchestration_run_id)
        if run is None:
            raise KeyError(route.orchestration_run_id)
        run = await self._request_run_cancellation(run)
        command_id = f"cancel:{run.run_id}:user_requested"
        if run.status == "canceled" and run.cancellation_command_id == command_id:
            return expected_version
        if run.status != "canceling" or run.cancellation_command_id != command_id:
            raise HITLConflictError(
                f"Run cancellation lost to durable state {run.status}"
            )
        results = await self._runtime.cancellation_coordinator.cancel_run(
            route.orchestration_run_id,
            reason="hitl_canceled",
            notify_remote=True,
        )
        if any(state not in TERMINAL_AGENT_CALL_STATES for state in results.values()):
            raise OrchestratorRoutingError(
                "local Agent-call cancellation remains pending"
            )
        abandoned = await self._runtime.hitl_port.abandon(
            interaction_id,
            call_record_id=route.call_record_id,
            reason="user_canceled",
        )
        if abandoned not in {"accepted", "replayed", "absent"}:
            raise HITLConflictError("HITL interaction could not be canceled")
        # Close the public interaction before settling the owning Run; a
        # terminal run_settled event is invalid while HITL remains active.
        await self._emit_hitl_resolved_events(
            conversation_id=conversation_id,
            spec=spec,
            route=route,
            status="canceled",
        )
        await self._runtime.session_host.reconcile_cancellation(run)
        await self._runtime.session_host.signal_run_cancellation(run, command_id)
        return expected_version

    async def _cancel_owned_hitl_for_run(self, run: Any, *, reason: str) -> None:
        hitl_port = getattr(self._runtime, "hitl_port", None)
        if hitl_port is None:
            return
        interactions = await hitl_port.get_eligible_interactions(run.conversation_id)
        for spec, route, _fingerprint in interactions:
            if route.orchestration_run_id != run.run_id:
                continue
            abandoned = await hitl_port.abandon(
                spec.interaction_id,
                call_record_id=route.call_record_id,
                reason=reason,
            )
            if abandoned not in {"accepted", "replayed", "absent"}:
                raise HITLConflictError("HITL interaction could not be canceled")
            await self._emit_hitl_resolved_events(
                conversation_id=run.conversation_id,
                spec=spec,
                route=route,
                status="canceled",
            )

    async def _wake_after_hitl_resume(
        self,
        call_record_id: str,
        *,
        answered_interaction_id: str | None = None,
    ) -> None:
        if self._runtime is None:
            return
        processor = getattr(self._runtime, "observation_processor", None)
        ledger = getattr(self._runtime, "call_ledger", None)
        if processor is None or ledger is None:
            return
        call = await ledger.load_by_record_id(call_record_id)
        if call is None:
            return
        # Skip only when still waiting on the exact challenge just answered.
        # A different pending interaction is a real continuation round and its
        # observation must be delivered back to the kernel for model-first
        # presentation rather than auto-published by the A2A runtime.
        if call.state in {"input_required", "auth_required"} and (
            answered_interaction_id is None
            or call.pending_interaction_id == answered_interaction_id
        ):
            return

        candidate_ids: list[str] = []
        inbox = getattr(self._runtime, "observation_inbox", None)
        if inbox is not None:
            try:
                matching = await inbox.list_due_for_call(
                    call_record_id,
                    due_at=datetime.now(UTC),
                    limit=100,
                )
            except Exception as exc:
                matching = []
                logger.emit(
                    E.HITL_WAKE_OBSERVATION_READ_FAILED,
                    exc=exc,
                    call_record_id=call_record_id,
                )
            matching.sort(
                key=lambda record: (
                    record.observation.event_kind == "terminal",
                    record.observation.observed_at,
                ),
                reverse=True,
            )
            candidate_ids.extend(record.observation_id for record in matching)
        candidate_ids.extend(reversed(call.recent_observation_ids))

        for observation_id in dict.fromkeys(candidate_ids):
            try:
                await processor.process(observation_id)
            except Exception as exc:
                logger.emit(
                    E.HITL_WAKE_OBSERVATION_PROCESSING_FAILED,
                    exc=exc,
                    call_record_id=call_record_id,
                    observation_id=observation_id,
                )
                continue
            break

    async def _emit_hitl_resolved_events(
        self,
        *,
        conversation_id: str,
        spec: A2AInteractionSpec,
        route: Any,
        status: str,
    ) -> None:
        if self._runtime is None:
            return
        run = await self._runtime.run_store.load(route.orchestration_run_id)
        canonical = run is not None
        delivery = getattr(self._runtime, "hitl_delivery", None)
        if delivery is None:
            if canonical:
                raise OrchestratorRoutingError("canonical HITL delivery is not bound")
            return
        call = await self._runtime.call_ledger.load_by_record_id(route.call_record_id)
        if call is None:
            if canonical:
                raise OrchestratorRoutingError(
                    "canonical HITL call is unavailable for public closure"
                )
            return
        await emit_hitl_resolved_events(
            record=call,
            interaction=spec,
            interaction_id=spec.interaction_id,
            status=status,
            hitl_delivery=delivery,
            run_store=self._runtime.run_store,
            canonical_control=getattr(
                self._runtime.continuation,
                "canonical_hitl_control",
                None,
            ),
        )

    async def get_pending_hitl(self, conversation_id: str) -> list[HITLRequest]:
        if self._runtime is None:
            return []

        interactions = await self._runtime.hitl_port.get_published_interactions(
            conversation_id
        )
        requests: list[HITLRequest] = []
        for spec, route, _fingerprint in interactions:
            run = await self._runtime.run_store.load(route.orchestration_run_id)
            if run is None:
                continue

            call = await self._runtime.call_ledger.load_by_record_id(
                route.call_record_id
            )
            if call is None:
                continue
            if call.state not in {"input_required", "auth_required"}:
                continue
            if call.pending_interaction_id != spec.interaction_id:
                continue

            user_message_id = run.request.user_message_id
            secret_values = tuple(
                getattr(self._runtime, "public_secret_values", ()) or ()
            )
            agent_name = public_agent_label(
                call,
                run,
                secret_values=secret_values,
            )
            for index, question in enumerate(spec.questions):
                choices = (
                    [
                        sanitize_public_text(
                            choice,
                            secret_values=secret_values,
                        )[:500]
                        for choice in list(question.choices)[:20]
                    ]
                    if question.choices is not None
                    else None
                )
                requests.append(
                    HITLRequest(
                        request_id=question.question_id,
                        conversation_id=conversation_id,
                        user_message_id=user_message_id,
                        source="agent",
                        prompt=sanitize_public_text(
                            question.prompt,
                            secret_values=secret_values,
                        )[:4000],
                        message_id=public_activity_message_id(call, run),
                        display_message_id=public_activity_message_id(call, run),
                        agent_name=agent_name,
                        orchestration_run_id=route.orchestration_run_id,
                        client_request_id=run.client_request_id,
                        prompt_type=prompt_type_for_question(question),
                        choices=choices,
                        **public_question_fields(question, secret_values=secret_values),
                        interaction_id=spec.interaction_id,
                        interaction_status="pending",
                        interaction_version=route.interaction_revision,
                        question_count=len(spec.questions),
                        question_index=index,
                    )
                )
        return requests

    # -- Room message adapter ------------------------------------

    async def _resolve_envelope_and_profile(
        self, request: ExecutionRequest, *, message_id: str
    ) -> tuple[RoomMessageEnvelope, Any]:
        """Resolve and validate the envelope without any orchestrator side effect."""
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator message adapter is not bound")
        if self._envelope_source is None:
            raise UnsupportedEnvelopeError(
                "orchestrator message adapter is not bound to a room envelope source"
            )
        envelope = await self._envelope_source.load_envelope(
            request, message_id=message_id
        )
        profile_id = map_mode_to_profile(envelope.mode)
        profile = self._runtime.profiles.get(profile_id)
        if profile is None:
            raise UnsupportedEnvelopeError(
                f"orchestrator profile {profile_id!r} is not resolved"
            )
        if (
            profile.initial_routing != _PROFILE_PINNED_INITIAL_ROUTING
            or profile.finalization != _PROFILE_PINNED_FINALIZATION
        ):
            raise UnsupportedEnvelopeError(
                "orchestrator cannot yet serve this profile's reserved "
                "routing/finalization dimensions"
            )
        return envelope, profile

    async def process_room_user_message(
        self, request: ExecutionRequest, *, message_id: str
    ) -> OrchestrationResponse:
        envelope, profile = await self._resolve_envelope_and_profile(
            request, message_id=message_id
        )
        conversation_id = request.conversation_id

        requesting_subject_id = envelope.requesting_subject_id or request.sender_id
        candidate_scope = _build_candidate_scope(
            conversation_id=conversation_id,
            agent_ids=envelope.candidate_agent_ids,
            scope_source=envelope.scope_source,
            group_id=envelope.group_id,
            requesting_subject_id=requesting_subject_id,
        )
        if not candidate_scope.agent_ids:
            # An empty scope cannot produce a meaningful kernel run; reject it
            # until the seam grows a zero-candidate synthesis path.
            raise UnsupportedEnvelopeError(
                "orchestrator candidate scope resolved to zero agents"
            )
        session_host = self._runtime.session_host
        session = session_host.get_session(conversation_id)
        if session is not None:
            if await session.has_active_run():
                raise SessionConflict("a Run is already active for this Room")
            # A session pins ONE Run id and ONE frozen catalog, so an idle
            # (terminal) session is replaced by a freshly prepared one for
            # every new message instead of replaying the stale Run id.
            session_host.drop_session(conversation_id)
        epoch = await self._runtime.epoch_store.read_active(conversation_id)
        if epoch is None:
            raise UnsupportedEnvelopeError("Room epoch is not active")
        room_context = await self._load_room_context(
            conversation_id,
            current_message_id=message_id,
            room_epoch=epoch.epoch,
        )
        resource_manifest = _build_resource_manifest(
            source_message_id=message_id,
            user_text=envelope.message_text,
            attachments=envelope.attachments,
            earlier_refs=room_context.earlier_refs,
        )
        run_id = f"run-{uuid4().hex}"
        prepared = await self._runtime.catalog_assembler.prepare(
            run_id=run_id,
            conversation_id=conversation_id,
            room_epoch=epoch.epoch,
            requesting_subject_id=requesting_subject_id,
            candidate_scope=candidate_scope,
            resource_manifest=resource_manifest,
            authorization_basis_digest=_sha256_hex(
                json.dumps(
                    candidate_scope.authorization_basis.model_dump(mode="json"),
                    sort_keys=True,
                )
            ),
            created_at=datetime.now(UTC),
        )
        await session_host.create_session(
            conversation_id=conversation_id,
            profile=profile,
            candidate_scope=candidate_scope,
            requesting_subject_id=requesting_subject_id,
            frozen_catalog=prepared.snapshot,
            resource_manifest=resource_manifest,
            conversation_history=room_context.history,
            run_factory=_PreparedRunFactory(run_id, self._run_factory),
        )

        message = UserMessage(
            message_id=message_id,
            content=[
                *(
                    [TextPart(text=_addressed_agents_note(envelope.addressed_agents))]
                    if envelope.addressed_agents
                    else []
                ),
                TextPart(
                    text=(
                        envelope.model_text
                        if envelope.model_text is not None
                        else envelope.message_text
                    )
                ),
                *[TextPart(text=block) for block in envelope.attachment_texts],
            ],
            created_at=datetime.now(UTC),
        )
        result = await session_host.prompt(
            conversation_id,
            message,
            client_request_id=request.client_request_id,
        )
        return OrchestrationResponse(
            task_id=result.run.run_id if getattr(result, "run", None) else None,
            conversation_id=conversation_id,
            success=True,
            status_code=200,
        )

    async def _load_room_context(
        self,
        conversation_id: str,
        *,
        current_message_id: str,
        room_epoch: int | None = None,
    ) -> RoomContext:
        """Load earlier turns' history and the files they left for follow-ups.

        Room memory keeps only each turn's message and final answer. Each
        earlier turn's Run supplies what memory drops: answers the user gave
        during the turn, and the attachments and Agent artifacts it holds,
        which stay selectable by reference in the new Run (same Room epoch,
        most recent first, bounded).
        """
        if self._room_memory_reader is None:
            return RoomContext()
        document = await self._room_memory_reader(conversation_id)
        if not document:
            return RoomContext()
        entries, user_turn_ids = _history_entries(
            normalize_room_memory(document), f"message:{current_message_id}"
        )
        runs: dict[str, Any] = {}
        earlier = await self._earlier_resources(
            user_turn_ids, runs, room_epoch=room_epoch
        )
        kept = {turn_id for turn_id, _ in entries if turn_id is not None}
        messages = [message for _, message in entries]
        records: dict[int, str] = {}
        for index, (turn_id, _message) in enumerate(entries):
            if turn_id is None:
                continue
            run = await self._turn_run(turn_id, runs)
            record = _earlier_turn_record(
                resolved_user_answers(run.transcript) if run is not None else [],
                [item for item in earlier if item[0] == turn_id],
                header="[platform record of this earlier turn]",
            )
            if record:
                records[index] = record
        older = _earlier_turn_record(
            [],
            [item for item in earlier if item[0] not in kept],
            header="[platform record of older turns in this room]",
        )
        if older and messages:
            messages[0] = ModelMessage(
                role=messages[0].role,
                content=[*messages[0].content, ModelTextPart(text=older)],
            )
        _attach_turn_records(messages, records)
        return RoomContext(
            history=tuple(messages),
            earlier_refs=tuple(ref for _, ref, _label in reversed(earlier)),
        )

    async def _earlier_resources(
        self,
        user_turn_ids: list[str],
        runs: dict[str, Any],
        *,
        room_epoch: int | None,
    ) -> list[tuple[str, PreparedResourceRef, str]]:
        """Most recent earlier attachments and artifacts, newest first."""
        if room_epoch is None:
            return []
        found: list[tuple[str, PreparedResourceRef, str]] = []
        seen: set[str] = set()
        for turn_id in reversed(user_turn_ids[-_MAX_EARLIER_RESOURCE_TURNS:]):
            run = await self._turn_run(turn_id, runs)
            if run is None or run.request.room_epoch != room_epoch:
                continue
            for ref, label in reversed(_run_resources(run)):
                if ref.ref_id in seen:
                    continue
                seen.add(ref.ref_id)
                found.append((turn_id, ref, label))
                if len(found) >= _MAX_EARLIER_RESOURCES:
                    return found
        return found

    async def _turn_run(self, turn_id: str, runs: dict[str, Any]) -> Any | None:
        if turn_id in runs:
            return runs[turn_id]
        run_store = getattr(self._runtime, "run_store", None)
        run = None
        if run_store is not None and turn_id.startswith("message:"):
            try:
                run = await run_store.load_by_user_message_id(
                    turn_id.removeprefix("message:")
                )
            except Exception as exc:
                logger.emit(E.EARLIER_TURN_RUN_LOAD_FAILED, exc=exc, turn_id=turn_id)
        runs[turn_id] = run
        return run


__all__ = [
    "AttachmentEnvelope",
    "DualRuntimeRouter",
    "MODE_PROFILE_MAP",
    "OrchestratorRoutingError",
    "RoomEnvelopeSource",
    "RoomMessageEnvelope",
    "RoomMessageEnvelopeResolver",
    "UnsupportedEnvelopeError",
    "map_mode_to_profile",
]
