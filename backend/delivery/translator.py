from datetime import datetime
from typing import Any

from common.dto import (
    AgentMessageFinal,
    AgentMessagePartial,
    ArtifactUpdateEvent,
    CancellationEvent,
    DeliveryEvent,
    ErrorEvent,
    HITLRequestEvent,
    HITLResolvedEvent,
    ProcessingStatusEvent,
    RunEventNotification,
    TaskSubmittedEvent,
    TaskUpdateEvent,
)


def to_sse_frame(
    event: DeliveryEvent,
    *,
    timestamp: datetime,
    room_seq: int | None = None,
    room_event_id: str | None = None,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    frame_timestamp = timestamp.isoformat()
    if isinstance(event, ProcessingStatusEvent):
        data: dict[str, Any] = {
            "status": event.status,
            "message_id": event.message_id,
            "details": event.details,
        }
        _add_optional(data, "agent_id", event.agent_id)
        _add_optional(data, "related_message_id", event.related_message_id)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_optional(data, "agents", event.agents)
        _add_optional(data, "delivery_id", event.delivery_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "processing_status",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, RunEventNotification):
        data = {
            "event_id": event.event_id,
            "run_id": event.run_id,
            "seq": event.seq,
            "type": event.run_event_type,
            "payload": event.payload.model_dump(mode="json"),
            "correlation_id": event.correlation_id,
        }
        _add_optional(data, "delivery_id", event.delivery_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "run_event",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, AgentMessagePartial):
        data = {
            "message_id": event.message_id,
            "agent_id": event.agent_id,
            "content_delta": event.content_delta,
        }
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "agent_response_partial",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, AgentMessageFinal):
        data = {
            "message_id": event.message_id,
            "agent_id": event.agent_id,
        }
        data.update(_without_reserved_keys(event.content))
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "agent_response",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, TaskSubmittedEvent):
        data = {
            "message_id": event.message_id,
            "task_id": event.task_id,
            "agent_name": event.agent_name,
            "status": event.status,
        }
        _add_optional(data, "run_id", event.run_id)
        _add_optional(data, "opaque_public_call_id", event.opaque_public_call_id)
        _add_optional(data, "agent_id", event.agent_id)
        _add_optional(data, "related_message_id", event.related_message_id)
        _add_optional(data, "created_at", event.created_at)
        _add_optional(data, "step_number", event.step_number)
        _add_optional(data, "total_steps", event.total_steps)
        _add_optional(data, "task_content", event.task_content)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "task_submitted",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, TaskUpdateEvent):
        data = {
            "message_id": event.message_id,
            "status": event.status,
            "requires_input": event.requires_input,
            "requires_auth": event.requires_auth,
        }
        _add_optional(data, "run_id", event.run_id)
        _add_optional(data, "opaque_public_call_id", event.opaque_public_call_id)
        _add_optional(data, "content", event.content)
        _add_optional(data, "error", event.error)
        _add_optional(data, "status_message", event.status_message)
        _add_optional(data, "agent_name", event.agent_name)
        _add_optional(data, "agent_id", event.agent_id)
        _add_optional(data, "related_message_id", event.related_message_id)
        _add_optional(data, "created_at", event.created_at)
        _add_optional(data, "step_number", event.step_number)
        _add_optional(data, "total_steps", event.total_steps)
        _add_optional(data, "task_content", event.task_content)
        _add_optional(data, "parts", event.parts)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_optional(data, "delivery_id", event.delivery_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "task_update",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, ArtifactUpdateEvent):
        data = {
            "message_id": event.message_id,
            "agent_id": event.agent_id,
            "artifact": event.artifact,
            "append": event.append,
            "last_chunk": event.last_chunk,
        }
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "artifact_update",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, ErrorEvent):
        data = {"error": event.error}
        _add_optional(data, "error_type", event.error_type)
        _add_optional(data, "message_id", event.message_id)
        _add_optional(data, "agent_id", event.agent_id)
        _add_optional(data, "retry_after_seconds", event.retry_after_seconds)
        _add_optional(data, "user_requests_used", event.user_requests_used)
        _add_optional(data, "user_requests_limit", event.user_requests_limit)
        _add_optional(data, "system_requests_used", event.system_requests_used)
        _add_optional(data, "system_requests_limit", event.system_requests_limit)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "error",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, CancellationEvent):
        data = {
            "message_id": event.message_id,
            "reason": event.reason,
        }
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "cancellation",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, HITLRequestEvent):
        data = {
            "request_id": event.request_id,
            "message_id": event.message_id,
            "prompt": event.prompt,
            "prompt_type": event.prompt_type,
            "source": event.source,
        }
        _add_optional(data, "run_id", event.run_id)
        _add_optional(data, "choices", event.choices)
        _add_optional(data, "header", event.header)
        data["required"] = event.required
        data["allow_other"] = event.allow_other
        _add_optional(
            data,
            "choice_options",
            [
                option.model_dump(mode="json", exclude_none=True)
                for option in event.choice_options
            ]
            if event.choice_options is not None
            else None,
        )
        _add_optional(data, "agent_id", event.agent_id)
        _add_optional(data, "agent_name", event.agent_name)
        _add_optional(data, "agent_label", event.agent_label)
        _add_optional(data, "source_step_id", event.source_step_id)
        _add_optional(data, "interaction_id", event.interaction_id)
        _add_optional(data, "interaction_status", event.interaction_status)
        _add_optional(data, "interaction_version", event.interaction_version)
        _add_optional(data, "application_status", event.application_status)
        data["question_count"] = event.question_count
        data["question_index"] = event.question_index
        _add_optional(data, "related_message_id", event.related_message_id)
        _add_optional(data, "related_user_message_id", event.related_user_message_id)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "hitl_request",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    if isinstance(event, HITLResolvedEvent):
        data = {
            "request_id": event.request_id,
            "message_id": event.message_id,
            "source": event.source,
            "status": event.status,
        }
        _add_optional(data, "interaction_id", event.interaction_id)
        _add_optional(data, "interaction_status", event.interaction_status)
        _add_optional(data, "interaction_version", event.interaction_version)
        _add_optional(data, "application_status", event.application_status)
        data["question_count"] = event.question_count
        data["question_index"] = event.question_index
        _add_optional(data, "run_id", event.run_id)
        _add_optional(data, "error_message", event.error_message)
        _add_optional(data, "answer_ref", event.answer_ref)
        _add_optional(data, "answer_text", event.answer_text)
        _add_optional(data, "answered_by", event.answered_by)
        _add_optional(data, "answered_by_client_id", event.answered_by_client_id)
        _add_optional(data, "related_message_id", event.related_message_id)
        _add_optional(data, "related_user_message_id", event.related_user_message_id)
        _add_optional(data, "client_request_id", event.client_request_id)
        _add_trace_id(data, event.trace_id)
        return _frame(
            event.conversation_id,
            "hitl_response",
            data,
            frame_timestamp,
            room_seq=room_seq,
            room_event_id=room_event_id,
            parent_event_id=parent_event_id,
        )

    raise TypeError(f"Unsupported delivery event: {type(event)!r}")


def snapshot_frame(
    conversation_id: str,
    snapshot_data: dict[str, Any],
    *,
    timestamp: datetime,
) -> dict[str, Any]:
    """Build the ``snapshot`` frame (Room Stream Snapshot plan §4)."""

    return _frame(
        conversation_id,
        "snapshot",
        snapshot_data,
        timestamp.isoformat(),
    )


def _frame(
    conversation_id: str,
    event_type: str,
    data: dict[str, Any],
    timestamp: str,
    *,
    room_seq: int | None = None,
    room_event_id: str | None = None,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    if room_seq is not None:
        data["room_seq"] = room_seq
    if room_event_id is not None:
        data["room_event_id"] = room_event_id
    if parent_event_id is not None:
        data["parent_event_id"] = parent_event_id
    return {
        "type": event_type,
        "timestamp": timestamp,
        "conversation_id": conversation_id,
        "data": data,
    }


def _add_optional(data: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        data[key] = value


def _add_trace_id(data: dict[str, Any], trace_id: str | None) -> None:
    if trace_id:
        data["trace_id"] = trace_id


def _without_reserved_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "timestamp"}


__all__ = ["snapshot_frame", "to_sse_frame"]
