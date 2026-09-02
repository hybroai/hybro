"""Focused tests for the production projection outbox worker and projectors."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError

from dal.orchestrator.event_store import MongoOrchestratorEventStore
from dal.orchestrator.projection import (
    MongoAppendEventProjector,
    MongoFinalMessageProjector,
    MongoTerminalRunStatusProjector,
    _repair_terminal_agent_cards,
)
from delivery.snapshot import RoomEventFold
from execution.orchestrator.in_memory import (
    InMemoryOrchestratorEventStore,
    InMemoryOrchestratorRunStore,
)
from execution.orchestrator.models import (
    AssistantMessage,
    DataPart,
    TextPart,
)
from execution.orchestrator.projection import (
    ProjectionOutboxWorker,
    SettlingProjectionDriver,
)
from execution.orchestrator.settlement import (
    TerminalCommitRequest,
    TerminalDecisionFacts,
    commit_terminal_decision,
)

from ._orchestrator_helpers import NOW, make_run
from .test_orchestrator_a2a_mongo_parity import FakeCollection


def _terminal_run(
    *,
    run_id: str = "run-1",
    room_id: str = "room-1",
    final_message_id: str = "final-1",
):
    run = make_run()
    run = run.model_copy(
        update={
            "run_id": run_id,
            "session_id": room_id,
            "room_id": room_id,
            "client_request_id": f"request-{run_id}",
            "request": run.request.model_copy(
                update={
                    "room_epoch": 1,
                    "user_message_id": f"user-{run_id}",
                    "request_fingerprint": f"fingerprint-{run_id}",
                }
            ),
            "status": "finalizing",
            "transcript": [
                *run.transcript,
                AssistantMessage(
                    message_id=final_message_id,
                    content=[TextPart(text="final answer")],
                    tool_calls=[],
                    finish_reason="stop",
                    usage=None,
                    created_at=NOW,
                ),
            ],
            "proposed_final_message_id": final_message_id,
        }
    )
    committed = commit_terminal_decision(
        run,
        facts=TerminalDecisionFacts(final_message_id=final_message_id),
        request=TerminalCommitRequest(
            expected_state_version=run.state_version,
            command_id="complete",
            event_id="event-terminal",
            event_sequence=1,
            event_intent_id="intent-event",
            final_message_intent_id="intent-message",
            public_run_intent_id="intent-run",
            final_message_target=room_id,
            public_run_target=run_id,
            created_at=NOW,
        ),
    )
    return committed.run


async def _stored_terminal_run(*, store=None, **kwargs):
    store = store or InMemoryOrchestratorRunStore()
    run = _terminal_run(**kwargs)
    created = await store.create(run, command_id="create")
    assert created.outcome == "accepted"
    return store, run


async def test_worker_scans_claims_projects_and_completes_all_intents():
    store, run = await _stored_terminal_run()
    projected = []

    async def project(intent, current_run):
        projected.append((intent.kind, current_run.run_id))
        return "accepted"

    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": project,
            "deliver_final_message": project,
            "project_terminal_run_status": project,
        },
        worker_id="worker",
    )
    assert await worker.run_once(due_at=NOW) == 3
    stored = await store.load(run.run_id)
    assert stored is not None
    assert {item.status for item in stored.projection_outbox} == {"completed"}
    assert stored.projection_state == "settled"
    assert {kind for kind, _ in projected} == {
        "append_orchestrator_event",
        "deliver_final_message",
        "project_terminal_run_status",
    }


async def test_event_append_replays_without_duplicate_events():
    store, run = await _stored_terminal_run()
    events = InMemoryOrchestratorEventStore()
    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": MongoAppendEventProjector(events).project,
            "deliver_final_message": _noop_projector,
            "project_terminal_run_status": _noop_projector,
        },
        worker_id="worker",
    )
    assert await worker.run_once(due_at=NOW) == 3
    assert len(events.events[run.run_id]) == 1
    # Re-run after settlement is a no-op: the completed intent is never replayed.
    assert await worker.run_once(due_at=NOW) == 0
    assert len(events.events[run.run_id]) == 1


async def test_legacy_microsecond_event_retry_settles_once_after_bson_reload():
    microsecond_created_at = NOW.replace(microsecond=616_500)
    run = _terminal_run()
    legacy_intents = []
    for intent in run.projection_outbox:
        if intent.kind != "append_orchestrator_event":
            legacy_intents.append(intent)
            continue
        legacy_intents.append(
            intent.model_copy(
                update={
                    "payload": {
                        **intent.payload,
                        "created_at": microsecond_created_at.isoformat().replace(
                            "+00:00", "Z"
                        ),
                    }
                }
            )
        )
    run = run.model_copy(update={"projection_outbox": legacy_intents})
    store = InMemoryOrchestratorRunStore()
    assert (await store.create(run, command_id="create")).outcome == "accepted"

    event_collection = _BsonMillisCollection()
    event_store = MongoOrchestratorEventStore(event_collection)
    event_projector = MongoAppendEventProjector(event_store)
    stored = await store.load(run.run_id)
    assert stored is not None
    event_intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "append_orchestrator_event"
    )

    # The event side effect wins, then the worker crashes/loses its completion
    # CAS while the intent remains pending. BSON reloads the timestamp at .616.
    assert await event_projector.project(event_intent, stored) == "accepted"
    assert event_collection.values[0]["created_at"].microsecond == 616_000
    assert await event_projector.project(event_intent, stored) == "replayed"
    assert len(event_collection.values) == 1
    still_pending = await store.load(run.run_id)
    assert still_pending is not None
    assert (
        next(
            item
            for item in still_pending.projection_outbox
            if item.intent_id == event_intent.intent_id
        ).status
        == "pending"
    )

    fold = _completed_turn_fold_before_settlement(run)
    settled_rows: list[dict] = []
    final_message_projections = 0

    async def deliver_final_message(intent, current_run):
        nonlocal final_message_projections
        del intent, current_run
        final_message_projections += 1
        return "accepted"

    async def project_terminal_status(intent, current_run):
        del intent
        event_id = f"public:{current_run.run_id}:run_settled"
        if settled_rows:
            return "replayed"
        row = _canonical_room_event(
            current_run,
            room_seq=7,
            event_type="run_settled",
            event_id=event_id,
            payload={
                "status": "completed",
                "started_at": current_run.created_at,
                "settled_at": current_run.updated_at,
                "duration_ms": 0,
                "final_message_id": current_run.proposed_final_message_id,
            },
        )
        assert fold.apply(row)
        settled_rows.append(row)
        return "accepted"

    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": event_projector.project,
            "deliver_final_message": deliver_final_message,
            "project_terminal_run_status": project_terminal_status,
        },
        worker_id="recovery-worker",
    )
    assert await worker.run_once(due_at=NOW) == 3
    final = await store.load(run.run_id)
    assert final is not None
    assert {item.status for item in final.projection_outbox} == {"completed"}
    assert final.projection_state == "settled"
    assert len(event_collection.values) == 1
    assert final_message_projections == 1
    assert len(settled_rows) == 1
    assert fold.turns[run.run_id]["state"] == "completed"

    assert await worker.run_once(due_at=NOW) == 0
    assert len(event_collection.values) == 1
    assert final_message_projections == 1
    assert len(settled_rows) == 1


async def test_final_message_projector_dedupes_on_message_id():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "deliver_final_message"
    )
    messages = _FakeMessageCollection()
    projector = MongoFinalMessageProjector(messages)
    assert await projector.project(intent, stored) == "accepted"
    assert await projector.project(intent, stored) == "replayed"
    assert len(messages.documents) == 1


async def test_final_message_projector_delivers_on_insert_and_replay():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "deliver_final_message"
    )
    messages = _FakeMessageCollection()
    deliveries: list[tuple[str, str, str]] = []

    async def deliver(current_run, final, content):
        deliveries.append((current_run.run_id, final.message_id, content))
        return True

    projector = MongoFinalMessageProjector(messages, deliver)
    assert await projector.project(intent, stored) == "accepted"
    assert await projector.project(intent, stored) == "replayed"
    assert deliveries == [
        (stored.run_id, intent.payload["message_id"], "final answer"),
        (stored.run_id, intent.payload["message_id"], "final answer"),
    ]


async def test_final_message_projector_projects_assistant_into_room_memory():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "deliver_final_message"
    )
    projected: list[tuple[str, str]] = []

    async def project_memory(room_id: str, message_id: str):
        projected.append((room_id, message_id))
        return {"projected": True}

    projector = MongoFinalMessageProjector(
        _FakeMessageCollection(), memory_projection=project_memory
    )
    assert await projector.project(intent, stored) == "accepted"
    assert projected == [(stored.room_id, intent.payload["message_id"])]


async def test_final_message_projector_retries_until_room_memory_exists():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "deliver_final_message"
    )
    attempts = 0

    async def project_memory(_room_id: str, _message_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"projected": False, "reason": "missing_room_memory"}
        return {"projected": True}

    projector = MongoFinalMessageProjector(
        _FakeMessageCollection(), memory_projection=project_memory
    )
    assert await projector.project(intent, stored) == "error"
    assert await projector.project(intent, stored) == "replayed"


async def test_final_message_projector_retries_failed_room_delivery():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "deliver_final_message"
    )
    attempts = 0

    async def deliver(_run, _final, _content):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    projector = MongoFinalMessageProjector(_FakeMessageCollection(), deliver)
    assert await projector.project(intent, stored) == "error"
    assert await projector.project(intent, stored) == "replayed"


async def test_terminal_run_outbox_repairs_working_agent_card():
    messages = _RecordingMessageUpdates()
    run = SimpleNamespace(
        room_id="room-1",
        run_id="run-1",
        status="completed",
        updated_at=NOW,
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-1",
                        buffered_terminal_result=SimpleNamespace(
                            status="completed",
                            content=[TextPart(text="Sunny, 27°C")],
                        ),
                    )
                ]
            )
        ],
    )

    await _repair_terminal_agent_cards(messages, run)

    assert messages.updates == [
        (
            {
                "room_id": "room-1",
                "message_id": "orchestrator:run-1:call-1",
                "extend_info.orchestrator_run_id": "run-1",
            },
            {
                "$set": {
                    "message_content.message_text": "Sunny, 27°C",
                    "message_content.message_task.status.state": "completed",
                    "message_content.message_task.status.timestamp": NOW.isoformat(),
                    "task_updated_at": NOW,
                }
            },
        )
    ]


async def test_terminal_run_outbox_renders_data_and_closes_unresolved_cards():
    messages = _RecordingMessageUpdates()
    run = SimpleNamespace(
        room_id="room-1",
        run_id="run-canceled",
        status="canceled",
        updated_at=NOW,
        tool_batches=[
            SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        call_id="call-data",
                        buffered_terminal_result=SimpleNamespace(
                            status="completed",
                            content=[DataPart(data={"temperature": 27})],
                        ),
                    ),
                    SimpleNamespace(
                        call_id="call-pending",
                        buffered_terminal_result=None,
                    ),
                ]
            )
        ],
    )

    await _repair_terminal_agent_cards(messages, run)

    data_update = messages.updates[0][1]["$set"]
    assert data_update["message_content.message_text"] == '{"temperature":27}'
    assert data_update["message_content.message_task.status.state"] == "completed"
    pending_update = messages.updates[1][1]["$set"]
    assert pending_update["message_content.message_task.status.state"] == "canceled"
    assert "message_content.message_text" not in pending_update


async def test_terminal_run_status_projector_updates_public_runs():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "project_terminal_run_status"
    )
    runs = _FakeRunsCollection()
    projector = MongoTerminalRunStatusProjector(runs)
    assert await projector.project(intent, stored) == "accepted"
    # The public row is keyed by the legacy run id convention (user message
    # id), which the preflight already created in ``processing``.
    assert runs.documents[stored.request.user_message_id]["state"] == "completed"
    assert stored.run_id not in runs.documents
    assert await projector.project(intent, stored) == "replayed"


async def test_terminal_run_status_projector_falls_back_without_user_message():
    run = _terminal_run(run_id="run-fallback")
    run = run.model_copy(
        update={"request": run.request.model_copy(update={"user_message_id": ""})}
    )
    store = InMemoryOrchestratorRunStore()
    assert (await store.create(run, command_id="create")).outcome == "accepted"
    stored = await store.load(run.run_id)
    intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "project_terminal_run_status"
    )
    runs = _FakeRunsCollection()
    projector = MongoTerminalRunStatusProjector(runs)
    assert await projector.project(intent, stored) == "accepted"
    assert runs.documents[run.run_id]["state"] == "completed"


async def test_worker_blocks_poison_intent_after_bounded_attempts():
    store, run = await _stored_terminal_run()

    async def fail(intent, current_run):
        return "error"

    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": fail,
            "deliver_final_message": _noop_projector,
            "project_terminal_run_status": _noop_projector,
        },
        worker_id="worker",
        max_attempts=2,
        backoff_base_seconds=1,
    )
    # Final-message projection can complete, but the dependent settlement
    # intent remains pending while the terminal event append is unresolved.
    assert await worker.run_once(due_at=NOW) == 1
    stored = await store.load(run.run_id)
    event_intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "append_orchestrator_event"
    )
    assert event_intent.status == "pending"
    assert event_intent.attempt_count == 1
    assert event_intent.next_attempt_at is not None

    later = event_intent.next_attempt_at + timedelta(seconds=1)
    assert await worker.run_once(due_at=later) == 0
    stored = await store.load(run.run_id)
    event_intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "append_orchestrator_event"
    )
    assert event_intent.status == "blocked"
    assert event_intent.blocked_reason == "projection attempts exceeded"
    assert stored.projection_state == "blocked"


async def test_settlement_waits_for_required_intents():
    store, run = await _stored_terminal_run()
    driver = SettlingProjectionDriver(store)
    stored = await store.load(run.run_id)
    assert stored.projection_state == "pending"

    # Settling before the worker completes required intents is a no-op.
    assert (await driver.settle(run.run_id)).projection_state == "pending"

    # Complete all required intents, then settlement transitions once.
    current = await store.load(run.run_id)
    for intent in list(current.projection_outbox):
        if intent.status != "pending":
            continue
        claimed = await store.claim_projection_intent(
            run.run_id,
            intent.intent_id,
            expected_state_version=current.state_version,
            owner_id="worker",
            lease_expires_at=NOW + timedelta(seconds=30),
        )
        current = claimed.run
        completed = await store.complete_projection_intent(
            run.run_id,
            intent.intent_id,
            expected_state_version=current.state_version,
            owner_id="worker",
        )
        current = completed.run

    settled = await driver.settle(run.run_id)
    assert settled.projection_state == "settled"


async def test_worker_crash_replay_completes_partial_projection():
    store, run = await _stored_terminal_run()
    stored = await store.load(run.run_id)

    # Simulate a worker that completed the event intent before crashing.
    event_intent = next(
        item
        for item in stored.projection_outbox
        if item.kind == "append_orchestrator_event"
    )
    claimed = await store.claim_projection_intent(
        run.run_id,
        event_intent.intent_id,
        expected_state_version=stored.state_version,
        owner_id="crashed-worker",
        lease_expires_at=NOW + timedelta(seconds=60),
    )
    completed = await store.complete_projection_intent(
        run.run_id,
        event_intent.intent_id,
        expected_state_version=claimed.run.state_version,
        owner_id="crashed-worker",
    )
    assert completed.outcome == "accepted"

    projected = []

    async def project(intent, current_run):
        projected.append(intent.kind)
        return "accepted"

    worker = ProjectionOutboxWorker(
        run_store=store,
        projectors={
            "append_orchestrator_event": project,
            "deliver_final_message": project,
            "project_terminal_run_status": project,
        },
        worker_id="recovery-worker",
    )
    assert await worker.run_once(due_at=NOW) == 2
    final = await store.load(run.run_id)
    assert final.projection_state == "settled"
    assert "append_orchestrator_event" not in projected


def _canonical_room_event(
    run,
    *,
    room_seq: int,
    event_type: str,
    payload: dict,
    event_id: str | None = None,
):
    return {
        "room_id": run.room_id,
        "room_seq": room_seq,
        "kind": "run_event",
        "payload_public": {
            "event_id": event_id or f"public:{run.run_id}:{event_type}:{room_seq}",
            "run_id": run.run_id,
            "seq": room_seq,
            "type": event_type,
            "payload": payload,
            "correlation_id": run.client_request_id,
        },
        "ts": NOW.isoformat(),
    }


def _completed_turn_fold_before_settlement(run):
    final_message_id = run.proposed_final_message_id
    assert final_message_id is not None
    internal_turn_id = "turn-final"
    fold = RoomEventFold()
    rows = [
        _canonical_room_event(
            run,
            room_seq=1,
            event_type="run_started",
            payload={
                "hybro_turn_id": run.run_id,
                "user_message_id": run.request.user_message_id,
                "started_at": run.created_at,
                "mode": "fast",
            },
        ),
        _canonical_room_event(
            run,
            room_seq=2,
            event_type="turn_start",
            payload={"internal_turn_id": internal_turn_id, "attempt": 1},
        ),
        _canonical_room_event(
            run,
            room_seq=3,
            event_type="message_start",
            payload={
                "internal_turn_id": internal_turn_id,
                "message_id": final_message_id,
                "role": "assistant",
            },
        ),
        _canonical_room_event(
            run,
            room_seq=4,
            event_type="message_end",
            payload={
                "internal_turn_id": internal_turn_id,
                "message_id": final_message_id,
                "stop_reason": "stop",
                "disposition": "final",
                "text": "final answer",
                "error_summary": None,
            },
        ),
        _canonical_room_event(
            run,
            room_seq=5,
            event_type="turn_end",
            payload={
                "internal_turn_id": internal_turn_id,
                "message_id": final_message_id,
                "tool_call_ids": [],
                "status": "completed",
            },
        ),
        {
            "room_id": run.room_id,
            "room_seq": 6,
            "kind": "agent_response",
            "payload_public": {
                "message_id": final_message_id,
                "agent_id": "system:hybro",
                "content": "final answer",
                "client_request_id": run.client_request_id,
                "related_message_id": run.request.user_message_id,
            },
            "ts": NOW.isoformat(),
        },
    ]
    assert all(fold.apply(row) for row in rows)
    assert fold.turns[run.run_id]["state"] == "active"
    assert fold.turns[run.run_id]["final_committed"] is True
    return fold


def _bson_millisecond_roundtrip(value):
    if isinstance(value, datetime):
        return value.replace(
            microsecond=(value.microsecond // 1000) * 1000,
            tzinfo=None,
        )
    if isinstance(value, dict):
        return {key: _bson_millisecond_roundtrip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bson_millisecond_roundtrip(item) for item in value]
    return deepcopy(value)


class _BsonMillisCollection(FakeCollection):
    async def insert_one(self, document):
        return await super().insert_one(_bson_millisecond_roundtrip(document))


async def _noop_projector(intent, run):
    del intent, run
    return "accepted"


class _FakeMessageCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}

    async def find_one(self, query):
        return self.documents.get(query["message_id"])

    async def insert_one(self, document):
        message_id = document["message_id"]
        if message_id in self.documents:
            raise DuplicateKeyError("message_id")
        self.documents[message_id] = document


class _RecordingMessageUpdates:
    def __init__(self) -> None:
        self.updates: list[tuple[dict, dict]] = []

    async def update_one(self, query, update, **kwargs):
        assert kwargs == {"upsert": False}
        self.updates.append((query, update))
        return True


class _FakeRunsCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}

    async def update_one(self, query, update, **kwargs):
        run_id = query["run_id"]
        existing = self.documents.get(run_id)
        if existing is not None and existing.get("state") in {
            "completed",
            "failed",
            "canceled",
        }:
            raise DuplicateKeyError("run_id")
        self.documents[run_id] = {**update.get("$set", {}), "run_id": run_id}
        return True

    async def find_one(self, query):
        return self.documents.get(query["run_id"])
