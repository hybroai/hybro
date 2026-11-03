from agent import sanitize_routing_agent_ids
from common.observability import events as E
from common.observability import log_id, log_ids
from common.utils.logger import get_logger
from conversation.mentions import canonical_mentions_or_error
from models.request import (
    RoomCenterUserMessageRequest,
)
from models.response import (
    RoomCenterUserMessageResponse,
    ScopeResolutionError,
)
from models.room import (
    Room,
)
from models.room_services_models import ResolvedRoutingScope

logger = get_logger(__name__)


def orchestration_request_info(
    request: RoomCenterUserMessageRequest,
) -> dict:
    info = request.extend_info if isinstance(request.extend_info, dict) else {}
    return dict(info)


def selected_agent_ids_from_request(
    request: RoomCenterUserMessageRequest,
) -> list[str] | None:
    scope = orchestration_request_info(request).get("agent_scope")
    if not isinstance(scope, dict) or scope.get("source") != "mention":
        return None
    value = scope.get("agent_ids")
    if not isinstance(value, list):
        return None
    selected_agent_ids: list[str] = []
    for agent_id in value:
        if isinstance(agent_id, str) and agent_id.strip():
            selected_agent_ids.append(agent_id.strip())
    return selected_agent_ids


class TargetScopeResolver:
    """Resolve and validate the agents a room message may target.

    Built from the runtime's current bindings: the room runtime store (agent
    lookups) and the capability-issue reader (routing exclusions).
    """

    def __init__(self, *, store, capability_issue_reader=None) -> None:
        self._store = store
        self._capability_issue_reader = capability_issue_reader

    async def _excluded_agent_ids(self) -> frozenset[str]:
        reader = self._capability_issue_reader
        if reader is None:
            return frozenset()
        return frozenset(await reader.get_excluded_agent_ids())

    async def sanitize(
        self,
        agent_ids,
        *,
        sender_user_id: str | None,
        required_input_modes: list[str] | None = None,
    ):
        return await sanitize_routing_agent_ids(
            agent_ids,
            lookup=self._store.get_agent_by_agent_id,
            user_id=sender_user_id,
            excluded_agent_ids=await self._excluded_agent_ids(),
            required_input_modes=required_input_modes,
        )

    async def validate_canonical_mentions(
        self,
        mentioned_agent_ids: list[str],
        sender_user_id: str | None,
        required_input_modes: list[str] | None = None,
    ) -> list[dict] | RoomCenterUserMessageResponse:
        """Validate and resolve mentioned_agent_ids into canonical mention dicts.

        Returns the mention list on success, or an error response on failure.
        Called once before persistence; the result is reused downstream.
        """
        agents, invalid_ids = await self.sanitize(
            mentioned_agent_ids,
            sender_user_id=sender_user_id,
            required_input_modes=required_input_modes,
        )
        return canonical_mentions_or_error(agents, invalid_ids)

    async def resolve_selected_candidate_scope(
        self,
        selected_agent_ids: list[str],
        sender_user_id: str | None,
        required_input_modes: list[str] | None = None,
    ) -> ResolvedRoutingScope | RoomCenterUserMessageResponse:
        agents, invalid_ids = await self.sanitize(
            selected_agent_ids,
            sender_user_id=sender_user_id,
            required_input_modes=required_input_modes,
        )
        selected_agent_set = {agent.agent_id: agent.agent_card.name for agent in agents}

        if invalid_ids:
            error_msg = (
                "Invalid or unauthorized selected candidate targets: "
                f"{', '.join(invalid_ids)}"
            )
            logger.emit(
                E.TARGET_SCOPE_REJECTED,
                scope_source="selected_candidates",
                reason="unauthorized_targets",
                rejected_agent_count=len(invalid_ids),
                rejected_agent_ids=log_ids(invalid_ids),
                user_id=sender_user_id,
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="unauthorized_candidate_scope",
                    message=error_msg,
                ),
                status_code=400,
            )

        if not selected_agent_set:
            error_msg = "Selected candidate scope is empty."
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="empty_scope",
                    message=error_msg,
                ),
                status_code=400,
            )

        return ResolvedRoutingScope(
            selected_agent_set=selected_agent_set,
            auto_assign_agents=False,
            agents=agents,
        )

    async def validate_candidate_scope_metadata(
        self,
        request: RoomCenterUserMessageRequest,
        selected_agent_ids: list[str],
        sender_user_id: str | None,
    ) -> RoomCenterUserMessageResponse | None:
        info = orchestration_request_info(request)
        scope = info.get("agent_scope")
        if not isinstance(scope, dict) or scope.get("source") != "saved_group":
            return None

        candidate_scope_group_id = scope.get("group_id")
        if (
            not isinstance(candidate_scope_group_id, str)
            or not candidate_scope_group_id.strip()
        ):
            error_msg = (
                "candidate_scope_group_id is required for saved_group candidate scope"
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="group_not_usable",
                    message=error_msg,
                ),
                status_code=400,
            )

        group_id = candidate_scope_group_id.strip()
        group = await self._store.get_agent_group_by_id(group_id)
        if not group:
            error_msg = (
                "The selected agent group no longer exists. "
                "Please choose a different group."
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="group_not_usable",
                    message=error_msg,
                ),
                status_code=404,
            )

        if group.type != "builtin" and group.owner_id != sender_user_id:
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="You do not have permission to use this saved group",
                status_code=403,
            )

        group_agent_ids = {str(agent_id) for agent_id in (group.agents or [])}
        if not group_agent_ids:
            error_msg = f"The selected agent group '{group.name}' has no members."
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="empty_scope",
                    message=error_msg,
                ),
                status_code=400,
            )

        out_of_group_ids = [
            agent_id
            for agent_id in selected_agent_ids
            if agent_id not in group_agent_ids
        ]
        if out_of_group_ids:
            error_msg = (
                "Selected candidate agents are not members of the selected saved group: "
                f"{', '.join(out_of_group_ids)}"
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="group_not_usable",
                    message=error_msg,
                ),
                status_code=400,
            )

        return None

    async def resolve_explicit_target_scope(
        self,
        room: Room,
        message_text: str,
        target_group: str,
        sender_user_id: str | None = None,
        required_input_modes: list[str] | None = None,
    ) -> ResolvedRoutingScope | RoomCenterUserMessageResponse:
        """Resolve selected agents and auto-assign behavior for a target group.

        Returns:
            A named routing scope on success, or a RoomCenterUserMessageResponse
            on scope-resolution failure.

        Deterministic failures (room_team empty, saved_group missing/unauthorized/empty)
        always return structured error responses — never empty tuples.
        """

        async def select_agents_all_agents_mode() -> (
            ResolvedRoutingScope | RoomCenterUserMessageResponse
        ):
            try:
                active_agents = await self._store.get_all_active_agents(
                    user_id=sender_user_id
                )
                active_ids = [agent.agent_id for agent in (active_agents or [])]
                full_agents, _rejected = await self.sanitize(
                    active_ids,
                    sender_user_id=sender_user_id,
                    required_input_modes=required_input_modes,
                )
                selected = {
                    agent.agent_id: agent.agent_card.name for agent in full_agents
                }
                logger.emit(
                    E.TARGET_SCOPE_RESOLVED,
                    conversation_id=room.conversation_id,
                    scope_source="all_agents",
                    candidate_count=len(full_agents),
                )
                return ResolvedRoutingScope(
                    selected_agent_set=selected,
                    auto_assign_agents=True,
                    agents=full_agents,
                )
            except Exception as exc:
                error_msg = "Agent selection failed. Please try again."
                logger.emit(
                    E.TARGET_SCOPE_RESOLUTION_FAILED,
                    exc=exc,
                    conversation_id=room.conversation_id,
                    scope_source="all_agents",
                    user_id=sender_user_id,
                )
                return RoomCenterUserMessageResponse(
                    message_id=None,
                    message=None,
                    success=False,
                    error=error_msg,
                    scope_resolution_error=ScopeResolutionError(
                        code="empty_scope",
                        message=error_msg,
                    ),
                    status_code=500,
                )

        if target_group == "all_agents":
            return await select_agents_all_agents_mode()

        if target_group == "room_team":
            if room.room_agent_set:
                room_agents, _rejected = await self.sanitize(
                    room.room_agent_set,
                    sender_user_id=sender_user_id,
                    required_input_modes=required_input_modes,
                )
                selected_agent_set = {
                    agent.agent_id: (
                        room.room_agent_set.get(agent.agent_id) or agent.agent_card.name
                    )
                    for agent in room_agents
                }
                logger.emit(
                    E.TARGET_SCOPE_RESOLVED,
                    conversation_id=room.conversation_id,
                    scope_source="room_team",
                    candidate_count=len(selected_agent_set),
                )
                return ResolvedRoutingScope(
                    selected_agent_set=selected_agent_set,
                    auto_assign_agents=True,
                    agents=room_agents,
                )

            error_msg = "This room has no agents. Add agents before sending a message."
            logger.emit(
                E.TARGET_SCOPE_REJECTED,
                conversation_id=room.conversation_id,
                scope_source="room_team",
                reason="empty_scope",
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="empty_scope",
                    message=error_msg,
                ),
                status_code=400,
            )

        # Custom group (saved-group override at send time)
        group = await self._store.get_agent_group_by_id(target_group)
        if not group:
            error_msg = "The selected agent group no longer exists. Please choose a different group."
            logger.emit(
                E.TARGET_SCOPE_REJECTED,
                conversation_id=room.conversation_id,
                scope_source="saved_group",
                reason="group_not_found",
                group_id=log_id(target_group),
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="group_not_usable",
                    message=error_msg,
                ),
                status_code=404,
            )

        if group.type != "builtin" and group.owner_id != sender_user_id:
            logger.emit(
                E.TARGET_SCOPE_REJECTED,
                conversation_id=room.conversation_id,
                scope_source="saved_group",
                reason="group_not_authorized",
                group_id=log_id(target_group),
                user_id=sender_user_id,
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="You do not have permission to use this saved group",
                status_code=403,
            )

        if group.agents:
            agents, _rejected = await self.sanitize(
                group.agents,
                sender_user_id=sender_user_id,
                required_input_modes=required_input_modes,
            )

            selected_agent_set = {
                agent.agent_id: agent.agent_card.name for agent in agents
            }
            logger.emit(
                E.TARGET_SCOPE_RESOLVED,
                conversation_id=room.conversation_id,
                scope_source="saved_group",
                group_id=log_id(target_group),
                candidate_count=len(selected_agent_set),
            )
            return ResolvedRoutingScope(
                selected_agent_set=selected_agent_set,
                auto_assign_agents=True,
                agents=agents,
            )

        error_msg = f"The selected agent group '{group.name}' has no members."
        logger.emit(
            E.TARGET_SCOPE_REJECTED,
            conversation_id=room.conversation_id,
            scope_source="saved_group",
            reason="empty_scope",
            group_id=log_id(target_group),
        )
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=error_msg,
            scope_resolution_error=ScopeResolutionError(
                code="empty_scope",
                message=error_msg,
            ),
            status_code=400,
        )


__all__ = [
    "TargetScopeResolver",
    "orchestration_request_info",
    "selected_agent_ids_from_request",
]
