"""PublicProjectionTranslator: private SessionEvent → public run_event payloads."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.dto import RunEventNotification
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.public_projection import (
    PUBLIC_RUN_EVENT_KINDS,
    PublicProjectionTranslator,
)
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.test_delivery_event_publisher import FakeTransport, make_publisher


def _event(event_type: str, payload: dict | None = None) -> SessionEvent:
    return SessionEvent(
        event_type=event_type,  # type: ignore[arg-type]
        session_id="session-1",
        run_id="run-1",
        causation_id="msg-1",
        sequence=7,
        timestamp=datetime.now(UTC),
        payload=payload or {},
        conversation_id="room-1",
        user_message_id="msg-1",
        client_request_id="crid-1",
    )


def test_public_kinds_are_the_canonical_turn_lifecycle_vocabulary():
    assert PUBLIC_RUN_EVENT_KINDS == {
        "run_started",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "turn_end",
        "retry_scheduled",
        "model_decision",
        "run_waiting_input",
        "run_resumed",
        "run_settled",
    }


def test_non_public_lifecycle_events_translate_to_none():
    translator = PublicProjectionTranslator()
    for event_type in (
        "session_started",
        "model_attempt_started",
        "model_attempt_failed",
        "model_turn_completed",
        "orchestrator_decision",
        "run_waiting_external",
    ):
        assert translator.translate(_event(event_type)) is None


def test_canonical_tool_label_enforces_configured_secret_policy():
    event = _event(
        "tool_execution_started",
        {
            "internal_turn_id": "turn-1",
            "call_id": "private-call",
            "public_call_id": "inv_weather_0001",
            "tool_name": "weather_lookup",
            "agent_label": "Weather token=hunter2",
            "arguments": {},
        },
    )
    public = PublicProjectionTranslator(
        secret_values=("hunter2",),
    ).translate(event)
    assert public is not None
    assert public.payload["tool_name"] == "Weather token[REDACTED]"
    assert "hunter2" not in str(public.payload)


@pytest.mark.asyncio
async def test_canonical_label_secret_never_reaches_persisted_room_event():
    sentinel = "PRIVATE_LABEL_SENTINEL"
    event = _event(
        "tool_execution_started",
        {
            "internal_turn_id": "turn-1",
            "call_id": "private-call",
            "public_call_id": "inv_weather_0001",
            "tool_name": "weather_lookup",
            "agent_label": f"Weather {sentinel}",
            "arguments": {},
        },
    )
    projected = PublicProjectionTranslator(secret_values=(sentinel,)).translate(event)
    assert projected is not None
    store = InMemoryRoomEventStore()
    publisher = make_publisher(transport=FakeTransport(), room_events=store)
    await publisher.emit_checked_identified(
        RunEventNotification(
            conversation_id=projected.conversation_id,
            event_id=projected.event_id,
            run_id=projected.run_id,
            seq=projected.seq,
            run_event_type=projected.kind,
            payload=projected.payload,
            correlation_id=projected.client_request_id,
        )
    )
    persisted = await store.read_range("room-1")
    assert sentinel not in str(persisted)
    assert "[REDACTED]" in str(persisted)


def test_translator_missing_room_id_degrades_to_empty_string():
    event = _event("model_retry_scheduled", {"attempt": 2, "error_class": "timeout"})
    event = event.model_copy(update={"conversation_id": None})
    public = PublicProjectionTranslator().translate(event)
    assert public is not None
    assert public.conversation_id == ""


def _agent_catalog():
    from execution.orchestrator.models import (
        FrozenToolCatalogEntry,
        FrozenToolCatalogSnapshot,
        ToolBindingRef,
        ToolDefinition,
    )

    return FrozenToolCatalogSnapshot(
        catalog_id="catalog-1",
        entries=[
            FrozenToolCatalogEntry(
                definition=ToolDefinition(
                    name="agent_weather",
                    label="Weather Agent - Forecast",
                    description="Weather",
                    input_schema={
                        "type": "object",
                        "properties": {"task": {"type": "string"}},
                    },
                    execution_mode="parallel",
                    side_effect_level="read",
                ),
                binding=ToolBindingRef(binding_id="binding-1", binding_digest="digest"),
                agent_display_name="Weather Agent",
            )
        ],
        created_at=datetime.now(UTC),
    )


def _canonical_tool_event(event_type: str, payload: dict):
    return _event(event_type, payload)


def test_canonical_run_started_identity_ignores_recovery_payload_ids():
    translator = PublicProjectionTranslator()
    durable_started_at = datetime(2030, 1, 1, tzinfo=UTC)
    initial = _event("run_started", {"mode": "ultimate"})
    recovery = _event(
        "run_started",
        {
            "mode": "ultimate",
            "public_event_id": "public:run-1:run_started",
            "started_at": durable_started_at,
        },
    ).model_copy(update={"sequence": 99})

    initial_public = translator.translate(initial)
    recovery_public = translator.translate(recovery)

    assert initial_public is not None
    assert recovery_public is not None
    assert initial_public.event_id == recovery_public.event_id
    assert recovery_public.payload["started_at"] == durable_started_at


def test_canonical_agent_execution_exposes_base_name_and_kind():
    translator = PublicProjectionTranslator()
    public = translator.translate(
        _canonical_tool_event(
            "tool_execution_started",
            {
                "internal_turn_id": "turn-1",
                "call_id": "private-call",
                "public_call_id": "inv_weather_0001",
                "tool_name": "agent_weather",
                "agent_label": "Weather Agent - Forecast",
                "arguments": {"task": "Check San Jose weather"},
            },
        ),
        catalog=_agent_catalog(),
    )
    assert public is not None
    assert public.kind == "tool_execution_start"
    assert public.payload["execution_kind"] == "agent"
    assert public.payload["target"] == {"name": "Weather Agent", "source": None}
    assert public.payload["tool_name"] == "Weather Agent - Forecast"
    assert public.payload["request_summary"] == "Check San Jose weather"


def test_canonical_agent_execution_omits_private_a2a_transport_metadata():
    translator = PublicProjectionTranslator()
    public = translator.translate(
        _canonical_tool_event(
            "tool_execution_started",
            {
                "internal_turn_id": "turn-1",
                "call_id": "private-call",
                "public_call_id": "inv_weather_0001",
                "tool_name": "agent_weather",
                "agent_label": "Weather Agent - Forecast",
                "arguments": {"task": "Check weather"},
                "message_metadata": {
                    "hybro.ai/a2a/selected-skill": {
                        "schema_version": 1,
                        "skill_id": "PRIVATE_SKILL_SENTINEL",
                    }
                },
                "part_metadata": {
                    "hybro.ai/a2a/part-provenance": {
                        "schema_version": 1,
                        "role": "orchestrator_instruction",
                    }
                },
            },
        ),
        catalog=_agent_catalog(),
    )

    assert public is not None
    assert "PRIVATE_SKILL_SENTINEL" not in str(public.payload)
    assert "hybro.ai/a2a/selected-skill" not in str(public.payload)
    assert "hybro.ai/a2a/part-provenance" not in str(public.payload)


def test_canonical_agent_execution_end_marks_private_detail_available():
    translator = PublicProjectionTranslator()
    public = translator.translate(
        _canonical_tool_event(
            "tool_execution_completed",
            {
                "internal_turn_id": "turn-1",
                "call_id": "private-call",
                "public_call_id": "inv_weather_0001",
                "tool_name": "agent_weather",
                "agent_label": "Weather Agent - Forecast",
                "result_status": "completed",
                "result_text": "Clear, 22C",
                "duration_ms": 120,
            },
        ),
        catalog=_agent_catalog(),
    )
    assert public is not None
    assert public.payload["execution_kind"] == "agent"
    assert public.payload["target"] == {"name": "Weather Agent", "source": None}
    assert public.payload["detail_available"] is True
    assert public.payload["result"] == ""
    assert "Clear, 22C" not in str(public.payload)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (
            "failed",
            "llm_quota_exceeded",
            {"error_code": "llm_quota_exceeded", "error_message": "Quota exceeded"},
        ),
        ("failed", "bad code", {}),
        ("completed", "llm_quota_exceeded", {}),
    ],
)
def test_canonical_agent_execution_end_exposes_agent_error_only_when_failed(
    status, code, expected
):
    public = PublicProjectionTranslator().translate(
        _canonical_tool_event(
            "tool_execution_completed",
            {
                "internal_turn_id": "turn-1",
                "call_id": "private-call",
                "public_call_id": "inv_weather_0001",
                "tool_name": "agent_weather",
                "agent_label": "Weather Agent - Forecast",
                "result_status": status,
                "result_is_error": status == "failed",
                "result_text": "",
                "result_error_code": code,
                "result_error_message": "Quota exceeded",
                "duration_ms": 120,
            },
        ),
        catalog=_agent_catalog(),
    )
    assert public is not None
    details = {
        key: public.payload[key]
        for key in ("error_code", "error_message")
        if key in public.payload
    }
    assert details == expected


def test_canonical_unknown_tool_is_plain_tool_execution_without_target():
    translator = PublicProjectionTranslator()
    public = translator.translate(
        _canonical_tool_event(
            "tool_execution_started",
            {
                "internal_turn_id": "turn-1",
                "call_id": "private-call",
                "public_call_id": "inv_tool_0000001",
                "tool_name": "request_user_input",
                "arguments": {"question": "Which city?"},
            },
        ),
        catalog=_agent_catalog(),
    )
    assert public is not None
    assert public.payload["execution_kind"] == "tool"
    assert "target" not in public.payload
    assert public.payload["request_summary"] == ""
    assert public.payload["tool_name"] == "request_user_input"


def test_model_decision_projects_answered_from_context_and_no_progress():
    translator = PublicProjectionTranslator()

    answered = translator.translate(
        _event(
            "model_decision",
            {
                "internal_turn_id": "turn-1",
                "decision": "answered_from_context",
                "agent_label": "Cyber Broker Agent",
                "question_summary": "Which cloud provider?",
                "source_summary": "from earlier messages and attachments",
            },
        )
    )
    assert answered is not None
    assert answered.kind == "model_decision"
    assert answered.payload["decision"] == "answered_from_context"
    assert answered.payload["agent_label"] == "Cyber Broker Agent"
    assert answered.payload["question_summary"] == "Which cloud provider?"
    assert answered.payload["source_summary"] == "from earlier messages and attachments"
    # The semantic event id must not embed raw public text.
    assert "Which cloud" not in answered.event_id

    no_progress = translator.translate(
        _event(
            "model_decision",
            {
                "internal_turn_id": "turn-1",
                "decision": "no_progress",
                "agent_label": "Cyber Broker Agent",
                "question_summary": "Which cloud provider?",
                "reason": "auto_reply_limit_reached",
            },
        )
    )
    assert no_progress is not None
    assert no_progress.payload["decision"] == "no_progress"
    assert no_progress.payload["reason"] == "auto_reply_limit_reached"
