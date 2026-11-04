"""SDK-confined direct A2A client for the orchestrator dispatch boundary.

This module is the only place where the orchestrator's provider-neutral
``DirectA2AClient`` contract (see ``execution/orchestrator/a2a_runtime/dispatch.py``)
is mapped onto the ``a2a`` SDK through ``a2a_adapter.client_facade``.

Architecture note
-----------------
``a2a_adapter`` is pinned by ``test_a2a_adapter_does_not_import_orchestrator_policy``
and ``test_a2a_adapter_import_boundary``: it must not import
``execution.orchestrator`` / ``execution.orchestration`` and may only import from
``a2a``, ``aiohttp``, ``common``, ``dal``, ``httpx`` and ``httpx_sse`` (plus the
standard library). The command DTOs and result DTOs therefore cross this boundary
by *structural typing*: the orchestrator's ``A2ADispatchCommand``,
``A2AContinuationCommand`` and ``A2ACancellationCommand`` satisfy the local
protocols below without this module importing them. The reverse direction (the
adapter producing ``A2ADispatchReceipt`` / ``NormalizedA2AObservation``) is solved
with injected factories: the composition root passes the actual Pydantic model
classes, and this module stays provider-neutral.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import uuid4

from common.a2a_constants import (
    HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE,
    HYBRO_A2A_ERROR_METADATA_KEY,
    HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY,
    HYBRO_A2A_INTERACTION_METADATA_KEY,
    HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE,
    HYBRO_A2A_PART_PROVENANCE_METADATA_KEY,
    HYBRO_A2A_SELECTED_SKILL_METADATA_KEY,
    normalize_task_state_value,
)
from common.dto.hitl import A2AInteractionSpec
from common.observability import events as E
from common.observability import get_logger
from common.types import Message, Task
from common.utils.a2a_artifacts import (
    canonical_data_part_bytes,
    data_part_digest,
    inline_data_artifact_identity,
)
from common.utils.a2a_helpers import extract_parts, get_text_from_message
from common.utils.time import utcnow

from .card_data import agent_card_dict, build_agent_card
from .client_facade import A2AClientFacadeError
from .message_factory import to_sdk_message
from .translators import facade_result_to_model, message_to_completed_task
from .webhook_payloads import (
    parse_push_notification_payload,
    parse_stream_response_payload,
)

logger = get_logger(__name__)
_MAX_INLINE_DATA_ARTIFACTS = 20
_MAX_FREE_TEXT_PROMPT_CHARS = 10_000
# Identity of the interaction Hybro derives from a plain A2A input-required.
FREE_TEXT_INTERACTION_PREFIX = "a2a-input-required:"
FREE_TEXT_QUESTION_ID = "agent_question"
_AGENT_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_MAX_AGENT_ERROR_MESSAGE_CHARS = 500

# ---------------------------------------------------------------------------
# Provider-neutral command mirrors (structural, never imported from the
# orchestrator package).
# ---------------------------------------------------------------------------


class _DispatchCommand(Protocol):
    command_id: str
    call_record_id: str
    invocation_id: str
    message_id: str
    binding_id: str
    agent_id: str
    skill_id: str | None
    endpoint_scope: str
    transport_kind: str
    direct_mode: str | None
    task: str
    materialized_resources: list[Any]
    conversation_id: str
    room_epoch: int
    deadline_at: Any
    # url plus a SecretStr token, or None when push is not configured.
    push_notification: Any


class _ContinuationCommand(Protocol):
    command_id: str
    transport_kind: str
    call_record_id: str
    interaction_id: str
    interaction_revision: int
    answer_digest: str
    answers: list[Any]
    binding_id: str
    binding_digest: str
    requesting_subject_digest: str
    task_id: str
    context_id: str
    conversation_id: str
    room_epoch: int
    created_at: Any


class _ModelReplyCommand(Protocol):
    command_id: str
    transport_kind: str
    call_record_id: str
    binding_id: str
    binding_digest: str
    requesting_subject_digest: str
    task_id: str
    context_id: str
    conversation_id: str
    room_epoch: int
    message_text: str
    interaction_fingerprint: str | None
    created_at: Any


class _CancellationCommand(Protocol):
    command_id: str
    transport_kind: str
    call_record_id: str
    reason: str
    deletion_id: str | None
    created_at: Any


# ---------------------------------------------------------------------------
# Injected SDK-facade functions and provider-neutral factories.
# ---------------------------------------------------------------------------


class SendMessageFn(Protocol):
    async def __call__(
        self,
        agent_card_data: Any,
        message_data: Any,
        *,
        accepted_output_modes: list[str] | None = None,
        push_notification_config: dict[str, Any] | None = None,
        blocking: bool = True,
        timeout: float = 600.0,
    ) -> dict[str, Any]: ...


class StreamMessageFn(Protocol):
    def __call__(
        self,
        agent_card_data: Any,
        message_data: Any,
        *,
        accepted_output_modes: list[str] | None = None,
        push_notification_config: dict[str, Any] | None = None,
        timeout: float = 600.0,
    ) -> AsyncIterator[dict[str, Any]]: ...


class CancelRemoteTaskFn(Protocol):
    async def __call__(
        self,
        agent_card_data: Any,
        task_id: str,
        *,
        timeout: float = 5.0,
    ) -> bool: ...


class FetchRemoteTaskFn(Protocol):
    async def __call__(
        self,
        agent_card_data: Any,
        task_id: str,
        *,
        timeout: float = 30.0,
    ) -> Task | None: ...


class FetchAgentCardFn(Protocol):
    async def __call__(
        self, agent_url: str, *, timeout: float = 30.0
    ) -> dict[str, Any]: ...


class ExceptionFactory(Protocol):
    def __call__(self, message: str) -> BaseException: ...


# ``CallResolver`` is the production seam for recovery/restart. A composition
# may inject a durable lookup (for example reading the call ledger) so that
# ``inspect`` / ``cancel`` / ``continue_task`` can recover authoritative
# ``task_id`` / ``context_id`` / ``endpoint_scope`` even when this process-local
# adapter has no in-memory dispatch history.
class CallResolver(Protocol):
    async def __call__(self, call_record_id: str) -> Mapping[str, Any] | None: ...


ReceiptFactory = Callable[..., Any]
ObservationFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class DirectCallAddress:
    call_record_id: str
    task_id: str | None = None
    context_id: str | None = None
    endpoint_scope: str | None = None
    agent_id: str | None = None


# ---------------------------------------------------------------------------
# Observation normalization (SDK-free Task -> provider-neutral kwargs).
# ---------------------------------------------------------------------------


def endpoint_scope_digest(endpoint_scope: str) -> str:
    """Stable binding scope shared with the candidate/binding adapters."""
    return sha256(endpoint_scope.encode()).hexdigest()


def _push_notification_config(
    command: _DispatchCommand, card: dict[str, Any]
) -> dict[str, Any] | None:
    """Push config for the send, only when the live card advertises push."""
    registration = command.push_notification
    if registration is None:
        return None
    if not build_agent_card(card).capabilities.push_notifications:
        return None
    return {"url": registration.url, "token": registration.token.get_secret_value()}


def _task_state_value(task: Any) -> str:
    return normalize_task_state_value(task.status.state) or "working"


def _terminal_status(task: Any) -> str | None:
    state = _task_state_value(task)
    return (
        state
        if state in {"completed", "failed", "canceled", "rejected", "expired"}
        else None
    )


def _event_kind(task: Any) -> str:
    state = _task_state_value(task)
    if state in {"input-required", "auth-required"}:
        return "input_required" if state == "input-required" else "auth_required"
    if _terminal_status(task) is not None:
        return "terminal"
    return "working"


def _extract_interaction_spec(task: Any, *, event_kind: str) -> dict[str, Any] | None:
    message = getattr(getattr(task, "status", None), "message", None)
    metadata = getattr(message, "metadata", None)
    raw = (
        metadata.get(HYBRO_A2A_INTERACTION_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if raw is None:
        if event_kind == "input_required":
            return _free_text_interaction_spec(task, message)
        return None
    try:
        spec = A2AInteractionSpec.model_validate(raw)
    except (TypeError, ValueError):
        return None
    return spec.model_dump(mode="json")


def _free_text_interaction_spec(task: Any, message: Any) -> dict[str, Any] | None:
    """Treat a plain A2A ``input-required`` question as one free-text question.

    Agents that do not speak Hybro's interaction metadata still ask the user
    through standard A2A. The status message text becomes the prompt; the
    answer goes back to the same task as plain text. Without text there is
    nothing to ask, so the observation keeps no spec.
    """
    prompt = get_text_from_message(message).strip()[:_MAX_FREE_TEXT_PROMPT_CHARS]
    if not prompt:
        return None
    # The A2A message id distinguishes repeated rounds that ask the same text.
    round_key = getattr(message, "message_id", None) or prompt
    digest = sha256(f"{getattr(task, 'id', '')}\n{round_key}".encode()).hexdigest()[:32]
    return {
        "schema_version": 1,
        "interaction_id": f"{FREE_TEXT_INTERACTION_PREFIX}{digest}",
        "questions": [
            {
                "question_id": FREE_TEXT_QUESTION_ID,
                "interaction_kind": "questionnaire",
                "prompt": prompt,
                "answer_kind": "text",
                "required": True,
            }
        ],
    }


def _agent_error(task: Any) -> tuple[str | None, str | None]:
    """Read the Agent's own failure code from the documented metadata key.

    Untrusted remote data: only a short identifier code and bounded message
    text are kept; anything else is ignored.
    """
    message = getattr(getattr(task, "status", None), "message", None)
    metadata = getattr(message, "metadata", None)
    raw = (
        metadata.get(HYBRO_A2A_ERROR_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(raw, Mapping):
        return None, None
    code = raw.get("code")
    if not isinstance(code, str) or not _AGENT_ERROR_CODE.fullmatch(code):
        return None, None
    text = raw.get("message")
    error_message = (
        text.strip()[:_MAX_AGENT_ERROR_MESSAGE_CHARS]
        if isinstance(text, str) and text.strip()
        else None
    )
    return code, error_message


def _is_free_text_interaction(interaction_id: str | None) -> bool:
    return bool(interaction_id) and str(interaction_id).startswith(
        FREE_TEXT_INTERACTION_PREFIX
    )


def _artifact_value(artifact: Any, field: str) -> Any:
    if isinstance(artifact, Mapping):
        return artifact.get(field)
    return getattr(artifact, field, None)


def _task_to_observation_kwargs(  # noqa: C901
    task: Any,
    *,
    source_kind: str,
    call_record_id: str,
    binding_scope: str,
    agent_id: str | None,
    task_id: str,
    context_id: str | None,
    observation_id: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    event_kind = _event_kind(task)
    status = _terminal_status(task)
    interaction_spec = (
        _extract_interaction_spec(task, event_kind=event_kind)
        if event_kind in {"input_required", "auth_required"}
        else None
    )
    interaction_digest = (
        sha256(
            json.dumps(
                interaction_spec,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if interaction_spec is not None
        else None
    )
    identity_kind = (
        f"{event_kind}-{interaction_digest}" if interaction_digest else event_kind
    )
    stable_id = (
        observation_id or f"{source_kind}-{call_record_id}-{task_id}-{identity_kind}"
    )
    if cursor:
        stable_id = f"{stable_id}-{cursor}"

    content: list[dict[str, Any]] = []
    inline_artifacts: list[dict[str, Any]] = []
    artifact_refs: list[str] = []
    artifact_text_parts: list[str] = []
    generated_file_descriptions: list[str] = []

    text = get_text_from_message(getattr(task.status, "message", None))
    # The observation already durably owns structured A2A output. Give each
    # DataPart a deterministic private Ref so later Agent calls can select and
    # rematerialize it without model-authored JSON copying.
    for artifact_index, artifact in enumerate(list(task.artifacts or [])):
        parts = _artifact_value(artifact, "parts") or []
        extracted = extract_parts(list(parts))
        artifact_text_parts.extend(extracted.text_parts)
        artifact_id = _artifact_value(artifact, "artifact_id")
        artifact_name = _artifact_value(artifact, "name")
        artifact_description = _artifact_value(artifact, "description")
        for part_index, data_part in enumerate(extracted.data_parts):
            data = data_part.get("data", {})
            raw_metadata = data_part.get("metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else None
            mime_type = str(
                data_part.get("mime_type")
                or (metadata or {}).get("mime_type")
                or "application/json"
            )
            content_index = len(content)
            content.append(
                {
                    "kind": "data",
                    "data": data,
                    "mime_type": mime_type,
                    "metadata": metadata,
                }
            )
            if len(inline_artifacts) >= _MAX_INLINE_DATA_ARTIFACTS:
                continue
            canonical = canonical_data_part_bytes(
                data, mime_type=mime_type, metadata=metadata
            )
            digest = data_part_digest(canonical)
            ref_id = inline_data_artifact_identity(
                observation_id=stable_id,
                artifact_id=(str(artifact_id) if artifact_id else None),
                artifact_index=artifact_index,
                part_index=part_index,
                content_digest=digest,
            )
            inline_artifacts.append(
                {
                    "ref_id": ref_id,
                    "artifact_id": str(artifact_id) if artifact_id else None,
                    "artifact_name": str(artifact_name) if artifact_name else None,
                    "artifact_description": (
                        str(artifact_description) if artifact_description else None
                    ),
                    "artifact_index": artifact_index,
                    "part_index": part_index,
                    "content_index": content_index,
                    "mime_type": mime_type,
                    "size_bytes": len(canonical),
                    "content_digest": digest,
                }
            )
            artifact_refs.append(ref_id)
        for file_part in extracted.file_parts:
            file = file_part.get("file") if isinstance(file_part, Mapping) else None
            uri = file.get("uri") if isinstance(file, Mapping) else None
            if isinstance(uri, str) and uri:
                artifact_refs.append(uri)
            name = (file.get("name") if isinstance(file, Mapping) else None) or "file"
            mime_type = (
                file.get("mime_type") or file.get("mimeType")
                if isinstance(file, Mapping)
                else None
            ) or "binary"
            generated_file_descriptions.append(f"{name} ({mime_type})")
        uri = _artifact_value(artifact, "uri")
        if isinstance(uri, str) and uri and not parts:
            artifact_refs.append(uri)

    if artifact_text_parts:
        artifact_text = "".join(artifact_text_parts)
        # Agents often repeat the result in the final status message; keep it once.
        if text and text.strip() != artifact_text.strip():
            text = f"{text}\n{artifact_text}".strip()
        else:
            text = artifact_text
    if not text and generated_file_descriptions:
        text = f"[Generated file: {', '.join(generated_file_descriptions)}]"
    if text:
        content.append({"kind": "text", "text": text})
    error_code, error_message = (
        _agent_error(task) if status in {"failed", "rejected"} else (None, None)
    )

    return {
        "observation_id": stable_id,
        "call_record_id": call_record_id,
        "source_kind": source_kind,
        "source_identity": f"{source_kind}:{binding_scope}:{task_id}:{identity_kind}:{cursor or ''}",
        "binding_scope": binding_scope,
        "event_kind": event_kind,
        "observed_at": utcnow(),
        "task_id": task_id,
        "context_id": context_id,
        "agent_id": agent_id,
        "status": status,
        "content": content,
        "inline_artifacts": inline_artifacts,
        "artifact_refs": list(dict.fromkeys(artifact_refs)),
        "interaction_spec": interaction_spec,
        "error_code": error_code,
        "error_message": error_message,
        "cursor": cursor,
    }


_RETRYABLE_AGENT_CARD_STATUS_CODES = frozenset({408, 425, 429})
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ClientConnectorError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionResetError",
        "NetworkError",
        "PoolTimeout",
        "ProxyError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "ServerDisconnectedError",
        "WriteError",
        "WriteTimeout",
        "gaierror",
    }
)
_NETWORK_ERROR_MARKERS = (
    "all connection attempts failed",
    "connection refused",
    "connection reset",
    "connection timed out",
    "network is unreachable",
    "name or service not known",
    "server disconnected",
    "temporary failure in name resolution",
    "timed out",
)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _exception_status_code(exc: BaseException) -> int | None:
    for current in _exception_chain(exc):
        value = getattr(current, "status_code", None)
        if type(value) is int:
            return value
        response = getattr(current, "response", None)
        value = getattr(response, "status_code", None)
        if type(value) is int:
            return value
    return None


def _is_retryable_agent_card_fetch_error(exc: BaseException) -> bool:
    status_code = _exception_status_code(exc)
    if status_code is not None:
        return status_code in _RETRYABLE_AGENT_CARD_STATUS_CODES or status_code >= 500
    for current in _exception_chain(exc):
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if current.__class__.__name__ in _NETWORK_EXCEPTION_NAMES:
            return True
        message = str(current).lower()
        if any(marker in message for marker in _NETWORK_ERROR_MARKERS):
            return True
    return False


_RECOVERABLE_MATERIALIZATION_ERROR_NAMES = frozenset(
    {
        "RecoverableAdapterError",
        "RecoverableEpochError",
        "RecoverableResourceError",
        "StaleRoomEpochError",
        "AmbiguousRemoteEffectError",
    }
)


def _is_recoverable_materialization_error(exc: BaseException) -> bool:
    """True when storage/epoch failures must stay non-terminal for recovery.

    ``a2a_adapter`` cannot import orchestrator error types, so recoverable
    subclasses are matched by class name across the module boundary.
    """
    if isinstance(exc, TimeoutError):
        return True
    return exc.__class__.__name__ in _RECOVERABLE_MATERIALIZATION_ERROR_NAMES


def _failed_materialization_observation_kwargs(
    *,
    source_kind: str,
    call_record_id: str,
    binding_scope: str,
    agent_id: str | None,
    task_id: str,
    context_id: str | None,
    error_message: str,
    cursor: str | None = None,
) -> dict[str, Any]:
    stable_id = f"{source_kind}-{call_record_id}-{task_id}-materialization-failed"
    if cursor:
        stable_id = f"{stable_id}-{cursor}"
    return {
        "observation_id": stable_id,
        "call_record_id": call_record_id,
        "source_kind": source_kind,
        "source_identity": f"{source_kind}:{binding_scope}:{task_id}:terminal:{cursor or ''}",
        "binding_scope": binding_scope,
        "event_kind": "terminal",
        "observed_at": utcnow(),
        "task_id": task_id,
        "context_id": context_id,
        "agent_id": agent_id,
        "status": "failed",
        "content": [{"kind": "text", "text": error_message}],
        "artifact_refs": [],
        "interaction_spec": None,
        "error_code": "artifact_materialization_failed",
        "error_message": error_message,
        "cursor": cursor,
    }


def _response_to_task(
    response: dict[str, Any], *, command_message_id: str
) -> Task | None:
    if not response or response.get("kind") == "error":
        return None
    try:
        model = facade_result_to_model(response)
    except ValueError:
        return None
    if isinstance(model, Message):
        return message_to_completed_task(
            model,
            context_id=model.context_id or command_message_id,
            task_id=str(uuid4()),
            artifact_id=str(uuid4()),
        )
    if isinstance(model, Task):
        return model
    return None


async def _materialize_task_artifacts_epoch_fenced(  # noqa: C901
    task: Any,
    *,
    epoch_owner: Any | None,
    conversation_id: str | None,
    room_epoch: int | None,
    call_record_id: str | None,
    message_id: str | None,
) -> None:
    """Commit inline FileWithBytes under the orchestrator's epoch write-lease fence."""
    if not getattr(task, "artifacts", None):
        return
    for artifact_position, artifact in enumerate(task.artifacts or []):
        artifact_id = (
            getattr(artifact, "artifact_id", None)
            or getattr(artifact, "id", None)
            or f"art-{artifact_position}"
        )
        parts = getattr(artifact, "parts", None) or []
        for part_slot, part in enumerate(parts):
            root = getattr(part, "root", part)
            if getattr(root, "kind", None) != "file":
                continue
            file_content = getattr(root, "file", None)
            if file_content is None:
                continue
            encoded = getattr(file_content, "bytes", None)
            if not isinstance(encoded, str) or not encoded:
                continue
            if epoch_owner is None or not conversation_id or room_epoch is None:
                raise RuntimeError(
                    "epoch_owner, conversation_id, and active room_epoch are required to persist inline agent file artifacts"
                )
            if len(encoded) > (50 * 1024 * 1024 * 4 // 3) + 4:
                raise ValueError("encoded payload exceeds max 50 MiB base64 length")
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError(
                    f"invalid base64 encoding in inline agent file artifact {getattr(file_content, 'name', None)!r}: {exc}"
                ) from exc
            if len(data) > 50 * 1024 * 1024:
                raise ValueError("payload exceeds 50 MiB limit")

            content_sha256 = sha256(data).hexdigest()
            file_name = (
                getattr(file_content, "name", None)
                or f"artifact-{artifact_position}-{part_slot}"
            )
            mime_type = (
                getattr(file_content, "mime_type", None)
                or getattr(file_content, "mimeType", None)
                or "application/octet-stream"
            )
            # Origin key uses "orchestrator-v3-a2a-inline" to distinguish direct inline
            # materializations from remote fetch origin keys ("orchestrator-v3-a2a").
            origin_key = sha256(
                "|".join(
                    (
                        "orchestrator-v3-a2a-inline",
                        str(call_record_id or "direct"),
                        str(artifact_id),
                        str(part_slot),
                        content_sha256,
                    )
                ).encode()
            ).hexdigest()
            content_url = await epoch_owner.commit(
                conversation_id=conversation_id,
                room_epoch=room_epoch,
                source_message_id=message_id or "direct",
                origin_key=origin_key,
                content=data,
                content_sha256=content_sha256,
                file_name=file_name,
                mime_type=mime_type,
                max_bytes=50 * 1024 * 1024,
            )
            file_content.uri = content_url
            file_content.bytes = None
            file_content.mime_type = mime_type
            file_content.name = file_name
            metadata = getattr(root, "metadata", None)
            if metadata is None or not isinstance(metadata, dict):
                metadata = {}
            metadata.update(
                {
                    "file_id": (
                        content_url.rsplit("/", 2)[-2]
                        if "/" in content_url
                        else content_url
                    ),
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "size_bytes": len(data),
                    "sha256": content_sha256,
                }
            )
            root.metadata = metadata


# ---------------------------------------------------------------------------
# Direct stream wrapper.
# ---------------------------------------------------------------------------


class DirectA2AStream:
    """Async iterator over normalized observations from one SDK stream."""

    def __init__(
        self,
        *,
        command: _DispatchCommand,
        stream: AsyncIterator[dict[str, Any]],
        observation_factory: ObservationFactory,
        binding_scope: str,
        epoch_owner: Any | None = None,
        on_frame_identity: Callable[[str | None, str | None], None] | None = None,
    ) -> None:
        self._command = command
        self._stream = stream
        self._observation_factory = observation_factory
        self._binding_scope = binding_scope
        self._epoch_owner = epoch_owner
        self._on_frame_identity = on_frame_identity
        self._closed = False
        self._frame_counter = 0
        self._last_task_id: str | None = None
        self._last_context_id: str | None = None
        self._accumulated_artifact_refs: list[str] = []

    def __aiter__(self) -> DirectA2AStream:
        return self

    async def __anext__(self) -> Any:
        if self._closed:
            raise StopAsyncIteration
        raw = await self._stream.__anext__()
        try:
            task = parse_stream_response_payload(raw, self._command.message_id)
        except ValueError:
            # Ignore malformed stream frames; the caller's deadline still bounds
            # the stream and recovery/inspection can reconcile missing evidence.
            return await self.__anext__()
        internal_task = task
        try:
            await _materialize_task_artifacts_epoch_fenced(
                internal_task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(self._command, "conversation_id", None),
                room_epoch=getattr(self._command, "room_epoch", None),
                call_record_id=getattr(self._command, "call_record_id", None),
                message_id=getattr(self._command, "message_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="stream_frame",
                call_record_id=self._command.call_record_id,
                task_id=internal_task.id or self._last_task_id,
            )
            self._frame_counter += 1
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="direct",
                call_record_id=self._command.call_record_id,
                binding_scope=self._binding_scope,
                agent_id=self._command.agent_id,
                task_id=internal_task.id or self._last_task_id or "",
                context_id=internal_task.context_id or self._last_context_id,
                error_message=f"Failed to materialize stream file artifact: {exc}",
                cursor=str(self._frame_counter),
            )
            return self._observation_factory(**kwargs)
        task_id = internal_task.id or self._last_task_id or ""
        context_id = internal_task.context_id or self._last_context_id
        self._last_task_id = task_id
        self._last_context_id = context_id
        if self._on_frame_identity is not None:
            self._on_frame_identity(task_id or None, context_id or None)
        self._frame_counter += 1
        cursor = (
            None
            if _terminal_status(internal_task) is not None
            else str(self._frame_counter)
        )
        kwargs = _task_to_observation_kwargs(
            internal_task,
            source_kind="direct",
            call_record_id=self._command.call_record_id,
            binding_scope=self._binding_scope,
            agent_id=self._command.agent_id,
            task_id=task_id,
            context_id=context_id,
            cursor=cursor,
        )

        current_refs = kwargs.get("artifact_refs", [])
        self._accumulated_artifact_refs.extend(current_refs)
        kwargs["artifact_refs"] = list(dict.fromkeys(self._accumulated_artifact_refs))

        return self._observation_factory(**kwargs)

    async def close(self, *, reason: str) -> None:
        del reason
        if self._closed:
            return
        self._closed = True
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


# ---------------------------------------------------------------------------
# The direct client adapter.
# ---------------------------------------------------------------------------


class OrchestratorDirectA2AClient:
    """Stateful SDK adapter satisfying the provider-neutral ``DirectA2AClient``."""

    def __init__(
        self,
        *,
        send_message: SendMessageFn,
        stream_message: StreamMessageFn,
        cancel_remote_task: CancelRemoteTaskFn,
        fetch_remote_task: FetchRemoteTaskFn,
        fetch_agent_card: FetchAgentCardFn,
        receipt_factory: ReceiptFactory,
        observation_factory: ObservationFactory,
        recoverable_transport_error_factory: ExceptionFactory,
        agent_card_contract_error_factory: ExceptionFactory,
        epoch_owner: Any | None = None,
        call_resolver: CallResolver | None = None,
        timeout: float = 600.0,
        poll_timeout: float = 30.0,
        cancel_timeout: float = 5.0,
        accepted_output_modes: Sequence[str] | None = None,
    ) -> None:
        self._send_message = send_message
        self._stream_message = stream_message
        self._cancel_remote_task = cancel_remote_task
        self._fetch_remote_task = fetch_remote_task
        self._fetch_agent_card = fetch_agent_card
        self._receipt = receipt_factory
        self._observation = observation_factory
        self._recoverable_transport_error = recoverable_transport_error_factory
        self._agent_card_contract_error = agent_card_contract_error_factory
        self._epoch_owner = epoch_owner
        self._call_resolver = call_resolver
        self._timeout = timeout
        self._poll_timeout = poll_timeout
        self._cancel_timeout = cancel_timeout
        self._accepted_output_modes = list(accepted_output_modes or ["text/plain"])
        self._addresses: dict[str, DirectCallAddress] = {}

    # -- address registry ---------------------------------------------------

    def _remember(
        self, command: Any, *, task_id: str | None, context_id: str | None
    ) -> None:
        existing = self._addresses.get(command.call_record_id)
        self._addresses[command.call_record_id] = DirectCallAddress(
            call_record_id=command.call_record_id,
            task_id=task_id or (existing.task_id if existing is not None else None),
            context_id=context_id
            or (existing.context_id if existing is not None else None),
            endpoint_scope=getattr(command, "endpoint_scope", None)
            or (existing.endpoint_scope if existing is not None else None),
            agent_id=getattr(command, "agent_id", None)
            or (existing.agent_id if existing is not None else None),
        )

    async def _resolve_call(self, command: Any) -> DirectCallAddress:
        call_record_id = command.call_record_id
        resolved = self._addresses.get(call_record_id)
        needs_resolver = resolved is None or not resolved.endpoint_scope
        if needs_resolver and self._call_resolver is not None:
            raw = await self._call_resolver(call_record_id)
            if raw is not None:
                resolved = DirectCallAddress(
                    call_record_id=call_record_id,
                    task_id=raw.get("task_id")
                    or (resolved.task_id if resolved is not None else None),
                    context_id=raw.get("context_id")
                    or (resolved.context_id if resolved is not None else None),
                    endpoint_scope=raw.get("endpoint_scope")
                    or (resolved.endpoint_scope if resolved is not None else None),
                    agent_id=raw.get("agent_id")
                    or (resolved.agent_id if resolved is not None else None),
                )
                self._addresses[call_record_id] = resolved
        if resolved is None:
            resolved = DirectCallAddress(call_record_id=call_record_id)
        # Command-local fields always win when present (they are authoritative
        # for the command being executed).
        command_task_id = getattr(command, "task_id", None)
        command_context_id = getattr(command, "context_id", None)
        command_endpoint_scope = getattr(command, "endpoint_scope", None)
        command_agent_id = getattr(command, "agent_id", None)
        return DirectCallAddress(
            call_record_id=call_record_id,
            task_id=command_task_id or resolved.task_id,
            context_id=command_context_id or resolved.context_id,
            endpoint_scope=command_endpoint_scope or resolved.endpoint_scope,
            agent_id=command_agent_id or resolved.agent_id,
        )

    async def _card(self, endpoint_scope: str) -> dict[str, Any]:
        fetch_error: Literal["transport", "contract"] | None = None
        try:
            raw_card = await self._fetch_agent_card(endpoint_scope)
        except A2AClientFacadeError as exc:
            fetch_error = (
                "transport" if _is_retryable_agent_card_fetch_error(exc) else "contract"
            )
        except Exception as exc:
            fetch_error = (
                "transport" if _is_retryable_agent_card_fetch_error(exc) else "contract"
            )
        if fetch_error == "transport":
            raise self._recoverable_transport_error(
                "Agent Card is temporarily unavailable."
            ) from None
        if fetch_error == "contract":
            raise self._agent_card_contract_error(
                "Agent Card could not be resolved."
            ) from None

        invalid_card = False
        try:
            normalized = agent_card_dict(raw_card)
            build_agent_card(normalized)
        except Exception:
            invalid_card = True
        if invalid_card:
            raise self._agent_card_contract_error("Agent Card is invalid.") from None
        return normalized

    # -- message construction ----------------------------------------------

    def _build_user_message(self, command: _DispatchCommand) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {
                "kind": "text",
                "text": command.task,
                "metadata": {
                    HYBRO_A2A_PART_PROVENANCE_METADATA_KEY: {
                        "schema_version": 1,
                        "role": HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE,
                    }
                },
            }
        ]
        for resource in command.materialized_resources:
            kind = getattr(resource, "kind", None)
            payload = getattr(resource, "payload", None)
            mime_type = getattr(resource, "mime_type", None)
            resource_metadata = getattr(resource, "metadata", None)
            if kind == "text":
                metadata = (
                    dict(resource_metadata)
                    if isinstance(resource_metadata, Mapping)
                    else {}
                )
                # Platform-reserved transport metadata is authoritative even
                # if an owner-supplied resource used the same key.
                metadata[HYBRO_A2A_PART_PROVENANCE_METADATA_KEY] = {
                    "schema_version": 1,
                    "role": HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE,
                }
                parts.append(
                    {
                        "kind": "text",
                        "text": str(payload),
                        "metadata": metadata,
                    }
                )
            elif kind == "data":
                metadata = (
                    dict(resource_metadata)
                    if isinstance(resource_metadata, Mapping)
                    else {}
                )
                if mime_type is not None:
                    metadata.setdefault("mime_type", mime_type)
                parts.append(
                    {
                        "kind": "data",
                        "data": payload,
                        "metadata": metadata or None,
                    }
                )
            elif kind == "file":
                if isinstance(payload, Mapping):
                    metadata = (
                        dict(resource_metadata)
                        if isinstance(resource_metadata, Mapping)
                        else {}
                    )
                    parts.append(
                        {
                            "kind": "file",
                            "file": {
                                "name": payload.get("name", "resource"),
                                "mime_type": payload.get("mime_type", mime_type),
                                "bytes": payload.get("bytes"),
                                "uri": payload.get("uri"),
                            },
                            "metadata": metadata or None,
                        }
                    )
        message_metadata: dict[str, Any] = {"agent_id": command.agent_id}
        if command.skill_id is not None:
            message_metadata[HYBRO_A2A_SELECTED_SKILL_METADATA_KEY] = {
                "schema_version": 1,
                "skill_id": command.skill_id,
            }
        return {
            "role": "user",
            "message_id": command.message_id,
            "parts": parts,
            "metadata": message_metadata,
        }

    def _build_continuation_message(
        self, command: _ContinuationCommand, *, address: DirectCallAddress
    ) -> dict[str, Any]:
        free_text = _is_free_text_interaction(command.interaction_id)
        text = _continuation_text(command.answers, bare=free_text)
        if free_text:
            # The Agent asked through plain A2A ``input-required``; it gets a
            # plain A2A reply. Hybro answer metadata would make an Agent that
            # reads it treat the reply as a typed answer to a question it
            # never declared.
            return {
                "role": "user",
                "message_id": command.command_id,
                "task_id": address.task_id,
                "context_id": address.context_id,
                "parts": [{"kind": "text", "text": text}],
                "metadata": {"agent_id": address.agent_id},
            }
        typed_answers = [
            answer.model_dump(mode="json")
            if hasattr(answer, "model_dump")
            else dict(answer)
            if isinstance(answer, Mapping)
            else answer
            for answer in command.answers
        ]
        return {
            "role": "user",
            # The durable continuation command is the remote idempotency key.
            # Recovery must never turn one human answer into a new A2A message.
            "message_id": command.command_id,
            "task_id": address.task_id,
            "context_id": address.context_id,
            "parts": [{"kind": "text", "text": text}],
            "metadata": {
                "agent_id": address.agent_id,
                HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY: {
                    "schema_version": 1,
                    "interaction_id": command.interaction_id,
                    "interaction_revision": command.interaction_revision,
                    # This is the durable platform digest. Never recompute it
                    # at the SDK boundary, where ordering/coercion could drift.
                    "answer_digest": command.answer_digest,
                    "answers": typed_answers,
                },
            },
        }

    def _build_model_reply_message(
        self, command: _ModelReplyCommand, *, address: DirectCallAddress
    ) -> dict[str, Any]:
        return {
            "role": "user",
            # Durable command id as the remote idempotency key, mirroring
            # typed-answer continuation delivery.
            "message_id": command.command_id,
            "task_id": address.task_id,
            "context_id": address.context_id,
            "parts": [{"kind": "text", "text": command.message_text}],
            "metadata": {"agent_id": address.agent_id},
        }

    # -- DirectA2AClient methods -------------------------------------------

    async def send(self, command: _DispatchCommand) -> Any:
        card = await self._card(command.endpoint_scope)
        response = await self._send_message(
            card,
            to_sdk_message(self._build_user_message(command)),
            accepted_output_modes=self._accepted_output_modes,
            push_notification_config=_push_notification_config(command, card),
            blocking=True,
            timeout=self._timeout,
        )
        task = _response_to_task(response, command_message_id=command.message_id)
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        try:
            await _materialize_task_artifacts_epoch_fenced(
                task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(command, "conversation_id", None),
                room_epoch=getattr(command, "room_epoch", None),
                call_record_id=getattr(command, "call_record_id", None),
                message_id=getattr(command, "message_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="send",
                call_record_id=command.call_record_id,
                task_id=task.id,
            )
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="direct",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(command.endpoint_scope),
                agent_id=command.agent_id,
                task_id=task.id,
                context_id=task.context_id,
                error_message=f"Failed to materialize agent file artifact: {exc}",
            )
            obs = self._observation(**kwargs)
            return self._receipt(
                outcome="terminal",
                task_id=task.id,
                context_id=task.context_id,
                terminal_observation=obs,
            )
        self._remember(command, task_id=task.id, context_id=task.context_id)
        event_kind = _event_kind(task)
        if event_kind in {"input_required", "auth_required"}:
            # The Agent answered immediately with a request for input. The
            # request is the durable result of this invocation: the kernel
            # decides on the next turn whether it can satisfy it from context
            # (re-dispatch) or must ask the user.
            observation = self._observation(
                **_task_to_observation_kwargs(
                    task,
                    source_kind="direct",
                    call_record_id=command.call_record_id,
                    binding_scope=endpoint_scope_digest(command.endpoint_scope),
                    agent_id=command.agent_id,
                    task_id=task.id,
                    context_id=task.context_id,
                )
            )
            return self._receipt(
                outcome="interaction",
                task_id=task.id,
                context_id=task.context_id,
                interaction_observation=observation,
            )
        status = _terminal_status(task)
        if status is None:
            return self._receipt(
                outcome="accepted", task_id=task.id, context_id=task.context_id
            )
        observation = self._observation(
            **_task_to_observation_kwargs(
                task,
                source_kind="direct",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(command.endpoint_scope),
                agent_id=command.agent_id,
                task_id=task.id,
                context_id=task.context_id,
            )
        )
        return self._receipt(
            outcome="terminal",
            task_id=task.id,
            context_id=task.context_id,
            terminal_observation=observation,
        )

    async def start_poll(self, command: _DispatchCommand) -> Any:
        card = await self._card(command.endpoint_scope)
        response = await self._send_message(
            card,
            to_sdk_message(self._build_user_message(command)),
            accepted_output_modes=self._accepted_output_modes,
            push_notification_config=_push_notification_config(command, card),
            blocking=False,
            timeout=self._poll_timeout,
        )
        task = _response_to_task(response, command_message_id=command.message_id)
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        self._remember(command, task_id=task.id, context_id=task.context_id)
        return self._receipt(
            outcome="accepted", task_id=task.id, context_id=task.context_id
        )

    async def open_stream(self, command: _DispatchCommand) -> DirectA2AStream:
        card = await self._card(command.endpoint_scope)
        generator = self._stream_message(
            card,
            to_sdk_message(self._build_user_message(command)),
            accepted_output_modes=self._accepted_output_modes,
            push_notification_config=_push_notification_config(command, card),
            timeout=self._timeout,
        )
        return DirectA2AStream(
            command=command,
            stream=generator,
            observation_factory=self._observation,
            binding_scope=endpoint_scope_digest(command.endpoint_scope),
            epoch_owner=self._epoch_owner,
            on_frame_identity=lambda task_id, context_id: self._remember(
                command, task_id=task_id, context_id=context_id
            ),
        )

    async def inspect(self, command: _DispatchCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        task = await self._fetch_remote_task(
            card, address.task_id, timeout=self._poll_timeout
        )
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        try:
            await _materialize_task_artifacts_epoch_fenced(
                task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(command, "conversation_id", None),
                room_epoch=getattr(command, "room_epoch", None),
                call_record_id=getattr(command, "call_record_id", None),
                message_id=getattr(command, "message_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="inspect",
                call_record_id=command.call_record_id,
                task_id=task.id,
            )
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="inspection",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
                error_message=f"Failed to materialize agent file artifact: {exc}",
            )
            obs = self._observation(**kwargs)
            return self._receipt(
                outcome="terminal",
                task_id=task.id,
                context_id=task.context_id,
                terminal_observation=obs,
            )
        self._remember(command, task_id=task.id, context_id=task.context_id)
        event_kind = _event_kind(task)
        if event_kind in {"input_required", "auth_required"}:
            observation = self._observation(
                **_task_to_observation_kwargs(
                    task,
                    source_kind="inspection",
                    call_record_id=command.call_record_id,
                    binding_scope=endpoint_scope_digest(address.endpoint_scope),
                    agent_id=address.agent_id,
                    task_id=task.id,
                    context_id=task.context_id,
                )
            )
            return self._receipt(
                outcome="interaction",
                task_id=task.id,
                context_id=task.context_id,
                interaction_observation=observation,
            )
        status = _terminal_status(task)
        if status is None:
            return self._receipt(
                outcome="accepted", task_id=task.id, context_id=task.context_id
            )
        observation = self._observation(
            **_task_to_observation_kwargs(
                task,
                source_kind="inspection",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
            )
        )
        return self._receipt(
            outcome="terminal",
            task_id=task.id,
            context_id=task.context_id,
            terminal_observation=observation,
        )

    async def read_push_notification(
        self,
        payload: Any,
        *,
        call_record_id: str,
        endpoint_scope: str,
        agent_id: str,
        conversation_id: str,
        room_epoch: int,
        message_id: str,
        expected_task_id: str,
        expected_context_id: str | None,
    ) -> dict[str, Any]:
        """Normalize one authenticated push body for the call it targets.

        Correlation is checked before any artifact is materialized. Only a
        terminal ``Task`` snapshot becomes an observation; status and artifact
        updates are deltas that omit earlier output.
        """
        frame, task = parse_push_notification_payload(payload)
        if task.id != expected_task_id or (
            task.context_id is not None
            and expected_context_id is not None
            and task.context_id != expected_context_id
        ):
            raise ValueError("push notification does not correlate with the call")
        event_kind = "artifact" if frame == "artifact_update" else _event_kind(task)
        observation = None
        if frame == "task" and event_kind == "terminal":
            binding_scope = endpoint_scope_digest(endpoint_scope)
            try:
                await _materialize_task_artifacts_epoch_fenced(
                    task,
                    epoch_owner=self._epoch_owner,
                    conversation_id=conversation_id,
                    room_epoch=room_epoch,
                    call_record_id=call_record_id,
                    message_id=message_id,
                )
            except Exception as exc:
                if _is_recoverable_materialization_error(exc):
                    raise
                logger.emit(
                    E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                    exc=exc,
                    operation="push",
                    call_record_id=call_record_id,
                    task_id=task.id,
                )
                kwargs = _failed_materialization_observation_kwargs(
                    source_kind="webhook",
                    call_record_id=call_record_id,
                    binding_scope=binding_scope,
                    agent_id=agent_id,
                    task_id=task.id,
                    context_id=task.context_id,
                    error_message=f"Failed to materialize agent file artifact: {exc}",
                )
            else:
                kwargs = _task_to_observation_kwargs(
                    task,
                    source_kind="webhook",
                    call_record_id=call_record_id,
                    binding_scope=binding_scope,
                    agent_id=agent_id,
                    task_id=task.id,
                    context_id=task.context_id,
                )
            observation = self._observation(**kwargs)
        return {
            "frame": frame,
            "task_id": task.id,
            "context_id": task.context_id,
            "event_kind": event_kind,
            "observation": observation,
        }

    def _receipt_from_task(
        self,
        task: Any,
        *,
        source_kind: str,
        call_record_id: str,
        binding_scope: str,
        agent_id: str | None,
        task_id: str,
        context_id: str | None,
    ) -> Any:
        event_kind = _event_kind(task)
        if event_kind in {"input_required", "auth_required"}:
            observation = self._observation(
                **_task_to_observation_kwargs(
                    task,
                    source_kind=source_kind,
                    call_record_id=call_record_id,
                    binding_scope=binding_scope,
                    agent_id=agent_id,
                    task_id=task_id,
                    context_id=context_id,
                )
            )
            return self._receipt(
                outcome="interaction",
                task_id=task_id,
                context_id=context_id,
                interaction_observation=observation,
            )
        status = _terminal_status(task)
        if status is None:
            return self._receipt(
                outcome="accepted", task_id=task_id, context_id=context_id
            )
        observation = self._observation(
            **_task_to_observation_kwargs(
                task,
                source_kind=source_kind,
                call_record_id=call_record_id,
                binding_scope=binding_scope,
                agent_id=agent_id,
                task_id=task_id,
                context_id=context_id,
            )
        )
        return self._receipt(
            outcome="terminal",
            task_id=task_id,
            context_id=context_id,
            terminal_observation=observation,
        )

    async def continue_task(self, command: _ContinuationCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        response = await self._send_message(
            card,
            to_sdk_message(self._build_continuation_message(command, address=address)),
            accepted_output_modes=self._accepted_output_modes,
            blocking=True,
            timeout=self._timeout,
        )
        task = _response_to_task(response, command_message_id=command.command_id)
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        try:
            await _materialize_task_artifacts_epoch_fenced(
                task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(command, "conversation_id", None),
                room_epoch=getattr(command, "room_epoch", None),
                call_record_id=getattr(command, "call_record_id", None),
                message_id=getattr(command, "command_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="continue_task",
                call_record_id=command.call_record_id,
                task_id=task.id,
            )
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="direct",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
                error_message=f"Failed to materialize agent file artifact: {exc}",
            )
            obs = self._observation(**kwargs)
            return self._receipt(
                outcome="terminal",
                task_id=task.id,
                context_id=task.context_id,
                terminal_observation=obs,
            )
        self._remember(command, task_id=task.id, context_id=task.context_id)
        return self._receipt_from_task(
            task,
            source_kind="direct",
            call_record_id=command.call_record_id,
            binding_scope=endpoint_scope_digest(address.endpoint_scope),
            agent_id=address.agent_id,
            task_id=task.id,
            context_id=task.context_id,
        )

    async def send_model_reply(self, command: _ModelReplyCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        response = await self._send_message(
            card,
            to_sdk_message(self._build_model_reply_message(command, address=address)),
            accepted_output_modes=self._accepted_output_modes,
            blocking=True,
            timeout=self._timeout,
        )
        task = _response_to_task(response, command_message_id=command.command_id)
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        try:
            await _materialize_task_artifacts_epoch_fenced(
                task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(command, "conversation_id", None),
                room_epoch=getattr(command, "room_epoch", None),
                call_record_id=getattr(command, "call_record_id", None),
                message_id=getattr(command, "command_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="model_reply",
                call_record_id=command.call_record_id,
                task_id=task.id,
            )
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="direct",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
                error_message=f"Failed to materialize agent file artifact: {exc}",
            )
            obs = self._observation(**kwargs)
            return self._receipt(
                outcome="terminal",
                task_id=task.id,
                context_id=task.context_id,
                terminal_observation=obs,
            )
        self._remember(command, task_id=task.id, context_id=task.context_id)
        return self._receipt_from_task(
            task,
            source_kind="direct",
            call_record_id=command.call_record_id,
            binding_scope=endpoint_scope_digest(address.endpoint_scope),
            agent_id=address.agent_id,
            task_id=task.id,
            context_id=task.context_id,
        )

    async def inspect_continuation(self, command: _ContinuationCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        task = await self._fetch_remote_task(
            card, address.task_id, timeout=self._poll_timeout
        )
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        try:
            await _materialize_task_artifacts_epoch_fenced(
                task,
                epoch_owner=self._epoch_owner,
                conversation_id=getattr(command, "conversation_id", None),
                room_epoch=getattr(command, "room_epoch", None),
                call_record_id=getattr(command, "call_record_id", None),
                message_id=getattr(command, "command_id", None),
            )
        except Exception as exc:
            if _is_recoverable_materialization_error(exc):
                raise
            logger.emit(
                E.TASK_ARTIFACTS_MATERIALIZATION_FAILED,
                exc=exc,
                operation="inspect_continuation",
                call_record_id=command.call_record_id,
                task_id=task.id,
            )
            kwargs = _failed_materialization_observation_kwargs(
                source_kind="inspection",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
                error_message=f"Failed to materialize agent file artifact: {exc}",
            )
            obs = self._observation(**kwargs)
            return self._receipt(
                outcome="terminal",
                task_id=task.id,
                context_id=task.context_id,
                terminal_observation=obs,
            )
        return self._receipt_from_task(
            task,
            source_kind="inspection",
            call_record_id=command.call_record_id,
            binding_scope=endpoint_scope_digest(address.endpoint_scope),
            agent_id=address.agent_id,
            task_id=task.id,
            context_id=task.context_id,
        )

    async def cancel(self, command: _CancellationCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        acknowledged = await self._cancel_remote_task(
            card, address.task_id, timeout=self._cancel_timeout
        )
        if not acknowledged:
            return self._receipt(outcome="delivery_uncertain")
        return self._receipt(
            outcome="accepted",
            task_id=address.task_id,
            context_id=address.context_id,
        )

    async def inspect_cancellation(self, command: _CancellationCommand) -> Any:
        address = await self._resolve_call(command)
        if not address.task_id or not address.endpoint_scope:
            return self._receipt(outcome="delivery_uncertain")
        card = await self._card(address.endpoint_scope)
        task = await self._fetch_remote_task(
            card, address.task_id, timeout=self._poll_timeout
        )
        if task is None:
            return self._receipt(outcome="delivery_uncertain")
        status = _terminal_status(task)
        if status is None:
            return self._receipt(
                outcome="accepted", task_id=task.id, context_id=task.context_id
            )
        observation = self._observation(
            **_task_to_observation_kwargs(
                task,
                source_kind="inspection",
                call_record_id=command.call_record_id,
                binding_scope=endpoint_scope_digest(address.endpoint_scope),
                agent_id=address.agent_id,
                task_id=task.id,
                context_id=task.context_id,
            )
        )
        return self._receipt(
            outcome="terminal",
            task_id=task.id,
            context_id=task.context_id,
            terminal_observation=observation,
        )


def _continuation_text(answers: list[Any], *, bare: bool = False) -> str:
    """Render answers as the A2A text part; ``bare`` omits question ids.

    Free-text interactions derived from plain ``input-required`` use ``bare``
    so the agent receives exactly what the user typed.
    """
    parts: list[str] = []
    for answer in answers:
        question_id = None if bare else getattr(answer, "question_id", None)
        value = getattr(answer, "answer", None)
        text = _answer_text(value)
        if text:
            parts.append(f"{question_id}: {text}" if question_id else text)
    return "\n".join(parts) if parts else "continue"


def _answer_text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    # Choice answers may carry free text the owner typed under "Other".
    other = getattr(value, "other", None)
    other_parts = [other] if isinstance(other, str) else []
    choices = getattr(value, "choices", None)
    if isinstance(choices, list):
        return ", ".join([*(str(item) for item in choices), *other_parts])
    choice = getattr(value, "choice", None)
    if isinstance(choice, str):
        return choice
    if other_parts:
        return other_parts[0]
    confirmed = getattr(value, "confirmed", None)
    if confirmed is not None:
        return str(bool(confirmed))
    decision = getattr(value, "decision", None)
    if decision is not None:
        return str(getattr(decision, "value", decision))
    reference = getattr(value, "authorization_reference", None)
    if isinstance(reference, str):
        return reference
    if hasattr(value, "model_dump"):
        return str(value.model_dump(mode="json"))
    return str(value)


__all__ = [
    "DirectA2AStream",
    "DirectCallAddress",
    "OrchestratorDirectA2AClient",
    "endpoint_scope_digest",
]
