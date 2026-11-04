from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from inspect import isawaitable
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from common.dto import (
    AgentInfo,
    AgentMessageInput,
    CreateRoomRequest,
    MembershipUpdateRequest,
    RoomInfo,
    RoomMessageInfo,
    SavedUserMessage,
    UserMessageInput,
)
from common.dto.base import to_stored_conversation_keys
from common.observability import NoopTracingProvider, log_id
from common.observability import events as E
from common.protocols import AgentRegistry, RoomRepository
from common.utils.a2a_helpers import sanitize_task_dict
from common.utils.logger import get_logger
from conversation.attachments import AttachmentMetadataReader
from conversation.dto import (
    RoomTimelineEntry,
    RoomTimelinePage,
    TimelinePosition,
    UserMessageInsertResult,
)
from conversation.idempotency import (
    IdempotencyConflictError,
    UserMessagePersistenceError,
)
from conversation.membership import RoomMembershipSeedSource, resolve_membership_seed
from conversation.translators import (
    agent_message_doc_from_input,
    create_room_doc,
    message_info_from_doc,
    room_info_from_doc,
    saved_user_message_from_doc,
    user_message_doc_from_input,
)
from models.quote import QuotedSnippet, QuoteSourceKind
from models.response import RoomCenterUserMessageResponse
from models.room import Room, RoomAgentMessage, RoomUserMessage

_CANONICAL_CARD_PREFIX = "orchestrator:"
_CANONICAL_PUBLIC_CALL_PREFIX = "inv_"


@runtime_checkable
class CanonicalAgentCallReader(Protocol):
    """Confirms that a canonical Agent Card names a call in the given room."""

    async def agent_call_exists(
        self,
        *,
        conversation_id: str,
        run_id: str,
        public_call_id: str,
    ) -> bool: ...


def _parse_canonical_card_id(message_id: str) -> tuple[str, str] | None:
    """Split ``orchestrator:{run_id}:{public_call_id}``; ``None`` when malformed."""

    if not message_id.startswith(_CANONICAL_CARD_PREFIX):
        return None
    separator = message_id.rfind(":" + _CANONICAL_PUBLIC_CALL_PREFIX)
    if separator <= len(_CANONICAL_CARD_PREFIX):
        return None
    run_id = message_id[len(_CANONICAL_CARD_PREFIX) : separator]
    public_call_id = message_id[separator + 1 :]
    if public_call_id == _CANONICAL_PUBLIC_CALL_PREFIX or ":" in public_call_id:
        return None
    return run_id, public_call_id


@runtime_checkable
class MessageRepository(Protocol):
    async def save_user_message(self, message: dict) -> str: ...
    async def get_user_message_by_idempotency_key(
        self, conversation_id: str, client_request_id: str
    ) -> dict | None: ...
    async def insert_user_message_idempotently(
        self, document: dict
    ) -> UserMessageInsertResult: ...
    async def save_agent_message(self, message: dict) -> str: ...
    async def get_by_id(self, message_id: str) -> dict | None: ...
    async def get_user_message_by_id(self, message_id: str) -> dict | None: ...
    async def get_agent_message_by_id(self, message_id: str) -> dict | None: ...

    async def update_user_message(self, message_id: str, updates: dict) -> bool: ...
    async def update_agent_message(self, message_id: str, updates: dict) -> bool: ...
    async def update_agent_message_if_not_terminal(
        self, message_id: str, updates: dict, terminal_states: list[str]
    ) -> bool: ...
    async def count_agent_messages(self, query: dict) -> int: ...
    async def get_by_ids(self, message_ids: list[str]) -> list[dict]: ...
    async def get_for_room(
        self, conversation_id: str, limit: int, before: datetime | None = None
    ) -> list[dict]: ...
    async def get_timeline_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before: TimelinePosition | None,
    ) -> RoomTimelinePage: ...
    async def get_thread(self, parent_message_id: str) -> list[dict]: ...
    async def update_status(self, message_id: str, status: str, **fields) -> bool: ...
    async def delete_for_room(self, conversation_id: str) -> dict[str, int]: ...
    async def get_user_messages_for_room(
        self, conversation_id: str, limit: int = 100, before: datetime | None = None
    ) -> list[dict]: ...
    async def get_agent_messages_for_room(
        self, conversation_id: str, limit: int = 100, before: datetime | None = None
    ) -> list[dict]: ...
    async def get_agent_messages_by_related_message_id(
        self, related_message_id: str
    ) -> list[dict]: ...


_ALLOWED_ROOM_UPDATE_KEYS = frozenset(
    {
        "room_name",
        "extend_info",
        "last_activity_at",
        "is_pinned",
        "pin_order",
    }
)

logger = get_logger(__name__)


class ConversationFacade:
    def __init__(
        self,
        *,
        repository: RoomRepository,
        message_repository: MessageRepository,
        agent_registry: AgentRegistry,
        membership_source: RoomMembershipSeedSource,
        quote_repository: Any | None = None,
        attachment_metadata_reader: AttachmentMetadataReader | None = None,
        id_factory: Callable[[], str],
        now: Callable[[], datetime],
        tracer: Any | None = None,
        epoch_store: Any | None = None,
    ) -> None:
        self._repository = repository
        self._message_repository = message_repository
        self._agent_registry = agent_registry
        self._membership_source = membership_source
        self._quote_repository = quote_repository
        self._attachment_metadata_reader = attachment_metadata_reader
        self._id_factory = id_factory
        self._now = now
        self._tracer = tracer or NoopTracingProvider()
        self._epoch_store = epoch_store
        self._agent_call_reader: CanonicalAgentCallReader | None = None

    def bind_agent_call_reader(self, reader: CanonicalAgentCallReader) -> None:
        """Late-bound: the canonical run store is composed after this facade."""
        self._agent_call_reader = reader

    async def get_room(self, conversation_id: str) -> RoomInfo | None:
        doc = await self._repository.get_by_id(conversation_id)
        if doc is not None and doc.get("lifecycle_state", "active") != "active":
            return None
        return room_info_from_doc(doc) if doc is not None else None

    async def get_room_agents(self, conversation_id: str) -> list[str]:
        room = await self.get_room(conversation_id)
        return list(room.agent_ids) if room is not None else []

    async def get_room_owner(self, conversation_id: str) -> str | None:
        doc = await self._repository.get_by_id(conversation_id)
        return _owner_id_from_doc(doc)

    async def create_room(self, request: CreateRoomRequest) -> RoomInfo:
        self._validate_create_room_request(request)
        resolved = await resolve_membership_seed(
            seed=request.membership_seed,
            owner_id=request.owner_id,
            agent_registry=self._agent_registry,
            membership_source=self._membership_source,
        )
        if self._epoch_store is None:
            raise RuntimeError("Room epoch store is not bound")
        epoch_store = self._epoch_store
        conversation_id = self._id_factory()
        creation_id = uuid4().hex
        doc = create_room_doc(
            conversation_id=conversation_id,
            owner_id=request.owner_id,
            owner_name=request.owner_name,
            room_name=request.room_name,
            agent_set=resolved.agent_set,
            created_at=self._now(),
            membership_origin=resolved.membership_origin,
            membership_origin_status=resolved.membership_origin_status,
            source_group_id=resolved.source_group_id,
            source_group_name=resolved.source_group_name,
            extend_info=request.extend_info,
            owner_product=request.owner_product,
        )
        await self._repository.create(doc)
        try:
            outcome, _ = await epoch_store.activate(
                conversation_id, creation_id, activated_at=self._now()
            )
        except Exception as exc:
            await self._compensate_room_creation(conversation_id)
            logger.emit(
                E.ROOM_EPOCH_ACTIVATION_FAILED,
                exc=exc,
                conversation_id=conversation_id,
            )
            raise ValueError("Room epoch activation failed") from exc
        if outcome == "conflict":
            await self._compensate_room_creation(conversation_id)
            raise ValueError("Room epoch activation conflict")
        return room_info_from_doc(doc)

    async def _compensate_room_creation(self, conversation_id: str) -> None:
        try:
            await self._repository.delete(conversation_id)
        except Exception as exc:
            logger.emit(
                E.ROOM_CREATION_COMPENSATION_FAILED,
                exc=exc,
                conversation_id=conversation_id,
            )

    async def delete_room(self, conversation_id: str, owner_id: str) -> bool:
        doc = await self._repository.get_by_id(conversation_id)
        if _owner_id_from_doc(doc) != owner_id:
            return False
        await self._message_repository.delete_for_room(conversation_id)
        return await self._repository.delete(conversation_id)

    async def update_room(self, conversation_id: str, updates: dict) -> RoomInfo | None:
        unknown = set(updates) - _ALLOWED_ROOM_UPDATE_KEYS
        if unknown:
            raise ValueError(f"Unknown room update keys: {sorted(unknown)}")
        updated = await self._repository.update_fields(conversation_id, dict(updates))
        return room_info_from_doc(updated) if updated is not None else None

    async def update_membership(
        self, conversation_id: str, request: MembershipUpdateRequest
    ) -> RoomInfo:
        doc = await self._repository.get_by_id(conversation_id)
        if doc is None:
            raise ValueError("Room not found")

        room = room_info_from_doc(doc)
        agent_set = dict(room.agent_set)
        for agent_id in request.remove_agent_ids or []:
            agent_set.pop(agent_id, None)

        additions = await self._resolve_agent_ids_for_update(
            list(request.add_agent_ids or []),
            requesting_user_id=room.owner_id,
        )
        agent_set.update(additions)

        origin = room.membership_origin
        status = room.membership_origin_status
        if origin in {"saved_group", "all_current_agents"}:
            status = "seeded_edited"
        else:
            origin = "manual"
            status = "manual"

        updated = await self._repository.set_membership(
            conversation_id,
            agent_set=agent_set,
            membership_origin=origin,
            membership_origin_status=status,
            source_group_id=room.source_group_id,
            source_group_name=room.source_group_name,
        )
        if updated is None:
            raise ValueError("Room not found")
        return room_info_from_doc(updated)

    async def delete_room_owned_messages(self, conversation_id: str) -> dict[str, int]:
        return await self._message_repository.delete_for_room(conversation_id)

    async def delete_room_quote(self, quote_id: str) -> bool:
        if self._quote_repository is None:
            return False
        return bool(await self._quote_repository.delete_by_id(quote_id))

    async def get_attachment_for_room_file(
        self, conversation_id: str, file_id: str
    ) -> dict | None:
        reader = self._attachment_metadata_reader
        if reader is None:
            return None
        return await reader.get_for_room_file(conversation_id, file_id)

    async def cleanup_room_owned_data(self, conversation_id: str) -> dict[str, int]:
        result: dict[str, int] = await self._message_repository.delete_for_room(
            conversation_id
        )
        if self._quote_repository is not None:
            result["quotes"] = await self._quote_repository.delete_for_room(
                conversation_id
            )
        return result

    async def save_user_message(
        self, conversation_id: str, message: UserMessageInput
    ) -> SavedUserMessage:
        await self._require_room(conversation_id)
        message_id = self._id_factory()
        doc = user_message_doc_from_input(
            conversation_id=conversation_id,
            message_id=message_id,
            message=message,
            created_at=self._now(),
        )
        await self._message_repository.save_user_message(doc)
        await self._touch_room_activity(conversation_id)
        return saved_user_message_from_doc(doc)

    def ensure_user_message_id(self, user_message: RoomUserMessage) -> str:
        if user_message.message_id == "":
            user_message.message_id = self._id_factory()
        return user_message.message_id

    async def persist_user_message(
        self,
        user_message: RoomUserMessage,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> UserMessageInsertResult:
        try:
            self.ensure_user_message_id(user_message)
            doc = to_stored_conversation_keys(
                user_message.model_dump(mode="json", exclude={"quote"})
            )
            _strip_file_urls(doc)
            if idempotency_fingerprint is not None:
                if idempotency_fingerprint_version is None:
                    raise ValueError("Idempotency fingerprint version is required")
                doc["idempotency_fingerprint"] = idempotency_fingerprint
                doc["idempotency_fingerprint_version"] = idempotency_fingerprint_version
                result = (
                    await self._message_repository.insert_user_message_idempotently(doc)
                )
                if result.created:
                    await self._touch_room_activity(user_message.conversation_id)
                return result

            message_id = await self._message_repository.save_user_message(doc)
            await self._touch_room_activity(user_message.conversation_id)
            return UserMessageInsertResult(
                message_id=message_id,
                created=True,
                document=doc,
            )
        except IdempotencyConflictError:
            raise
        except UserMessagePersistenceError:
            raise
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="persist_user_message",
                conversation_id=user_message.conversation_id,
                message_id=user_message.message_id,
                client_request_id=log_id(user_message.client_request_id),
            )
            raise UserMessagePersistenceError(
                "Failed to persist room user message"
            ) from exc

    async def save_agent_message(
        self, conversation_id: str, message: AgentMessageInput
    ) -> str:
        await self._require_room(conversation_id)
        message_id = self._id_factory()
        doc = agent_message_doc_from_input(
            conversation_id=conversation_id,
            message_id=message_id,
            message=message,
            created_at=self._now(),
        )
        saved_id = await self._message_repository.save_agent_message(doc)
        await self._touch_room_activity(conversation_id)
        return saved_id

    async def _touch_room_activity(self, conversation_id: str) -> None:
        try:
            activity_at = self._now()
            touch_activity = getattr(self._repository, "touch_activity", None)
            if touch_activity is not None:
                result = touch_activity(conversation_id, activity_at)
            else:
                result = self._repository.update_fields(
                    conversation_id,
                    {"last_activity_at": activity_at},
                )
            if isawaitable(result):
                await result
        except Exception as exc:
            logger.emit(
                E.ROOM_ACTIVITY_UPDATE_FAILED,
                exc=exc,
                conversation_id=conversation_id,
            )

    async def update_agent_message(
        self, message_id: str, message: RoomAgentMessage
    ) -> bool:
        try:
            update_data = _strip_unset_task_tracking_fields(
                to_stored_conversation_keys(
                    message.model_dump(exclude_unset=True, mode="json")
                )
            )
            return bool(
                await self._message_repository.update_agent_message(
                    message_id,
                    update_data,
                )
            )
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="update_agent_message",
                message_id=message_id,
            )
            return False

    async def update_agent_message_status(
        self, message_id: str, status: str, **kwargs: Any
    ) -> bool:
        return await self._message_repository.update_status(
            message_id, status, **kwargs
        )

    async def get_message(self, message_id: str) -> RoomMessageInfo | None:
        doc = await self._message_repository.get_by_id(message_id)
        return message_info_from_doc(doc) if doc is not None else None

    async def get_user_message_model(self, message_id: str) -> RoomUserMessage | None:
        getter = getattr(self._message_repository, "get_user_message_by_id", None)
        doc = await getter(message_id) if getter is not None else None
        return _safe_parse_user_message(doc)

    async def get_user_message_by_idempotency_key(
        self,
        conversation_id: str,
        client_request_id: str,
    ) -> dict | None:
        return await self._message_repository.get_user_message_by_idempotency_key(
            conversation_id,
            client_request_id,
        )

    async def get_agent_message_model(self, message_id: str) -> RoomAgentMessage | None:
        getter = getattr(self._message_repository, "get_agent_message_by_id", None)
        doc = await getter(message_id) if getter is not None else None
        return _safe_parse_agent_message(doc)

    async def get_user_messages_for_room(
        self, conversation_id: str
    ) -> list[RoomUserMessage]:
        try:
            docs = await self._message_repository.get_user_messages_for_room(
                conversation_id
            )
            return [
                message
                for doc in docs
                if (message := _safe_parse_user_message(doc)) is not None
            ]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_user_messages_for_room",
                conversation_id=conversation_id,
            )
            return []

    async def get_agent_messages_for_room(
        self, conversation_id: str
    ) -> list[RoomAgentMessage]:
        try:
            docs = await self._message_repository.get_agent_messages_for_room(
                conversation_id
            )
            messages = [
                message
                for doc in docs
                if (message := _safe_parse_agent_message(doc)) is not None
            ]
            return messages
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_messages_for_room",
                conversation_id=conversation_id,
            )
            return []

    async def get_timeline_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before: TimelinePosition | None,
    ) -> RoomTimelinePage:
        page = await self._message_repository.get_timeline_page(
            conversation_id,
            limit=limit,
            before=before,
        )
        entries: list[RoomTimelineEntry] = []
        for entry in page.entries:
            try:
                if entry.source == "user":
                    message = _parse_timeline_user_message(entry.message)
                else:
                    message = _parse_timeline_agent_message(entry.message)
            except Exception as exc:
                message_id = (
                    entry.message.get("message_id")
                    if isinstance(entry.message, dict)
                    else None
                )
                logger.emit(
                    E.TIMELINE_MESSAGE_PARSE_FAILED,
                    exc=exc,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    message_source=entry.source,
                )
                raise ValueError("Invalid stored room timeline message") from None
            entries.append(RoomTimelineEntry(source=entry.source, message=message))
        return RoomTimelinePage(
            entries=entries,
            has_more=page.has_more,
            next_position=page.next_position,
        )

    async def get_agent_messages_by_related_message_id(
        self, related_message_id: str
    ) -> list[RoomAgentMessage]:
        try:
            docs = (
                await self._message_repository.get_agent_messages_by_related_message_id(
                    related_message_id
                )
            )
            return [
                message
                for doc in docs
                if (message := _safe_parse_agent_message(doc)) is not None
            ]
        except Exception as exc:
            logger.emit(
                E.CONVERSATION_STORE_OPERATION_FAILED,
                exc=exc,
                operation="get_agent_messages_by_related_message_id",
                related_message_id=related_message_id,
            )
            return []

    async def get_messages_for_room(
        self, conversation_id: str, limit: int = 100, before: datetime | None = None
    ) -> list[RoomMessageInfo]:
        return [
            message_info_from_doc(doc)
            for doc in await self._message_repository.get_for_room(
                conversation_id, limit, before
            )
        ]

    async def get_messages_by_ids(
        self, message_ids: list[str]
    ) -> list[RoomMessageInfo]:
        docs = await self._message_repository.get_by_ids(message_ids)
        by_id = {str(doc.get("message_id")): doc for doc in docs}
        return [
            message_info_from_doc(by_id[message_id])
            for message_id in message_ids
            if message_id in by_id
        ]

    async def materialize_quote(
        self,
        *,
        room: Room,
        request: Any,
        user_message: RoomUserMessage,
    ) -> RoomCenterUserMessageResponse | None:
        payload = user_message.quote
        if payload is None:
            return None
        text = payload.text.strip()
        if not text:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="Quote text is required",
                status_code=400,
            )

        source_kind = (
            payload.source_kind.value
            if hasattr(payload.source_kind, "value")
            else payload.source_kind
        )
        source_error = await self._quote_source_error(
            conversation_id=room.conversation_id,
            source_message_id=payload.source_message_id,
            source_kind=str(source_kind),
        )
        if source_error is not None:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=source_error,
                status_code=400,
            )

        if self._quote_repository is None:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="Could not save quoted context. Try again.",
                status_code=503,
            )

        try:
            snippet = QuotedSnippet(
                conversation_id=room.conversation_id,
                created_by_user_id=request.user_id or user_message.user_id or "",
                text=text,
                source_message_id=payload.source_message_id,
                source_kind=str(source_kind),
                source_agent_id=payload.source_agent_id,
                sender_display_name=payload.sender_display_name,
            )
            qid = await self._quote_repository.insert(snippet)
        except Exception as exc:
            logger.emit(
                E.QUOTE_SNIPPET_CREATION_FAILED,
                exc=exc,
                conversation_id=room.conversation_id,
                user_id=request.user_id or user_message.user_id,
                client_request_id=log_id(user_message.client_request_id),
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="Could not save quoted context. Try again.",
                status_code=500,
            )

        if not isinstance(qid, str):
            qid = str(qid)
        user_message.quote_id = qid
        extend_info = dict(user_message.extend_info or {})
        extend_info["quoted_text"] = text
        if payload.sender_display_name:
            extend_info["quoted_sender_name"] = payload.sender_display_name
        extend_info["quote_id"] = qid
        user_message.extend_info = extend_info
        user_message.quote = None
        return None

    async def _quote_source_error(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        source_kind: str,
    ) -> str | None:
        """Return the 400 error for an invalid quote source, else ``None``."""

        if source_message_id.startswith(_CANONICAL_CARD_PREFIX):
            valid = await self._canonical_quote_source_is_valid(
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                source_kind=source_kind,
            )
            return None if valid else "Invalid quote source"
        if source_kind in {"unknown", QuoteSourceKind.UNKNOWN.value, ""}:
            return None
        source_message = await self.get_message(source_message_id)
        if source_message is None or source_message.conversation_id != conversation_id:
            return "Invalid quote source"
        expected_message_type = {
            "user_turn": "user",
            "agent": "agent",
            "synthesis": "agent",
        }.get(source_kind.lower())
        if (
            expected_message_type is not None
            and source_message.message_type != expected_message_type
        ):
            return "Invalid quote source type"
        return None

    async def _canonical_quote_source_is_valid(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        source_kind: str,
    ) -> bool:
        """Canonical Agent Cards are not room messages; check the call record.

        Fails closed: a malformed id, a non-``agent`` kind, a missing reader, a
        reader error, and an unknown or foreign call are indistinguishable.
        """

        if source_kind != QuoteSourceKind.AGENT.value:
            return False
        identity = _parse_canonical_card_id(source_message_id)
        if identity is None or self._agent_call_reader is None:
            return False
        run_id, public_call_id = identity
        try:
            return await self._agent_call_reader.agent_call_exists(
                conversation_id=conversation_id,
                run_id=run_id,
                public_call_id=public_call_id,
            )
        except Exception as exc:
            logger.emit(
                E.QUOTE_SOURCE_LOOKUP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                run_id=log_id(run_id),
            )
            return False

    async def get_message_thread(self, parent_message_id: str) -> list[RoomMessageInfo]:
        return [
            message_info_from_doc(doc)
            for doc in await self._message_repository.get_thread(parent_message_id)
        ]

    async def verify_room_agent_membership(
        self, conversation_id: str, agent_id: str
    ) -> bool:
        return agent_id in await self.get_room_agents(conversation_id)

    def _validate_create_room_request(self, request: CreateRoomRequest) -> None:
        if not request.owner_id:
            raise ValueError("owner_id is required")
        if not request.owner_name:
            raise ValueError("owner_name is required")
        if not request.room_name:
            raise ValueError("room_name is required")

    async def _resolve_agent_ids_for_update(
        self,
        agent_ids: list[str],
        *,
        requesting_user_id: str | None,
    ) -> dict[str, str]:
        if not agent_ids:
            return {}
        agents = await self._agent_registry.get_agents_by_ids(agent_ids)
        agents_by_id = {agent.agent_id: agent for agent in agents}
        missing = [agent_id for agent_id in agent_ids if agent_id not in agents_by_id]
        if missing:
            raise ValueError(f"Unknown or deleted agent IDs: {', '.join(missing)}")

        inaccessible: list[str] = []
        inactive: list[str] = []
        for agent in agents:
            if agent.status != "active":
                inactive.append(agent.agent_id)
            elif not _is_visible(agent, requesting_user_id):
                inaccessible.append(agent.agent_id)

        if inaccessible:
            raise ValueError(
                f"Access denied to private agents: {', '.join(inaccessible)}"
            )
        if inactive:
            raise ValueError(f"Inactive agent IDs: {', '.join(inactive)}")
        return {agent.agent_id: agent.name or agent.agent_id for agent in agents}

    async def _require_room(self, conversation_id: str) -> dict:
        doc = await self._repository.get_by_id(conversation_id)
        if doc is None:
            raise ValueError("Room not found")
        return doc


def _is_visible(agent: AgentInfo, user_id: str | None) -> bool:
    return agent.is_public or (user_id is not None and agent.provider_id == user_id)


def _owner_id_from_doc(doc: dict | None) -> str | None:
    if doc is None or not doc.get("room_owner_id"):
        return None
    return str(doc["room_owner_id"])


def _parse_timeline_user_message(doc: Any) -> RoomUserMessage:
    if not isinstance(doc, dict):
        raise ValueError("invalid user timeline document")
    return RoomUserMessage.model_validate(doc)


def _parse_timeline_agent_message(doc: Any) -> RoomAgentMessage:
    if not isinstance(doc, dict):
        raise ValueError("invalid agent timeline document")
    content = doc.get("message_content")
    if content and isinstance(content, dict):
        task = content.get("message_task")
        if task and isinstance(task, dict):
            sanitize_task_dict(task)
    return RoomAgentMessage.model_validate(doc)


def _log_message_parse_failure(
    document_kind: str, doc: dict, exc: BaseException
) -> None:
    logger.emit(
        E.CONVERSATION_DOCUMENT_PARSE_FAILED,
        exc=exc,
        document_kind=document_kind,
        conversation_id=doc.get("room_id", doc.get("conversation_id")),
        message_id=doc.get("message_id"),
    )


def _safe_parse_user_message(doc: dict | None) -> RoomUserMessage | None:
    if doc is None:
        return None
    try:
        return RoomUserMessage.model_validate(doc)
    except Exception as exc:
        _log_message_parse_failure("room_user_message", doc, exc)
        return None


def _safe_parse_agent_message(doc: dict | None) -> RoomAgentMessage | None:
    if doc is None:
        return None
    try:
        content = doc.get("message_content")
        if content and isinstance(content, dict):
            task = content.get("message_task")
            if task and isinstance(task, dict):
                sanitize_task_dict(task)
        return RoomAgentMessage.model_validate(doc)
    except Exception as exc:
        _log_message_parse_failure("room_agent_message", doc, exc)
        return None


def _strip_file_urls(doc: dict) -> None:
    target = doc.get("$set", doc)
    content = target.get("message_content")
    if not content:
        return
    for attachment in content.get("attachments") or []:
        attachment.pop("file_url", None)


def _strip_unset_task_tracking_fields(update_data: dict[str, Any]) -> dict[str, Any]:
    task_tracking_fields = {
        "last_notified_state",
        "agent_url",
        "task_created_at",
        "task_updated_at",
        "task_content",
    }
    for field in task_tracking_fields:
        if update_data.get(field) is None:
            update_data.pop(field, None)
    if update_data.get("has_task_tracking") is False:
        update_data.pop("has_task_tracking", None)
    return update_data
