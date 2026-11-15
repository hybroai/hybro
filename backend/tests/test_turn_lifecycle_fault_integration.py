from __future__ import annotations

import pytest

from common.dto import AgentMessageFinal, RunEventNotification
from container import (
    _canonical_final_message_end_parent_id,
    _latest_canonical_parent_id,
)
from delivery.snapshot import SnapshotService
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.models import AssistantMessage
from execution.orchestrator.public_projection import PublicProjectionTranslator
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.test_delivery_event_publisher import FakeTransport, make_publisher


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_turn_start", [False, True])
async def test_recovery_restores_missing_start_boundaries_before_child_terminals(
    preexisting_turn_start: bool,
):
    active = make_run().model_copy(
        update={
            "active_internal_turn_id": "turn-crash",
            "active_assistant_message_id": "assistant-crash",
            "active_attempt": 1,
        }
    )
    kernel, store, _, _ = await make_kernel(
        [final_events("recovered answer")], run=active
    )
    room_events = InMemoryRoomEventStore()
    publisher = make_publisher(transport=FakeTransport(), room_events=room_events)
    translator = PublicProjectionTranslator()
    parent_id: str | None = None

    async def publish(notification):
        nonlocal parent_id
        _status, emitted_id = await publisher.emit_checked_identified(
            notification, parent_event_id=parent_id
        )
        if emitted_id is not None:
            parent_id = emitted_id

    await publish(
        RunEventNotification(
            conversation_id="room-1",
            event_id=f"public:{active.run_id}:run_started",
            run_id=active.run_id,
            seq=0,
            run_event_type="run_started",
            payload={
                "hybro_turn_id": active.run_id,
                "user_message_id": "user-1",
                "started_at": NOW,
                "mode": "fast",
            },
            correlation_id="request-1",
        )
    )
    if preexisting_turn_start:
        await publish(
            RunEventNotification(
                conversation_id="room-1",
                event_id=f"public:{active.run_id}:turn-crash:turn_start:1",
                run_id=active.run_id,
                seq=1,
                run_event_type="turn_start",
                payload={"internal_turn_id": "turn-crash", "attempt": 1},
                correlation_id="request-1",
            )
        )

    async def read_events(conversation_id, run_id):
        return [
            row
            for row in await room_events.read_range(
                conversation_id, include_skipped=True
            )
            if row.get("run_id") == run_id
        ]

    async def lifecycle(event_type, run, payload):
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=run.state_version,
                timestamp=NOW,
                payload=payload,
                conversation_id=run.conversation_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
            )
        )
        if projected is not None:
            await publish(
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

    kernel.canonical_event_reader = read_events
    result = await kernel.run(
        active.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert result.outcome == "final_answer"
    rows = await room_events.read_range("room-1")
    crash_events = [
        row["payload_public"]
        for row in rows
        if row["kind"] == "run_event"
        and isinstance(row.get("payload_public"), dict)
        and isinstance(row["payload_public"].get("payload"), dict)
        and row["payload_public"]["payload"].get("internal_turn_id") == "turn-crash"
    ]
    kinds = [event["type"] for event in crash_events]
    assert kinds.count("turn_start") == 1
    assert kinds.count("message_start") == 1
    assert kinds.index("turn_start") < kinds.index("message_start")
    assert kinds.index("message_start") < kinds.index("message_end")
    assert kinds.index("message_end") < kinds.index("turn_end")
    snapshot = await SnapshotService(store=room_events).snapshot("room-1", force=True)
    crash_turn = snapshot["turns"][0]["internal_turns"][0]
    assert crash_turn["status"] == "aborted"


@pytest.mark.asyncio
async def test_recovery_replays_missing_tool_turn_end_after_publication_failure_once():
    kernel, store, _, _ = await make_kernel(
        [tool_events(("call-weather", "fake_agent_echo", '{"value":"ok"}'))]
    )
    run_id = next(iter(store.runs))
    run = await store.load(run_id)
    assert run is not None
    room_events = InMemoryRoomEventStore()
    publisher = make_publisher(transport=FakeTransport(), room_events=room_events)
    translator = PublicProjectionTranslator()
    parent_id: str | None = None

    async def publish(notification):
        nonlocal parent_id
        _status, emitted_id = await publisher.emit_checked_identified(
            notification, parent_event_id=parent_id
        )
        if emitted_id is not None:
            parent_id = emitted_id

    await publish(
        RunEventNotification(
            conversation_id=run.conversation_id,
            event_id=f"public:{run.run_id}:run_started",
            run_id=run.run_id,
            seq=0,
            run_event_type="run_started",
            payload={
                "hybro_turn_id": run.run_id,
                "user_message_id": run.request.user_message_id,
                "started_at": NOW,
                "mode": "fast",
            },
            correlation_id=run.client_request_id,
        )
    )

    fail_turn_end = True

    async def lifecycle(event_type, current, payload):
        nonlocal fail_turn_end
        if event_type == "turn_completed" and fail_turn_end:
            fail_turn_end = False
            raise OSError("turn_completed append failed")
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=current.session_id,
                run_id=current.run_id,
                causation_id=current.request.user_message_id,
                sequence=current.state_version,
                timestamp=NOW,
                payload=payload,
                conversation_id=current.conversation_id,
                user_message_id=current.request.user_message_id,
                client_request_id=current.client_request_id,
            )
        )
        if projected is None:
            return
        await publish(
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

    async def read_events(conversation_id, target_run_id):
        return [
            row
            for row in await room_events.read_range(
                conversation_id, include_skipped=True
            )
            if row.get("run_id") == target_run_id
        ]

    kernel.canonical_event_reader = read_events
    with pytest.raises(OSError, match="turn_completed append failed"):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    crashed = await store.load(run_id)
    assert crashed is not None
    assert crashed.active_internal_turn_id is not None
    batch = crashed.tool_batches[0]
    assert batch.results_flushed is True
    assert all(entry.public_terminal_emitted for entry in batch.entries)

    from tests._orchestrator_helpers import ScriptedModelRuntime

    kernel.model_runtime = ScriptedModelRuntime([final_events("recovered")])
    result = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "final_answer"
    assert result.run.active_internal_turn_id is None

    rows = await room_events.read_range(run.conversation_id, include_skipped=True)
    run_payloads = [
        row["payload_public"]
        for row in rows
        if row["kind"] == "run_event" and isinstance(row.get("payload_public"), dict)
    ]
    assert (
        sum(payload.get("type") == "tool_execution_end" for payload in run_payloads)
        == 1
    )
    first_turn_id = str(
        next(
            payload["payload"]["internal_turn_id"]
            for payload in run_payloads
            if payload.get("type") == "message_end"
            and payload.get("payload", {}).get("disposition") == "commentary"
        )
    )
    assert (
        sum(
            payload.get("type") == "turn_end"
            and payload.get("payload", {}).get("internal_turn_id") == first_turn_id
            for payload in run_payloads
        )
        == 1
    )
    snapshot = await SnapshotService(store=room_events).snapshot(
        run.conversation_id, force=True
    )
    assert snapshot["room_seq"] == await room_events.latest_seq(run.conversation_id)


def test_restart_parent_recovery_uses_latest_lifecycle_or_hitl_control():
    records = [
        {"room_seq": 1, "room_event_id": "root", "kind": "run_event"},
        {"room_seq": 2, "room_event_id": "message-end", "kind": "run_event"},
        {"room_seq": 3, "room_event_id": "hitl-wait", "kind": "run_event"},
        {"room_seq": 4, "room_event_id": "hitl-response", "kind": "hitl_resolved"},
    ]
    assert _latest_canonical_parent_id(records) == "hitl-response"


@pytest.mark.asyncio
async def test_kernel_room_log_final_settlement_snapshot_recovers_crash_boundaries_once():
    kernel, store, _, _ = await make_kernel([final_events("durable answer")])
    run_id = next(iter(store.runs))
    room_events = InMemoryRoomEventStore()
    publisher = make_publisher(
        transport=FakeTransport(),
        room_events=room_events,
    )
    translator = PublicProjectionTranslator()
    parent_id: str | None = None

    async def publish_notification(notification):
        nonlocal parent_id
        _status, event_id = await publisher.emit_checked_identified(
            notification,
            parent_event_id=parent_id,
        )
        if event_id is not None:
            parent_id = event_id

    await publish_notification(
        RunEventNotification(
            conversation_id="room-1",
            event_id=f"public:{run_id}:run_started",
            run_id=run_id,
            seq=0,
            run_event_type="run_started",
            payload={
                "hybro_turn_id": run_id,
                "user_message_id": "user-1",
                "started_at": NOW,
                "mode": "fast",
            },
            correlation_id="request-1",
        )
    )

    crashed_after_terminal_append = False

    async def lifecycle(event_type, run, payload):
        nonlocal crashed_after_terminal_append
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=run.state_version,
                timestamp=NOW,
                payload=payload,
                conversation_id=run.conversation_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
            )
        )
        if projected is None:
            return
        await publish_notification(
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
        if event_type == "message_completed" and not crashed_after_terminal_append:
            crashed_after_terminal_append = True
            raise OSError("crash after durable message_end append")

    with pytest.raises(OSError, match="message_end"):
        await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)

    async def read_events(conversation_id, expected_run_id):
        return [
            row
            for row in await room_events.read_range(
                conversation_id, include_skipped=True
            )
            if row.get("run_id") == expected_run_id
        ]

    kernel.canonical_event_reader = read_events
    result = await kernel.run(run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "final_answer"
    final = next(
        message
        for message in result.run.transcript
        if isinstance(message, AssistantMessage)
        and message.message_id == result.run.proposed_final_message_id
    )

    final_event = AgentMessageFinal(
        conversation_id=result.run.conversation_id,
        message_id=final.message_id,
        agent_id="system:hybro",
        content={
            "content": "durable answer",
            "related_message_id": "user-1",
            "client_request_id": "request-1",
        },
        delivery_id=f"orchestrator:{run_id}:final:{final.message_id}",
    )
    # Simulate a restart: the process cache currently points at turn_end, but
    # agent_response must parent to the exact final message_end read from disk.
    before_final = await room_events.read_range("room-1")
    final_parent_id = _canonical_final_message_end_parent_id(
        before_final, final.message_id
    )
    assert final_parent_id is not None
    assert final_parent_id != parent_id
    await publisher.emit_checked_identified(
        final_event, parent_event_id=final_parent_id
    )
    await publisher.emit_checked_identified(
        final_event, parent_event_id=final_parent_id
    )

    settled = RunEventNotification(
        conversation_id=result.run.conversation_id,
        event_id=f"public:{run_id}:run_settled",
        run_id=run_id,
        seq=result.run.state_version,
        run_event_type="run_settled",
        payload={
            "status": "completed",
            "started_at": result.run.created_at,
            "settled_at": result.run.updated_at,
            "duration_ms": 0,
            "final_message_id": final.message_id,
        },
        correlation_id="request-1",
    )
    final_row = next(
        row
        for row in await room_events.read_range("room-1")
        if row["kind"] == "agent_response"
    )
    await publisher.emit_checked_identified(
        settled, parent_event_id=final_row["room_event_id"]
    )
    await publisher.emit_checked_identified(
        settled, parent_event_id=final_row["room_event_id"]
    )

    rows = await room_events.read_range("room-1")
    final_row = next(row for row in rows if row["kind"] == "agent_response")
    message_end_row = next(
        row
        for row in rows
        if row["kind"] == "run_event"
        and row["payload_public"].get("type") == "message_end"
    )
    settled_row = next(
        row
        for row in rows
        if row["kind"] == "run_event"
        and row["payload_public"].get("type") == "run_settled"
    )
    assert final_row["parent_event_id"] == message_end_row["room_event_id"]
    assert settled_row["parent_event_id"] == final_row["room_event_id"]
    kinds = [row["kind"] for row in rows]
    assert kinds.count("agent_response") == 1
    assert (
        sum(
            row["payload_public"].get("type") == "run_settled"
            for row in rows
            if row["kind"] == "run_event"
        )
        == 1
    )
    assert (
        sum(
            row["payload_public"].get("type") == "message_end"
            for row in rows
            if row["kind"] == "run_event"
        )
        == 1
    )

    snapshot = await SnapshotService(store=room_events).snapshot("room-1", force=True)
    turn = snapshot["turns"][0]
    assert turn["final_committed"] is True
    assert turn["state"] == "completed"
    assert turn["final_answer"]["text"] == "durable answer"
