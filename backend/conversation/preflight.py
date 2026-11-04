from dataclasses import dataclass

from common.observability import bind_log_context, log_id
from common.observability import events as E
from common.utils.logger import get_logger
from conversation.candidate_scope import normalize_candidate_scope
from conversation.mentions import parse_agent_mentions
from conversation.target_scope import (
    orchestration_request_info,
)
from models.request import (
    RoomCenterUserMessageRequest,
)
from models.response import (
    RoomCenterUserMessageResponse,
)
from models.room import (
    Room,
    RoomUserMessage,
)
from models.room_services_models import ParseResult, ResolvedRoutingScope

logger = get_logger(__name__)


_PERSISTED_EXECUTION_MODES = frozenset({"direct", "supervisor", "passthrough"})


def persisted_execution_mode(info: dict) -> str:
    """The request's execution mode as stored on the user message."""
    mode = info.get("execution_mode")
    return mode if mode in _PERSISTED_EXECUTION_MODES else "direct"


_PUBLIC_USER_MESSAGE_EXTEND_INFO_STRING_KEYS = (
    "quoted_text",
    "quoted_sender_name",
    "quote_id",
)


@dataclass(slots=True)
class RoomMessagePreflightContext:
    request: RoomCenterUserMessageRequest
    target_group: str
    mentioned_agent_ids: list[str] | None
    user_message: RoomUserMessage
    client_request_id: str | None
    room: Room
    message_text: str
    pre_resolved_mentions: list[dict] | None
    pre_resolved_scope: ResolvedRoutingScope | None
    pre_resolved_selected_scope: ResolvedRoutingScope | None


class MessagePreflight:
    """Build the orchestration envelope for a persisted user message.

    Built from the runtime's current room store and target-scope resolver.
    """

    def __init__(self, *, store, target_scope) -> None:
        self._store = store
        self._target_scope = target_scope

    async def prepare_orchestration_envelope(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
        selected_agent_set: dict,
        explicit_mentions: list[dict] | None,
        client_request_id: str | None,
    ) -> ParseResult:
        info = orchestration_request_info(request)
        scope = info.get("agent_scope")
        scope = scope if isinstance(scope, dict) else {"source": "room_default"}
        candidate_scope_mode = str(scope.get("source") or "room_default")
        candidate_scope_group_id = scope.get("group_id")
        sanitized_group_id = (
            candidate_scope_group_id.strip()
            if (
                candidate_scope_mode == "saved_group"
                and isinstance(candidate_scope_group_id, str)
                and candidate_scope_group_id.strip()
            )
            else None
        )
        candidate_scope = normalize_candidate_scope(
            conversation_id=request.conversation_id,
            source=candidate_scope_mode,
            group_id=sanitized_group_id,
            selected_agent_set=selected_agent_set,
            selected_by_user_id=request.user_id,
        )

        existing_extend_info = (
            user_message.extend_info
            if isinstance(user_message.extend_info, dict)
            else {}
        )
        envelope = {
            key: value
            for key in _PUBLIC_USER_MESSAGE_EXTEND_INFO_STRING_KEYS
            if isinstance((value := existing_extend_info.get(key)), str)
        }
        envelope.update(
            {
                "orchestration": True,
                "execution_mode": persisted_execution_mode(info),
                "orchestration_run_id": user_message.message_id,
                "orchestration_status": "created",
                "candidate_scope_snapshot_id": candidate_scope.snapshot_id,
                "candidate_scope_source": candidate_scope.source,
                "candidate_scope_mode": candidate_scope.source,
                "candidate_agent_ids": list(candidate_scope.agent_ids),
                "candidate_scope_snapshot_version": candidate_scope.revision,
                "mentioned_agent_ids": [
                    mention["agent_id"] for mention in (explicit_mentions or [])
                ],
                "client_request_id": client_request_id,
            }
        )
        if candidate_scope.group_id:
            envelope["candidate_scope_group_id"] = candidate_scope.group_id

        user_message.extend_info = envelope
        persisted = await self._store.update_room_user_message_by_message_id(
            user_message.message_id,
            user_message,
        )
        if not persisted:
            logger.emit(
                E.ORCHESTRATION_ENVELOPE_PERSIST_FAILED,
                conversation_id=request.conversation_id,
                message_id=user_message.message_id,
                client_request_id=log_id(client_request_id),
            )
            return ParseResult(success=False)
        logger.emit(
            E.ORCHESTRATION_ENVELOPE_PREPARED,
            conversation_id=request.conversation_id,
            message_id=user_message.message_id,
            client_request_id=log_id(client_request_id),
            candidate_count=len(selected_agent_set),
        )
        return ParseResult(success=True)

    async def run(
        self,
        context: RoomMessagePreflightContext,
    ) -> RoomCenterUserMessageResponse:
        with bind_log_context(
            conversation_id=context.request.conversation_id,
            client_request_id=context.client_request_id,
            message_id=context.user_message.message_id,
        ):
            return await self._run(context)

    async def _run(
        self,
        context: RoomMessagePreflightContext,
    ) -> RoomCenterUserMessageResponse:
        request = context.request
        target_group = context.target_group
        user_message = context.user_message
        client_request_id = context.client_request_id
        room = context.room
        message_text = context.message_text
        pre_resolved_mentions = context.pre_resolved_mentions
        pre_resolved_scope = context.pre_resolved_scope
        pre_resolved_selected_scope = context.pre_resolved_selected_scope
        selected_scope_locked = pre_resolved_selected_scope is not None

        # ── Dispatch using pre-resolved scope ─────────────────────────────
        if selected_scope_locked:
            selected_agent_set = pre_resolved_selected_scope.selected_agent_set
            auto_assign = pre_resolved_selected_scope.auto_assign_agents
            agents = pre_resolved_selected_scope.agents
        elif pre_resolved_mentions:
            selected_agent_set = {
                mention["agent_id"]: mention["agent_name"]
                for mention in pre_resolved_mentions
            }
            agents = []
            for mention in pre_resolved_mentions:
                agent = await self._store.get_agent_by_agent_id(mention["agent_id"])
                if agent is not None:
                    agents.append(agent)
            auto_assign = False

        # Parse inline <@id|name> mentions from message text when the caller did
        # not provide canonical mentioned_agent_ids. This runs AFTER persistence,
        # so inline mentions are best-effort and do not get reject-before-persist
        # protection.
        #
        if pre_resolved_mentions is None:
            # When target_group is "all_agents", the room_agent_set may be empty
            # (e.g. newly created rooms from the homepage).  In that case, resolve
            # mentions against all active agents so inline @-mentions are honoured
            # regardless of room membership.
            if target_group == "all_agents":
                all_agents = await self._store.get_all_active_agents(
                    user_id=request.user_id,
                )
                effective_agent_set = {
                    a.agent_id: a.agent_card.name for a in (all_agents or [])
                }
            else:
                effective_agent_set = room.room_agent_set

            mentions = parse_agent_mentions(message_text, effective_agent_set)

            if mentions:
                mention_agents, _rejected = await self._target_scope.sanitize(
                    [mention["agent_id"] for mention in mentions],
                    sender_user_id=request.user_id,
                    required_input_modes=None,
                )
                eligible_mention_ids = {agent.agent_id for agent in mention_agents}
                mentions = [
                    mention
                    for mention in mentions
                    if mention["agent_id"] in eligible_mention_ids
                ]
            if mentions:
                pre_resolved_mentions = mentions
                if not selected_scope_locked:
                    selected_agent_set = {
                        mention["agent_id"]: mention["agent_name"]
                        for mention in mentions
                    }
                    agents = []
                    for mention in mentions:
                        agent = await self._store.get_agent_by_agent_id(
                            mention["agent_id"]
                        )
                        if agent is not None:
                            agents.append(agent)
                    auto_assign = False

        # Target scope dispatch: reuse pre-resolved scope or run all_agents LLM.
        if selected_scope_locked:
            pass
        elif pre_resolved_mentions:
            pass
        elif target_group == "all_agents":
            selection_result = await self._target_scope.resolve_explicit_target_scope(
                room,
                message_text,
                target_group,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(selection_result, RoomCenterUserMessageResponse):
                # all_agents runs after persist — attach the real message_id
                # so the frontend knows the user message exists in the DB
                # and doesn't rollback optimistic state.
                selection_result.message_id = user_message.message_id
                selection_result.preflight_outcome = "failed"
                selection_result.preflight_details = (
                    selection_result.error or "Agent selection failed"
                )
                return selection_result
            selected_agent_set = selection_result.selected_agent_set
            auto_assign = selection_result.auto_assign_agents
            agents = selection_result.agents
        elif pre_resolved_scope is not None:
            selected_agent_set = pre_resolved_scope.selected_agent_set
            auto_assign = pre_resolved_scope.auto_assign_agents
            agents = pre_resolved_scope.agents
        else:
            selected_agent_set = {}
            auto_assign = True
            agents = []

        logger.emit(
            E.MESSAGE_PREFLIGHT_SCOPE_RESOLVED,
            conversation_id=request.conversation_id,
            message_id=user_message.message_id,
            client_request_id=log_id(client_request_id),
            target_group=log_id(target_group),
            candidate_count=len(selected_agent_set),
            auto_assign=auto_assign,
        )

        # Orchestration assembles its own context from the durable run state.
        parse_result = await self.prepare_orchestration_envelope(
            request=request,
            user_message=user_message,
            selected_agent_set=selected_agent_set,
            explicit_mentions=pre_resolved_mentions,
            client_request_id=client_request_id,
        )

        if not parse_result.success:
            logger.emit(
                E.MESSAGE_PREFLIGHT_FAILED,
                conversation_id=request.conversation_id,
                message_id=user_message.message_id,
                client_request_id=log_id(client_request_id),
                canceled=parse_result.canceled,
            )
            return RoomCenterUserMessageResponse(
                message_id=user_message.message_id,
                message=user_message,
                success=parse_result.canceled,
                error="Failed to parse user message"
                if not parse_result.canceled
                else None,
                status_code=200 if parse_result.canceled else 500,
                preflight_outcome="canceled" if parse_result.canceled else "failed",
                preflight_details=None
                if parse_result.canceled
                else "Failed to parse user message",
            )

        logger.emit(
            E.MESSAGE_PREFLIGHT_COMPLETED,
            conversation_id=request.conversation_id,
            message_id=user_message.message_id,
            client_request_id=log_id(client_request_id),
            preflight_outcome="ready",
        )
        return RoomCenterUserMessageResponse(
            message_id=user_message.message_id,
            dispatch_root_message_id=user_message.message_id,
            message=user_message,
            success=True,
            error=None,
            status_code=200,
            preflight_outcome="ready",
        )


__all__ = [
    "MessagePreflight",
    "RoomMessagePreflightContext",
    "persisted_execution_mode",
]
