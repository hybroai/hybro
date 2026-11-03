from __future__ import annotations

import inspect
from typing import Protocol

from common.a2a_task_projection import (
    extract_public_completed_status_text,
    public_persisted_task_data,
    resolve_public_agent_response_text,
    resolve_public_task_label,
)
from common.observability import events as E
from common.public_prompt import concrete_agent_input_prompt
from common.types import Artifact, Part, Task, TaskState, TextPart
from common.utils.a2a_helpers import get_message_from_task, get_text_from_message
from common.utils.logger import get_logger
from conversation.attachments import AttachmentMetadataReader
from conversation.dto import RoomTimelinePage
from models.room import (
    CoordinatorAgentId,
    MessageContent,
    RoomAgentMessage,
    RoomMessage,
    RoomUserMessage,
)

logger = get_logger(__name__)

_PUBLIC_USER_MESSAGE_EXTEND_INFO_STRING_KEYS = (
    "quoted_text",
    "quoted_sender_name",
    "quote_id",
)
_PUBLIC_ORCHESTRATION_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "budget_exhausted": "failed",
}
_GENERIC_AGENT_INPUT_PROMPT = "The agent needs additional information."


class HITLProjectionReader(Protocol):
    async def get_hitl_request(self, request_id: str) -> dict | None: ...


def _public_agent_name(extend_info: object) -> str | None:
    if not isinstance(extend_info, dict):
        return None
    value = extend_info.get("public_agent_name")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:160]


def _public_user_message_extend_info(extend_info: object) -> dict[str, str] | None:
    if not isinstance(extend_info, dict):
        return None

    public_extend_info = {
        key: value
        for key in _PUBLIC_USER_MESSAGE_EXTEND_INFO_STRING_KEYS
        if isinstance((value := extend_info.get(key)), str)
    }
    orchestration_status = _PUBLIC_ORCHESTRATION_STATUS_MAP.get(
        extend_info.get("orchestration_status")
    )
    if orchestration_status is not None:
        public_extend_info["orchestration_status"] = orchestration_status

    return public_extend_info or None


class RoomTimelineProjector:
    def __init__(
        self,
        *,
        hitl_reader: HITLProjectionReader,
        attachment_metadata_reader: AttachmentMetadataReader,
    ) -> None:
        self._hitl_reader = hitl_reader
        self._attachment_metadata_reader = attachment_metadata_reader

    async def trusted_hitl_projection(  # noqa: C901
        self,
        agent_message: RoomAgentMessage,
        task: Task,
    ) -> tuple[dict[str, object] | None, str | None]:
        metadata = task.metadata
        request_id = (
            metadata.get("hitl_request_id") if isinstance(metadata, dict) else None
        )
        if not isinstance(request_id, str) or not request_id:
            return None, None

        try:
            request = self._hitl_reader.get_hitl_request(request_id)
            if inspect.isawaitable(request):
                request = await request
        except Exception as exc:
            logger.emit(
                E.HITL_PROJECTION_VERIFICATION_FAILED,
                exc=exc,
                conversation_id=agent_message.conversation_id,
                message_id=agent_message.message_id,
                hitl_request_id=request_id,
            )
            return None, None

        if not isinstance(request, dict):
            return None, None
        if (
            request.get("request_id") != request_id
            or request.get("room_id") != agent_message.conversation_id
            or request.get("public_source") not in {"agent", "supervisor", "system"}
            or request.get("status") in {"canceled", "expired"}
        ):
            return None, None
        projected_message_id = request.get("display_message_id") or request.get(
            "continuation_message_id"
        )
        if projected_message_id != agent_message.message_id:
            return None, None
        request_agent_id = request.get("agent_id")
        if request_agent_id is not None and request_agent_id != agent_message.agent_id:
            return None, None
        request_task_id = request.get("a2a_task_id")
        if request_task_id is not None and request_task_id != task.id:
            return None, None
        request_context_id = request.get("a2a_context_id")
        if request_context_id is not None and request_context_id != task.context_id:
            return None, None

        prompt = request.get("prompt")
        prompt_type = getattr(
            request.get("prompt_type"), "value", request.get("prompt_type")
        )
        choices = request.get("choices")
        if request.get("public_source") == "agent":
            prompt = concrete_agent_input_prompt(prompt)
            if prompt is None:
                prompt = _GENERIC_AGENT_INPUT_PROMPT
                prompt_type = "text"
                choices = None

        trusted: dict[str, object] = {
            "hitl_request_id": request_id,
            "hitl_prompt": prompt,
            "hitl_prompt_type": prompt_type,
            "hitl_choices": choices,
        }
        question_count = int(request.get("question_count") or 1)
        optional_fields = {
            "hitl_a2a_task_id": request.get("a2a_task_id"),
            "hitl_a2a_context_id": request.get("a2a_context_id"),
            "hitl_interaction_id": request.get("interaction_id"),
            "hitl_question_count": question_count,
            "hitl_question_index": int(request.get("question_index") or 0),
            "user_answer": request.get("user_input"),
        }
        trusted.update(
            {key: value for key, value in optional_fields.items() if value is not None}
        )
        return trusted, request_id

    async def project(self, page: RoomTimelinePage) -> list[RoomMessage]:
        projected: list[RoomMessage] = []
        for entry in page.entries:
            if entry.source == "user":
                projected.append(await self._project_user_message(entry.message))
            else:
                projected.append(await self._project_agent_message(entry.message))
        return projected

    async def _project_user_message(self, user_msg: RoomUserMessage) -> RoomMessage:
        message_content = (
            user_msg.message_content.model_copy(deep=True)
            if user_msg.message_content is not None
            else None
        )
        if message_content is not None and message_content.attachments:
            for attachment in message_content.attachments:
                metadata = await self._attachment_metadata_reader.get_for_room_file(
                    user_msg.conversation_id,
                    attachment.file_id,
                )
                attachment.file_url = (
                    metadata.get("content_url")
                    if isinstance(metadata, dict)
                    and metadata.get("status") == "ready"
                    and isinstance(metadata.get("content_url"), str)
                    else None
                )
        return RoomMessage(
            conversation_id=user_msg.conversation_id,
            message_id=user_msg.message_id,
            client_request_id=user_msg.client_request_id,
            message_type="user",
            message_content=message_content,
            message_created_at=user_msg.message_created_at,
            user_id=user_msg.user_id,
            extend_info=_public_user_message_extend_info(user_msg.extend_info),
        )

    async def _project_agent_message(  # noqa: C901
        self, agent_msg: RoomAgentMessage
    ) -> RoomMessage:
        stored_task = (
            agent_msg.message_content.message_task
            if agent_msg.message_content is not None
            else None
        )
        trusted_hitl_metadata = None
        trusted_hitl_request_id = None
        if stored_task is not None:
            (
                trusted_hitl_metadata,
                trusted_hitl_request_id,
            ) = await self.trusted_hitl_projection(agent_msg, stored_task)
            public_task_data = public_persisted_task_data(stored_task)
            if trusted_hitl_metadata is not None:
                public_task_data["metadata"] = trusted_hitl_metadata
            public_task = Task.model_validate(public_task_data)
        else:
            public_task = None

        preferred_agent_content = (
            extract_public_completed_status_text(stored_task)
            if stored_task is not None
            else None
        )
        fallback_agent_content = (
            get_text_from_message(get_message_from_task(public_task))
            if public_task is not None
            else None
        )
        agent_content = resolve_public_agent_response_text(
            agent_msg,
            preferred_text=preferred_agent_content,
            fallback_text=fallback_agent_content,
        )
        public_state = (
            getattr(public_task.status.state, "value", public_task.status.state)
            if public_task is not None and public_task.status is not None
            else None
        )
        if (
            agent_content
            and public_task is not None
            and public_state == TaskState.completed.value
            and not public_task.artifacts
        ):
            public_task.artifacts = [
                Artifact(
                    artifact_id=f"{agent_msg.message_id}-legacy-response",
                    name="response",
                    parts=[Part(root=TextPart(text=agent_content))],
                )
            ]

        is_system_hybro = agent_msg.agent_id == CoordinatorAgentId.SYSTEM_HYBRO
        public_task_label = None
        public_extend_info: dict[str, object] = {}
        if not is_system_hybro:
            public_task_label = resolve_public_task_label(
                agent_msg.extend_info,
                agent_msg.agent_id or "agent",
            )
            public_extend_info["public_task_label"] = public_task_label
            public_agent_name = _public_agent_name(agent_msg.extend_info)
            if public_agent_name is not None:
                public_extend_info["public_agent_name"] = public_agent_name
            public_dispatch_text = (
                agent_msg.extend_info.get("public_dispatch_text")
                if isinstance(agent_msg.extend_info, dict)
                else None
            )
            if isinstance(public_dispatch_text, str) and public_dispatch_text.strip():
                public_extend_info["public_dispatch_text"] = (
                    public_dispatch_text.strip()
                )
        elif isinstance(agent_msg.extend_info, dict):
            for key in (
                "is_coordinator_summary",
                "source_user_message_id",
                "summary_origin",
                "summary_type",
            ):
                value = agent_msg.extend_info.get(key)
                if value is not None:
                    public_extend_info[key] = value
        if trusted_hitl_request_id is not None:
            public_extend_info["hitl_request_id"] = trusted_hitl_request_id

        return RoomMessage(
            conversation_id=agent_msg.conversation_id,
            message_id=agent_msg.message_id,
            client_request_id=agent_msg.client_request_id,
            message_type="agent",
            message_content=MessageContent(
                message_text=agent_content or "",
                message_task=public_task,
            ),
            message_created_at=agent_msg.message_created_at,
            agent_id=agent_msg.agent_id,
            related_message_id=agent_msg.related_message_id,
            step_number=agent_msg.step_number,
            total_steps=agent_msg.total_steps,
            task_updated_at=agent_msg.task_updated_at,
            task_content=public_task_label,
            extend_info=public_extend_info or None,
        )
