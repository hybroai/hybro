from common.observability import bind_log_context, log_id
from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow
from conversation.attachments import resolve_and_apply_room_attachments
from conversation.candidate_scope import SUPPORTED_CANDIDATE_SCOPE_SOURCES
from conversation.dto import UserMessageInsertResult
from conversation.idempotency import (
    IdempotencyConflictError,
    UnexpectedUserMessageDuplicateError,
    UserMessagePersistenceError,
    stored_fingerprint_matches,
)
from conversation.preflight import (
    RoomMessagePreflightContext,
    persisted_execution_mode,
)
from conversation.target_scope import (
    orchestration_request_info,
    selected_agent_ids_from_request,
)
from conversation.user_message_persistence import (
    UserMessageCommitCommand,
    UserMessageCommitService,
)
from models.request import (
    RoomCenterUserMessageRequest,
)
from models.response import (
    RoomCenterUserMessageResponse,
    ScopeResolutionError,
)
from models.room import (
    MAX_MESSAGE_LENGTH,
    Room,
    RoomUserMessage,
)
from models.room_services_models import ResolvedRoutingScope

logger = get_logger(__name__)


class RoomMessageSender:
    """Validate, persist, and replay a room user message.

    Built from the runtime's current bindings: the room store, the target-scope
    resolver, the conversation facade, the user-message commit service, and the
    attachment metadata reader.
    """

    def __init__(
        self,
        *,
        store,
        target_scope,
        facade,
        user_message_commit: UserMessageCommitService | None,
        attachment_metadata_reader,
    ) -> None:
        self._store = store
        self._target_scope = target_scope
        self._facade = facade
        self._user_message_commit = user_message_commit
        self._attachment_metadata_reader = attachment_metadata_reader

    def _require_facade(self):
        if self._facade is None:
            raise RuntimeError(
                "ConversationRuntime.bind_facade() not called - startup incomplete"
            )
        return self._facade

    def _require_user_message_commit(self) -> UserMessageCommitService:
        if self._user_message_commit is None:
            raise RuntimeError(
                "ConversationRuntime user-message commit service has not been bound"
            )
        return self._user_message_commit

    async def _resolve_and_apply_attachments(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
    ) -> RoomCenterUserMessageResponse | None:
        return await resolve_and_apply_room_attachments(
            request=request,
            user_message=user_message,
            attachment_reader=self._attachment_metadata_reader,
        )

    async def get_idempotent_user_message(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        idempotency_fingerprint: str,
        idempotency_fingerprint_version: int,
    ) -> RoomCenterUserMessageResponse | None:
        """Return a stable replay/conflict response without running side effects."""

        existing = await self._require_facade().get_user_message_by_idempotency_key(
            conversation_id,
            client_request_id,
        )
        if existing is None:
            return None
        message_id = existing.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise UserMessagePersistenceError(
                "Idempotency record is missing a valid message_id"
            )

        if not stored_fingerprint_matches(
            existing,
            fingerprint=idempotency_fingerprint,
            fingerprint_version=idempotency_fingerprint_version,
        ):
            return RoomCenterUserMessageResponse(
                conversation_id=conversation_id,
                message_id=None,
                message=None,
                success=False,
                error=(
                    "The client_request_id was already used for a different request"
                ),
                status_code=409,
            )

        return RoomCenterUserMessageResponse(
            conversation_id=conversation_id,
            message_id=message_id,
            dispatch_root_message_id=None,
            message=None,
            success=True,
            error=None,
            status_code=200,
        )

    async def persist_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: str = "room_team",
        mentioned_agent_ids: list[str] | None = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> tuple[RoomCenterUserMessageResponse, RoomMessagePreflightContext | None]:
        """Validate, scope-check, and persist the user message before heavy preflight."""
        client_request_id = (
            request.client_request_id.strip()
            if isinstance(getattr(request, "client_request_id", None), str)
            else None
        )
        request.client_request_id = client_request_id
        with bind_log_context(
            conversation_id=request.conversation_id,
            client_request_id=client_request_id,
        ):
            return await self._persist_message_to_room(
                request,
                client_request_id,
                target_group,
                mentioned_agent_ids,
                idempotency_fingerprint=idempotency_fingerprint,
                idempotency_fingerprint_version=idempotency_fingerprint_version,
            )

    async def _persist_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        client_request_id: str | None,
        target_group: str,
        mentioned_agent_ids: list[str] | None,
        *,
        idempotency_fingerprint: str | None,
        idempotency_fingerprint_version: int | None,
    ) -> tuple[RoomCenterUserMessageResponse, RoomMessagePreflightContext | None]:
        if (
            client_request_id
            and idempotency_fingerprint is not None
            and idempotency_fingerprint_version is not None
        ):
            replay = await self.get_idempotent_user_message(
                conversation_id=request.conversation_id or "",
                client_request_id=client_request_id,
                idempotency_fingerprint=idempotency_fingerprint,
                idempotency_fingerprint_version=idempotency_fingerprint_version,
            )
            if replay is not None:
                return replay, None

        validation_response = self._validate_send_message_request(request)
        if validation_response:
            return validation_response, None

        user_message = request.message
        if user_message is not None:
            # The authenticated request boundary, not client-supplied message
            # metadata, owns room/sender identity and the canonical turn key.
            user_message.conversation_id = request.conversation_id
            user_message.user_id = request.user_id
            user_message.client_request_id = client_request_id
            _discard_client_quote_display(user_message)
            if idempotency_fingerprint is not None:
                # Canonical sendMessage accepts only user-authored content and
                # relationship fields. Everything else below is server-owned and
                # deliberately excluded from the semantic fingerprint.
                user_message.message_id = ""
                user_message.message_created_at = utcnow()
                user_message.message_type = "user"
                user_message.agent_id = None
                user_message.run_id = None
                user_message.step_number = None
                user_message.total_steps = None
                user_message.task_updated_at = None
                user_message.task_content = None
                user_message.processing_claimed_at = None
                user_message.quote_id = None
                user_message.message_content.message_task = None
                user_message.extend_info = None

        # Resolve attachments from both sources before persistence
        att_err = await self._resolve_and_apply_attachments(request, user_message)
        if att_err is not None:
            return att_err, None

        # ── Pre-persist scope validation ──────────────────────────────────
        # Fetch room early: needed for scope resolution and downstream flags.
        room = await self._store.get_room_by_room_id(request.conversation_id)
        if not room:
            return (
                RoomCenterUserMessageResponse(
                    message_id=None,
                    message=None,
                    success=False,
                    error="Room not found",
                    status_code=404,
                ),
                None,
            )

        orchestration_info = orchestration_request_info(request)
        execution_mode = persisted_execution_mode(orchestration_info)
        scope = orchestration_info.get("agent_scope")
        scope = scope if isinstance(scope, dict) else {"source": "room_default"}
        user_extend_info = (
            user_message.extend_info
            if isinstance(user_message.extend_info, dict)
            else {}
        )
        # Persist the canonical scope alongside the mode: the orchestrator
        # ingress reconstructs its envelope from the persisted user message
        # (never from in-flight requests).
        user_message.extend_info = {
            **user_extend_info,
            "execution_mode": execution_mode,
            "agent_scope": scope,
        }
        message_text = user_message.message_content.message_text

        # Validate deterministic scope BEFORE persistence so rejected messages
        # never get a real message_id in the database.
        # - canonical mentioned_agent_ids: validated via shared helper
        # - room_team / saved_group: validated via shared helper
        # - all_agents: LLM-driven, cannot pre-validate (persists first)
        # - inline text mentions: best-effort, not covered (persists first)
        pre_resolved_mentions: list[dict] | None = None
        pre_resolved_scope: ResolvedRoutingScope | None = None
        pre_resolved_selected_scope: ResolvedRoutingScope | None = None
        selected_agent_ids = selected_agent_ids_from_request(request)
        candidate_scope_mode = str(scope.get("source") or "room_default")

        if candidate_scope_mode not in SUPPORTED_CANDIDATE_SCOPE_SOURCES:
            supported_modes = ", ".join(sorted(SUPPORTED_CANDIDATE_SCOPE_SOURCES))
            error_msg = (
                f"Unsupported candidate_scope_mode {candidate_scope_mode!r}; "
                f"expected one of: {supported_modes}"
            )
            return (
                RoomCenterUserMessageResponse(
                    message_id=None,
                    message=None,
                    success=False,
                    error=error_msg,
                    scope_resolution_error=ScopeResolutionError(
                        code="invalid_target",
                        message=error_msg,
                    ),
                    status_code=400,
                ),
                None,
            )
        logger.emit(
            E.USER_MESSAGE_PERSIST_STARTED,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            client_request_id=log_id(client_request_id),
            target_group=log_id(target_group),
            candidate_scope_mode=candidate_scope_mode,
            selected_agent_count=len(selected_agent_ids or []),
            mentioned_agent_count=len(mentioned_agent_ids or []),
            message_char_count=len(message_text or ""),
        )

        if selected_agent_ids is not None:
            metadata_error = await self._target_scope.validate_candidate_scope_metadata(
                request,
                selected_agent_ids,
                sender_user_id=request.user_id,
            )
            if metadata_error is not None:
                return metadata_error, None
            scope_result = await self._target_scope.resolve_selected_candidate_scope(
                selected_agent_ids,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(scope_result, RoomCenterUserMessageResponse):
                return scope_result, None
            pre_resolved_selected_scope = scope_result
            if mentioned_agent_ids:
                mention_result = await self._target_scope.validate_canonical_mentions(
                    mentioned_agent_ids,
                    sender_user_id=request.user_id,
                    required_input_modes=None,
                )
                if isinstance(mention_result, RoomCenterUserMessageResponse):
                    return mention_result, None
                pre_resolved_mentions = mention_result
                candidate_ids = set(pre_resolved_selected_scope.selected_agent_set)
                out_of_scope_mentions = [
                    mention["agent_id"]
                    for mention in pre_resolved_mentions
                    if mention["agent_id"] not in candidate_ids
                ]
                if out_of_scope_mentions:
                    error_msg = (
                        "Mentioned agents are outside the selected candidate scope: "
                        f"{', '.join(out_of_scope_mentions)}"
                    )
                    return (
                        RoomCenterUserMessageResponse(
                            message_id=None,
                            message=None,
                            success=False,
                            error=error_msg,
                            scope_resolution_error=ScopeResolutionError(
                                code="mention_outside_candidate_scope",
                                message=error_msg,
                            ),
                            status_code=400,
                        ),
                        None,
                    )
        elif mentioned_agent_ids:
            mention_result = await self._target_scope.validate_canonical_mentions(
                mentioned_agent_ids,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(mention_result, RoomCenterUserMessageResponse):
                return mention_result, None
            pre_resolved_mentions = mention_result
        elif target_group != "all_agents":
            scope_result = await self._target_scope.resolve_explicit_target_scope(
                room,
                message_text,
                target_group,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(scope_result, RoomCenterUserMessageResponse):
                return scope_result, None
            pre_resolved_scope = scope_result

        # ── Persist ───────────────────────────────────────────────────────
        qerr = await self._materialize_room_quote(room, request, user_message)
        if isinstance(qerr, RoomCenterUserMessageResponse):
            return qerr, None

        try:
            persistence = await self._persist_user_message(
                user_message,
                room_agent_set=room.room_agent_set if room else {},
                idempotency_fingerprint=idempotency_fingerprint,
                idempotency_fingerprint_version=idempotency_fingerprint_version,
            )
        except IdempotencyConflictError:
            await self._delete_uncommitted_quote(user_message)
            return (
                RoomCenterUserMessageResponse(
                    conversation_id=request.conversation_id,
                    message_id=None,
                    message=None,
                    success=False,
                    error=(
                        "The client_request_id was already used for a different request"
                    ),
                    status_code=409,
                ),
                None,
            )
        except UnexpectedUserMessageDuplicateError:
            await self._delete_uncommitted_quote(user_message)
            return (
                RoomCenterUserMessageResponse(
                    message_id=None,
                    message=None,
                    success=False,
                    error="User message uniqueness conflict",
                    status_code=500,
                ),
                None,
            )
        except UserMessagePersistenceError:
            await self._delete_uncommitted_quote(user_message)
            return (
                RoomCenterUserMessageResponse(
                    message_id=None,
                    message=None,
                    success=False,
                    error="Failed to add message",
                    status_code=500,
                ),
                None,
            )

        if not persistence.created:
            # This request lost the unique-index race. Only its own random quote
            # and pending attachment claims are compensated; winner state remains.
            await self._delete_uncommitted_quote(user_message)
            return (
                RoomCenterUserMessageResponse(
                    conversation_id=request.conversation_id,
                    message_id=persistence.message_id,
                    dispatch_root_message_id=None,
                    message=None,
                    success=True,
                    error=None,
                    status_code=200,
                ),
                None,
            )

        logger.emit(
            E.USER_MESSAGE_PERSISTED,
            conversation_id=request.conversation_id,
            message_id=user_message.message_id,
            client_request_id=log_id(client_request_id),
            target_group=log_id(target_group),
        )

        return (
            RoomCenterUserMessageResponse(
                conversation_id=request.conversation_id,
                message_id=user_message.message_id,
                dispatch_root_message_id=user_message.message_id,
                message=None,
                success=True,
                error=None,
                status_code=200,
            ),
            RoomMessagePreflightContext(
                request=request,
                target_group=target_group,
                mentioned_agent_ids=mentioned_agent_ids,
                user_message=user_message,
                client_request_id=client_request_id,
                room=room,
                message_text=message_text,
                pre_resolved_mentions=pre_resolved_mentions,
                pre_resolved_scope=pre_resolved_scope,
                pre_resolved_selected_scope=pre_resolved_selected_scope,
            ),
        )

    def _check_message_text_length(
        self, message: RoomUserMessage | None
    ) -> RoomCenterUserMessageResponse | None:
        """Reject messages exceeding MAX_MESSAGE_LENGTH (SDR 2.10)."""
        if (
            message
            and message.message_content
            and message.message_content.message_text
            and len(message.message_content.message_text) > MAX_MESSAGE_LENGTH
        ):
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=f"Message text exceeds maximum length of {MAX_MESSAGE_LENGTH} characters",
                status_code=400,
            )
        return None

    def _validate_send_message_request(
        self, request: RoomCenterUserMessageRequest
    ) -> RoomCenterUserMessageResponse | None:
        """Validate required fields for a user message send."""
        if request.conversation_id is None:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        if request.message is None:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="Message is required",
                status_code=400,
            )

        size_err = self._check_message_text_length(request.message)
        if size_err:
            return size_err

        return None

    async def _materialize_room_quote(
        self,
        room: Room,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
    ) -> RoomCenterUserMessageResponse | None:
        facade = self._require_facade()
        return await facade.materialize_quote(
            room=room,
            request=request,
            user_message=user_message,
        )

    async def _delete_uncommitted_quote(
        self,
        user_message: RoomUserMessage,
    ) -> None:
        quote_id = getattr(user_message, "quote_id", None)
        if not quote_id:
            return
        try:
            await self._require_facade().delete_room_quote(quote_id)
        except Exception as exc:
            logger.emit(
                E.QUOTE_DELETION_FAILED,
                exc=exc,
                conversation_id=user_message.conversation_id,
                quote_id=quote_id,
                client_request_id=log_id(user_message.client_request_id),
            )

    async def _persist_user_message(
        self,
        user_message: RoomUserMessage,
        *,
        room_agent_set: dict[str, str] | None = None,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> UserMessageInsertResult:
        return await self._require_user_message_commit().commit(
            UserMessageCommitCommand(
                message=user_message,
                room_agent_set=room_agent_set or {},
                idempotency_fingerprint=idempotency_fingerprint,
                idempotency_fingerprint_version=idempotency_fingerprint_version,
            )
        )


__all__ = ["RoomMessageSender"]


_QUOTE_DISPLAY_KEYS = ("quoted_text", "quoted_sender_name", "quote_id")


def _discard_client_quote_display(user_message: RoomUserMessage) -> None:
    """Drop client-supplied quote display fields.

    Only ``materialize_quote`` writes them, from the structured ``quote`` after
    it persists the quote record. A tab built before the plain-text path was
    removed may still send them without ``quote``; that message is sent without
    a quote rather than rejected.
    """

    extend_info = user_message.extend_info
    if not isinstance(extend_info, dict):
        return
    present = [key for key in _QUOTE_DISPLAY_KEYS if key in extend_info]
    if not present:
        return
    if user_message.quote is None:
        logger.emit(
            E.QUOTE_DISPLAY_FIELDS_DISCARDED,
            conversation_id=user_message.conversation_id,
            client_request_id=log_id(user_message.client_request_id),
            field_names=present,
        )
    user_message.extend_info = {
        key: value
        for key, value in extend_info.items()
        if key not in _QUOTE_DISPLAY_KEYS
    } or None
