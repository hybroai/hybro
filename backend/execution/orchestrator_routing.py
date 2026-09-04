"""Orchestrator ingress adapter for the single execution path.

This module is the *only* orchestrator surface the product entry points
(``execution/facade.py`` and ``api_gateway/routes``) are allowed to import. It
owns the ingress concerns that translate between the legacy
``OrchestrationRequest`` envelope and the orchestrator runtime:

* **Run-creation** — ``process_room_user_message`` prepares a Run and drives
  the orchestrator ``RoomSessionHost`` prompt for every new user message.
* **Ingress routing** — webhook observations, HITL answers, and cancellation
  are dispatched to the orchestrator runtime (the only runtime).

The heavy translation between the ``OrchestrationRequest`` envelope and the
orchestrator's ``RoomSessionHost`` inputs also lives here, so
``execution/facade.py`` stays orchestrator-import-free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from common.dto.execution import CancellationAck, HITLRequest
from common.dto.hitl import (
    A2AInteractionSpec,
    HITLAnswerKind,
    HITLConfirmationAnswer,
    HITLMultiChoiceAnswer,
    HITLQuestionAnswer,
    HITLSingleChoiceAnswer,
    HITLTextAnswer,
)
from common.utils.logger import get_logger
from context_memory.translators import normalize_room_memory
from execution.hitl.exceptions import (
    HITLConflictError,
    HITLDeliveryUncertainError,
    HITLRoomMismatchError,
)
from execution.orchestrator.a2a_runtime.hitl_prompt import prompt_type_for_question
from execution.orchestrator.a2a_runtime.interaction_outcome import (
    emit_hitl_resolved_events,
    public_activity_message_id,
    public_agent_label,
)
from execution.orchestrator.a2a_runtime.ledger import TERMINAL_AGENT_CALL_STATES
from execution.orchestrator.a2a_runtime.models import NormalizedA2AObservation
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
from execution.orchestrator.public_text import sanitize_public_text
from execution.orchestrator.session import (
    DefaultRunFactory,
    RunFactory,
    SessionConflict,
)
from models.request import OrchestrationRequest
from models.response import OrchestrationResponse

logger = get_logger(__name__)

MODE_PROFILE_MAP = {
    "fast": "fast",
    "direct": "fast",
    "ultimate": "ultimate",
    "supervisor": "ultimate",
}

_PROFILE_PINNED_INITIAL_ROUTING = "explicit_agent_first"
_PROFILE_PINNED_FINALIZATION = "pass_through"
_MAX_ROOM_HISTORY_MESSAGES = 10
_MAX_ROOM_HISTORY_CHARS_PER_MESSAGE = 4_000

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
    """The legacy envelope requests something the orchestrator cannot serve yet."""


class OrchestratorRoutingError(RuntimeError):
    """The routing seam is misconfigured or a required binding is missing."""


class OrchestratorHITLNotOwnedError(OrchestratorRoutingError):
    """The interaction is not owned by the orchestrator A2A ingress.

    Supervisor ``ask_user`` interactions materialize through the unified
    Execution HITL service and must fall back to the legacy manager instead
    of the orchestrator call-ledger ingress.
    """


class WebhookAuthenticationError(RuntimeError):
    """Webhook auth failure carrying the HTTP status the legacy route would use."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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


class RoomEnvelopeSource(Protocol):
    async def load_envelope(
        self, request: OrchestrationRequest
    ) -> RoomMessageEnvelope: ...


class WebhookTokenVerifier(Protocol):
    """Legacy ``verify_webhook_token_for_task``-shaped verifier."""

    async def __call__(self, message_id: str, token: str) -> tuple[bool, str]: ...


class RoomMessageEnvelopeResolver:
    """Resolve the orchestrator inputs from the persisted legacy user message.

    ``agent_scope``/``execution_mode`` are written by ``ExecutionFacade`` into
    the user message ``extend_info`` before orchestration is scheduled, so this
    reader reconstructs the mode and candidate scope without importing the
    legacy executor.
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

    async def load_envelope(self, request: OrchestrationRequest) -> RoomMessageEnvelope:
        message_id = request.room_user_message_id
        if not message_id:
            raise UnsupportedEnvelopeError(
                "orchestrator requires a room_user_message_id"
            )
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

        extend_info = getattr(message, "extend_info", None)
        # The live request carries the route-validated mode and scope; they
        # are authoritative for Run creation. The persisted extend_info is the
        # fallback for recovery/re-entry paths without a live request (and for
        # the legacy supervisor preflight's whitelist rewrite).
        live_mode = getattr(request, "mode", None)
        mode = (
            live_mode
            if isinstance(live_mode, str) and live_mode.strip()
            else (
                extend_info.get("execution_mode")
                if isinstance(extend_info, dict)
                else None
            )
        )
        if not isinstance(mode, str) or not mode.strip():
            raise UnsupportedEnvelopeError(
                "orchestrator envelope is missing execution_mode"
            )

        live_scope = getattr(request, "agent_scope", None)
        scope = (
            live_scope
            if isinstance(live_scope, dict)
            else (
                extend_info.get("agent_scope")
                if isinstance(extend_info, dict)
                else None
            )
        )
        if not isinstance(scope, dict):
            # The legacy supervisor preflight whitelists extend_info keys and
            # persists the candidate scope under its own names, so
            # supervisor-mode messages never carry ``agent_scope``.
            # Reconstruct the canonical scope from those fields.
            source = (
                extend_info.get("candidate_scope_source")
                if isinstance(extend_info, dict)
                else None
            )
            agent_ids = (
                extend_info.get("candidate_agent_ids")
                if isinstance(extend_info, dict)
                else None
            )
            if source in {"mention", "saved_group", "all_agents", "room_default"}:
                scope = {"source": source}
                if source == "mention" and isinstance(agent_ids, list):
                    scope["agent_ids"] = [
                        str(agent_id) for agent_id in agent_ids if agent_id
                    ]
                if source == "saved_group":
                    group_id = extend_info.get("candidate_scope_group_id")
                    if isinstance(group_id, str) and group_id.strip():
                        scope["group_id"] = group_id.strip()
        candidate_agent_ids = await self._resolve_candidate_agent_ids(
            request.room_id, scope, user_id=request.user_id
        )
        scope_source = str(scope.get("source") or "explicit_selection")
        group_id = (
            scope.get("group_id") if isinstance(scope.get("group_id"), str) else None
        )

        attachments = _attachments_from_message(content)
        attachment_texts = await self._resolve_attachment_texts(attachments)
        requesting_subject_id = request.user_id
        return RoomMessageEnvelope(
            message_text=message_text,
            mode=mode,
            candidate_agent_ids=candidate_agent_ids,
            attachments=attachments,
            attachment_texts=attachment_texts,
            requesting_subject_id=requesting_subject_id,
            scope_source=scope_source,
            group_id=group_id,
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
        self, room_id: str | None, scope: Any, *, user_id: str | None = None
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
            if room_id is None:
                raise UnsupportedEnvelopeError(
                    "orchestrator room scope requires room_id"
                )
            return await self._list_room_agent_ids(room_id)
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
    room_id: str,
    agent_ids: list[str],
    scope_source: str = "explicit_selection",
    group_id: str | None = None,
    requesting_subject_id: str | None = None,
) -> CandidateScopeSnapshot:
    basis = AuthorizationBasis(
        kind=_SCOPE_AUTHORIZATION_KINDS.get(scope_source, "explicit_selection"),
        room_id=room_id,
        group_id=group_id,
        selected_by_user_id=requesting_subject_id or None,
    )
    return CandidateScopeSnapshot(
        snapshot_id=f"scope-{_sha256_hex(json.dumps([room_id, sorted(agent_ids), scope_source, group_id or '']))}",
        source=scope_source,
        room_id=room_id,
        group_id=group_id,
        agent_ids=list(dict.fromkeys(agent_ids)),
        authorization_basis=basis,
    )


def _build_resource_manifest(
    *,
    source_message_id: str,
    user_text: str | None,
    attachments: list[AttachmentEnvelope] | None,
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
    return RunResourceManifestSnapshot(
        manifest_id=f"manifest-{_sha256_hex(json.dumps([ref.model_dump(mode='json') for ref in refs]))}",
        refs=refs,
        content_digest=_sha256_hex(
            json.dumps([ref.model_dump(mode="json") for ref in refs])
        ),
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _map_legacy_answers(
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
                answer=_answer_for_kind(question.answer_kind, user_input or ""),
            )
        )
    if set(questions) != {answer.question_id for answer in mapped}:
        raise HITLConflictError(
            "The input request changed before submission. Refresh and try again."
        )
    return mapped


def _answer_for_kind(kind: HITLAnswerKind, user_input: str) -> Any:
    if kind == HITLAnswerKind.TEXT:
        return HITLTextAnswer(text=user_input)
    if kind == HITLAnswerKind.SINGLE_CHOICE:
        return HITLSingleChoiceAnswer(choice=user_input)
    if kind == HITLAnswerKind.MULTI_CHOICE:
        choices = [choice.strip() for choice in user_input.split(",") if choice.strip()]
        return HITLMultiChoiceAnswer(choices=choices)
    if kind == HITLAnswerKind.CONFIRMATION:
        normalized = user_input.strip().lower()
        return HITLConfirmationAnswer(
            confirmed=normalized in {"true", "yes", "1", "confirmed", "approve"}
        )
    raise UnsupportedEnvelopeError(
        f"orchestrator cannot serve HITL answer kind {kind.value!r} from legacy text"
    )


def _webhook_event_body(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _first_mapping(*values: Any) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict):
            return value
    return None


def _protocol_message_id_from_mapping(raw: dict[str, Any] | None) -> str | None:
    if raw is None:
        return None
    return _first_str(raw.get("messageId"), raw.get("message_id"))


def _webhook_protocol_message_id(
    payload: dict[str, Any], *, task: Any | None = None
) -> str | None:
    """Prefer a protocol messageId over synthetic parser/task IDs."""
    if task is not None:
        status_message = getattr(getattr(task, "status", None), "message", None)
        message_id = getattr(status_message, "message_id", None)
        if isinstance(message_id, str) and message_id.strip():
            return message_id.strip()

    body = _webhook_event_body(payload)
    status = body.get("status") if isinstance(body.get("status"), dict) else {}
    status_update = _first_mapping(body.get("statusUpdate"), body.get("status_update"))
    status_update_status = (
        status_update.get("status")
        if isinstance(status_update, dict)
        and isinstance(status_update.get("status"), dict)
        else {}
    )
    message = _first_mapping(
        body if body.get("kind") in {"message", "MESSAGE"} else None,
        body.get("message"),
        status.get("message") if isinstance(status, dict) else None,
        status_update_status.get("message")
        if isinstance(status_update_status, dict)
        else None,
    )
    return _protocol_message_id_from_mapping(message)


def _webhook_protocol_task_id(payload: dict[str, Any]) -> str | None:
    body = _webhook_event_body(payload)
    status_update = _first_mapping(body.get("statusUpdate"), body.get("status_update"))
    task = body.get("task") if isinstance(body.get("task"), dict) else None
    return _first_str(
        body.get("taskId"),
        body.get("task_id"),
        body.get("id") if body.get("kind") == "task" else None,
        task.get("id") if isinstance(task, dict) else None,
        status_update.get("taskId") if isinstance(status_update, dict) else None,
        status_update.get("task_id") if isinstance(status_update, dict) else None,
    )


def _webhook_event_cursor(payload: dict[str, Any], *, task: Any | None = None) -> str:
    """Stable per-event discriminator for webhook ingress identity."""
    message_id = _webhook_protocol_message_id(payload, task=task)
    if message_id is not None:
        return f"msg:{message_id}"

    if task is not None:
        status_message = getattr(getattr(task, "status", None), "message", None)
        metadata = getattr(status_message, "metadata", None)
        if isinstance(metadata, dict):
            interaction = metadata.get("hybro.ai/a2a/interaction")
            if isinstance(interaction, dict):
                interaction_id = interaction.get("interaction_id")
                if isinstance(interaction_id, str) and interaction_id.strip():
                    return f"interaction:{interaction_id.strip()}"

    body = _webhook_event_body(payload)
    return f"fp:{_sha256_hex(json.dumps(body, sort_keys=True, default=str))}"


def _deterministic_observed_at(cursor: str) -> datetime:
    """Stable observed_at for webhook event identity / payload digests.

    Wall-clock arrival remains on the inbox ``received_at`` field. Using a
    cursor-derived timestamp keeps identical webhook replays idempotent.
    """
    seconds = int(_sha256_hex(cursor)[:8], 16) % 1_700_000_000
    return datetime.fromtimestamp(seconds, tz=UTC)


def _observation_from_webhook_payload(
    payload: dict[str, Any], call: Any
) -> NormalizedA2AObservation:
    """Normalize orchestrator webhook payloads into durable observations.

    Prefer the shared StreamResponse parser so typed HITL metadata on
    ``status.message`` survives asynchronous ``status-update`` envelopes.
    Fall back to the legacy minimal extractor when the payload is not a
    recognized A2A stream frame.
    """
    from a2a_adapter.orchestrator_direct_client import _task_to_observation_kwargs
    from a2a_adapter.webhook_payloads import parse_stream_response_payload

    message_id = getattr(call, "assistant_message_id", None) or call.call_record_id
    try:
        task = parse_stream_response_payload(payload, message_id)
        # Prefer the ledger-bound task id (and protocol taskId) over parser
        # UUIDs synthesized for kind=message frames so replays stay idempotent.
        task_id = (
            call.a2a_task_id
            or _webhook_protocol_task_id(payload)
            or task.id
            or message_id
        )
        context_id = task.context_id or call.a2a_context_id
        cursor = _webhook_event_cursor(payload, task=task)
        observation = NormalizedA2AObservation(
            **_task_to_observation_kwargs(
                task,
                source_kind="webhook",
                call_record_id=call.call_record_id,
                binding_scope=call.endpoint_scope_digest,
                agent_id=call.agent_id,
                task_id=task_id,
                context_id=context_id,
                cursor=cursor,
            )
        )
        return observation.model_copy(
            update={"observed_at": _deterministic_observed_at(cursor)}
        )
    except (TypeError, ValueError):
        pass

    source = _webhook_event_body(payload)
    task_id, context_id, status, text = _extract_webhook_identity(source)
    event_kind = _event_kind_for_status(status)
    content = [TextPart(text=text)] if text else []
    resolved_task_id = task_id or call.a2a_task_id
    cursor = _webhook_event_cursor(payload)
    return NormalizedA2AObservation(
        observation_id=(
            f"webhook-{call.call_record_id}-{resolved_task_id or ''}"
            f"-{event_kind}-{cursor}"
        ),
        call_record_id=call.call_record_id,
        source_kind="webhook",
        source_identity=(
            f"webhook:{call.endpoint_scope_digest}:{resolved_task_id or ''}:"
            f"{event_kind}:{cursor}"
        ),
        binding_scope=call.endpoint_scope_digest,
        event_kind=event_kind,
        observed_at=_deterministic_observed_at(cursor),
        task_id=resolved_task_id,
        context_id=context_id or call.a2a_context_id,
        agent_id=call.agent_id,
        status=status if event_kind == "terminal" else None,
        content=content,
        artifact_refs=[],
        interaction_spec=None,
        error_code=None,
        error_message=None,
        cursor=cursor,
    )


def _extract_webhook_identity(
    source: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str]:
    if isinstance(source.get("task"), dict):
        task = source["task"]
        status = _status_value(task.get("status"))
        text = _extract_webhook_text(task)
        return (
            _first_str(task.get("id"), task.get("task_id"), task.get("taskId")),
            _first_str(
                task.get("context_id"), task.get("contextId"), task.get("contextId")
            ),
            status,
            text,
        )
    raw = source.get("statusUpdate") or source.get("status_update")
    if isinstance(raw, dict):
        status = _status_value(raw.get("status"))
        text = _extract_webhook_text(raw.get("status"))
        return (
            _first_str(raw.get("task_id"), raw.get("taskId")),
            _first_str(raw.get("context_id"), raw.get("contextId")),
            status,
            text,
        )
    message = source.get("message")
    if isinstance(message, dict):
        return (
            _first_str(message.get("task_id"), message.get("taskId")),
            _first_str(message.get("context_id"), message.get("contextId")),
            "completed",
            _extract_webhook_text(message),
        )
    return None, None, None, ""


def _status_value(status: Any) -> str | None:
    if not isinstance(status, dict):
        return None
    value = status.get("state")
    if not isinstance(value, str):
        return None
    return value


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_webhook_text(value: Any) -> str:  # noqa: C901
    if not isinstance(value, dict):
        return ""
    message = value.get("message")
    if isinstance(message, dict):
        parts = message.get("parts") or message.get("content")
        if isinstance(parts, list):
            texts = []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text)
            if texts:
                return "".join(texts)
        text = message.get("text")
        if isinstance(text, str):
            return text
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            text = _extract_webhook_text(artifact)
            if text:
                return text
    return ""


def _event_kind_for_status(status: str | None) -> str:
    if status in {"input-required", "input_required"}:
        return "input_required"
    if status in {"auth-required", "auth_required"}:
        return "auth_required"
    if status in {"completed", "failed", "canceled", "rejected", "expired"}:
        return "terminal"
    return "working"


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
    runtime; every ingress (message, cancellation, HITL answer, webhook) is
    served by the orchestrator directly.
    """

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        envelope_source: RoomEnvelopeSource | None = None,
        run_factory: RunFactory | None = None,
        webhook_token_verifier: WebhookTokenVerifier | None = None,
        room_memory_reader: RoomMemoryReader | None = None,
    ) -> None:
        self._runtime = runtime
        self._envelope_source = envelope_source
        self._run_factory = run_factory or DefaultRunFactory()
        self._webhook_token_verifier = webhook_token_verifier
        self._room_memory_reader = room_memory_reader

    # -- Ingress routing -------------------------------------------------

    async def record_observation(
        self, observation: NormalizedA2AObservation
    ) -> tuple[str, Any]:
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator ingress is not bound")
        return await self._runtime.observation_ingress.record(observation)

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
                run.run_id, reason=reason, deletion_id=deletion_id
            )
            if any(
                state not in TERMINAL_AGENT_CALL_STATES for state in results.values()
            ):
                return self._pending_cancellation_ack()
            await self._cancel_owned_hitl_for_run(run, reason=reason)
            if post_claim_cleanup is not None:
                await post_claim_cleanup()
            settled = await self._runtime.session_host.reconcile_cancellation(run)
        except Exception:
            logger.warning(
                "orchestrator local cancellation reconciliation remains pending",
                extra={"run_id": run.run_id},
                exc_info=True,
            )
            return self._pending_cancellation_ack()
        if settled.run.status != "canceled":
            return self._pending_cancellation_ack()
        try:
            await self._runtime.session_host.signal_run_cancellation(run, command_id)
        except Exception:
            logger.warning(
                "orchestrator post-cancellation cleanup failed",
                extra={"run_id": run.run_id},
                exc_info=True,
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
        room_id: str,
    ) -> str:
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator HITL ingress is not bound")
        read = await self._runtime.hitl_port.read_interaction(interaction_id)
        if read is None:
            raise OrchestratorHITLNotOwnedError(interaction_id)
        spec, route, _fingerprint = read
        if route.room_id != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        run = await self._runtime.run_store.load(route.orchestration_run_id)
        if run is None or run.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }:
            return "canceled"
        mapped = _map_legacy_answers(spec, answers)
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
            raise HITLDeliveryUncertainError(
                "HITL answer was recorded but continuation delivery is uncertain"
            )
        return state

    async def cancel_hitl_interaction(
        self,
        *,
        room_id: str,
        interaction_id: str,
        expected_version: int,
    ) -> int:
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator HITL ingress is not bound")
        read = await self._runtime.hitl_port.read_interaction(interaction_id)
        if read is None:
            raise KeyError(interaction_id)
        spec, route, _fingerprint = read
        if route.room_id != room_id:
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
            room_id=room_id,
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
        interactions = await hitl_port.get_eligible_interactions(run.room_id)
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
                room_id=run.room_id,
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
            except Exception:
                matching = []
                logger.warning(
                    "HITL wake could not read exact pending observations",
                    extra={"call_record_id": call_record_id},
                    exc_info=True,
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
            except Exception:
                logger.warning(
                    "HITL wake failed to process observation; trying older id",
                    extra={
                        "call_record_id": call_record_id,
                        "observation_id": observation_id,
                    },
                    exc_info=True,
                )
                continue
            break

    async def _emit_hitl_resolved_events(
        self,
        *,
        room_id: str,
        spec: A2AInteractionSpec,
        route: Any,
        status: str,
    ) -> None:
        if self._runtime is None:
            return
        run = await self._runtime.run_store.load(route.orchestration_run_id)
        canonical = getattr(run, "lifecycle_family", None) == "canonical"
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

    async def get_pending_hitl(self, room_id: str) -> list[HITLRequest]:
        if self._runtime is None:
            return []

        interactions = await self._runtime.hitl_port.get_published_interactions(room_id)
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
                        room_id=room_id,
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
                        interaction_id=spec.interaction_id,
                        interaction_status="pending",
                        interaction_version=route.interaction_revision,
                        question_count=len(spec.questions),
                        question_index=index,
                    )
                )
        return requests

    async def _authenticate_webhook(self, message_id: str, token: str) -> None:
        """Authenticate an orchestrator webhook exactly like the legacy route."""
        if self._webhook_token_verifier is None:
            raise OrchestratorRoutingError(
                "orchestrator webhook token verifier is not bound"
            )
        if not token:
            raise WebhookAuthenticationError(401, "Missing authorization token")
        is_valid, error_reason = await self._webhook_token_verifier(message_id, token)
        if not is_valid:
            if error_reason == "task_not_found":
                raise WebhookAuthenticationError(
                    404, "Task not found. The task may not have been created yet."
                )
            if error_reason == "invalid_token":
                raise WebhookAuthenticationError(401, "Invalid token")
            raise WebhookAuthenticationError(500, "Token verification failed")

    async def _resolve_webhook_call(self, message_id: str) -> Any | None:
        """Resolve a webhook by A2A task id first, then by call record id."""
        ledger = self._runtime.call_ledger
        call = await ledger.find_by_task_id(message_id)
        if call is None:
            call = await ledger.load_by_record_id(message_id)
        return call

    async def route_webhook(
        self, *, message_id: str, payload: dict[str, Any], token: str
    ) -> None:
        """Record an authenticated orchestrator-owned webhook.

        Correlation resolves the A2A ``task_id`` alias first and then falls
        back to the orchestrator ``call_record_id``. Webhooks authenticate
        against the call's room-scoped assistant message id through the
        injected token verifier (``verify_webhook_token_for_task``). A webhook
        that does not correlate to an orchestrator call is a hard error; there
        is no legacy executor left to fall back to.
        """
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator webhook ingress is not bound")
        call = await self._resolve_webhook_call(message_id)
        if call is None:
            raise WebhookAuthenticationError(
                404, "Task not found. The task may not have been created yet."
            )
        await self._authenticate_webhook(call.assistant_message_id, token)
        observation = _observation_from_webhook_payload(payload, call)
        await self._runtime.observation_ingress.record(observation)

    # -- Room message adapter ------------------------------------

    async def _resolve_envelope_and_profile(
        self, request: OrchestrationRequest
    ) -> tuple[RoomMessageEnvelope, Any]:
        """Resolve and validate the envelope without any orchestrator side effect."""
        if self._runtime is None:
            raise OrchestratorRoutingError("orchestrator message adapter is not bound")
        if self._envelope_source is None:
            raise UnsupportedEnvelopeError(
                "orchestrator message adapter is not bound to a room envelope source"
            )
        room_id = request.room_id
        if not room_id:
            raise UnsupportedEnvelopeError("orchestrator requires room_id")

        envelope = await self._envelope_source.load_envelope(request)
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

    async def preflight_room_user_message(self, request: OrchestrationRequest) -> None:
        """Validate servability before Run assignment side effects.

        Resolves the persisted envelope, profile, and candidate scope without
        creating a session, epoch, or Run. ``UnsupportedEnvelopeError`` means
        the legacy engine must serve the message.
        """
        await self._resolve_envelope_and_profile(request)

    async def process_room_user_message(
        self, request: OrchestrationRequest
    ) -> OrchestrationResponse:
        envelope, profile = await self._resolve_envelope_and_profile(request)
        room_id = request.room_id

        requesting_subject_id = envelope.requesting_subject_id or request.user_id or ""
        candidate_scope = _build_candidate_scope(
            room_id=room_id,
            agent_ids=envelope.candidate_agent_ids,
            scope_source=envelope.scope_source,
            group_id=envelope.group_id,
            requesting_subject_id=requesting_subject_id,
        )
        if not candidate_scope.agent_ids:
            # An empty scope cannot produce a meaningful kernel run; keep the
            # legacy executor's empty-scope behavior until the seam grows a
            # zero-candidate synthesis path.
            raise UnsupportedEnvelopeError(
                "orchestrator candidate scope resolved to zero agents"
            )
        resource_manifest = _build_resource_manifest(
            source_message_id=request.room_user_message_id or "",
            user_text=envelope.message_text,
            attachments=envelope.attachments,
        )

        session_host = self._runtime.session_host
        session = session_host.get_session(room_id)
        if session is not None:
            if await session.has_active_run():
                raise SessionConflict("a Run is already active for this Room")
            # A session pins ONE Run id and ONE frozen catalog, so an idle
            # (terminal) session is replaced by a freshly prepared one for
            # every new message instead of replaying the stale Run id.
            session_host.drop_session(room_id)
        epoch = await self._runtime.epoch_store.read_active(room_id)
        if epoch is None:
            raise UnsupportedEnvelopeError("Room epoch is not active")
        run_id = f"run-{uuid4().hex}"
        prepared = await self._runtime.catalog_assembler.prepare(
            run_id=run_id,
            room_id=room_id,
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
        conversation_history = await self._load_conversation_history(
            room_id,
            current_message_id=request.room_user_message_id or "",
        )
        await session_host.create_session(
            room_id=room_id,
            profile=profile,
            candidate_scope=candidate_scope,
            requesting_subject_id=requesting_subject_id,
            frozen_catalog=prepared.snapshot,
            resource_manifest=resource_manifest,
            conversation_history=conversation_history,
            run_factory=_PreparedRunFactory(run_id, self._run_factory),
        )

        message = UserMessage(
            message_id=request.room_user_message_id or f"user-{uuid4().hex}",
            content=[
                TextPart(text=envelope.message_text),
                *[TextPart(text=block) for block in envelope.attachment_texts],
            ],
            created_at=datetime.now(UTC),
        )
        result = await session_host.prompt(
            room_id,
            message,
            client_request_id=request.client_request_id,
        )
        return OrchestrationResponse(
            task_id=result.run.run_id if getattr(result, "run", None) else None,
            room_id=room_id,
            success=True,
            status_code=200,
        )

    async def _load_conversation_history(
        self,
        room_id: str,
        *,
        current_message_id: str,
    ) -> tuple[ModelMessage, ...]:
        if self._room_memory_reader is None:
            return ()
        document = await self._room_memory_reader(room_id)
        if not document:
            return ()
        state = normalize_room_memory(document)
        current_turn_id = f"message:{current_message_id}"
        messages: list[ModelMessage] = []
        for turn in state.conversation_history:
            if turn.turn_id == current_turn_id:
                continue
            content = turn.content or turn.brief_summary
            if not isinstance(content, str) or not content.strip():
                continue
            content = content.strip()[:_MAX_ROOM_HISTORY_CHARS_PER_MESSAGE]
            if turn.role == "user":
                messages.append(
                    ModelMessage(
                        role="user",
                        content=[ModelTextPart(text=content)],
                    )
                )
                continue
            if turn.role == "supervisor" or (
                turn.role == "agent" and turn.agent_id == "system:hybro"
            ):
                messages.append(
                    ModelMessage(
                        role="assistant",
                        content=[ModelTextPart(text=content)],
                    )
                )
        messages = messages[-_MAX_ROOM_HISTORY_MESSAGES:]
        while messages and messages[0].role != "user":
            messages.pop(0)
        return tuple(messages)


__all__ = [
    "AttachmentEnvelope",
    "DualRuntimeRouter",
    "MODE_PROFILE_MAP",
    "OrchestratorRoutingError",
    "RoomEnvelopeSource",
    "RoomMessageEnvelope",
    "RoomMessageEnvelopeResolver",
    "UnsupportedEnvelopeError",
    "WebhookAuthenticationError",
    "WebhookTokenVerifier",
    "map_mode_to_profile",
]
