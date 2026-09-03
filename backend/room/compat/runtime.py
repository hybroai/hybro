import re
import uuid
from dataclasses import dataclass
from uuid import uuid4

from agent.routing_safety import sanitize_routing_agent_ids
from common.dto import (
    AgentRoutingCandidate,
    CreateRoomRequest,
    ExplicitAgentMention,
    MembershipSeed,
    ParsedUserMessageRequest,
    RoomInfo,
    UserMessageInsertResult,
)
from common.protocols.context_memory_protocols import (
    ContextAssemblyPort,
    RoomMemoryCleanupPort,
)
from common.types import (
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from common.types import (
    MessageRole as Role,
)
from common.utils.cancellation import CancellationToken
from common.utils.context_utils import build_minimal_context
from common.utils.logger import get_logger
from common.utils.time import utcnow
from context_memory.projection import _human_size, build_turn_content
from execution.orchestration.candidate_scope import (
    SUPPORTED_CANDIDATE_SCOPE_SOURCES,
    normalize_candidate_scope,
)
from llm_gateway.errors import LLMServiceNotBoundError
from models.agent import AgentStatus
from models.memory import RoomMemory
from models.orchestration import TERMINAL_ORCHESTRATION_STATUSES
from models.request import (
    RoomCenterAgentMessageRequest,
    RoomCenterRoomMessageRequest,
    RoomCenterRoomSettingRequest,
    RoomCenterUserMessageRequest,
)
from models.response import (
    RoomAgentRef,
    RoomCenterActiveRunsResponse,
    RoomCenterAgentMessageResponse,
    RoomCenterRoomMessageResponse,
    RoomCenterRoomSettingResponse,
    RoomCenterUserMessageResponse,
    ScopeResolutionError,
)
from models.room import (
    MAX_MESSAGE_LENGTH,
    CoordinatorAgentId,
    MembershipOriginStatus,
    MessageContent,
    Room,
    RoomAgentMessage,
    RoomUserMessage,
    UserAttachment,
)
from models.room_services_models import ParseResult, ResolvedRoutingScope
from room.a2a_file_parts import AttachmentDispatchContext, AttachmentPreflightFailure
from room.attachments import (
    ResolvedAttachments as _ResolvedAttachments,
)
from room.attachments import (
    build_message_parts as platform_build_message_parts,
)
from room.attachments import (
    resolve_and_apply_room_attachments,
    resolve_room_attachments,
)
from room.compat.unbound import (
    UNBOUND_CANCELLATION_CONTROL,
    UNBOUND_RUNTIME_STORE,
)
from room.deletion import RoomDeletionService
from room.idempotency import (
    IdempotencyConflictError,
    UnexpectedUserMessageDuplicateError,
    UserMessagePersistenceError,
    stored_fingerprint_matches,
)
from room.timeline import (
    TimelineCursorError,
    decode_timeline_cursor,
    encode_timeline_cursor,
)
from room.timeline_projection import RoomTimelineProjector
from room.user_message_persistence import (
    UserMessageCommitCommand,
    UserMessageCommitService,
)

logger = get_logger(__name__)

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
    token: CancellationToken


def _agent_to_routing_candidate(agent) -> AgentRoutingCandidate:
    card = agent.agent_card
    capabilities = card.capabilities if isinstance(card.capabilities, dict) else {}
    skills = []
    if isinstance(card.skills, list):
        for skill in card.skills:
            if isinstance(skill, dict):
                skills.append(str(skill.get("name") or skill.get("id") or "Unknown"))
            else:
                skills.append(str(getattr(skill, "name", None) or skill))
    return AgentRoutingCandidate(
        agent_id=str(agent.agent_id),
        name=str(card.name),
        description=str(card.description or ""),
        capabilities=capabilities,
        skills=skills,
    )


class RoomServices:
    def __init__(self, *, room_store=None):
        if room_store is None:
            room_store = UNBOUND_RUNTIME_STORE
        self._store = room_store
        self.message_parser_service = None
        self.cancellation_control = UNBOUND_CANCELLATION_CONTROL
        self._room_files = None
        self._facade = None
        self._bound = False
        self._context_assembly: ContextAssemblyPort | None = None
        self._room_memory_cleanup: RoomMemoryCleanupPort | None = None
        self._attachment_metadata_reader = None
        self._attachment_content_reader = None
        self._a2a_inline_file_max_raw_bytes = 5 * 1024 * 1024
        self._a2a_inline_message_max_encoded_bytes = 6_990_508
        self._attachment_cleanup = None
        self._quote_writer = None
        self._capability_issue_reader = None
        self._agent_message_preparation = None
        self._timeline_projector: RoomTimelineProjector | None = None
        self._room_deletion: RoomDeletionService | None = None
        self._user_message_commit: UserMessageCommitService | None = None

    def reset_bindings(self) -> None:
        """Clear process-global composition state before a new lifespan starts."""
        self.__init__()

    def _release_cancellation_token(
        self,
        message_id: str,
        token: CancellationToken | None,
    ) -> None:
        if token is not None:
            self.cancellation_control.release_token(message_id, token)

    def bind_room_files(self, room_files) -> None:
        self._room_files = room_files

    def bind_store(self, store) -> None:
        """Inject the room runtime persistence store explicitly at startup."""
        self._store = store

    def bind_cancellation_control(self, *, cancellation_control) -> None:
        if cancellation_control is None:
            raise RuntimeError("RoomServices cancellation_control is required")
        self.cancellation_control = cancellation_control

    @property
    def room_files(self):
        files = getattr(self, "_room_files", None)
        if files is None:
            raise RuntimeError(
                "RoomServices.bind_room_files() not called - startup incomplete"
            )
        return files

    def bind_facade(self, facade) -> None:
        self._facade = facade
        self._bound = True

    def bind_context_memory(
        self,
        *,
        context_assembly: ContextAssemblyPort | None = None,
        room_memory_cleanup: RoomMemoryCleanupPort | None = None,
    ) -> None:
        self._context_assembly = context_assembly
        self._room_memory_cleanup = room_memory_cleanup
        preparation = getattr(self, "_agent_message_preparation", None)
        if preparation is not None and context_assembly is not None:
            preparation.bind_context_assembly(context_assembly)

    def bind_message_parser_service(self, service) -> None:
        self.message_parser_service = service

    def bind_attachment_metadata_reader(self, reader) -> None:
        self._attachment_metadata_reader = reader

    def bind_attachment_content_reader(self, reader) -> None:
        self._attachment_content_reader = reader

    def bind_a2a_inline_file_limits(
        self,
        *,
        max_raw_bytes: int,
        max_encoded_bytes: int,
    ) -> None:
        self._a2a_inline_file_max_raw_bytes = max_raw_bytes
        self._a2a_inline_message_max_encoded_bytes = max_encoded_bytes

    def bind_attachment_cleanup(self, cleanup) -> None:
        self._attachment_cleanup = cleanup

    def bind_quote_writer(self, writer) -> None:
        self._quote_writer = writer

    def bind_capability_issue_reader(self, reader) -> None:
        self._capability_issue_reader = reader

    def bind_agent_message_preparation(self, service) -> None:
        self._agent_message_preparation = service
        context_assembly = getattr(self, "_context_assembly", None)
        if context_assembly is not None:
            service.bind_context_assembly(context_assembly)

    def _require_agent_message_preparation(self):
        preparation = getattr(self, "_agent_message_preparation", None)
        if preparation is None:
            raise RuntimeError(
                "RoomServices agent message preparation service has not been bound"
            )
        return preparation

    def bind_timeline_projector(self, projector: RoomTimelineProjector) -> None:
        self._timeline_projector = projector

    def bind_room_deletion(self, service: RoomDeletionService) -> None:
        self._room_deletion = service

    def missing_required_bindings(self) -> list[str]:
        checks = (
            (
                "runtime_store",
                getattr(self, "_store", None) is not None
                and getattr(self, "_store", None) is not UNBOUND_RUNTIME_STORE,
            ),
            ("facade", getattr(self, "_facade", None) is not None),
            (
                "cancellation_control",
                getattr(self, "cancellation_control", None) is not None
                and getattr(self, "cancellation_control", None)
                is not UNBOUND_CANCELLATION_CONTROL,
            ),
            (
                "message_parser_service",
                getattr(self, "message_parser_service", None) is not None,
            ),
            (
                "user_message_commit",
                getattr(self, "_user_message_commit", None) is not None,
            ),
            (
                "timeline_projector",
                getattr(self, "_timeline_projector", None) is not None,
            ),
            (
                "room_deletion",
                getattr(self, "_room_deletion", None) is not None,
            ),
            (
                "agent_message_preparation",
                getattr(self, "_agent_message_preparation", None) is not None,
            ),
        )
        return [name for name, is_bound in checks if not is_bound]

    def bind_user_message_commit(self, service: UserMessageCommitService) -> None:
        self._user_message_commit = service

    def _require_user_message_commit(self) -> UserMessageCommitService:
        service = getattr(self, "_user_message_commit", None)
        if service is None:
            raise RuntimeError(
                "RoomServices user-message commit service has not been bound"
            )
        return service

    def _require_room_deletion(self) -> RoomDeletionService:
        service = getattr(self, "_room_deletion", None)
        if service is None:
            raise RuntimeError("RoomServices room deletion service has not been bound")
        return service

    def _require_timeline_projector(self) -> RoomTimelineProjector:
        projector = getattr(self, "_timeline_projector", None)
        if projector is None:
            raise RuntimeError("RoomServices timeline projector has not been bound")
        return projector

    async def _routing_excluded_agent_ids(self) -> frozenset[str]:
        reader = getattr(self, "_capability_issue_reader", None)
        if reader is None:
            return frozenset()
        return frozenset(await reader.get_excluded_agent_ids())

    async def _sanitize_routing_scope(
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
            excluded_agent_ids=await self._routing_excluded_agent_ids(),
            required_input_modes=required_input_modes,
        )

    def _require_facade(self):
        if not getattr(self, "_bound", False) or getattr(self, "_facade", None) is None:
            raise RuntimeError(
                "RoomServices.bind_facade() not called - startup incomplete"
            )
        return self._facade

    @staticmethod
    def _assembled_context_text(assembled) -> str:
        metadata = getattr(assembled, "metadata", {}) or {}
        return metadata.get("context", "")

    def _build_routing_context_from_memory(
        self,
        room_memory: RoomMemory,
        current_task: str,
    ) -> str:
        """Build small pre-routing context from canonical room history."""
        context_assembly = getattr(self, "_context_assembly", None)
        if context_assembly is not None:
            try:
                assembled = context_assembly.assemble_supervisor_context_from_memory(
                    room_memory,
                    current_task,
                    agent_registry=[],
                    max_turns=5,
                )
                return self._assembled_context_text(assembled)
            except Exception as exc:
                logger.warning(
                    "RoomServices: routing context assembly failed: %s",
                    exc,
                )

        # Compatibility helper remains canonical: RoomMemory exposes the top-level
        # conversation_history expected by build_minimal_context's structural input.
        return build_minimal_context(
            room_memory,
            current_task=current_task,
            max_turns=5,
        )

    @staticmethod
    def _legacy_room_from_info(info: RoomInfo) -> Room:
        return Room(
            room_id=info.room_id,
            room_name=info.room_name,
            room_owner_id=info.owner_id,
            room_owner_name=info.owner_name or "",
            room_agent_set=dict(info.agent_set)
            if info.agent_set
            else {agent_id: agent_id for agent_id in info.agent_ids},
            room_created_at=info.created_at or utcnow(),
            last_activity_at=info.last_activity_at or info.created_at or utcnow(),
            is_pinned=info.is_pinned,
            pin_order=info.pin_order,
            membership_origin=info.membership_origin,
            membership_origin_status=RoomServices._legacy_membership_origin_status(
                info.membership_origin_status
            ),
            source_group_id=info.source_group_id,
            source_group_name=info.source_group_name,
            extend_info=info.extend_info,
            processing_message_id=info.processing_message_id,
        )

    @staticmethod
    def _legacy_membership_origin_status(status: str | None) -> str:
        if status == "active":
            return MembershipOriginStatus.MANUAL.value
        return status or MembershipOriginStatus.MANUAL.value

    def _room_setting_response_from_info(
        self,
        info: RoomInfo,
        *,
        active_runs: list[dict] | None = None,
    ) -> RoomCenterRoomSettingResponse:
        room = self._legacy_room_from_info(info)
        return RoomCenterRoomSettingResponse(
            room_id=room.room_id,
            room=room,
            active_runs=active_runs,
            success=True,
            error=None,
            status_code=200,
        )

    def _membership_seed_from_request(
        self,
        request: RoomCenterRoomSettingRequest,
    ) -> MembershipSeed:
        requesting_user_id = request.requesting_user_id or request.room_owner_id
        if request.membership_seed_input is not None:
            return MembershipSeed(
                mode=request.membership_seed_input,
                agent_ids=request.room_agent_ids,
                group_id=request.seed_group_id,
                requesting_user_id=requesting_user_id,
            )
        if request.seed_all_current_agents:
            return MembershipSeed(
                mode="all_current_agents",
                requesting_user_id=requesting_user_id,
            )
        if request.applied_from_group:
            return MembershipSeed(
                mode="saved_group",
                group_id=request.applied_from_group,
                requesting_user_id=requesting_user_id,
            )
        agent_ids = None
        if request.room_agent_set is not None:
            agent_ids = list(self._normalize_room_agent_set(request.room_agent_set))
        return MembershipSeed(
            mode="manual",
            agent_ids=agent_ids,
            requesting_user_id=requesting_user_id,
        )

    @staticmethod
    def _room_error_response(
        *,
        room_id: str | None = None,
        error: str,
        status_code: int | None = None,
    ) -> RoomCenterRoomSettingResponse:
        if status_code is None:
            lower = error.lower()
            if "permission" in lower or "access denied" in lower:
                status_code = 403
            elif "not found" in lower:
                status_code = 404
            else:
                status_code = 400
        return RoomCenterRoomSettingResponse(
            room_id=room_id,
            room=None,
            success=False,
            error=error,
            status_code=status_code,
        )

    # === room_agent_set normalization helpers ===
    @staticmethod
    def _looks_like_agent_id(value: str) -> bool:
        """
        Heuristic check to determine if a string looks like an agent_id (UUID style).
        """
        if not isinstance(value, str):
            return False
        try:
            uuid.UUID(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _derive_required_input_modes(
        user_message: RoomUserMessage,
    ) -> list[str] | None:
        """Return resolved MIME types if the message has attachments, else None.

        The return value signals binary "has attachments" to AgentMatcher's I/O scoring.
        A non-None list (even empty after dedup) means attachments are present.
        """
        attachments = (
            user_message.message_content.attachments
            if user_message.message_content
            else None
        )
        if not attachments:
            return None
        return [att.mime_type for att in attachments]

    def _normalize_room_agent_set(self, room_agent_set: dict | None) -> dict[str, str]:
        """
        Normalize room_agent_set to the canonical shape: {agent_id: agent_name}.

        Historically some data used {agent_name: agent_id}. This method detects
        the dominant pattern and returns a mapping keyed by agent_id so that:
        - Multiple agents with the same name are supported
        - Backend logic can rely on agent_id keys.
        """
        if not room_agent_set:
            return {}

        # Count how many keys/values look like IDs
        keys_look_like_ids = sum(
            1 for k in room_agent_set.keys() if self._looks_like_agent_id(k)
        )
        values_look_like_ids = sum(
            1 for v in room_agent_set.values() if self._looks_like_agent_id(v)
        )

        # If keys already look like IDs (or it's ambiguous), assume correct shape
        if keys_look_like_ids >= values_look_like_ids:
            # Cast to concrete type for callers
            return dict(room_agent_set)

        # Otherwise we likely have {agent_name: agent_id} and should flip it
        normalized: dict[str, str] = {}
        for agent_name, agent_id in room_agent_set.items():
            if not isinstance(agent_id, str):
                # Skip malformed entries
                continue
            normalized[agent_id] = str(agent_name)

        return normalized

    # room setting management
    async def create_new_room(
        self, room_create_request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if room_create_request.room_name is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                success=False,
                error="Room name is required",
                status_code=400,
            )
        if room_create_request.room_owner_id is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                success=False,
                error="Room owner id is required",
                status_code=400,
            )
        if room_create_request.room_owner_name is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                success=False,
                error="Room owner name is required",
                status_code=400,
            )

        try:
            info = await facade.create_room(
                CreateRoomRequest(
                    owner_id=room_create_request.room_owner_id,
                    owner_name=room_create_request.room_owner_name,
                    room_name=room_create_request.room_name,
                    membership_seed=self._membership_seed_from_request(
                        room_create_request
                    ),
                    extend_info=room_create_request.extend_info or None,
                )
            )
        except ValueError as exc:
            return self._room_error_response(error=str(exc))
        return self._room_setting_response_from_info(info)

    async def inquiry_room_setting(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_id is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        info = await facade.get_room(room_id)
        if info is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room not found",
                status_code=404,
            )
        return self._room_setting_response_from_info(info)

    async def inquiry_active_runs(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterActiveRunsResponse:
        """Return non-terminal runs for a room (same run shape as inquiry_room_setting)."""
        facade = self._require_facade()
        if request.room_id is None:
            return RoomCenterActiveRunsResponse(
                room_id=None,
                active_runs=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        info = await facade.get_room(room_id)
        if info is None:
            return RoomCenterActiveRunsResponse(
                room_id=None,
                active_runs=None,
                success=False,
                error="Room not found",
                status_code=404,
            )

        active_runs: list[dict] = []

        turn_completion_kind: str | None = None
        trigger_msg_id = request.trigger_message_id
        if trigger_msg_id and not any(
            r.get("trigger_message_id") == trigger_msg_id for r in active_runs
        ):
            try:
                turn_completion_kind = await facade.get_turn_completion_kind(
                    trigger_msg_id
                )
            except Exception:
                pass

        return RoomCenterActiveRunsResponse(
            room_id=info.room_id,
            active_runs=active_runs,
            turn_completion_kind=turn_completion_kind,
            success=True,
            error=None,
            status_code=200,
        )

    async def inquiry_rooms_by_room_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_owner_id is None:
            return RoomCenterRoomSettingResponse(
                room_list=None,
                success=False,
                error="Room owner id is required",
                status_code=400,
            )

        room_owner_id = request.room_owner_id
        infos = await facade.list_rooms_for_owner(room_owner_id)
        return RoomCenterRoomSettingResponse(
            room_list=[self._legacy_room_from_info(info) for info in infos],
            success=True,
            error=None,
            status_code=200,
        )

    async def inquiry_room_history_by_owner_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_owner_id is None:
            return RoomCenterRoomSettingResponse(
                room_list=None,
                success=False,
                error="Room owner id is required",
                status_code=400,
            )

        infos = await facade.list_room_history_for_owner(
            request.room_owner_id,
            limit=100,
        )
        return RoomCenterRoomSettingResponse(
            room_list=[self._legacy_room_from_info(info) for info in infos],
            success=True,
            error=None,
            status_code=200,
        )

    async def update_room_agent_set(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_id is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        has_membership_input = (
            request.membership_seed_input is not None
            or request.room_agent_set is not None
            or request.seed_all_current_agents
            or request.applied_from_group is not None
        )
        if not has_membership_input:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room agent set or canonical membership input is required",
                status_code=400,
            )
        try:
            info = await facade.replace_membership(
                room_id,
                self._membership_seed_from_request(request),
                requesting_user_id=request.requesting_user_id,
            )
        except ValueError as exc:
            return self._room_error_response(room_id=room_id, error=str(exc))
        return self._room_setting_response_from_info(info)

    async def update_room_name(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_id is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        if request.room_name is None:
            return RoomCenterRoomSettingResponse(
                room_id=None,
                room=None,
                success=False,
                error="Room name is required",
                status_code=400,
            )
        try:
            info = await facade.update_room(room_id, {"room_name": request.room_name})
        except ValueError as exc:
            return self._room_error_response(room_id=room_id, error=str(exc))
        if info is None:
            return self._room_error_response(
                room_id=None,
                error="Room not found",
                status_code=404,
            )
        return self._room_setting_response_from_info(info)

    async def update_room_history_fields(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        facade = self._require_facade()
        if request.room_id is None:
            return self._room_error_response(
                room_id=None, error="Room id is required", status_code=400
            )
        updates: dict = {}
        if request.is_pinned is not None:
            updates["is_pinned"] = request.is_pinned
            if not request.is_pinned:
                updates["pin_order"] = None
        if request.pin_order is not None:
            updates["pin_order"] = request.pin_order
        if not updates:
            return self._room_error_response(
                room_id=request.room_id,
                error="No history fields supplied",
                status_code=400,
            )
        try:
            info = await facade.update_room(request.room_id, updates)
        except ValueError as exc:
            return self._room_error_response(room_id=request.room_id, error=str(exc))
        if info is None:
            return self._room_error_response(
                room_id=request.room_id, error="Room not found", status_code=404
            )
        return self._room_setting_response_from_info(info)

    async def update_room_default_mode(
        self,
        room_id: str,
        *,
        use_supervisor: bool,
    ) -> bool:
        return bool(
            await self._require_facade().update_room_default_mode(
                room_id,
                use_supervisor=use_supervisor,
            )
        )

    async def delete_room_by_room_id(
        self, request: RoomCenterRoomSettingRequest
    ) -> RoomCenterRoomSettingResponse:
        return await self._require_room_deletion().delete_room_by_room_id(request)

    # --- Attachment resolution helpers ---

    async def _resolve_attachments(
        self,
        file_ids: list[str],
        room_id: str,
    ) -> "_ResolvedAttachments | RoomCenterUserMessageResponse":
        return await resolve_room_attachments(
            file_ids=file_ids,
            room_id=room_id,
            attachment_reader=getattr(self, "_attachment_metadata_reader", None),
        )

    async def _resolve_and_apply_attachments(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
    ) -> RoomCenterUserMessageResponse | None:
        return await resolve_and_apply_room_attachments(
            request=request,
            user_message=user_message,
            attachment_reader=getattr(self, "_attachment_metadata_reader", None),
        )

    async def _build_message_parts(
        self,
        text: str,
        attachments: list[UserAttachment] | None,
        agent_card,
        context: AttachmentDispatchContext | None = None,
    ) -> list[Part] | AttachmentPreflightFailure:
        return await platform_build_message_parts(
            text=text,
            attachments=attachments,
            agent_card=agent_card,
            content_reader=getattr(self, "_attachment_content_reader", None),
            max_raw_bytes=self._a2a_inline_file_max_raw_bytes,
            max_encoded_bytes=self._a2a_inline_message_max_encoded_bytes,
            context=context,
        )

    # room user message management
    def parse_agent_mentions(
        self, message_text: str, room_agent_set: dict
    ) -> list[dict]:
        """
        Parse @agent mentions in format "<@agent-id|agentname>"

        Args:
            message_text: User input text with format "<@agent-id|agentname>"
            room_agent_set: Agent set in the room {agent_id: agent_name}

        Returns:
            list[dict]: Parsed mentions [{"agent_id": "xxx", "agent_name": "yyy", "mention_text": "<@xxx|yyy>"}]
        """
        mentions = []

        # pattern: <@agent_id|agent_name>
        slack_pattern = r"<@([^|]+)\|([^>]+)>"

        for match in re.finditer(slack_pattern, message_text):
            agent_id = match.group(1).strip()
            position = match.start()

            # Check if agent exists in room by agent_id
            if agent_id in room_agent_set:
                # Agent found in room
                room_agent_name = room_agent_set[agent_id]
                mentions.append(
                    {
                        "agent_id": agent_id,
                        "agent_name": room_agent_name,  # Use the name from room_agent_set
                        "mention_text": match.group(0),
                        "position": position,
                    }
                )
            else:
                logger.warning(
                    "Inline mention %s not in room agent set — ignored", agent_id
                )

        # Sort by position to maintain order
        mentions.sort(key=lambda x: x["position"])
        return mentions

    def extract_agent_message_content(
        self,
        message_text: str,
        target_agent_id: str,
        target_agent_name: str,  # kept for signature compatibility
        all_mentions: list,
    ) -> str:
        """
        Extract message content relevant to a specific agent
        Remove @mentions and return clean task content

        Args:
            message_text: Original message text
            target_agent_id: Target agent ID
            target_agent_name: Target agent name
            all_mentions: All parsed mentions from the message

        Returns:
            str: Clean message content relevant to the target agent
        """
        # Find all mentions for this specific agent
        agent_mentions = [m for m in all_mentions if m["agent_id"] == target_agent_id]

        if not agent_mentions:
            # No mentions found, remove all mentions and return clean content
            processed_text = message_text
            for mention in all_mentions:
                processed_text = processed_text.replace(mention["mention_text"], "")
            return re.sub(r"\s+", " ", processed_text).strip()

        # Strategy 1: Extract text around each mention of this agent
        relevant_parts = []

        for mention in agent_mentions:
            mention_pos = mention.get("position")
            mention_text = mention.get("mention_text", "")

            if mention_pos is None:
                context_clean = message_text
                for m in all_mentions:
                    context_clean = context_clean.replace(m.get("mention_text", ""), "")
                context_clean = re.sub(r"\s+", " ", context_clean).strip()
                if context_clean and context_clean not in relevant_parts:
                    relevant_parts.append(context_clean)
                continue

            start_pos = mention_pos
            end_pos = mention_pos + len(mention_text)

            # Extend backwards to find sentence start
            while start_pos > 0 and message_text[start_pos - 1] not in ".!?\n":
                start_pos -= 1

            # Extend forwards to find sentence end
            while end_pos < len(message_text) and message_text[end_pos] not in ".!?\n":
                end_pos += 1

            # Include the sentence ending punctuation
            if end_pos < len(message_text) and message_text[end_pos] in ".!?\n":
                end_pos += 1

            # Extract the relevant sentence/context
            context = message_text[start_pos:end_pos].strip()

            # Remove ALL mentions from this context, not just replace with @agent_name
            context_clean = context
            for m in all_mentions:
                context_clean = context_clean.replace(m["mention_text"], "")

            # Clean up whitespace
            context_clean = re.sub(r"\s+", " ", context_clean).strip()

            if context_clean and context_clean not in relevant_parts:
                relevant_parts.append(context_clean)

        # Join all relevant parts
        if relevant_parts:
            return " ".join(relevant_parts)
        else:
            # Fallback: return original message with all mentions removed
            processed_text = message_text
            for mention in all_mentions:
                processed_text = processed_text.replace(mention["mention_text"], "")
            return re.sub(r"\s+", " ", processed_text).strip()

    def group_mentions_by_context(self, message_text: str, mentions: list) -> dict:
        """
        Group mentions by their shared context/sentence and detect consecutive mentions

        Args:
            message_text: Original message text
            mentions: List of parsed mentions

        Returns:
            dict: {context_text: {"mentions": [mentions], "is_consecutive": bool}}
        """
        context_groups = {}

        for mention in mentions:
            mention_pos = mention.get("position")
            mention_text = mention.get("mention_text", "")

            if mention_pos is None:
                context = message_text.strip()
            else:
                start_pos = mention_pos
                end_pos = mention_pos + len(mention_text)

                while start_pos > 0 and message_text[start_pos - 1] not in ".!?\n":
                    start_pos -= 1

                while (
                    end_pos < len(message_text) and message_text[end_pos] not in ".!?\n"
                ):
                    end_pos += 1

                if end_pos < len(message_text) and message_text[end_pos] in ".!?\n":
                    end_pos += 1

                context = message_text[start_pos:end_pos].strip()

            # Group mentions by context
            if context not in context_groups:
                context_groups[context] = {"mentions": [], "is_consecutive": False}
            context_groups[context]["mentions"].append(mention)

        # Detect consecutive mentions within each context
        for _context, group_info in context_groups.items():
            mentions_in_context = group_info["mentions"]
            if len(mentions_in_context) > 1:
                all_have_position = all("position" in m for m in mentions_in_context)
                if not all_have_position:
                    continue

                mentions_in_context.sort(key=lambda x: x["position"])

                is_consecutive = True
                for i in range(len(mentions_in_context) - 1):
                    current_mention = mentions_in_context[i]
                    next_mention = mentions_in_context[i + 1]

                    # Get text between mentions
                    between_start = current_mention["position"] + len(
                        current_mention["mention_text"]
                    )
                    between_end = next_mention["position"]
                    between_text = message_text[between_start:between_end].strip()

                    # If there's significant text between mentions (more than just spaces/commas),
                    # they're not consecutive
                    if len(between_text) > 10 or any(
                        word in between_text.lower()
                        for word in [
                            "and",
                            "then",
                            "also",
                            "but",
                            "however",
                            "meanwhile",
                        ]
                    ):
                        is_consecutive = False
                        break

                group_info["is_consecutive"] = is_consecutive

        return context_groups

    def create_shared_message_content(
        self, context_text: str, mentions_in_context: list
    ) -> str:
        """
        Create message content for multiple agents sharing the same context
        Remove all @mentions and return clean task content

        Args:
            context_text: The shared context/sentence
            mentions_in_context: List of mentions in this context

        Returns:
            str: Clean message content without @mentions
        """
        processed_text = context_text

        # Remove all mentions (both simple @agent and Slack-style <@id|name>)
        for mention in mentions_in_context:
            mention_text = mention["mention_text"]
            processed_text = processed_text.replace(mention_text, "")

        # Clean up extra spaces and normalize whitespace
        processed_text = re.sub(r"\s+", " ", processed_text).strip()

        return processed_text

    def create_task_for_agent(
        self,
        user_message: RoomUserMessage,
        agent_id: str,
        agent_name: str,
        all_mentions: list,
    ) -> Task:
        """
        Create a2a Task for specific agent with relevant message content only

        Args:
            user_message: User message
            agent_id: Target agent ID
            agent_name: Target agent name
            all_mentions: All parsed mentions from the message

        Returns:
            Task: a2a protocol Task object
        """
        # Extract relevant message content for this agent
        original_text = user_message.message_content.message_text
        agent_relevant_text = self.extract_agent_message_content(
            original_text, agent_id, agent_name, all_mentions
        )

        # Create Message
        message = Message(
            message_id=user_message.message_id,
            role=Role.USER,
            parts=[Part(root=TextPart(text=agent_relevant_text))],
            context_id=user_message.room_id,
            metadata={},
        )

        # Create Task status
        task_status = TaskStatus(
            state=TaskState.submitted, timestamp=utcnow().isoformat()
        )

        # Create Task
        task = Task(
            id=str(uuid4()),
            context_id=user_message.room_id,
            status=task_status,
            history=[message],
        )

        return task

    async def create_task_for_agents_group(
        self, user_message: RoomUserMessage, mentions_group: list, shared_content: str
    ) -> list:
        """
        Create a2a Tasks for a group of agents sharing the same message content

        Args:
            user_message: User message
            mentions_group: List of mentions sharing the same context
            shared_content: Shared message content

        Returns:
            list: List of Task objects for each agent
        """
        tasks = []

        for mention in mentions_group:
            agent_id = mention["agent_id"]
            agent_name = mention["agent_name"]

            # Create Message with shared content
            message = Message(
                message_id=f"{user_message.message_id}_{agent_id}",  # Unique ID per agent
                role=Role.USER,
                parts=[Part(root=TextPart(text=shared_content))],
                context_id=user_message.room_id,
                metadata={},
            )

            # Create Task status
            task_status = TaskStatus(
                state=TaskState.submitted, timestamp=utcnow().isoformat()
            )

            # Create Task
            task = Task(
                id=str(uuid4()),
                context_id=user_message.room_id,
                status=task_status,
                history=[message],
            )

            tasks.append({"task": task, "agent_id": agent_id, "agent_name": agent_name})

        return tasks

    def _generate_agent_message_content(self, content: str) -> MessageContent:
        """
        Generate agent message content based on content.
        """
        a2a_message = Message(
            message_id=str(uuid4()),
            role=Role.USER,
            parts=[Part(root=TextPart(text=content))],
            context_id=str(uuid4()),
            metadata={},
        )

        # Create Task status
        task_status = TaskStatus(
            state=TaskState.submitted, timestamp=utcnow().isoformat()
        )

        # Create a2a Task
        task = Task(
            id=str(uuid4()),
            context_id=str(uuid4()),
            status=task_status,
            history=[a2a_message],
        )

        # Store the task; message_text is left empty until the agent produces output
        # (streaming artifacts or terminal response will populate it).
        return MessageContent(message_task=task)

    def _generate_new_agent_message(
        self,
        room_id: str,
        related_message_id: str,
        agent_id: str,
        content: str,
        user_id: str | None = None,
        extend_info: dict | None = None,
        step_number: int | None = None,
        total_steps: int | None = None,
        task_content: str | None = None,
        turn_id: str | None = None,
        client_request_id: str | None = None,
    ) -> RoomAgentMessage:
        """
        Generate a new agent message.

        Args:
            room_id: The room ID
            related_message_id: The related message ID (parent in dependency chain)
            agent_id: The agent ID (can be None for auto-assignment)
            content: The task content
            user_id: The user ID
            extend_info: Additional info
            step_number: Current step number in the workflow (1-indexed)
            total_steps: Total number of steps in the workflow
            task_content: The task description being processed
        """
        return RoomAgentMessage(
            room_id=room_id,
            related_message_id=related_message_id
            if related_message_id
            else str(uuid4()),
            agent_id=agent_id if agent_id else None,
            user_id=user_id,
            message_id=str(uuid4()),
            message_content=self._generate_agent_message_content(content),
            message_created_at=utcnow(),
            extend_info=extend_info if extend_info else None,
            step_number=step_number,
            total_steps=total_steps,
            task_content=task_content
            or content,  # Use task_content if provided, else content
            turn_id=turn_id,
            client_request_id=client_request_id,
        )

    def create_agent_message(
        self,
        room_id: str,
        related_message_id: str,
        agent_id: str,
        content: str,
        user_id: str | None = None,
        step_number: int | None = None,
        total_steps: int | None = None,
        task_content: str | None = None,
        turn_id: str | None = None,
        client_request_id: str | None = None,
    ) -> RoomAgentMessage:
        """Public wrapper around ``_generate_new_agent_message`` for use by
        the supervisor executor and other external callers that need to create
        individual agent messages without accessing a private method."""
        return self._generate_new_agent_message(
            room_id=room_id,
            related_message_id=related_message_id,
            agent_id=agent_id,
            content=content,
            user_id=user_id,
            step_number=step_number,
            total_steps=total_steps,
            task_content=task_content,
            turn_id=turn_id,
            client_request_id=client_request_id,
        )

    async def _generate_agent_messages_based_on_parsed_result(
        self,
        parsed_result: dict,
        user_message_id: str,
        room_id: str,
        user_id: str | None = None,
        extend_info: dict | None = None,
        turn_id: str | None = None,
        client_request_id: str | None = None,
    ) -> list[RoomAgentMessage]:
        """
        Generate agent messages based on parsed result from LLM.
        All steps are converted to agent messages, even if agent_id is None.

        Args:
            parsed_result: Output from parse_user_message_by_llm()
                {
                    "message_type": str,
                    "original_text": str,
                    "task_steps": [
                        {
                            "step_id": str,
                            "agent_id": str | None,
                            "agent_name": str | None,
                            "task_content": str,
                            "dependencies": [step_id, ...]
                        }
                    ]
                }
            user_message_id: User message ID (root for dependency chain)
            room_id: Room ID

        Returns:
            list[RoomAgentMessage]: Generated agent messages (agent_id may be None)
        """

        agent_messages = []
        task_steps = parsed_result.get("task_steps", [])

        if not task_steps:
            logger.warning("No task steps in parsed result")
            return agent_messages

        # In direct chat the single step's task_content is the raw user message,
        # which shouldn't be echoed in the task status bubble.
        is_direct_chat = parsed_result.get("message_type") == "DIRECT_CHAT"

        original_text = parsed_result.get("original_text", "")

        # Calculate total steps for progress tracking
        total_steps = len(task_steps)

        # Map step_id to generated agent_message_id for dependency resolution
        step_to_message_id = {}

        for step_index, step in enumerate(task_steps, start=1):
            step_id = step.get("step_id")
            agent_id = step.get("agent_id")  # Can be None
            task_content = step.get("task_content", "")
            dependencies = step.get("dependencies", [])

            # Skip only if no task content
            if not task_content:
                logger.warning(f"Step {step_id} has no task content, skipping")
                continue

            # Resolve related_message_id based on dependencies
            if not dependencies:
                # No dependencies: relate directly to user message
                related_message_id = user_message_id
            else:
                # Has dependencies: relate to the last dependency's agent message
                last_dependency_step_id = dependencies[-1]
                related_message_id = step_to_message_id.get(
                    last_dependency_step_id,
                    user_message_id,  # Fallback if dependency not found
                )

                # Log if dependency not found
                if last_dependency_step_id not in step_to_message_id:
                    logger.warning(
                        f"Step {step_id} depends on {last_dependency_step_id}, "
                        f"but it's not found. Using user message as fallback."
                    )

            # Create a2a Message with step tracking info
            agent_message = self._generate_new_agent_message(
                room_id,
                related_message_id,
                agent_id,
                task_content,
                user_id=user_id,
                extend_info=extend_info,
                step_number=step_index,
                total_steps=total_steps,
                turn_id=turn_id,
                client_request_id=client_request_id,
            )

            # In direct chat the task_content equals the user's original message,
            # which would be redundantly echoed in the task status bubble.
            # Clear it so the frontend shows a generic "Working on your request…" instead.
            # Also clear in multi-agent rooms when the LLM simply passed through the
            # user's message verbatim (no meaningful decomposition).
            if is_direct_chat or task_content.strip() == original_text.strip():
                agent_message.task_content = None

            agent_messages.append(agent_message)

            # Store mapping for dependency resolution
            step_to_message_id[step_id] = agent_message.message_id

            # Save to database
            agent_message_success = await self._store.add_room_agent_message(
                agent_message
            )
            if not agent_message_success:
                logger.warning(
                    f"Failed to add agent message {agent_message.message_id}"
                )

            logger.info(
                f"Generated agent message {agent_message.message_id} for step {step_id} ({step_index}/{total_steps})"
            )

        return agent_messages

    @staticmethod
    def _build_agent_registry(
        agents: list | None,
        selected_agent_set: dict,
    ) -> list:
        """Build an ``AgentProfile`` list from resolved agents or the agent set."""
        from models.supervisor import AgentProfile

        registry: list[AgentProfile] = []
        if agents:
            for agent in agents:
                registry.append(AgentProfile.from_agent(agent))
        else:
            for agent_id, agent_name in selected_agent_set.items():
                registry.append(
                    AgentProfile(
                        agent_id=agent_id,
                        agent_name=agent_name,
                        description="",
                        is_healthy=False,
                    )
                )
        return registry

    @staticmethod
    def _orchestration_request_info(
        request: RoomCenterUserMessageRequest,
    ) -> dict:
        info = request.extend_info if isinstance(request.extend_info, dict) else {}
        return dict(info)

    @classmethod
    def _selected_agent_ids_from_request(
        cls,
        request: RoomCenterUserMessageRequest,
    ) -> list[str] | None:
        scope = cls._orchestration_request_info(request).get("agent_scope")
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

    async def _resolve_selected_candidate_scope(
        self,
        selected_agent_ids: list[str],
        sender_user_id: str | None,
        required_input_modes: list[str] | None = None,
    ) -> ResolvedRoutingScope | RoomCenterUserMessageResponse:
        agents, invalid_ids = await self._sanitize_routing_scope(
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
            logger.warning(
                "Selected candidate targets rejected (invalid/unauthorized): %s",
                invalid_ids,
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

    async def _validate_candidate_scope_metadata(
        self,
        request: RoomCenterUserMessageRequest,
        selected_agent_ids: list[str],
        sender_user_id: str | None,
    ) -> RoomCenterUserMessageResponse | None:
        info = self._orchestration_request_info(request)
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

    async def _prepare_orchestration_envelope(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
        selected_agent_set: dict,
        explicit_mentions: list[dict] | None,
        client_request_id: str | None,
    ) -> ParseResult:
        info = self._orchestration_request_info(request)
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
            room_id=request.room_id,
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
                "execution_mode": "supervisor",
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
            logger.warning(
                "RoomServices: failed to persist orchestration envelope for message %s",
                user_message.message_id,
            )
            return ParseResult(success=False)
        logger.info(
            "RoomServices: orchestration envelope prepared for message %s (%d candidates)",
            user_message.message_id,
            len(selected_agent_set),
        )
        return ParseResult(success=True)

    async def parse_user_message(
        self,
        room_id: str,
        user_message_id: str,
        message_text: str,
        selected_agent_set: dict,
        user_id: str | None = None,
        auto_assign_agents: bool = False,
        target_group: str | None = None,
        agents: list | None = None,
        conversation_context: str | None = None,
        token: CancellationToken | None = None,
        client_request_id: str | None = None,
        explicit_mentions: list[dict] | None = None,
        required_input_modes: list[str] | None = None,
    ) -> ParseResult:
        """
        Parse user message

        Args:
            room_id: The room ID
            user_message_id: The user message ID
            message_text: The message text to parse
            selected_agent_set: Dict of {agent_id: agent_name} chosen for this request
            auto_assign_agents: If True (Auto mode), LLM will auto-assign agents
            agents: Full Agent objects for detailed LLM context (optional)
            explicit_mentions: Canonical agent mentions to include as routing intent

        Returns:
            ParseResult with ``success`` and ``canceled`` flags.  The caller
            is responsible for sending the appropriate SSE terminal status.
        """
        # Check for cancellation before parsing
        if token and token.is_cancelled:
            logger.info(
                "RoomServices: Message parsing cancelled for %s, stopping all processing",
                user_message_id,
            )
            return ParseResult(success=False, canceled=True)

        # Direct chat: a single Agent does not require LLM decomposition.
        direct_chat = len(selected_agent_set) == 1

        if direct_chat:
            agent_id, agent_name = next(iter(selected_agent_set.items()))
            parsed_result = {
                "message_type": "DIRECT_CHAT",
                "original_text": message_text,
                "needs_decomposition": False,
                "task_steps": [
                    {
                        "step_id": "step_1",
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "task_content": message_text,
                        "dependencies": [],
                    }
                ],
            }
            logger.info("Direct chat mode: skipping LLM parsing for single agent")
        else:
            # Parse user message with full agent details for better LLM assignment
            if self.message_parser_service is None:
                raise LLMServiceNotBoundError("MessageParserLLMService is not bound")
            parsed_result = await self.message_parser_service.parse_user_message(
                ParsedUserMessageRequest(
                    message_text=message_text,
                    selected_agents=selected_agent_set,
                    auto_assign_agents=auto_assign_agents,
                    agents=[_agent_to_routing_candidate(agent) for agent in agents],
                    conversation_context=conversation_context,
                    explicit_mentions=[
                        ExplicitAgentMention(
                            agent_id=str(mention.get("agent_id", "")),
                            agent_name=str(mention.get("agent_name", "")),
                            mention_text=mention.get("mention_text"),
                        )
                        for mention in (explicit_mentions or [])
                    ],
                )
            )

        logger.info(
            "message_parse_completed",
            extra={
                "outcome": "success" if parsed_result else "empty",
                "message_type": (
                    parsed_result.get("message_type") if parsed_result else None
                ),
                "step_count": (
                    len(parsed_result.get("task_steps", [])) if parsed_result else 0
                ),
            },
        )

        if not parsed_result:
            logger.warning("No parsed result from LLM")
            return ParseResult(success=False)

        extend_info = {
            "allowed_agent_ids": list(selected_agent_set.keys()),
            "target_group": target_group,
            "is_direct_chat": direct_chat,
        }
        if required_input_modes is not None:
            extend_info["required_input_modes"] = required_input_modes

        agent_messages = await self._generate_agent_messages_based_on_parsed_result(
            parsed_result,
            user_message_id,
            room_id,
            user_id=user_id,
            extend_info=extend_info,
            client_request_id=client_request_id,
        )

        return (
            ParseResult(success=True) if agent_messages else ParseResult(success=False)
        )

    async def get_idempotent_user_message(
        self,
        *,
        room_id: str,
        client_request_id: str,
        idempotency_fingerprint: str,
        idempotency_fingerprint_version: int,
    ) -> RoomCenterUserMessageResponse | None:
        """Return a stable replay/conflict response without running side effects."""

        existing = await self._require_facade().get_user_message_by_idempotency_key(
            room_id,
            client_request_id,
        )
        if existing is None:
            return None
        message_id = existing.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise UserMessagePersistenceError(
                "Idempotency record is missing a valid message_id"
            )

        stored_fingerprint = existing.get("idempotency_fingerprint")
        if stored_fingerprint is None:
            logger.warning(
                "Legacy idempotency replay without fingerprint "
                "room_id=%s client_request_id=%s message_id=%s",
                room_id,
                client_request_id,
                message_id,
            )
        elif not stored_fingerprint_matches(
            existing,
            fingerprint=idempotency_fingerprint,
            fingerprint_version=idempotency_fingerprint_version,
        ):
            return RoomCenterUserMessageResponse(
                room_id=room_id,
                message_id=None,
                message=None,
                success=False,
                error=(
                    "The client_request_id was already used for a different request"
                ),
                status_code=409,
            )

        return RoomCenterUserMessageResponse(
            room_id=room_id,
            message_id=message_id,
            dispatch_root_message_id=None,
            message=None,
            success=True,
            error=None,
            status_code=200,
        )

    async def send_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: str = "room_team",
        mentioned_agent_ids: list[str] | None = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> RoomCenterUserMessageResponse:
        """Add and parse user message to room and return execution preflight metadata."""
        persisted_response, preflight_context = await self.persist_message_to_room(
            request,
            target_group,
            mentioned_agent_ids,
            idempotency_fingerprint=idempotency_fingerprint,
            idempotency_fingerprint_version=idempotency_fingerprint_version,
        )
        if preflight_context is None:
            return persisted_response
        return await self.run_message_preflight_to_room(preflight_context)

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
        if (
            client_request_id
            and idempotency_fingerprint is not None
            and idempotency_fingerprint_version is not None
        ):
            replay = await self.get_idempotent_user_message(
                room_id=request.room_id or "",
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
            user_message.room_id = request.room_id
            user_message.user_id = request.user_id
            user_message.client_request_id = client_request_id
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
                extend_info = (
                    user_message.extend_info
                    if isinstance(user_message.extend_info, dict)
                    else {}
                )
                legacy_quote_keys = (
                    ()
                    if user_message.quote is not None
                    else ("quoted_text", "quoted_sender_name")
                )
                user_message.extend_info = {
                    key: value
                    for key in legacy_quote_keys
                    if isinstance((value := extend_info.get(key)), str)
                } or None

        # Resolve attachments from both sources before persistence
        att_err = await self._resolve_and_apply_attachments(request, user_message)
        if att_err is not None:
            return att_err, None

        # ── Pre-persist scope validation ──────────────────────────────────
        # Fetch room early: needed for scope resolution and downstream flags.
        room = await self._store.get_room_by_room_id(request.room_id)
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

        orchestration_info = self._orchestration_request_info(request)
        execution_mode = orchestration_info.get("execution_mode")
        if execution_mode not in {"direct", "supervisor"}:
            execution_mode = "direct"
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
        selected_agent_ids = self._selected_agent_ids_from_request(request)
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
        logger.info(
            "room_send_message_persist_started room_id=%s user_id=%s "
            "client_request_id=%s target_group=%s "
            "candidate_scope_mode=%s "
            "selected_count=%d mentioned_count=%d message_len=%d",
            request.room_id,
            request.user_id,
            client_request_id,
            target_group,
            candidate_scope_mode,
            len(selected_agent_ids or []),
            len(mentioned_agent_ids or []),
            len(message_text or ""),
        )

        if selected_agent_ids is not None:
            metadata_error = await self._validate_candidate_scope_metadata(
                request,
                selected_agent_ids,
                sender_user_id=request.user_id,
            )
            if metadata_error is not None:
                return metadata_error, None
            scope_result = await self._resolve_selected_candidate_scope(
                selected_agent_ids,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(scope_result, RoomCenterUserMessageResponse):
                return scope_result, None
            pre_resolved_selected_scope = scope_result
            if mentioned_agent_ids:
                mention_result = await self._validate_canonical_mentions(
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
            mention_result = await self._validate_canonical_mentions(
                mentioned_agent_ids,
                sender_user_id=request.user_id,
                required_input_modes=None,
            )
            if isinstance(mention_result, RoomCenterUserMessageResponse):
                return mention_result, None
            pre_resolved_mentions = mention_result
        elif target_group != "all_agents":
            scope_result = await self._resolve_explicit_target_scope(
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
                    room_id=request.room_id,
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
                    room_id=request.room_id,
                    message_id=persistence.message_id,
                    dispatch_root_message_id=None,
                    message=None,
                    success=True,
                    error=None,
                    status_code=200,
                ),
                None,
            )

        # Create a CancellationToken early in the pipeline so the parse step
        # (and later the queue step) can detect cancels
        # via the token.  If the user already hit cancel before we got here,
        # the token is pre-signalled.
        token = self.cancellation_control.create_token(user_message.message_id)
        try:
            # Hydrate an L1 miss before the long post-persistence parse begins.
            # check_cancelled signals the newly owned token when Redis has a tombstone.
            await self.cancellation_control.check_cancelled(user_message.message_id)
        except BaseException:
            self._release_cancellation_token(user_message.message_id, token)
            raise
        logger.info(
            "room_send_message_persisted room_id=%s message_id=%s "
            "client_request_id=%s target_group=%s",
            request.room_id,
            user_message.message_id,
            client_request_id,
            target_group,
        )

        return (
            RoomCenterUserMessageResponse(
                room_id=request.room_id,
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
                token=token,
            ),
        )

    async def run_message_preflight_to_room(
        self,
        context: RoomMessagePreflightContext,
    ) -> RoomCenterUserMessageResponse:
        try:
            return await self._run_message_preflight_to_room(context)
        finally:
            # The preflight token never crosses into orchestration. A ready ack
            # starts a separate execution which creates and hydrates its own token.
            self.discard_message_preflight(context)

    def discard_message_preflight(
        self,
        context: RoomMessagePreflightContext,
    ) -> None:
        self._release_cancellation_token(
            context.user_message.message_id,
            getattr(context, "token", None),
        )

    async def _run_message_preflight_to_room(
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

            mentions = self.parse_agent_mentions(message_text, effective_agent_set)

            if mentions:
                mention_agents, _rejected = await self._sanitize_routing_scope(
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
            selection_result = await self._resolve_explicit_target_scope(
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

        logger.info(
            "room_send_message_preflight_ready room_id=%s message_id=%s "
            "client_request_id=%s target_group=%s candidate_count=%d auto_assign=%s",
            request.room_id,
            user_message.message_id,
            client_request_id,
            target_group,
            len(selected_agent_set),
            auto_assign,
        )

        # Orchestration assembles its own context from the durable run state.
        parse_result = await self._prepare_orchestration_envelope(
            request=request,
            user_message=user_message,
            selected_agent_set=selected_agent_set,
            explicit_mentions=pre_resolved_mentions,
            client_request_id=client_request_id,
        )

        if not parse_result.success:
            logger.warning(
                "room_send_message_preflight_failed room_id=%s message_id=%s "
                "client_request_id=%s canceled=%s",
                request.room_id,
                user_message.message_id,
                client_request_id,
                parse_result.canceled,
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

        logger.info(
            "room_send_message_preflight_completed room_id=%s message_id=%s "
            "client_request_id=%s preflight_outcome=ready",
            request.room_id,
            user_message.message_id,
            client_request_id,
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
        """Validate required fields for send_message_to_room."""
        if request.room_id is None:
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
        except Exception:
            logger.warning(
                "Failed to remove uncommitted quoted snippet %s for room %s",
                quote_id,
                user_message.room_id,
                exc_info=True,
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

    async def _handle_mentions_flow(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
        mentions: list[dict],
    ) -> RoomCenterUserMessageResponse:
        """Deterministically fan out to mentioned agents and finish.

        Note: Does NOT send processing_status COMPLETED here — the actual agent
        execution happens in a background task (process_room_user_message) which
        sends COMPLETED when all agents finish.  Sending it here would prematurely
        clear the frontend processing state and hide the Stop button.
        """
        room = await self._store.get_room_by_room_id(request.room_id)
        mention_response = await self.parse_user_message_with_mentions(
            room, user_message, mentions
        )
        return mention_response

    async def _validate_canonical_mentions(
        self,
        mentioned_agent_ids: list[str],
        sender_user_id: str | None,
        required_input_modes: list[str] | None = None,
    ) -> list[dict] | RoomCenterUserMessageResponse:
        """Validate and resolve mentioned_agent_ids into canonical mention dicts.

        Returns the mention list on success, or an error response on failure.
        Called once before persistence; the result is reused downstream.
        """
        agents, invalid_ids = await self._sanitize_routing_scope(
            mentioned_agent_ids,
            sender_user_id=sender_user_id,
            required_input_modes=required_input_modes,
        )
        canonical_mentions = [
            (
                {
                    "agent_id": agent.agent_id,
                    "agent_name": agent.agent_card.name,
                    "mention_text": (f"<@{agent.agent_id}|{agent.agent_card.name}>"),
                }
            )
            for agent in agents
        ]

        if invalid_ids:
            error_msg = (
                f"Invalid or unauthorized mention targets: {', '.join(invalid_ids)}"
            )
            logger.warning(
                "Canonical mention targets rejected (invalid/unauthorized): %s",
                invalid_ids,
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error=error_msg,
                scope_resolution_error=ScopeResolutionError(
                    code="unauthorized_mention",
                    message=error_msg,
                ),
                status_code=400,
            )
        return canonical_mentions

    async def _resolve_explicit_target_scope(
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
                full_agents, _rejected = await self._sanitize_routing_scope(
                    active_ids,
                    sender_user_id=sender_user_id,
                    required_input_modes=required_input_modes,
                )
                selected = {
                    agent.agent_id: agent.agent_card.name for agent in full_agents
                }
                logger.info(
                    "All Agents mode: Providing all %s active agents to Supervisor",
                    len(full_agents),
                )
                return ResolvedRoutingScope(
                    selected_agent_set=selected,
                    auto_assign_agents=True,
                    agents=full_agents,
                )
            except Exception as e:
                error_msg = "Agent selection failed. Please try again."
                logger.error(
                    "All Agents mode selection failed: %s — returning scope error", e
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
                logger.info(
                    "Room Default mode: Using %s room agents as candidate scope",
                    len(room.room_agent_set),
                )
                room_agents, _rejected = await self._sanitize_routing_scope(
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
                return ResolvedRoutingScope(
                    selected_agent_set=selected_agent_set,
                    auto_assign_agents=True,
                    agents=room_agents,
                )

            error_msg = "This room has no agents. Add agents before sending a message."
            logger.warning(
                "Room Default mode: room has no agents — returning scope-resolution error"
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
            logger.warning(
                "Custom group %s not found — returning scope-resolution error",
                target_group,
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
            logger.warning(
                "Sender %s not authorized to use saved group %s (owner: %s)",
                sender_user_id,
                target_group,
                group.owner_id,
            )
            return RoomCenterUserMessageResponse(
                message_id=None,
                message=None,
                success=False,
                error="You do not have permission to use this saved group",
                status_code=403,
            )

        if group.agents:
            agents, _rejected = await self._sanitize_routing_scope(
                group.agents,
                sender_user_id=sender_user_id,
                required_input_modes=required_input_modes,
            )

            selected_agent_set = {
                agent.agent_id: agent.agent_card.name for agent in agents
            }
            logger.info(
                "Custom group '%s': Using %s agents",
                group.name,
                len(selected_agent_set),
            )
            return ResolvedRoutingScope(
                selected_agent_set=selected_agent_set,
                auto_assign_agents=True,
                agents=agents,
            )

        error_msg = f"The selected agent group '{group.name}' has no members."
        logger.warning(
            "Custom group '%s' has no agents — returning scope-resolution error",
            group.name,
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

    async def _resolve_room_agent_refs(
        self, agent_set: dict | None, viewer_user_id: str | None = None
    ) -> tuple[list[RoomAgentRef], str]:
        """Resolve agent IDs into RoomAgentRef objects and compute room_default_status.

        Args:
            agent_set: {agent_id: agent_name} dict from the room snapshot.
            viewer_user_id: The current user requesting the read. When provided,
                private agents not owned by this user are marked ``inaccessible``.

        Returns (resolved_agents, room_default_status).
        """
        if not agent_set:
            return [], "empty"

        refs: list[RoomAgentRef] = []
        for agent_id, agent_name in agent_set.items():
            agent = await self._store.get_agent_by_agent_id(agent_id)
            if not agent:
                refs.append(
                    RoomAgentRef(id=agent_id, name=agent_name, availability="deleted")
                )
            elif agent.agent_status != AgentStatus.active:
                refs.append(
                    RoomAgentRef(
                        id=agent_id, name=agent.agent_card.name, availability="inactive"
                    )
                )
            elif (
                not agent.is_public
                and viewer_user_id
                and getattr(agent, "provider_id", None) != viewer_user_id
            ):
                refs.append(
                    RoomAgentRef(
                        id=agent_id,
                        name=agent.agent_card.name,
                        availability="inaccessible",
                    )
                )
            else:
                refs.append(
                    RoomAgentRef(
                        id=agent_id,
                        name=agent.agent_card.name,
                        availability="available",
                    )
                )

        available_count = sum(1 for r in refs if r.availability == "available")
        if available_count == len(refs):
            status = "ok"
        elif available_count > 0:
            status = "degraded"
        else:
            status = "all_unavailable"

        return refs, status

    async def _handle_no_agents_fallback(
        self,
        request: RoomCenterUserMessageRequest,
        user_message: RoomUserMessage,
        target_group: str,
    ) -> RoomCenterUserMessageResponse:
        """Send a system message when no agents are available."""
        logger.warning(
            "No room agents and none found via selection; sending system agent response"
        )

        fallback_agent_message = self._generate_new_agent_message(
            room_id=request.room_id,
            related_message_id=user_message.message_id,
            agent_id=CoordinatorAgentId.SYSTEM,
            content=(
                "I couldn't find any agents for this room or via selection. "
                "Please choose agents or a group and try again."
            ),
            user_id=user_message.user_id,
            extend_info={
                "system_fallback": True,
                "reason": "no_agents_found",
                "target_group": target_group,
            },
            client_request_id=user_message.client_request_id,
        )

        added = await self._store.add_room_agent_message(fallback_agent_message)
        if not added:
            return RoomCenterUserMessageResponse(
                message_id=user_message.message_id,
                message=user_message,
                success=False,
                error="Failed to add fallback agent message",
                status_code=500,
                preflight_outcome="failed",
                preflight_details="Failed to add fallback agent message",
            )

        return RoomCenterUserMessageResponse(
            message_id=user_message.message_id,
            message=user_message,
            success=True,
            error=None,
            status_code=200,
            preflight_outcome="completed",
        )

    async def parse_user_message_with_mentions(
        self,
        room: Room,
        message: RoomUserMessage,
        mentions: list[dict],
    ) -> RoomCenterUserMessageResponse:
        """Create deterministic tasks for inline mentions without LLM routing."""
        room_id = room.room_id

        # Group mentions by context and detect consecutive patterns
        context_groups = self.group_mentions_by_context(
            message.message_content.message_text, mentions
        )

        created_agent_messages = []
        failed_message_count = 0
        failed_context_count = 0
        for context_text, group_info in context_groups.items():
            mentions_in_context = group_info["mentions"]
            is_consecutive = group_info["is_consecutive"]

            try:
                # Create shared message content for this context
                shared_content = self.create_shared_message_content(
                    context_text, mentions_in_context
                )

                # Create tasks for all agents in this context
                tasks_group = await self.create_task_for_agents_group(
                    message, mentions_in_context, shared_content
                )

                if is_consecutive:
                    # Consecutive mentions: chain dependencies in order
                    previous_message_id = (
                        message.message_id
                    )  # Start with user message ID

                    for _i, task_info in enumerate(tasks_group):
                        agent_message = RoomAgentMessage(
                            room_id=room_id,
                            message_id=str(uuid4()),
                            related_message_id=previous_message_id,
                            agent_id=task_info["agent_id"],
                            user_id=message.user_id,
                            client_request_id=message.client_request_id,
                            message_content=MessageContent(
                                message_task=task_info["task"]
                            ),
                            message_created_at=utcnow(),
                            task_content=shared_content,
                        )

                        agent_message_success = (
                            await self._store.add_room_agent_message(agent_message)
                        )
                        if agent_message_success:
                            created_agent_messages.append(agent_message)
                            previous_message_id = agent_message.message_id
                        else:
                            failed_message_count += 1
                else:
                    # Non-consecutive: relate all to the user message
                    for task_info in tasks_group:
                        agent_message = RoomAgentMessage(
                            room_id=room_id,
                            message_id=str(uuid4()),
                            related_message_id=message.message_id,
                            agent_id=task_info["agent_id"],
                            user_id=message.user_id,
                            client_request_id=message.client_request_id,
                            message_content=MessageContent(
                                message_task=task_info["task"]
                            ),
                            message_created_at=utcnow(),
                            task_content=shared_content,
                        )

                        agent_message_success = (
                            await self._store.add_room_agent_message(agent_message)
                        )
                        if agent_message_success:
                            created_agent_messages.append(agent_message)
                        else:
                            failed_message_count += 1

            except Exception:
                failed_context_count += 1
                logger.warning(
                    "Mention fan-out context failed room_id=%s message_id=%s",
                    room_id,
                    message.message_id,
                    exc_info=True,
                )

        if not created_agent_messages:
            error = "Failed to create agent messages for mentioned agents"
            logger.error(
                "Mention fan-out created no agent messages room_id=%s message_id=%s "
                "failed_messages=%d failed_contexts=%d",
                room_id,
                message.message_id,
                failed_message_count,
                failed_context_count,
            )
            return RoomCenterUserMessageResponse(
                message_id=message.message_id,
                message=message,
                success=False,
                error=error,
                status_code=500,
                preflight_outcome="failed",
                preflight_details=error,
            )

        if failed_message_count or failed_context_count:
            logger.warning(
                "Mention fan-out partially persisted room_id=%s message_id=%s "
                "created=%d failed_messages=%d failed_contexts=%d",
                room_id,
                message.message_id,
                len(created_agent_messages),
                failed_message_count,
                failed_context_count,
            )

        return RoomCenterUserMessageResponse(
            message_id=message.message_id,
            dispatch_root_message_id=message.message_id,
            message=message,
            success=True,
            error=None,
            status_code=200,
            preflight_outcome="ready",
        )

    async def _build_room_awareness(self, *args, **kwargs):
        return await self._require_agent_message_preparation()._build_room_awareness(
            *args, **kwargs
        )

    def _build_agent_execution_context_from_memory(self, **kwargs) -> str:
        return self._require_agent_message_preparation()._build_agent_execution_context_from_memory(
            **kwargs
        )

    async def process_agent_message(
        self,
        request: RoomCenterAgentMessageRequest,
        room_memory: "RoomMemory | None" = None,
        quoted_text: str | None = None,
        orchestration_user_message_id: str | None = None,
    ) -> RoomCenterAgentMessageResponse:
        return await self._require_agent_message_preparation().process_agent_message(
            request,
            room_memory=room_memory,
            quoted_text=quoted_text,
            orchestration_user_message_id=orchestration_user_message_id,
        )

    async def update_agent_message_by_message_id(
        self, request: RoomCenterAgentMessageRequest
    ) -> RoomCenterAgentMessageResponse:
        if request.message_id is None:
            return RoomCenterAgentMessageResponse(
                message=None,
                success=False,
                error="Message id is required",
                status_code=400,
            )

        message_id = request.message_id
        message = request.message
        if message is None:
            return RoomCenterAgentMessageResponse(
                message=None,
                success=False,
                error="Message is required",
                status_code=400,
            )

        update_message_success = await self._require_facade().update_agent_message(
            message_id,
            message,
        )
        if update_message_success:
            return RoomCenterAgentMessageResponse(
                message=message, success=True, error=None, status_code=200
            )
        else:
            return RoomCenterAgentMessageResponse(
                message=None,
                success=False,
                error="Failed to update message",
                status_code=500,
            )

    async def inquiry_user_messages_by_room_id(
        self, request: RoomCenterUserMessageRequest
    ) -> RoomCenterUserMessageResponse:
        if request.room_id is None:
            return RoomCenterUserMessageResponse(
                message_list=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        messages = await self._require_facade().get_user_messages_for_room(room_id)

        for msg in messages:
            if msg.message_content and msg.message_content.attachments:
                for att in msg.message_content.attachments:
                    try:
                        room_files = self.room_files
                    except RuntimeError:
                        room_files = None
                    att.file_url = (
                        await room_files.get_url(att.file_id)
                        if room_files is not None
                        else att.file_url or f"/api/v1/files/{att.file_id}/content"
                    )

        return RoomCenterUserMessageResponse(
            message_list=messages, success=True, error=None, status_code=200
        )

    async def inquiry_agent_messages_by_room_id(
        self, request: RoomCenterAgentMessageRequest
    ) -> RoomCenterAgentMessageResponse:
        if request.room_id is None:
            return RoomCenterAgentMessageResponse(
                message_list=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        room_id = request.room_id
        messages = await self._require_facade().get_agent_messages_for_room(room_id)

        return RoomCenterAgentMessageResponse(
            message_list=messages, success=True, error=None, status_code=200
        )

    async def inquiry_agent_message_by_message_id(
        self, request: RoomCenterAgentMessageRequest
    ) -> RoomCenterAgentMessageResponse:
        if request.message_id is None:
            return RoomCenterAgentMessageResponse(
                message=None,
                success=False,
                error="Message id is required",
                status_code=400,
            )

        message_id = request.message_id
        message = await self._require_facade().get_agent_message_model(message_id)
        return RoomCenterAgentMessageResponse(
            message=message, success=True, error=None, status_code=200
        )

    async def inquiry_user_message_by_message_id(
        self, request: RoomCenterUserMessageRequest
    ) -> RoomCenterUserMessageResponse:
        if request.message_id is None:
            return RoomCenterUserMessageResponse(
                message=None,
                success=False,
                error="Message id is required",
                status_code=400,
            )

        message_id = request.message_id
        message = await self._require_facade().get_user_message_model(message_id)
        return RoomCenterUserMessageResponse(
            message=message, success=True, error=None, status_code=200
        )

    async def inquiry_agent_messages_by_related_message_id(
        self, request: RoomCenterAgentMessageRequest
    ) -> RoomCenterAgentMessageResponse:
        if request.related_message_id is None:
            return RoomCenterAgentMessageResponse(
                message_list=None,
                success=False,
                error="Related message id is required",
                status_code=400,
            )

        related_message_id = request.related_message_id
        messages = (
            await self._require_facade().get_agent_messages_by_related_message_id(
                related_message_id
            )
        )
        return RoomCenterAgentMessageResponse(
            message_list=messages, success=True, error=None, status_code=200
        )

    async def inquiry_room_messages_by_room_id(
        self, request: RoomCenterRoomMessageRequest
    ) -> RoomCenterRoomMessageResponse:
        """
        Retrieve all messages in a room, including user messages and agent messages.
        For user messages: return message_text from message_content
        For agent messages: return text from sanitized completed artifacts.
        Sort by creation time and return
        """
        if request.room_id is None:
            return RoomCenterRoomMessageResponse(
                message_list=None,
                success=False,
                error="Room id is required",
                status_code=400,
            )

        try:
            room_id = request.room_id

            limit = request.limit if request.limit is not None else 200
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 200
            ):
                return RoomCenterRoomMessageResponse(
                    room_id=room_id,
                    message_list=None,
                    success=False,
                    error="limit must be an integer between 1 and 200",
                    status_code=400,
                )
            try:
                before = (
                    decode_timeline_cursor(request.cursor, room_id=room_id)
                    if request.cursor is not None
                    else None
                )
            except TimelineCursorError:
                return RoomCenterRoomMessageResponse(
                    room_id=room_id,
                    message_list=None,
                    success=False,
                    error="Invalid timeline cursor",
                    status_code=400,
                )

            page = await self._require_facade().get_timeline_page(
                room_id,
                limit=limit,
                before=before,
            )
            combined_messages = await self._require_timeline_projector().project(page)
            next_cursor = (
                encode_timeline_cursor(room_id, page.next_position)
                if page.has_more and page.next_position is not None
                else None
            )
            return RoomCenterRoomMessageResponse(
                room_id=room_id,
                message_list=combined_messages,
                has_more=page.has_more,
                next_cursor=next_cursor,
                success=True,
                error=None,
                status_code=200,
            )

        except Exception:
            logger.error(
                "Failed to retrieve room timeline room_id=%s",
                request.room_id,
                exc_info=True,
            )
            return RoomCenterRoomMessageResponse(
                room_id=request.room_id,
                message_list=None,
                success=False,
                error="Failed to retrieve room timeline",
                status_code=500,
            )

    async def update_user_message_orchestration_status(
        self,
        message_id: str,
        status: str,
    ) -> bool:
        """Persist the public orchestration status on a user message."""
        user_message = await self._store.get_room_user_message_by_message_id(message_id)
        if user_message is None:
            return False
        if not isinstance(user_message.extend_info, dict):
            return False
        if not (
            user_message.extend_info.get("orchestration") is True
            or user_message.extend_info.get("orchestration_run_id")
        ):
            return True
        terminal = status in {
            terminal_status.value for terminal_status in TERMINAL_ORCHESTRATION_STATUSES
        }
        already_projected = user_message.extend_info.get(
            "orchestration_status"
        ) == status and (not terminal or user_message.processing_claimed_at is None)
        if already_projected:
            return True

        user_message.extend_info["orchestration_status"] = status
        if terminal:
            user_message.processing_claimed_at = None
        updated = await self._store.update_room_user_message_by_message_id(
            message_id,
            user_message,
        )
        if updated:
            return True

        # Mongo reports a no-op write as unmodified. A concurrent or replayed
        # projection is successful when the persisted envelope is already at
        # the requested state.
        persisted = await self._store.get_room_user_message_by_message_id(message_id)
        return bool(
            persisted is not None
            and isinstance(persisted.extend_info, dict)
            and persisted.extend_info.get("orchestration_status") == status
            and (not terminal or persisted.processing_claimed_at is None)
        )


# Singleton export
room_runtime = RoomServices()
room_services = room_runtime


__all__ = [
    "RoomServices",
    "_ResolvedAttachments",
    "_human_size",
    "build_turn_content",
    "room_runtime",
    "room_services",
]
