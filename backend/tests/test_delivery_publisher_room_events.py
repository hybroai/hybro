"""Publisher persist-before-broadcast over the room event log (plan §5)."""

from __future__ import annotations

import pytest

from common.dto import (
    AgentMessageFinal,
    DeliveryEmitStatus,
    ProcessingStatusEvent,
    RunEventNotification,
)
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.test_delivery_event_publisher import (
    FakeDeduplicator,
    FakeTransport,
    make_publisher,
)


@pytest.mark.asyncio
async def test_delta_emit_persists_before_broadcast_and_threads_seq():
    store = InMemoryRoomEventStore()
    transport = FakeTransport()
    publisher = make_publisher(transport=transport, room_events=store)

    status, room_event_id = await publisher.emit_checked_identified(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert status == DeliveryEmitStatus.DELIVERED
    assert room_event_id is not None
    assert len(transport.frames) == 1
    _, frame = transport.frames[0]
    assert frame["data"]["room_seq"] == 1
    assert frame["data"]["room_event_id"] == room_event_id

    # The persisted doc holds the public payload WITHOUT the delivery-level
    # seq fields (they are threaded at frame-build time).
    records = await store.read_range("room-1")
    assert len(records) == 1
    assert records[0]["kind"] == "processing_status"
    assert "room_seq" not in records[0]["payload_public"]
    assert "room_event_id" not in records[0]["payload_public"]
    assert records[0]["persist_state"] == "settled"


@pytest.mark.asyncio
async def test_non_terminal_retry_reuses_same_sequence():
    store = InMemoryRoomEventStore()
    transport = FakeTransport()
    publisher = make_publisher(transport=transport, room_events=store)
    event = RunEventNotification(
        conversation_id="room-1",
        event_id="public:run-1:turn_start:3",
        run_id="run-1",
        seq=3,
        run_event_type="turn_start",
        payload={"internal_turn_id": "turn-1", "attempt": 1},
        correlation_id="client-1",
    )

    first_status, first_id = await publisher.emit_checked_identified(event)
    second_status, second_id = await publisher.emit_checked_identified(event)

    assert first_status == DeliveryEmitStatus.DELIVERED
    assert second_status == DeliveryEmitStatus.DELIVERED
    # Run events use their stable event_id as the idempotency key: the retry
    # reuses the SAME persisted doc and sequence.
    assert first_id == second_id
    assert await store.latest_seq("room-1") == 1
    assert transport.frames[0][1]["data"]["room_seq"] == 1
    assert transport.frames[1][1]["data"]["room_seq"] == 1


@pytest.mark.asyncio
async def test_final_message_retry_reuses_one_durable_room_event():
    store = InMemoryRoomEventStore()
    transport = FakeTransport()
    publisher = make_publisher(transport=transport, room_events=store)
    event = AgentMessageFinal(
        conversation_id="room-1",
        message_id="assistant-1",
        agent_id="system:hybro",
        content={"content": "Final answer"},
        delivery_id="orchestrator:run-1:final:assistant-1",
    )

    first_status, first_id = await publisher.emit_checked_identified(event)
    second_status, second_id = await publisher.emit_checked_identified(event)

    assert first_status == DeliveryEmitStatus.DELIVERED
    assert second_status == DeliveryEmitStatus.DELIVERED
    assert first_id == second_id
    assert await store.latest_seq("room-1") == 1
    records = await store.read_range("room-1")
    assert len(records) == 1
    assert records[0]["kind"] == "agent_response"


@pytest.mark.asyncio
async def test_failed_final_fanout_still_returns_persisted_identity():
    store = InMemoryRoomEventStore()
    transport = FakeTransport()
    transport.delivered_count = 0
    publisher = make_publisher(transport=transport, room_events=store)
    publisher.event_bus.sse_accepted = False

    status, room_event_id = await publisher.emit_checked_identified(
        AgentMessageFinal(
            conversation_id="room-1",
            message_id="assistant-1",
            agent_id="system:hybro",
            content={"content": "Final answer"},
            delivery_id="orchestrator:run-1:final:assistant-1",
        )
    )

    assert status == DeliveryEmitStatus.FAILED
    assert room_event_id is not None
    assert await store.latest_seq("room-1") == 1


@pytest.mark.asyncio
async def test_dedup_short_circuits_skip_persistence():
    # IN_FLIGHT / ALREADY_DELIVERED outcomes must not append
    # room_events docs: those events already have their persisted doc.
    store = InMemoryRoomEventStore()
    publisher = make_publisher(
        transport=FakeTransport(),
        room_events=store,
        dedup=FakeDeduplicator(result=False),
    )

    status, _ = await publisher.emit_checked_identified(
        ProcessingStatusEvent(
            conversation_id="room-1",
            message_id="msg-1",
            status="completed",
            delivery_id="terminal:evt-1:processing",
        )
    )

    assert status == DeliveryEmitStatus.ALREADY_DELIVERED
    assert await store.latest_seq("room-1") == 0


@pytest.mark.asyncio
async def test_distinct_deltas_with_identical_content_do_not_collapse():
    # Per-stream monotonic idempotency component: two partial deltas carrying
    # the same content are distinct logical events and persist as two docs.
    store = InMemoryRoomEventStore()
    publisher = make_publisher(transport=FakeTransport(), room_events=store)

    from common.dto import AgentMessagePartial

    for _ in range(2):
        await publisher.emit_checked_identified(
            AgentMessagePartial(
                conversation_id="room-1",
                message_id="msg-1",
                agent_id="a1",
                content_delta="Hi",
            )
        )

    assert await store.latest_seq("room-1") == 2
    records = await store.read_range("room-1")
    assert [record["room_seq"] for record in records] == [1, 2]


@pytest.mark.asyncio
async def test_parent_event_id_flows_through_persist_and_frame():
    store = InMemoryRoomEventStore()
    transport = FakeTransport()
    publisher = make_publisher(transport=transport, room_events=store)

    decision_status, decision_id = await publisher.emit_checked_identified(
        RunEventNotification(
            conversation_id="room-1",
            event_id="public:run-1:turn_start:2",
            run_id="run-1",
            seq=2,
            run_event_type="turn_start",
            payload={"internal_turn_id": "turn-1", "attempt": 1},
            correlation_id="client-1",
        )
    )
    assert decision_status == DeliveryEmitStatus.DELIVERED

    child_status, _ = await publisher.emit_checked_identified(
        RunEventNotification(
            conversation_id="room-1",
            event_id="public:run-1:message_start:4",
            run_id="run-1",
            seq=4,
            run_event_type="message_start",
            payload={"internal_turn_id": "turn-1", "message_id": "assistant-1"},
            correlation_id="client-1",
        ),
        parent_event_id=decision_id,
    )
    assert child_status == DeliveryEmitStatus.DELIVERED
    assert transport.frames[1][1]["data"]["parent_event_id"] == decision_id

    records = await store.read_range("room-1")
    assert records[1]["parent_event_id"] == decision_id
