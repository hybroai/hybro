import asyncio
from datetime import UTC, datetime

import pytest

from common.dto import (
    DeliveryEmitStatus,
    HITLRequestEvent,
    HITLResolvedEvent,
    MessageCommitted,
    ProcessingStatusEvent,
    RunEventNotification,
    RunStateChanged,
)
from common.observability import get_current_trace_id, trace_id_context
from delivery.config import DeliveryConfig
from delivery.event_publisher import EventPublisherImpl
from delivery.sse.deduplication import (
    DeliveryReservation,
    DeliveryReservationStatus,
    TerminalStatusDeduplicator,
)

NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


def fixed_now():
    return NOW


class RecordingTaskRunner:
    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def __call__(self, coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeTransport:
    def __init__(self):
        self.frames: list[tuple[str, dict]] = []
        self.error: Exception | None = None
        self.delivered_count = 1

    async def broadcast_frame_to_room(self, conversation_id: str, frame: dict) -> int:
        if self.error is not None:
            raise self.error
        self.frames.append((conversation_id, frame))
        return self.delivered_count


class FakeBus:
    def __init__(self):
        self.sse: list[tuple[str, dict]] = []
        self.dead_letters: list[dict] = []
        self.sse_trace_ids: list[str | None] = []
        self.sse_error: Exception | None = None
        self.dead_letter_error: Exception | None = None
        self.sse_accepted = True

    async def publish_sse(self, conversation_id: str, frame: dict) -> bool:
        if self.sse_error is not None:
            raise self.sse_error
        self.sse_trace_ids.append(get_current_trace_id())
        self.sse.append((conversation_id, frame))
        return self.sse_accepted

    async def publish_dead_letter(self, envelope: dict) -> None:
        if self.dead_letter_error is not None:
            raise self.dead_letter_error
        self.dead_letters.append(envelope)


class FakeDeduplicator:
    """Two-phase deduplicator double; ``result=False`` reports a duplicate."""

    reservation_ttl_seconds = 30

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def reserve(self, *, conversation_id, message_id, status, delivery_id=None):
        self.calls.append((conversation_id, message_id, status))
        status_value = (
            DeliveryReservationStatus.RESERVED
            if self.result
            else DeliveryReservationStatus.ALREADY_DELIVERED
        )
        return DeliveryReservation(status_value, f"{conversation_id}:{message_id}")

    async def renew(self, reservation):
        return True

    async def confirm(self, reservation):
        return True

    async def mark_delivered_after_acceptance(self, reservation, *, status):
        return None

    async def release(self, **_kwargs):
        return None


class FakeMetrics:
    def __init__(self):
        self.increments: list[tuple[str, float, dict | None]] = []

    def increment(self, name, value=1.0, tags=None):
        self.increments.append((name, value, tags))

    def gauge(self, name, value, tags=None):
        pass

    def timing(self, name, value_ms, tags=None):
        pass


def make_publisher(
    *,
    transport=None,
    bus=None,
    dedup=None,
    task_runner=None,
    metrics=None,
    config=None,
    room_events=None,
    single_instance=False,
):
    return EventPublisherImpl(
        sse_transport=transport or FakeTransport(),
        event_bus=bus or FakeBus(),
        deduplicator=dedup or FakeDeduplicator(),
        config=config or DeliveryConfig(),
        now=fixed_now,
        instance_id="worker-1",
        metrics=metrics,
        room_events=room_events,
        single_instance=single_instance,
    )


def test_hitl_idempotency_keys_are_scoped_to_room_and_interaction():
    publisher = make_publisher()
    request_a = HITLRequestEvent(
        conversation_id="room-a",
        request_id="cloud_providers",
        message_id="agent-message",
        source="agent",
        prompt="Which providers?",
        prompt_type="text",
        interaction_id="interaction-a",
        question_count=2,
        question_index=1,
    )
    request_b = request_a.model_copy(
        update={"conversation_id": "room-b", "interaction_id": "interaction-b"}
    )
    resolved_a = HITLResolvedEvent(
        conversation_id="room-a",
        request_id="cloud_providers",
        message_id="agent-message",
        source="agent",
        status="responded",
        interaction_id="interaction-a",
        question_count=2,
        question_index=1,
    )

    request_key_a = publisher._idempotency_key(request_a, {})
    request_key_b = publisher._idempotency_key(request_b, {})
    resolved_key_a = publisher._idempotency_key(resolved_a, {})
    assert request_key_a == ("hitl_request:room-a:interaction-a:cloud_providers:1")
    assert request_key_b != request_key_a
    assert resolved_key_a == (
        "hitl_response:room-a:interaction-a:cloud_providers:responded"
    )


@pytest.mark.asyncio
async def test_emit_processing_status_translates_local_and_fanout_with_metrics():
    transport = FakeTransport()
    bus = FakeBus()
    metrics = FakeMetrics()
    publisher = make_publisher(transport=transport, bus=bus, metrics=metrics)

    delivered = await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    frame = {
        "type": "processing_status",
        "timestamp": NOW.isoformat(),
        "conversation_id": "room-1",
        "data": {
            "status": "processing",
            "message_id": "msg-1",
            "details": None,
        },
    }
    assert transport.frames == [("room-1", frame)]
    assert bus.sse == [("room-1", frame)]
    assert delivered is True
    assert metrics.increments == [
        (
            "hybro_delivery_events_emitted_total",
            1.0,
            {"event_type": "processing_status"},
        )
    ]


@pytest.mark.asyncio
async def test_long_fanout_renews_reservation_until_confirmed():
    from tests.test_delivery_deduplication import FakeRedisKV

    class SlowTransport(FakeTransport):
        async def broadcast_frame_to_room(self, conversation_id, frame):
            await asyncio.sleep(1.1)
            return await super().broadcast_frame_to_room(conversation_id, frame)

    redis = FakeRedisKV()
    config = DeliveryConfig(
        terminal_reservation_ttl_seconds=1,
        terminal_dedup_ttl_seconds=10,
    )
    publisher = make_publisher(
        transport=SlowTransport(),
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    renewals = [
        call for call in redis.compare_sets if call[1] == call[2] and call[3] == 1
    ]
    assert len(renewals) >= 2
    assert redis.compare_sets[-1][2] == "delivered:failed"
    assert redis.compare_sets[-1][3] == 10


@pytest.mark.asyncio
async def test_lost_reservation_after_fanout_writes_cross_instance_marker():
    from tests.test_delivery_deduplication import SharedNXRedisKV

    class LostRedis(SharedNXRedisKV):
        async def compare_set(self, key, expected_value, value, *, ttl):
            self.compare_sets.append((key, expected_value, value, ttl))
            return False

    class SlowTransport(FakeTransport):
        async def broadcast_frame_to_room(self, conversation_id, frame):
            await asyncio.sleep(0.4)
            return await super().broadcast_frame_to_room(conversation_id, frame)

    redis = LostRedis()
    config = DeliveryConfig(
        terminal_reservation_ttl_seconds=1,
        terminal_dedup_ttl_seconds=10,
    )
    publisher = make_publisher(
        transport=SlowTransport(),
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert redis.values == {
        "terminal:delivery:terminal:evt-1:processing": "delivered:failed"
    }
    assert redis.set_calls[-1][2] == 10

    other_instance = make_publisher(
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis)
    )
    assert (
        await other_instance.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED
    )
    assert len(publisher.sse_transport.frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation_result", [False, RuntimeError("redis failed")])
async def test_post_fanout_confirmation_failure_does_not_redeliver_across_instances(
    confirmation_result,
):
    from tests.test_delivery_deduplication import SharedNXRedisKV

    class ConfirmationFailureRedis(SharedNXRedisKV):
        async def compare_set(self, key, expected_value, value, *, ttl):
            self.compare_sets.append((key, expected_value, value, ttl))
            if isinstance(confirmation_result, Exception):
                raise confirmation_result
            return confirmation_result

    redis = ConfirmationFailureRedis()
    transport = FakeTransport()
    config = DeliveryConfig()
    publisher = make_publisher(
        transport=transport,
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert len(transport.frames) == 1
    assert redis.set_calls[-1] == (
        "terminal:delivery:terminal:evt-1:processing",
        "delivered:failed",
        config.terminal_dedup_ttl_seconds,
    )

    other_transport = FakeTransport()
    other_instance = make_publisher(
        transport=other_transport,
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    assert (
        await other_instance.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED
    )
    assert other_transport.frames == []


@pytest.mark.asyncio
async def test_accepted_result_survives_confirmation_and_marker_write_failure():
    from tests.test_delivery_deduplication import FakeRedisKV

    class FailingMarkerRedis(FakeRedisKV):
        async def compare_set(self, key, expected_value, value, *, ttl):
            raise RuntimeError("confirm failed")

        async def set(self, key, value, *, ttl):
            raise RuntimeError("marker failed")

    redis = FailingMarkerRedis()
    transport = FakeTransport()
    config = DeliveryConfig()
    publisher = make_publisher(
        transport=transport,
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert await publisher.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED
    assert len(transport.frames) == 1


@pytest.mark.asyncio
async def test_accepted_delivery_returns_when_connected_redis_commands_hang():
    from tests.test_delivery_deduplication import SharedNXRedisKV

    class HangingRedis(SharedNXRedisKV):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def _hang_until_released(self):
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue

        async def compare_set(self, key, expected_value, value, *, ttl):
            await self._hang_until_released()
            return False

        async def set(self, key, value, *, ttl):
            await self._hang_until_released()
            self.values[key] = value

    redis = HangingRedis()
    transport = FakeTransport()
    config = DeliveryConfig(terminal_redis_io_timeout_seconds=0.01)
    dedup = TerminalStatusDeduplicator(config=config, redis_kv=redis)
    publisher = make_publisher(
        transport=transport,
        config=config,
        dedup=dedup,
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )
    started_at = asyncio.get_running_loop().time()

    try:
        result = await publisher.emit_checked(event)
        elapsed = asyncio.get_running_loop().time() - started_at

        assert result == DeliveryEmitStatus.DELIVERED
        assert elapsed < 0.1
        assert len(transport.frames) == 1
        assert "delivery:terminal:evt-1:processing" in dedup.cache
        assert set(dedup._redis_tasks.values()) == {"confirm", "mark-delivered"}
        assert (
            await publisher.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED
        )
    finally:
        redis.release.set()
        owned = tuple(dedup._redis_tasks)
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)

    assert dedup._redis_tasks == {}


@pytest.mark.asyncio
async def test_checked_emit_persists_marker_when_redis_reservation_recovers():
    from tests.test_delivery_deduplication import FakeRedisKV

    class ReservationFailureRedis(FakeRedisKV):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        async def setnx(self, key, value, ttl):
            self.calls.append((key, value, ttl))
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("reservation unavailable")
            if key in self.values:
                return False
            self.values[key] = value
            return True

    redis = ReservationFailureRedis()
    transport = FakeTransport()
    config = DeliveryConfig()
    publisher = make_publisher(
        transport=transport,
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert redis.set_calls == [
        (
            "terminal:delivery:terminal:evt-1:processing",
            "delivered:failed",
            config.terminal_dedup_ttl_seconds,
        )
    ]
    other_instance = make_publisher(
        config=config,
        dedup=TerminalStatusDeduplicator(config=config, redis_kv=redis),
    )
    assert (
        await other_instance.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED
    )
    assert len(transport.frames) == 1


@pytest.mark.asyncio
async def test_run_event_checked_emit_distinguishes_inflight_and_confirmed():
    from tests.test_delivery_deduplication import SharedNXRedisKV

    redis = SharedNXRedisKV()
    crashed = TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis)
    await crashed.reserve(
        conversation_id="room-1",
        message_id="evt-1",
        status="delivered",
        delivery_id="terminal:evt-1:run-event",
    )
    transport = FakeTransport()
    publisher = make_publisher(
        transport=transport,
        dedup=TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis),
    )
    event = RunEventNotification(
        conversation_id="room-1",
        event_id="evt-1",
        delivery_id="terminal:evt-1:run-event",
        run_id="run-1",
        seq=2,
        run_event_type="turn_start",
        payload={"internal_turn_id": "turn-1", "attempt": 1},
        correlation_id="client-1",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.IN_FLIGHT
    assert transport.frames == []

    redis.values.clear()  # crashed reservation lease expires
    assert await publisher.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert transport.frames[0][1]["data"]["delivery_id"] == ("terminal:evt-1:run-event")

    replay = make_publisher(
        dedup=TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis)
    )
    assert await replay.emit_checked(event) == DeliveryEmitStatus.ALREADY_DELIVERED


@pytest.mark.asyncio
async def test_emit_suppresses_terminal_duplicate_and_counts_dedup():
    transport = FakeTransport()
    bus = FakeBus()
    dedup = FakeDeduplicator(result=False)
    metrics = FakeMetrics()
    publisher = make_publisher(
        transport=transport,
        bus=bus,
        dedup=dedup,
        metrics=metrics,
    )

    delivered = await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="completed"
        )
    )

    assert transport.frames == []
    assert bus.sse == []
    assert delivered is False
    assert dedup.calls == [("room-1", "msg-1", "completed")]
    assert metrics.increments == [
        (
            "hybro_delivery_events_deduplicated_total",
            1.0,
            {"event_type": "processing_status"},
        )
    ]


@pytest.mark.asyncio
async def test_local_delivery_failure_does_not_prevent_fanout_or_dead_letter():
    transport = FakeTransport()
    transport.error = RuntimeError("local disconnected")
    bus = FakeBus()
    publisher = make_publisher(transport=transport, bus=bus)

    await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert len(bus.sse) == 1
    assert bus.dead_letters == []
    assert list(publisher.dead_letters) == []


@pytest.mark.asyncio
async def test_zero_local_subscribers_and_no_broker_stays_pending():
    from tests.test_delivery_deduplication import SharedNXRedisKV

    redis = SharedNXRedisKV()
    transport = FakeTransport()
    transport.delivered_count = 0
    bus = FakeBus()
    bus.sse_accepted = False
    publisher = make_publisher(
        transport=transport,
        bus=bus,
        dedup=TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.FAILED
    assert redis.values == {}


def _hitl_request() -> HITLRequestEvent:
    return HITLRequestEvent(
        conversation_id="room-1",
        request_id="travel_dates",
        message_id="agent-message",
        source="agent",
        prompt="When do you travel?",
        prompt_type="text",
        interaction_id="interaction-1",
        question_count=1,
        question_index=1,
    )


@pytest.mark.asyncio
async def test_single_instance_accepts_a_frame_with_no_local_subscriber():
    # A session started through MCP has no browser on the room; the persisted
    # HITL request must still count as delivered so the Run can park.
    transport = FakeTransport()
    transport.delivered_count = 0
    bus = FakeBus()
    bus.sse_accepted = False
    publisher = make_publisher(transport=transport, bus=bus, single_instance=True)

    assert await publisher.emit_checked(_hitl_request()) == DeliveryEmitStatus.DELIVERED
    assert len(transport.frames) == 1
    assert bus.sse == []


@pytest.mark.asyncio
async def test_single_instance_reports_a_failed_local_broadcast():
    transport = FakeTransport()
    transport.error = RuntimeError("transport down")
    publisher = make_publisher(transport=transport, single_instance=True)

    assert await publisher.emit_checked(_hitl_request()) == DeliveryEmitStatus.FAILED


@pytest.mark.asyncio
async def test_zero_local_subscribers_and_failed_fanout_never_confirm_global_marker():
    from tests.test_delivery_deduplication import SharedNXRedisKV

    redis = SharedNXRedisKV()
    transport = FakeTransport()
    transport.delivered_count = 0
    bus = FakeBus()
    bus.sse_error = RuntimeError("redis pubsub down")
    publisher = make_publisher(
        transport=transport,
        bus=bus,
        dedup=TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis),
    )
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="failed",
        delivery_id="terminal:evt-1:processing",
    )

    assert await publisher.emit_checked(event) == DeliveryEmitStatus.FAILED
    assert redis.values == {}

    connected_transport = FakeTransport()
    replay = make_publisher(
        transport=connected_transport,
        dedup=TerminalStatusDeduplicator(config=DeliveryConfig(), redis_kv=redis),
    )
    assert await replay.emit_checked(event) == DeliveryEmitStatus.DELIVERED
    assert len(connected_transport.frames) == 1


@pytest.mark.asyncio
async def test_fanout_failure_is_dead_lettered_without_raising():
    bus = FakeBus()
    bus.sse_error = RuntimeError("redis down")
    publisher = make_publisher(bus=bus)

    delivered = await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert delivered is True
    assert len(bus.dead_letters) == 1
    assert publisher.dead_letters[0]["failure_stage"] == "sse_fanout"


@pytest.mark.asyncio
async def test_emit_returns_false_when_translation_fails(monkeypatch):
    bus = FakeBus()
    publisher = make_publisher(bus=bus)

    def fail_translation(*_args, **_kwargs):
        raise ValueError("PRIVATE_TRANSLATION_SENTINEL")

    monkeypatch.setattr(
        "delivery.event_publisher.to_sse_frame",
        fail_translation,
    )

    delivered = await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert delivered is False
    assert bus.dead_letters[0]["failure_stage"] == "translate"


@pytest.mark.asyncio
async def test_emit_returns_false_when_all_frontend_paths_fail():
    transport = FakeTransport()
    transport.error = RuntimeError("local down")
    bus = FakeBus()
    bus.sse_error = RuntimeError("redis down")
    publisher = make_publisher(transport=transport, bus=bus)

    delivered = await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert delivered is False
    assert bus.dead_letters[0]["failure_stage"] == "sse_fanout"


@pytest.mark.asyncio
async def test_failed_terminal_delivery_releases_dedup_reservation_for_retry():
    transport = FakeTransport()
    bus = FakeBus()
    dedup = TerminalStatusDeduplicator(config=DeliveryConfig())
    publisher = make_publisher(transport=transport, bus=bus, dedup=dedup)
    event = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-terminal",
        status="completed",
    )
    transport.error = RuntimeError("local down")
    bus.sse_error = RuntimeError("redis down")

    assert await publisher.emit(event) is False

    transport.error = None
    bus.sse_error = None
    assert await publisher.emit(event) is True
    assert await publisher.emit(event) is False
    assert len(transport.frames) == 1
    assert len(bus.sse) == 1


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_emit_internal_schedules_handlers_and_publishes_without_sse():
    transport = FakeTransport()
    bus = FakeBus()
    runner = RecordingTaskRunner()
    publisher = make_publisher(transport=transport, bus=bus, task_runner=runner)
    received = []

    async def handler(event):
        received.append(event)

    event = MessageCommitted(
        conversation_id="room-1",
        message_id="msg-1",
        message_type="user",
        timestamp=NOW,
    )
    publisher.register_internal_handler("message_committed", handler)

    await publisher.emit_internal(event)
    await asyncio.gather(*runner.tasks)

    assert received == [event]
    assert bus.internal == [event]
    assert transport.frames == []


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_emit_internal_can_wait_for_local_handlers_before_returning():
    runner = RecordingTaskRunner()
    publisher = make_publisher(task_runner=runner)
    order: list[str] = []

    async def handler(event):
        order.append("handler-start")
        await asyncio.sleep(0)
        order.append("handler-done")

    event = MessageCommitted(
        conversation_id="room-1",
        message_id="msg-1",
        message_type="user",
        timestamp=NOW,
    )
    publisher.register_internal_handler("message_committed", handler)

    await publisher.emit_internal(event, wait_for_local_handlers=True)

    assert order == ["handler-start", "handler-done"]
    assert all(task.done() for task in runner.tasks)


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_emit_internal_can_skip_redis_fanout_for_local_only_events():
    transport = FakeTransport()
    bus = FakeBus()
    runner = RecordingTaskRunner()
    publisher = make_publisher(transport=transport, bus=bus, task_runner=runner)
    received = []

    async def handler(event):
        received.append(event)

    event = MessageCommitted(
        conversation_id="room-1",
        message_id="msg-1",
        message_type="user",
        timestamp=NOW,
    )
    publisher.register_internal_handler("message_committed", handler)

    await publisher.emit_internal(
        event,
        wait_for_local_handlers=True,
        broadcast=False,
    )

    assert received == [event]
    assert bus.internal == []
    assert transport.frames == []


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_emit_does_not_dispatch_internal_handlers():
    runner = RecordingTaskRunner()
    publisher = make_publisher(task_runner=runner)
    called = []
    publisher.register_internal_handler(
        "processing_status", lambda event: called.append(event)
    )

    await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1", message_id="msg-1", status="processing"
        )
    )

    assert called == []
    assert runner.tasks == []


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_multiple_internal_handlers_and_handler_exception_dead_letter():
    bus = FakeBus()
    runner = RecordingTaskRunner()
    publisher = make_publisher(bus=bus, task_runner=runner)
    received = []

    async def good_handler(event):
        received.append(event.event_type)

    async def bad_handler(event):
        raise RuntimeError("handler failed")

    event = RunStateChanged(
        run_id="run-1",
        conversation_id="room-1",
        old_state="queued",
        new_state="processing",
        timestamp=NOW,
    )
    publisher.register_internal_handler("run_state_changed", good_handler)
    publisher.register_internal_handler("run_state_changed", bad_handler)

    await publisher.emit_internal(event)
    await asyncio.gather(*runner.tasks, return_exceptions=True)

    assert received == ["run_state_changed"]
    assert any(
        letter["failure_stage"] == "internal_handler" for letter in bus.dead_letters
    )


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_remote_internal_event_restores_trace_context_and_rejects_mismatch():
    runner = RecordingTaskRunner()
    publisher = make_publisher(task_runner=runner)
    observed = []
    publisher.register_internal_handler(
        "message_committed",
        lambda event: observed.append(get_current_trace_id()),
    )

    await publisher.handle_remote_internal_event(
        {
            "kind": "internal_event",
            "origin": "worker-2",
            "event_type": "message_committed",
            "trace_id": "trace-remote",
            "event": {
                "event_type": "message_committed",
                "timestamp": NOW.isoformat(),
                "payload": {},
                "room_id": "room-1",
                "message_id": "msg-1",
                "message_type": "user",
                "agent_id": None,
            },
        }
    )
    await asyncio.gather(*runner.tasks)

    assert observed == ["trace-remote"]
    assert get_current_trace_id() is None

    await publisher.handle_remote_internal_event(
        {
            "kind": "internal_event",
            "origin": "worker-2",
            "event_type": "message_committed",
            "event": {
                "event_type": "run_state_changed",
                "timestamp": NOW.isoformat(),
                "payload": {},
                "run_id": "run-1",
                "room_id": "room-1",
                "old_state": "queued",
                "new_state": "processing",
            },
        }
    )
    assert len(runner.tasks) == 1


@pytest.mark.asyncio
async def test_trace_context_is_added_to_typed_frames():
    transport = FakeTransport()
    bus = FakeBus()
    publisher = make_publisher(transport=transport, bus=bus)

    with trace_id_context("trace-123"):
        await publisher.emit(
            ProcessingStatusEvent(
                conversation_id="room-1", message_id="msg-1", status="processing"
            )
        )

    assert transport.frames[0][1]["data"]["trace_id"] == "trace-123"
    assert bus.sse_trace_ids == ["trace-123"]


@pytest.mark.asyncio
async def test_explicit_event_trace_is_used_for_cross_instance_publish():
    transport = FakeTransport()
    bus = FakeBus()
    publisher = make_publisher(transport=transport, bus=bus)

    await publisher.emit(
        ProcessingStatusEvent(
            conversation_id="room-1",
            message_id="msg-1",
            status="processing",
            trace_id="trace-from-event",
        )
    )

    assert bus.sse_trace_ids == ["trace-from-event"]
    assert bus.sse[0][1]["data"]["trace_id"] == "trace-from-event"


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_stop_cancels_blocked_handler_after_configured_timeout():
    runner = RecordingTaskRunner()
    publisher = make_publisher(
        task_runner=runner,
        config=DeliveryConfig(handler_shutdown_timeout_seconds=0.01),
    )

    async def blocked_handler(event):
        await asyncio.Event().wait()

    event = MessageCommitted(
        conversation_id="room-1",
        message_id="msg-1",
        message_type="user",
        timestamp=NOW,
    )
    publisher.register_internal_handler("message_committed", blocked_handler)
    await publisher.emit_internal(event)

    await publisher.stop()

    assert runner.tasks[0].cancelled()


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        MessageCommitted(
            conversation_id="room-1",
            message_id="msg-1",
            message_type="user",
            timestamp=NOW,
        ),
        RunStateChanged(
            run_id="run-1",
            conversation_id="room-1",
            old_state="queued",
            new_state="processing",
            timestamp=NOW,
        ),
    ],
)
async def test_all_internal_event_union_members_schedule_local_handlers(event):
    runner = RecordingTaskRunner()
    publisher = make_publisher(task_runner=runner)
    received = []

    publisher.register_internal_handler(
        event.event_type, lambda item: received.append(item)
    )

    await publisher.emit_internal(event)
    await asyncio.gather(*runner.tasks)

    assert received == [event]


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_remote_internal_events_cover_all_union_members_and_multiple_handlers():
    runner = RecordingTaskRunner()
    publisher = make_publisher(task_runner=runner)
    first = []
    second = []
    events = [
        MessageCommitted(
            conversation_id="room-1",
            message_id="msg-1",
            message_type="user",
            timestamp=NOW,
        ),
        RunStateChanged(
            run_id="run-1",
            conversation_id="room-1",
            old_state="queued",
            new_state="processing",
            timestamp=NOW,
        ),
    ]

    for event in events:
        publisher.register_internal_handler(
            event.event_type,
            lambda item, bucket=first: bucket.append(item.event_type),
        )
        publisher.register_internal_handler(
            event.event_type,
            lambda item, bucket=second: bucket.append(item.event_type),
        )
        await publisher.handle_remote_internal_event(
            {
                "kind": "internal_event",
                "origin": "worker-2",
                "event_type": event.event_type,
                "event": event.model_dump(mode="json"),
            }
        )

    await asyncio.gather(*runner.tasks)

    assert first == [event.event_type for event in events]
    assert second == [event.event_type for event in events]


@pytest.mark.skip(reason="internal eventing coverage moved to test_common_eventing")
@pytest.mark.asyncio
async def test_emit_internal_with_no_subscribers_is_noop_for_handlers():
    runner = RecordingTaskRunner()
    bus = FakeBus()
    publisher = make_publisher(bus=bus, task_runner=runner)
    event = RunStateChanged(
        run_id="run-1",
        conversation_id="room-1",
        old_state="queued",
        new_state="processing",
        timestamp=NOW,
    )

    await publisher.emit_internal(event)

    assert runner.tasks == []
    assert bus.internal == [event]
