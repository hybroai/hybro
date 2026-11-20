"""SessionFacade invariants, event-based waiting, and the client projection.

Fakes stand in for the bound protocols (container management, history,
execution, approvals, target resolution). The room event log is the real
in-memory store, so the waiter reads the same record shape it reads in
production.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from common.dto import (
    CreateRoomRequest,
    ExecutionAck,
    ExecutionRequest,
    HITLRequest,
    MembershipUpdateRequest,
    RoomInfo,
    RoomMessageInfo,
    RunInfo,
    RunState,
)
from common.errors import AppError, ConflictError, NotFoundError
from models.session import SessionMessageRequest
from session.facade import SessionFacade, SessionOwner
from session.models import SessionRecord, SessionRequest
from session.waiter import SessionWaiter
from source.protocols import NO_TOKEN, SourceIdentity
from tests.fakes.room_events import InMemoryRoomEventStore

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
OWNER = SessionOwner(user_id="owner-1", name="Estate Agent")
SOURCE = NO_TOKEN
CI = SourceIdentity(source_id="src_ci", name="CI bot", has_token=True)
OTHER = SourceIdentity(source_id="src_other", name="Other bot", has_token=True)
AGENT = "agent-travel"
OTHER_AGENT = "agent-other"

FINGERPRINT_CONFLICT = "The client_request_id was already used for a different request"
WAITING_FOR_INPUT = (
    "Room is waiting for your input before it can process another message."
)
ALREADY_PROCESSING = "Room is already processing another message."


# -- Fakes ------------------------------------------------------------------


class FakeSessionStore:
    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}
        self.reserve_calls = 0
        # Simulates a concurrent winner that sets the container id between
        # this request's reservation and its own conditional write.
        self.race_conversation_id: str | None = None

    async def reserve(self, record: SessionRecord) -> SessionRecord:
        self.reserve_calls += 1
        for existing in self.records.values():
            if (
                existing.owner_user_id == record.owner_user_id
                and existing.source_id == record.source_id
                and existing.creation_request_id == record.creation_request_id
            ):
                return existing
        self.records[record.session_id] = record
        return record

    async def get(self, session_id: str) -> SessionRecord | None:
        return self.records.get(session_id)

    async def set_conversation_if_unset(
        self, session_id: str, conversation_id: str
    ) -> SessionRecord | None:
        record = self.records.get(session_id)
        if record is None:
            return None
        if self.race_conversation_id and record.conversation_id is None:
            record = record.model_copy(
                update={"conversation_id": self.race_conversation_id}
            )
        if record.conversation_id is None:
            record = record.model_copy(update={"conversation_id": conversation_id})
        self.records[session_id] = record
        return record

    async def record_send(
        self,
        session_id: str,
        *,
        client_request_id: str,
        user_message_id: str,
        source_id: str,
        event_seq: int,
        at: datetime,
    ) -> SessionRecord | None:
        current = self.records[session_id]
        if current.request(client_request_id) is not None:
            return current
        record = current.model_copy(
            update={
                "last_client_request_id": client_request_id,
                "last_user_message_id": user_message_id,
                "last_send_event_seq": event_seq,
                "last_activity_at": at,
                "requests": (
                    *current.requests,
                    SessionRequest(
                        client_request_id=client_request_id,
                        user_message_id=user_message_id,
                        source_id=source_id,
                        sent_at=at,
                    ),
                ),
            }
        )
        self.records[session_id] = record
        return record

    async def record_accepted_agent(
        self, session_id: str, agent_id: str
    ) -> SessionRecord | None:
        record = self.records[session_id].model_copy(
            update={"last_accepted_agent_id": agent_id}
        )
        self.records[session_id] = record
        return record

    async def mark_closed(self, session_id: str, at: datetime) -> SessionRecord | None:
        record = self.records[session_id].model_copy(
            update={"status": "closed", "closed_at": at, "last_activity_at": at}
        )
        self.records[session_id] = record
        return record

    async def list_for_owner(
        self, owner_user_id: str, limit: int = 100
    ) -> list[SessionRecord]:
        records = [r for r in self.records.values() if r.owner_user_id == owner_user_id]
        return sorted(records, key=lambda r: r.last_activity_at, reverse=True)[:limit]

    async def list_open_conversations(self, owner_user_id: str) -> dict[str, str]:
        return {
            r.conversation_id: r.session_id
            for r in self.records.values()
            if r.owner_user_id == owner_user_id
            and r.status == "open"
            and r.conversation_id
        }


class FakeRooms:
    """RoomManagement + RoomRegistry over an in-memory container set."""

    def __init__(self) -> None:
        self.rooms: dict[str, RoomInfo] = {}
        self.created: list[CreateRoomRequest] = []
        self.deleted: list[tuple[str, str]] = []
        self.membership_updates: list[tuple[str, MembershipUpdateRequest]] = []
        self.names = {AGENT: "Travel Planner", OTHER_AGENT: "Other"}
        self._ids = iter(f"conv-{n}" for n in range(1, 100))

    async def create_room(self, request: CreateRoomRequest) -> RoomInfo:
        self.created.append(request)
        agent_ids = list(request.membership_seed.agent_ids or [])
        room = RoomInfo(
            conversation_id=next(self._ids),
            room_name=request.room_name,
            owner_id=request.owner_id,
            owner_name=request.owner_name,
            agent_ids=agent_ids,
            agent_set={a: self.names[a] for a in agent_ids},
            owner_product=request.owner_product,
        )
        self.rooms[room.conversation_id] = room
        return room

    async def delete_room(self, conversation_id: str, owner_id: str) -> bool:
        self.deleted.append((conversation_id, owner_id))
        return self.rooms.pop(conversation_id, None) is not None

    async def update_room(self, conversation_id: str, updates: dict) -> RoomInfo | None:
        raise AssertionError("not used")

    async def update_membership(
        self, conversation_id: str, request: MembershipUpdateRequest
    ) -> RoomInfo:
        self.membership_updates.append((conversation_id, request))
        room = self.rooms[conversation_id]
        agent_set = dict(room.agent_set)
        for agent_id in request.add_agent_ids or []:
            agent_set[agent_id] = self.names[agent_id]
        room = room.model_copy(
            update={"agent_set": agent_set, "agent_ids": list(agent_set)}
        )
        self.rooms[conversation_id] = room
        return room

    async def get_room(self, conversation_id: str) -> RoomInfo | None:
        return self.rooms.get(conversation_id)

    async def get_room_agents(self, conversation_id: str) -> list[str]:
        room = self.rooms.get(conversation_id)
        return list(room.agent_ids) if room else []

    async def get_room_owner(self, conversation_id: str) -> str | None:
        room = self.rooms.get(conversation_id)
        return room.owner_id if room else None


class FakeMessages:
    """RoomHistoryReader + RoomMessageStore reads."""

    def __init__(self) -> None:
        self.by_id: dict[str, RoomMessageInfo] = {}

    def add(self, message: RoomMessageInfo) -> None:
        self.by_id[message.message_id] = message

    async def get_messages_by_ids(self, message_ids: list[str]):
        return [self.by_id[m] for m in message_ids if m in self.by_id]

    async def get_messages_for_room(self, conversation_id, limit=100, before=None):
        return [m for m in self.by_id.values() if m.conversation_id == conversation_id][
            :limit
        ]

    async def get_message_thread(self, parent_message_id: str):
        return []

    async def get_message(self, message_id: str):
        return self.by_id.get(message_id)


@dataclass
class FakeEngine:
    events: InMemoryRoomEventStore
    ack: ExecutionAck | None = None
    acks: list[ExecutionAck] = field(default_factory=list)
    runs: list[RunInfo] = field(default_factory=list)
    runs_after_conflict: list[RunInfo] | None = None
    executed: list[ExecutionRequest] = field(default_factory=list)
    scheduled: list[tuple[ExecutionRequest, ExecutionAck]] = field(default_factory=list)
    canceled: list[tuple[str, str, str]] = field(default_factory=list)
    cancel_error: Exception | None = None
    # Events to append when the run is scheduled, as (type, payload) pairs.
    on_schedule: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    run_id: str = "run-1"
    # Message ids whose orchestration task has not ended.
    pending_messages: set[str] = field(default_factory=set)
    _message_counter: int = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionAck:
        self.executed.append(request)
        # Let a concurrent send interleave here, as a real engine call does.
        await asyncio.sleep(0)
        if self.acks:
            ack = self.acks.pop(0)
        elif self.ack is not None:
            ack = self.ack
        else:
            self._message_counter += 1
            ack = ExecutionAck(
                conversation_id=request.conversation_id,
                message_id=f"msg-{self._message_counter}",
                success=True,
                should_start_orchestration=True,
                preflight_outcome="ready",
            )
        if ack.status_code == 409 and self.runs_after_conflict is not None:
            self.runs = self.runs_after_conflict
        return ack

    async def start_orchestration(self, request, ack) -> None:
        raise AssertionError("session must schedule, never await, orchestration")

    def schedule_orchestration(self, request, ack) -> None:
        self.scheduled.append((request, ack))
        self.pending_events = [
            (request.conversation_id, request.client_request_id, kind, payload)
            for kind, payload in self.on_schedule
        ]

    def is_orchestration_pending(self, message_id: str) -> bool:
        return message_id in self.pending_messages

    async def flush(self) -> None:
        for conversation_id, correlation, kind, payload in getattr(
            self, "pending_events", []
        ):
            await append_event(
                self.events, conversation_id, kind, payload, correlation, self.run_id
            )
        self.pending_events = []

    async def cancel(self, conversation_id, message_id, *, requested_by_user_id):
        self.canceled.append((conversation_id, message_id, requested_by_user_id))
        if self.cancel_error is not None:
            raise self.cancel_error
        return True

    async def get_runs_for_room(self, conversation_id: str) -> list[RunInfo]:
        return [run for run in self.runs if run.conversation_id == conversation_id]

    async def get_latest_runs_for_rooms(self, room_ids):
        return {
            run.conversation_id: run
            for run in self.runs
            if run.conversation_id in room_ids
        }

    async def cancel_inflight_tasks(self) -> int:
        return 0


@dataclass
class FakeApprovals:
    pending: list[HITLRequest] = field(default_factory=list)
    canceled: list[tuple[str, str, int]] = field(default_factory=list)
    cancel_error: Exception | None = None

    async def get_pending_hitl(self, conversation_id: str) -> list[HITLRequest]:
        return [p for p in self.pending if p.conversation_id == conversation_id]

    async def cancel_hitl_interaction(
        self, conversation_id, interaction_id, expected_version
    ):
        self.canceled.append((conversation_id, interaction_id, expected_version))
        if self.cancel_error is not None:
            raise self.cancel_error
        return expected_version + 1

    async def resolve_hitl_batch(self, *args, **kwargs):
        raise AssertionError("session never answers approvals")


class FakeSources:
    """SourceDirectory over the identities a test uses."""

    def __init__(self) -> None:
        self.known = {s.source_id: s for s in (NO_TOKEN, CI, OTHER)}

    async def identities(self, source_ids: list[str]) -> dict[str, SourceIdentity]:
        return {i: self.known[i] for i in source_ids if i in self.known}


class FakeResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def resolve_target(self, agent_id: str, group_id: str | None = None):
        self.calls.append((agent_id, group_id))
        if self.error is not None:
            raise self.error
        from common.dto import AgentInfo

        return AgentInfo(agent_id=agent_id, name="Travel Planner")


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: list = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.on_sleep):
            await hook()


# -- Event helpers ----------------------------------------------------------


async def append_event(
    events: InMemoryRoomEventStore,
    conversation_id: str,
    kind: str,
    payload: dict[str, Any],
    correlation_id: str | None,
    run_id: str,
    ts: datetime | None = None,
) -> None:
    if kind == "hitl_response":
        payload_public = {"run_id": run_id, **payload}
    else:
        payload_public = {
            "event_id": f"evt-{kind}-{run_id}",
            "run_id": run_id,
            "seq": 1,
            "type": kind,
            "payload": payload,
            "correlation_id": correlation_id,
        }
        kind = "run_event"
    await events.append(
        conversation_id=conversation_id, kind=kind, payload_public=payload_public, ts=ts
    )


def settled(status: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    payload = {
        "status": status,
        "started_at": NOW.isoformat(),
        "settled_at": NOW.isoformat(),
        "duration_ms": 10,
        **extra,
    }
    return "run_settled", payload


STARTED = ("run_started", {"mode": "passthrough"})
WAITING = (
    "run_waiting_input",
    {"interaction_id": "int-1", "request_ids": ["req-1"], "requested_at": "x"},
)
REJECTED_FRAME = ("hitl_response", {"status": "canceled", "interaction_id": "int-1"})


# -- Harness ------------------------------------------------------------------


@dataclass
class Harness:
    facade: SessionFacade
    store: FakeSessionStore
    rooms: FakeRooms
    messages: FakeMessages
    engine: FakeEngine
    approvals: FakeApprovals
    events: InMemoryRoomEventStore
    clock: VirtualClock
    resolver: FakeResolver

    async def send(self, source: SourceIdentity = SOURCE, **values: Any):
        data = {
            "agent_id": AGENT,
            "text": "Plan a weekend in Lyon",
            "client_request_id": "cr-1",
            "client_label": "Buyer agent",
        }
        data.update(values)
        receipt = await self.facade.send(
            SessionMessageRequest(**data),
            owner=OWNER,
            source=source,
            resolver=self.resolver,
        )
        # The run proceeds after the receipt returns.
        await self.engine.flush()
        return receipt

    async def check(self, session_id: str = "sess-1", source: SourceIdentity = SOURCE):
        return await self.facade.check(session_id, owner=OWNER, source=source)


def make_harness(
    *,
    on_schedule: list[tuple[str, dict[str, Any]]] | None = None,
    start_grace_seconds: float = 10.0,
    resolver: FakeResolver | None = None,
) -> Harness:
    events = InMemoryRoomEventStore()
    clock = VirtualClock()
    engine = FakeEngine(events=events, on_schedule=list(on_schedule or []))
    store = FakeSessionStore()
    rooms = FakeRooms()
    messages = FakeMessages()
    approvals = FakeApprovals()
    waiter = SessionWaiter(events, start_grace_seconds=start_grace_seconds)
    facade = SessionFacade(
        store=store,
        rooms=rooms,
        registry=rooms,
        history=messages,
        messages=messages,
        engine=engine,
        approvals=approvals,
        waiter=waiter,
        sources=FakeSources(),
        now=lambda: NOW + timedelta(seconds=clock.now),
        id_factory=iter(f"sess-{n}" for n in range(1, 100)).__next__,
    )
    return Harness(
        facade=facade,
        store=store,
        rooms=rooms,
        messages=messages,
        engine=engine,
        approvals=approvals,
        events=events,
        clock=clock,
        resolver=resolver or FakeResolver(),
    )


def final_message(message_id: str, conversation_id: str, text: str) -> RoomMessageInfo:
    return RoomMessageInfo(
        conversation_id=conversation_id,
        message_id=message_id,
        message_type="agent",
        content={"message_text": text},
        agent_id="system:hybro",
        parent_message_id="msg-1",
    )


# -- Creation and reuse -------------------------------------------------------


@pytest.mark.asyncio
async def test_first_send_reserves_then_creates_session_container():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    h.messages.add(final_message("f1", "conv-1", "Here is your plan."))

    result = await h.send()

    assert result.session_id == "sess-1"
    assert result.status == "created"
    assert result.client_request_id == "cr-1"
    assert result.agent_id == AGENT
    record = h.store.records["sess-1"]
    assert record.conversation_id == "conv-1"
    assert record.creation_request_id == "cr-1"
    assert record.client_label == "Buyer agent"
    created = h.rooms.created[0]
    assert created.owner_product == "session"
    assert created.owner_id == OWNER.user_id
    assert created.owner_name == "Estate Agent"
    assert created.room_name.startswith("Buyer agent: Plan a weekend")
    # The agent joins only after the accepted ack.
    assert created.membership_seed.mode == "manual"
    assert created.membership_seed.agent_ids == []
    status = await h.check()
    assert status.state == "completed"
    assert status.history[-1].reply == "Here is your plan."


@pytest.mark.asyncio
async def test_execution_request_is_passthrough_with_one_agent_mention():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    await h.send(subnet_id="subnet-1")

    request = h.engine.executed[0]
    assert request.mode == "passthrough"
    assert request.conversation_id == "conv-1"
    assert request.sender_id == OWNER.user_id
    assert request.sender_name == "Buyer agent"
    assert request.client_request_id == "cr-1"
    assert request.message == {
        "conversation_id": "conv-1",
        "message_id": "",
        "message_type": "user",
        "message_content": {"message_text": "Plan a weekend in Lyon"},
    }
    # ExecutionFacade.execute validates the message into a RoomUserMessage.
    from models.room import RoomUserMessage

    assert (
        RoomUserMessage.model_validate(request.message).message_content.message_text
        == "Plan a weekend in Lyon"
    )
    assert request.agent_scope.source == "mention"
    assert request.agent_scope.agent_ids == [AGENT]
    assert h.resolver.calls == [(AGENT, "subnet-1")]
    # Scheduled, not awaited.
    assert len(h.engine.scheduled) == 1


@pytest.mark.asyncio
async def test_member_and_last_accepted_agent_recorded_after_accepted_ack():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    await h.send()

    record = h.store.records["sess-1"]
    assert record.last_accepted_agent_id == AGENT
    assert record.last_client_request_id == "cr-1"
    assert record.last_user_message_id == "msg-1"
    assert h.rooms.rooms["conv-1"].agent_ids == [AGENT]


@pytest.mark.asyncio
async def test_failed_session_write_still_starts_the_run():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    async def failing_record_send(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("session store unavailable")

    h.store.record_send = failing_record_send  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="session store unavailable"):
        await h.send()

    # The engine stored the message, so its run must start even though the
    # session record was not updated; otherwise it stays processing forever.
    assert len(h.engine.scheduled) == 1
    assert h.engine.scheduled[0][1].message_id == "msg-1"


@pytest.mark.asyncio
async def test_retry_after_failed_session_write_reports_the_run():
    h = make_harness(
        on_schedule=[STARTED, settled("completed", final_message_id="f1")],
        start_grace_seconds=1.0,
    )
    h.messages.add(final_message("f1", "conv-1", "Here is your plan."))
    record_send = h.store.record_send

    async def failing_record_send(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("session store unavailable")

    h.store.record_send = failing_record_send  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await h.send()
    h.store.record_send = record_send  # type: ignore[method-assign]
    await h.engine.flush()

    # The retry is replayed after the run settled; it must not skip its events.
    h.engine.ack = _replay_ack()
    replay = await h.send(session_id="sess-1", client_label=None)
    h.clock.now = 2.0
    status = await h.check()

    assert replay.state == "completed"
    assert status.state == "completed"
    assert status.history[-1].reply == "Here is your plan."
    assert len(h.engine.scheduled) == 1


@pytest.mark.asyncio
async def test_rejected_ack_does_not_add_member_or_record_agent():
    h = make_harness()
    h.engine.ack = ExecutionAck(
        success=False, error="Agent is not routable", status_code=400
    )

    with pytest.raises(AppError) as exc_info:
        await h.send()

    assert exc_info.value.code == "request_rejected"
    record = h.store.records["sess-1"]
    assert record.last_accepted_agent_id is None
    assert record.last_client_request_id is None
    assert h.rooms.rooms["conv-1"].agent_ids == []
    assert h.engine.scheduled == []


@pytest.mark.asyncio
async def test_reuse_by_handle_adds_second_agent_to_same_container():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    first = await h.send()

    second = await h.send(
        session_id=first.session_id,
        agent_id=OTHER_AGENT,
        client_request_id="cr-2",
        client_label=None,
    )

    assert second.session_id == first.session_id
    assert len(h.rooms.created) == 1
    assert h.engine.executed[1].conversation_id == "conv-1"
    assert h.rooms.rooms["conv-1"].agent_ids == [AGENT, OTHER_AGENT]
    assert h.store.records["sess-1"].last_accepted_agent_id == OTHER_AGENT


@pytest.mark.asyncio
async def test_duplicate_creation_request_reuses_the_reserved_session():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    first = await h.send()

    retry = await h.send()

    assert retry.session_id == first.session_id
    assert h.store.reserve_calls == 2
    assert len(h.store.records) == 1
    assert len(h.rooms.created) == 1


@pytest.mark.asyncio
async def test_retry_completes_a_reservation_left_without_container():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    # A crash after the reservation left a record without a container.
    h.store.records["sess-crashed"] = SessionRecord(
        session_id="sess-crashed",
        owner_user_id=OWNER.user_id,
        source_id=SOURCE.source_id,
        client_label="Buyer agent",
        creation_request_id="cr-1",
        opened_at=NOW,
        last_activity_at=NOW,
    )

    result = await h.send()

    assert result.session_id == "sess-crashed"
    assert h.store.records["sess-crashed"].conversation_id == "conv-1"
    assert (await h.check("sess-crashed")).state == "completed"


@pytest.mark.asyncio
async def test_concurrent_create_keeps_one_container_and_deletes_the_loser():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    h.rooms.rooms["conv-winner"] = RoomInfo(
        conversation_id="conv-winner",
        room_name="winner",
        owner_id=OWNER.user_id,
        owner_product="session",
    )
    h.store.race_conversation_id = "conv-winner"

    await h.send()

    assert h.store.records["sess-1"].conversation_id == "conv-winner"
    assert h.rooms.deleted == [("conv-1", OWNER.user_id)]
    assert h.engine.executed[0].conversation_id == "conv-winner"


@pytest.mark.asyncio
async def test_unknown_or_foreign_handle_is_not_found():
    h = make_harness()
    with pytest.raises(AppError) as exc_info:
        await h.send(session_id="missing")
    assert exc_info.value.code == "session_not_found"

    h.store.records["foreign"] = SessionRecord(
        session_id="foreign",
        owner_user_id="someone-else",
        client_label="x",
        opened_at=NOW,
        last_activity_at=NOW,
    )
    with pytest.raises(AppError) as exc_info:
        await h.send(session_id="foreign")
    assert exc_info.value.code == "session_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotFoundError("Agent", AGENT), "agent_not_found"),
        (ConflictError("Agent is not active"), "agent_unavailable"),
    ],
)
async def test_target_resolution_happens_before_any_write(error, code):
    h = make_harness(resolver=FakeResolver(error))

    with pytest.raises(AppError) as exc_info:
        await h.send()

    assert exc_info.value.code == code
    assert h.store.records == {}
    assert h.rooms.created == []
    assert h.engine.executed == []


# -- Validation ---------------------------------------------------------------


def _unvalidated(**values: Any) -> SessionMessageRequest:
    data = {
        "agent_id": AGENT,
        "text": "hi",
        "client_request_id": "cr-1",
        "client_label": "Buyer agent",
        "session_id": None,
        "subnet_id": None,
    }
    data.update(values)
    return SessionMessageRequest.model_construct(**data)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_request_id", ["", "   ", "x" * 129])
async def test_client_request_id_is_required_and_bounded(client_request_id):
    h = make_harness()

    with pytest.raises(AppError) as exc_info:
        await h.facade.send(
            _unvalidated(client_request_id=client_request_id),
            owner=OWNER,
            source=SOURCE,
            resolver=h.resolver,
        )

    assert exc_info.value.code == "invalid_request"
    assert h.engine.executed == []


@pytest.mark.asyncio
async def test_client_request_id_is_normalized():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    result = await h.send(client_request_id="  cr-9  ")

    assert result.client_request_id == "cr-9"
    assert h.engine.executed[0].client_request_id == "cr-9"


@pytest.mark.asyncio
async def test_text_limit_is_enforced():
    h = make_harness()
    with pytest.raises(AppError) as exc_info:
        await h.facade.send(
            _unvalidated(text="x" * 10_001),
            owner=OWNER,
            source=SOURCE,
            resolver=h.resolver,
        )
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_first_send_requires_client_label():
    h = make_harness()
    with pytest.raises(AppError) as exc_info:
        await h.facade.send(
            _unvalidated(client_label=None),
            owner=OWNER,
            source=SOURCE,
            resolver=h.resolver,
        )
    assert exc_info.value.code == "invalid_request"


# -- Busy and conflicts -------------------------------------------------------


def _run(state, *, trigger="msg-1", room="conv-1") -> RunInfo:
    return RunInfo(
        run_id="run-1", conversation_id=room, state=state, trigger_message_id=trigger
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RunState.AWAITING_INPUT, "waiting_on_owner"),
        (RunState.QUEUED, "working"),
        (RunState.PROCESSING, "working"),
        ("canceling", "working"),
    ],
)
async def test_busy_send_is_not_accepted(state, expected):
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    h.engine.runs = [_run(state)]

    result = await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )

    assert result.status == "busy"
    assert result.state == expected
    assert result.message.startswith("Not recorded")
    assert len(h.engine.executed) == 1


@pytest.mark.asyncio
async def test_fingerprint_conflict_maps_to_request_conflict():
    h = make_harness()
    h.engine.ack = ExecutionAck(
        success=False, error=FINGERPRINT_CONFLICT, status_code=409
    )

    with pytest.raises(AppError) as exc_info:
        await h.send()

    assert exc_info.value.code == "request_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "runs", "expected"),
    [
        (WAITING_FOR_INPUT, [_run(RunState.AWAITING_INPUT)], "waiting_on_owner"),
        (ALREADY_PROCESSING, [_run(RunState.AWAITING_INPUT)], "waiting_on_owner"),
        (ALREADY_PROCESSING, [_run(RunState.PROCESSING)], "working"),
    ],
)
async def test_other_execute_conflicts_reread_runs(message, runs, expected):
    h = make_harness()
    h.engine.ack = ExecutionAck(success=False, error=message, status_code=409)
    h.engine.runs_after_conflict = runs

    result = await h.send()

    assert result.status == "busy"
    assert result.state == expected


def test_pinned_execute_conflict_strings_match_the_engine():
    """The facade maps 409 acks by their fixed messages; pin them at source."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    facade_source = (root / "execution/facade.py").read_text()
    runtime_source = (root / "conversation/message_sending.py").read_text()
    assert WAITING_FOR_INPUT in facade_source
    assert ALREADY_PROCESSING in facade_source
    assert '"The client_request_id was already used for a different request"' in (
        runtime_source
    )

    from session.facade import FINGERPRINT_CONFLICT_ERROR

    assert FINGERPRINT_CONFLICT_ERROR == FINGERPRINT_CONFLICT


@pytest.mark.asyncio
async def test_preflight_404_keeps_its_code():
    h = make_harness()
    h.engine.ack = ExecutionAck(success=False, error="Room not found", status_code=404)

    with pytest.raises(AppError) as exc_info:
        await h.send()

    assert exc_info.value.code == "not_found"


# -- Waiting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_input_reports_waiting_on_owner_without_question_text():
    h = make_harness(on_schedule=[STARTED, WAITING])

    result = await h.send()
    status = await h.check(result.session_id)

    assert status.state == "waiting_on_owner"
    assert status.history[-1].state == "waiting_on_owner"
    assert status.history[-1].reply is None
    assert "Which hotel" not in status.model_dump_json()
    assert "Which hotel" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_tool_events_after_waiting_input_keep_waiting_on_owner():
    """The live kernel emits suspension tool events after run_waiting_input."""
    h = make_harness(
        on_schedule=[
            STARTED,
            WAITING,
            ("tool_execution_start", {}),
            ("model_decision", {}),
            ("tool_execution_update", {"status": "suspended"}),
        ]
    )

    result = await h.send()

    assert (await h.check(result.session_id)).state == "waiting_on_owner"


@pytest.mark.asyncio
async def test_send_returns_a_receipt_without_waiting_for_the_run():
    h = make_harness(on_schedule=[STARTED])

    receipt = await h.send()

    assert receipt.status == "created"
    assert receipt.state == "working"
    assert receipt.message.startswith("Created.")
    assert "session_id" in receipt.message
    assert receipt.created_at == NOW
    assert receipt.updated_at == NOW
    assert h.clock.now == 0.0


@pytest.mark.asyncio
async def test_no_correlated_event_reports_run_not_started_after_the_grace():
    h = make_harness(on_schedule=[], start_grace_seconds=2.0)
    await h.send()

    assert (await h.check()).state == "working"

    h.clock.now = 2.0
    status = await h.check()

    assert status.state == "failed"
    assert status.reason == "run_not_started"


@pytest.mark.asyncio
async def test_pending_orchestration_reports_working_past_the_grace():
    # A loaded backend can take longer than the grace to emit the first run
    # event; reporting failed then would flip to completed later.
    h = make_harness(on_schedule=[], start_grace_seconds=2.0)
    h.engine.pending_messages.add("msg-1")
    receipt = await h.send()
    h.clock.now = 30.0

    status = await h.check()

    assert receipt.state == "working"
    assert status.state == "working"
    assert status.reason is None
    assert [(item.state, item.reason) for item in status.history] == [("working", None)]

    h.engine.pending_messages.clear()
    status = await h.check()

    assert status.state == "failed"
    assert status.reason == "run_not_started"


@pytest.mark.asyncio
async def test_events_from_other_requests_are_ignored():
    h = make_harness(on_schedule=[STARTED])
    await append_event(
        h.events,
        "conv-1",
        *settled("completed", final_message_id="old"),
        correlation_id="other",
        run_id="run-0",
    )

    await h.send()

    assert (await h.check()).state == "working"


@pytest.mark.asyncio
async def test_failed_settlement_reports_public_summary():
    h = make_harness(
        on_schedule=[
            STARTED,
            settled(
                "failed",
                failure_code="internal_error",
                error_summary="The run could not be completed.",
            ),
        ]
    )

    await h.send()
    status = await h.check()

    assert status.state == "failed"
    assert status.reason == "The run could not be completed."
    assert status.history[-1].reason == "The run could not be completed."


@pytest.mark.asyncio
async def test_budget_exhaustion_maps_to_failed():
    h = make_harness(
        on_schedule=[
            STARTED,
            settled(
                "failed",
                failure_code="budget_exhausted",
                error_summary="The run exhausted its execution budget.",
            ),
        ]
    )

    await h.send()

    assert (await h.check()).state == "failed"


@pytest.mark.asyncio
async def test_owner_reject_maps_to_rejected():
    h = make_harness(
        on_schedule=[
            STARTED,
            WAITING,
            REJECTED_FRAME,
            settled("canceled", cancellation_code="user_requested"),
        ]
    )

    await h.send()

    assert (await h.check()).state == "rejected"


@pytest.mark.asyncio
async def test_other_cancellation_maps_to_failed():
    h = make_harness(
        on_schedule=[STARTED, settled("canceled", cancellation_code="shutdown")]
    )

    await h.send()

    assert (await h.check()).state == "failed"


# -- Replay -------------------------------------------------------------------


def _replay_ack() -> ExecutionAck:
    return ExecutionAck(
        conversation_id="conv-1",
        message_id="msg-1",
        success=True,
        should_start_orchestration=False,
    )


@pytest.mark.asyncio
async def test_replay_after_settlement_returns_the_settled_outcome():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    h.messages.add(final_message("f1", "conv-1", "Here is your plan."))
    first = await h.send()
    h.engine.ack = _replay_ack()

    replay = await h.send(session_id=first.session_id, client_label=None)

    assert replay.state == "completed"
    assert len(h.engine.scheduled) == 1
    assert (await h.check()).history[-1].reply == "Here is your plan."


@pytest.mark.asyncio
async def test_replay_during_active_run_skips_busy_check():
    h = make_harness(on_schedule=[STARTED])
    first = await h.send()
    h.engine.runs = [_run(RunState.PROCESSING)]
    h.engine.ack = _replay_ack()

    replay = await h.send(session_id=first.session_id, client_label=None)

    assert replay.status == "created"
    assert replay.state == "working"
    assert len(h.engine.executed) == 2


@pytest.mark.asyncio
async def test_replay_after_run_not_started_still_reports_failed():
    h = make_harness(on_schedule=[], start_grace_seconds=1.0)
    first = await h.send()
    h.clock.now = 1.0
    h.engine.ack = _replay_ack()

    replay = await h.send(session_id=first.session_id, client_label=None)

    assert replay.status == "created"
    assert replay.state == "failed"
    assert (await h.check()).reason == "run_not_started"


# -- Check, list, record ------------------------------------------------------


@pytest.mark.asyncio
async def test_check_resolves_from_events_and_returns_the_reply():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    await append_event(
        h.events,
        "conv-1",
        "run_resumed",
        {"interaction_id": "int-1"},
        "cr-1",
        "run-1",
    )

    status = await h.check(first.session_id)
    assert status.state == "working"

    await append_event(
        h.events,
        "conv-1",
        *settled("completed", final_message_id="f1"),
        correlation_id="cr-1",
        run_id="run-1",
    )
    h.messages.add(final_message("f1", "conv-1", "Booked."))
    status = await h.check(first.session_id)

    assert status.state == "completed"
    assert [entry.reply for entry in status.history] == ["Booked."]
    assert [agent.agent_id for agent in status.agents] == [AGENT]
    assert status.created_at == NOW
    assert "conversation_id" not in status.model_dump()


@pytest.mark.asyncio
async def test_owner_record_reports_paused_agent_from_trigger_message():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    second_agent_send = h.store.records["sess-1"].model_copy(
        update={"last_accepted_agent_id": OTHER_AGENT}
    )
    h.store.records["sess-1"] = second_agent_send
    h.engine.runs = [_run(RunState.AWAITING_INPUT, trigger="msg-1")]
    h.messages.add(
        RoomMessageInfo(
            conversation_id="conv-1",
            message_id="msg-1",
            message_type="user",
            content={"message_text": "x"},
            metadata={"extend_info": {"candidate_agent_ids": [AGENT]}},
        )
    )

    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)

    assert record.state == "waiting_on_owner"
    assert record.paused_agent_id == AGENT


@pytest.mark.asyncio
async def test_paused_agent_falls_back_to_last_accepted_agent():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()

    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)

    assert record.paused_agent_id == AGENT


@pytest.mark.asyncio
async def test_check_without_correlated_events_reports_run_not_started():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f")])
    first = await h.send()
    h.store.records["sess-1"] = h.store.records["sess-1"].model_copy(
        update={"last_client_request_id": "never-ran"}
    )
    h.clock.now = 11.0

    status = await h.check(first.session_id)

    assert status.state == "failed"
    assert status.reason == "run_not_started"


@pytest.mark.asyncio
async def test_list_and_record_are_owner_views_with_conversation_id():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()

    listing = await h.facade.list_for_owner(owner=OWNER)
    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)

    assert [item.session_id for item in listing.sessions] == [first.session_id]
    item = listing.sessions[0]
    assert item.state == "waiting_on_owner"
    assert item.conversation_id == "conv-1"
    assert item.client_label == "Buyer agent"
    assert record.conversation_id == "conv-1"
    assert record.state == "waiting_on_owner"
    assert record.status == "open"

    other = SessionOwner(user_id="someone-else", name="x")
    assert (await h.facade.list_for_owner(owner=other)).sessions == []
    with pytest.raises(AppError):
        await h.facade.record_for_owner(first.session_id, owner=other)


# -- Close --------------------------------------------------------------------


def _pending(version: int = 3) -> HITLRequest:
    return HITLRequest(
        request_id="req-1",
        conversation_id="conv-1",
        user_message_id="msg-1",
        source="agent",
        prompt="Which hotel may I book?",
        interaction_id="int-1",
        interaction_version=version,
    )


@pytest.mark.asyncio
async def test_close_with_pending_approval_cancels_the_interaction():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    h.approvals.pending = [_pending(version=3)]
    h.engine.runs = [_run(RunState.AWAITING_INPUT)]

    closed = await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert closed.status == "closed"
    assert h.approvals.canceled == [("conv-1", "int-1", 3)]
    assert h.engine.canceled == []
    assert h.store.records["sess-1"].status == "closed"


@pytest.mark.asyncio
async def test_close_without_approval_cancels_the_active_run():
    h = make_harness(on_schedule=[STARTED])
    first = await h.send()
    h.engine.runs = [_run(RunState.PROCESSING, trigger="msg-1")]

    await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert h.engine.canceled == [("conv-1", "msg-1", OWNER.user_id)]


@pytest.mark.asyncio
async def test_close_tolerates_cancellation_errors():
    h = make_harness(on_schedule=[STARTED])
    first = await h.send()
    h.engine.runs = [_run(RunState.PROCESSING)]
    h.engine.cancel_error = RuntimeError(
        "local Agent-call cancellation remains pending"
    )

    closed = await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert closed.status == "closed"


@pytest.mark.asyncio
async def test_close_cancels_the_run_when_no_interaction_cancel_succeeds():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    h.approvals.pending = [_pending()]
    h.approvals.cancel_error = RuntimeError("version conflict")
    h.engine.runs = [_run(RunState.AWAITING_INPUT, trigger="msg-1")]

    closed = await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert closed.status == "closed"
    # The interaction could not be canceled, so the run itself is stopped.
    assert h.engine.canceled == [("conv-1", "msg-1", OWNER.user_id)]


@pytest.mark.asyncio
async def test_owner_list_keeps_a_waiting_session_beyond_the_page():
    h = make_harness(on_schedule=[STARTED, WAITING])
    await h.send()
    h.engine.runs = [_run(RunState.AWAITING_INPUT, trigger="msg-1")]
    waiting = h.store.records["sess-1"]
    # 100 newer sessions fill the page; none of them waits on the owner.
    for index in range(100):
        newer = waiting.model_copy(
            update={
                "session_id": f"newer-{index}",
                "conversation_id": None,
                "creation_request_id": f"newer-{index}",
                "requests": (),
                "last_client_request_id": None,
                "last_activity_at": waiting.last_activity_at
                + timedelta(minutes=index + 1),
            }
        )
        h.store.records[newer.session_id] = newer

    sessions = (await h.facade.list_for_owner(owner=OWNER)).sessions

    listed = {session.session_id: session.state for session in sessions}
    assert listed["sess-1"] == "waiting_on_owner"


@pytest.mark.asyncio
async def test_close_tolerates_approval_version_conflict():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    h.approvals.pending = [_pending()]
    h.approvals.cancel_error = RuntimeError("version conflict")

    closed = await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert closed.status == "closed"


@pytest.mark.asyncio
async def test_closed_session_refuses_sends_and_takes_state_precedence():
    h = make_harness(
        on_schedule=[
            STARTED,
            WAITING,
            REJECTED_FRAME,
            settled("canceled", cancellation_code="user_requested"),
        ]
    )
    first = await h.send()
    await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    status = await h.check(first.session_id)
    assert status.state == "closed"

    with pytest.raises(AppError) as exc_info:
        await h.send(session_id=first.session_id, client_label=None)
    assert exc_info.value.code == "session_closed"


@pytest.mark.asyncio
async def test_close_is_idempotent():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f")])
    first = await h.send()

    await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)
    again = await h.facade.close(first.session_id, owner=OWNER, source=SOURCE)

    assert again.status == "closed"


# -- Waiter -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiter_pages_past_the_read_limit():
    events = InMemoryRoomEventStore()
    for index in range(7):
        await append_event(events, "conv", "run_event_noise", {}, f"other-{index}", "r")
    await append_event(
        events, "conv", *settled("completed", final_message_id="f"), "cr", "run-1"
    )
    waiter = SessionWaiter(events, start_grace_seconds=1, page_size=3)

    outcome = await waiter.scan("conv", "cr", after=0)

    assert outcome.settlement is not None
    assert outcome.settlement["final_message_id"] == "f"
    assert outcome.last_seq == 8


@pytest.mark.asyncio
async def test_waiter_starts_after_the_recorded_sequence():
    events = InMemoryRoomEventStore()
    await append_event(
        events, "conv", *settled("completed", final_message_id="old"), "cr", "run-0"
    )
    waiter = SessionWaiter(events, start_grace_seconds=1)

    after = await waiter.latest_seq("conv")
    outcome = await waiter.scan("conv", "cr", after=after)

    assert outcome.seen is False


def test_session_record_ids_are_opaque_and_long():
    from session.models import new_session_id

    first, second = new_session_id(), new_session_id()
    assert first != second
    assert len(first) >= 32


def test_timestamps_round_trip():
    record = SessionRecord(
        session_id="s",
        owner_user_id="o",
        client_label="c",
        opened_at=NOW,
        last_activity_at=NOW + timedelta(seconds=1),
    )
    assert record.status == "open"
    assert record.conversation_id is None
    assert record.last_send_event_seq == 0


@pytest.mark.asyncio
async def test_first_message_titles_the_session_for_the_owner():
    h = make_harness(on_schedule=[STARTED, WAITING, STARTED, WAITING])
    first = await h.send(text="  Plan a weekend\n in   Lyon  ")
    await h.send(
        session_id=first.session_id,
        text="Something else",
        client_request_id="cr-2",
        client_label=None,
    )

    assert h.store.records["sess-1"].title == "Plan a weekend in Lyon"
    listing = await h.facade.list_for_owner(owner=OWNER)
    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)
    assert listing.sessions[0].title == "Plan a weekend in Lyon"
    assert record.title == "Plan a weekend in Lyon"


@pytest.mark.asyncio
async def test_long_first_message_title_is_shortened():
    h = make_harness(on_schedule=[STARTED, WAITING])
    await h.send(text="word " * 100)

    title = h.store.records["sess-1"].title
    assert title is not None
    assert len(title) <= 120
    assert title.endswith("…")


@pytest.mark.asyncio
async def test_activity_time_follows_the_latest_event_of_the_request():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send()
    answered = NOW + timedelta(minutes=3)
    await append_event(
        h.events,
        "conv-1",
        "run_resumed",
        {"interaction_id": "int-1"},
        correlation_id="cr-1",
        run_id=h.engine.run_id,
        ts=answered.replace(tzinfo=None),
    )

    status = await h.check(first.session_id)
    listing = await h.facade.list_for_owner(owner=OWNER)
    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)

    assert status.updated_at == answered
    assert listing.sessions[0].last_activity_at == answered
    assert record.last_activity_at == answered


@pytest.mark.asyncio
async def test_reply_times_are_utc():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    message = final_message("f1", "conv-1", "Here is your plan.").model_copy(
        update={"created_at": datetime(2026, 10, 15, 21, 53, 48)}
    )
    h.messages.add(message)

    await h.send()
    status = await h.check()

    [entry] = status.history
    assert entry.replied_at == datetime(2026, 10, 15, 21, 53, 48, tzinfo=UTC)
    assert entry.model_dump(mode="json")["replied_at"] == "2026-10-15T21:53:48Z"
    assert status.updated_at == datetime(2026, 10, 15, 21, 53, 48, tzinfo=UTC)


def user_message(message_id: str, text: str) -> RoomMessageInfo:
    return RoomMessageInfo(
        conversation_id="conv-1",
        message_id=message_id,
        message_type="user",
        content={"message_text": text},
        metadata={"extend_info": {"candidate_agent_ids": [AGENT]}},
    )


def started(message_id: str) -> tuple[str, dict[str, Any]]:
    return ("run_started", {"mode": "passthrough", "user_message_id": message_id})


@pytest.mark.asyncio
async def test_history_lists_each_request_with_its_reply_oldest_first():
    h = make_harness(
        on_schedule=[
            started("msg-1"),
            ("tool_execution_start", {"tool_name": "Travel Planner Agent - Trips"}),
            settled("completed", final_message_id="f1"),
        ]
    )
    h.messages.add(user_message("msg-1", "Plan a weekend in Lyon"))
    h.messages.add(final_message("f1", "conv-1", "Day 1: Vieux Lyon"))
    first = await h.send(text="Plan a weekend in Lyon")
    h.engine.on_schedule = [started("msg-2"), WAITING]
    h.messages.add(user_message("msg-2", "Make it three days"))
    await h.send(
        session_id=first.session_id,
        client_request_id="cr-2",
        client_label=None,
        text="Make it three days",
    )
    # The owner's question and answer are in the log but never reach the client.
    await h.events.append(
        conversation_id="conv-1",
        kind="hitl_request",
        payload_public={"request_id": "q1", "prompt": "Which hotel budget?"},
    )

    status = await h.check()

    assert [
        (entry.client_request_id, entry.text, entry.state, entry.reply)
        for entry in status.history
    ] == [
        ("cr-1", "Plan a weekend in Lyon", "completed", "Day 1: Vieux Lyon"),
        ("cr-2", "Make it three days", "waiting_on_owner", None),
    ]
    assert status.history[0].agent_id == AGENT
    # The room's own name for the agent wins over the tool label.
    assert status.history[0].agent_name == status.agents[0].name
    assert status.state == "waiting_on_owner"
    assert "Which hotel budget" not in status.model_dump_json()


@pytest.mark.asyncio
async def test_history_includes_a_request_whose_run_has_not_started():
    h = make_harness(on_schedule=[])
    h.messages.add(user_message("msg-1", "Plan a weekend in Lyon"))

    await h.send(text="Plan a weekend in Lyon")
    [entry] = (await h.check()).history

    assert entry.client_request_id == "cr-1"
    assert entry.text == "Plan a weekend in Lyon"
    assert entry.state == "working"


# -- Earlier requests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sends_of_one_request_list_it_once():
    h = make_harness(on_schedule=[started("msg-1")])
    first = await h.send()
    h.engine.acks = [
        ExecutionAck(
            conversation_id="conv-1",
            message_id="msg-2",
            success=True,
            should_start_orchestration=True,
            preflight_outcome="ready",
        ),
        ExecutionAck(
            conversation_id="conv-1",
            message_id="msg-2",
            success=True,
            should_start_orchestration=False,
        ),
    ]
    data = SessionMessageRequest(
        session_id=first.session_id,
        agent_id=AGENT,
        text="Make it three days",
        client_request_id="cr-2",
    )

    receipts = await asyncio.gather(
        h.facade.send(data, owner=OWNER, source=SOURCE, resolver=h.resolver),
        h.facade.send(data, owner=OWNER, source=SOURCE, resolver=h.resolver),
    )

    record = h.store.records["sess-1"]
    assert [r.client_request_id for r in record.requests] == ["cr-1", "cr-2"]
    assert record.last_client_request_id == "cr-2"
    assert record.last_user_message_id == record.requests[-1].user_message_id
    # The replay starts nothing; only the first send scheduled a run.
    assert len(h.engine.scheduled) == 2
    assert [(r.status, r.state) for r in receipts] == [
        ("accepted", "working"),
        ("accepted", "working"),
    ]


@pytest.mark.asyncio
async def test_the_creating_send_schedules_its_run_even_when_it_loses_the_record():
    h = make_harness(on_schedule=[started("msg-1")])
    first = await h.send()
    start = ExecutionAck(
        conversation_id="conv-1",
        message_id="msg-2",
        success=True,
        should_start_orchestration=True,
        preflight_outcome="ready",
    )
    replay = ExecutionAck(
        conversation_id="conv-1",
        message_id="msg-2",
        success=True,
        should_start_orchestration=False,
    )
    calls = 0

    async def execute(request: ExecutionRequest) -> ExecutionAck:
        # The creating call is slow; the replay returns and records first.
        nonlocal calls
        calls += 1
        h.engine.executed.append(request)
        if calls == 1:
            for _ in range(3):
                await asyncio.sleep(0)
            return start
        await asyncio.sleep(0)
        return replay

    real_latest_seq = h.facade._waiter.latest_seq

    async def latest_seq(conversation_id: str) -> int:
        # A room event lands between the two sends' reads.
        seq = await real_latest_seq(conversation_id)
        await h.events.append(
            conversation_id=conversation_id, kind="noise", payload_public={}
        )
        return seq

    real_earlier = h.facade._earlier_receipt
    earlier_calls: list[str] = []

    async def earlier_receipt(record, earlier, agent_id):
        earlier_calls.append(earlier.client_request_id)
        return await real_earlier(record, earlier, agent_id)

    h.engine.execute = execute  # type: ignore[method-assign]
    h.facade._waiter.latest_seq = latest_seq  # type: ignore[method-assign]
    h.facade._earlier_receipt = earlier_receipt  # type: ignore[method-assign]
    data = SessionMessageRequest(
        session_id=first.session_id,
        agent_id=AGENT,
        text="Make it three days",
        client_request_id="cr-2",
    )

    receipts = await asyncio.gather(
        h.facade.send(data, owner=OWNER, source=SOURCE, resolver=h.resolver),
        h.facade.send(data, owner=OWNER, source=SOURCE, resolver=h.resolver),
    )

    scheduled = [request.client_request_id for request, _ack in h.engine.scheduled]
    assert scheduled == ["cr-1", "cr-2"]
    assert [r.status for r in receipts] == ["accepted", "accepted"]
    # The creating send lost the record and reported the recorded request.
    assert earlier_calls == ["cr-2"]
    assert h.store.records["sess-1"].last_accepted_agent_id == AGENT


def _stale_get_once(h: Harness, snapshot: SessionRecord) -> None:
    """The next read returns the record as it was before a concurrent send."""
    real_get = h.store.get
    calls = 0

    async def get(session_id: str) -> SessionRecord | None:
        nonlocal calls
        calls += 1
        return snapshot if calls == 1 else await real_get(session_id)

    h.store.get = get  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_losing_duplicate_send_reports_the_recorded_request():
    h = make_harness(start_grace_seconds=1.0)
    h.engine.on_schedule = [
        started("msg-1"),
        settled("completed", final_message_id="f1"),
    ]
    first = await h.send()
    snapshot = h.store.records["sess-1"]
    h.engine.run_id = "run-2"
    h.engine.on_schedule = [
        started("msg-2"),
        settled("completed", final_message_id="f2"),
    ]
    await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )
    winner = h.store.records["sess-1"]
    # The losing send read the session before cr-2 was recorded, and its
    # engine call returns after the run settled and the start grace passed.
    h.clock.now = 30.0
    h.engine.ack = ExecutionAck(
        conversation_id="conv-1",
        message_id="msg-2",
        success=True,
        should_start_orchestration=False,
    )
    _stale_get_once(h, snapshot)

    receipt = await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )

    assert (receipt.status, receipt.state) == ("accepted", "completed")
    assert h.store.records["sess-1"] == winner


@pytest.mark.asyncio
async def test_a_losing_duplicate_send_is_not_reported_busy():
    h = make_harness()
    h.engine.on_schedule = [
        started("msg-1"),
        settled("completed", final_message_id="f1"),
    ]
    first = await h.send()
    snapshot = h.store.records["sess-1"]
    h.engine.run_id = "run-2"
    h.engine.on_schedule = [started("msg-2")]
    await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )
    # cr-2's run is active when the losing send, holding the old record,
    # reaches the busy check.
    h.engine.runs = [_run(RunState.PROCESSING, trigger="msg-2")]
    h.engine.ack = ExecutionAck(
        conversation_id="conv-1",
        message_id="msg-2",
        success=True,
        should_start_orchestration=False,
    )
    _stale_get_once(h, snapshot)

    receipt = await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )

    assert (receipt.status, receipt.state) == ("accepted", "working")


@pytest.mark.asyncio
async def test_reusing_an_earlier_request_id_for_other_text_conflicts():
    h = make_harness()
    first = await _two_completed_requests(h)
    h.engine.ack = ExecutionAck(
        conversation_id="conv-1",
        message_id=None,
        success=False,
        error=FINGERPRINT_CONFLICT,
        status_code=409,
    )

    with pytest.raises(AppError) as exc_info:
        await h.send(session_id=first.session_id, client_label=None, text="Other")

    assert exc_info.value.code == "request_conflict"


async def _two_completed_requests(h: Harness):
    h.engine.on_schedule = [
        started("msg-1"),
        settled("completed", final_message_id="f1"),
    ]
    h.messages.add(user_message("msg-1", "Plan a weekend in Lyon"))
    h.messages.add(final_message("f1", "conv-1", "Day 1: Vieux Lyon"))
    first = await h.send(text="Plan a weekend in Lyon")
    h.engine.run_id = "run-2"
    h.engine.on_schedule = [
        started("msg-2"),
        settled("completed", final_message_id="f2"),
    ]
    h.messages.add(user_message("msg-2", "Make it three days"))
    h.messages.add(final_message("f2", "conv-1", "Day 3: Croix-Rousse"))
    await h.send(
        session_id=first.session_id,
        client_request_id="cr-2",
        client_label=None,
        text="Make it three days",
    )
    return first


@pytest.mark.asyncio
async def test_retrying_an_earlier_request_returns_its_receipt_without_resending():
    h = make_harness(start_grace_seconds=1.0)
    first = await _two_completed_requests(h)
    h.clock.now = 60.0
    h.engine.ack = _replay_ack()

    retry = await h.send(
        session_id=first.session_id, client_label=None, text="Plan a weekend in Lyon"
    )

    assert retry.client_request_id == "cr-1"
    assert retry.status == "created"
    assert retry.state == "completed"
    # The engine replays the stored message; no run starts and no send is added.
    assert len(h.engine.scheduled) == 2
    assert len(h.store.records["sess-1"].requests) == 2
    status = await h.check()
    assert status.state == "completed"
    assert [(e.client_request_id, e.state) for e in status.history] == [
        ("cr-1", "completed"),
        ("cr-2", "completed"),
    ]


@pytest.mark.asyncio
async def test_retrying_an_earlier_request_while_a_later_one_runs_is_not_busy():
    h = make_harness()
    h.engine.on_schedule = [
        started("msg-1"),
        settled("completed", final_message_id="f1"),
    ]
    h.messages.add(final_message("f1", "conv-1", "Day 1: Vieux Lyon"))
    first = await h.send()
    h.engine.run_id = "run-2"
    h.engine.on_schedule = [started("msg-2")]
    await h.send(
        session_id=first.session_id, client_request_id="cr-2", client_label=None
    )
    h.engine.runs = [_run(RunState.PROCESSING, trigger="msg-2")]
    h.engine.ack = _replay_ack()

    retry = await h.send(session_id=first.session_id, client_label=None)

    assert retry.status == "created"
    assert retry.state == "completed"
    assert len(h.engine.scheduled) == 2
    assert (await h.check()).state == "working"


@pytest.mark.asyncio
async def test_an_unstarted_request_stays_in_history_after_the_next_send():
    h = make_harness(on_schedule=[], start_grace_seconds=1.0)
    h.messages.add(user_message("msg-1", "Plan a weekend in Lyon"))
    first = await h.send(text="Plan a weekend in Lyon")
    h.clock.now = 5.0
    h.engine.on_schedule = [started("msg-2"), WAITING]
    h.messages.add(user_message("msg-2", "Make it three days"))
    await h.send(
        session_id=first.session_id,
        client_request_id="cr-2",
        client_label=None,
        text="Make it three days",
    )

    history = (await h.check()).history

    assert [(e.client_request_id, e.text, e.state, e.reason) for e in history] == [
        ("cr-1", "Plan a weekend in Lyon", "failed", "run_not_started"),
        ("cr-2", "Make it three days", "waiting_on_owner", None),
    ]


@pytest.mark.asyncio
async def test_waiter_history_reads_each_event_once_and_keeps_rejects_per_run(
    monkeypatch,
):
    from session import waiter as waiter_module

    events = InMemoryRoomEventStore()
    for index in range(1, 4):
        correlation, run_id = f"cr-{index}", f"run-{index}"
        await append_event(events, "conv", *STARTED, correlation, run_id)
        if index == 2:
            await append_event(events, "conv", *REJECTED_FRAME, None, run_id)
            await append_event(
                events, "conv", *settled("canceled"), correlation, run_id
            )
        else:
            await append_event(
                events, "conv", *settled("completed"), correlation, run_id
            )
    calls = 0
    absorb = waiter_module.CorrelatedOutcome.absorb

    def counting(self, record, correlation_id):
        nonlocal calls
        calls += 1
        return absorb(self, record, correlation_id)

    monkeypatch.setattr(waiter_module.CorrelatedOutcome, "absorb", counting)
    waiter = SessionWaiter(events, start_grace_seconds=1)

    outcomes = await waiter.history("conv")

    assert {key: o.rejected for key, o in outcomes.items()} == {
        "cr-1": False,
        "cr-2": True,
        "cr-3": False,
    }
    # Six run events plus one owner frame fanned out to the two known requests.
    assert calls == 8


# -- Sources ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_session_and_each_step_name_the_source_that_sent_them():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    first = await h.send(source=CI, client_label=None)
    await h.send(source=CI, session_id=first.session_id, client_request_id="cr-2")

    record = h.store.records[first.session_id]
    assert record.source_id == CI.source_id
    assert [r.source_id for r in record.requests] == [CI.source_id] * 2


@pytest.mark.asyncio
async def test_a_no_token_session_is_recorded_under_no_token():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    first = await h.send()

    record = h.store.records[first.session_id]
    assert record.source_id == NO_TOKEN.source_id
    assert record.requests[0].source_id == NO_TOKEN.source_id


@pytest.mark.asyncio
async def test_client_label_is_optional_with_a_token_and_names_fall_back():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    first = await h.send(source=CI, client_label=None)

    record = h.store.records[first.session_id]
    assert record.client_label is None
    assert h.rooms.created[0].room_name.startswith("CI bot: Plan a weekend")
    assert h.engine.executed[0].sender_name == "CI bot"


@pytest.mark.asyncio
async def test_client_label_is_kept_with_a_token_when_given():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    await h.send(source=CI, client_label="nightly job")

    assert h.engine.executed[0].sender_name == "nightly job"


@pytest.mark.asyncio
async def test_opening_without_a_token_still_needs_client_label():
    h = make_harness()
    with pytest.raises(AppError) as exc_info:
        await h.send(client_label=None)
    assert exc_info.value.code == "invalid_request"
    assert "client_label" in exc_info.value.message
    assert h.store.records == {}


@pytest.mark.asyncio
async def test_another_source_cannot_read_send_to_or_close_a_session():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    first = await h.send(source=CI)

    for source in (OTHER, NO_TOKEN):
        with pytest.raises(AppError) as read:
            await h.check(first.session_id, source=source)
        with pytest.raises(AppError) as sent:
            await h.send(
                source=source, session_id=first.session_id, client_request_id="x"
            )
        with pytest.raises(AppError) as closed:
            await h.facade.close(first.session_id, owner=OWNER, source=source)
        assert read.value.code == sent.value.code == closed.value.code
        assert read.value.code == "session_not_found"
    assert h.store.records[first.session_id].status == "open"
    assert (await h.check(first.session_id, source=CI)).session_id == first.session_id


@pytest.mark.asyncio
async def test_a_session_from_before_sources_counts_as_no_token():
    h = make_harness()
    h.store.records["old"] = SessionRecord(
        session_id="old",
        owner_user_id=OWNER.user_id,
        client_label="legacy",
        opened_at=NOW,
        last_activity_at=NOW,
    )

    assert (await h.check("old", source=NO_TOKEN)).session_id == "old"
    with pytest.raises(AppError):
        await h.check("old", source=CI)
    listing = await h.facade.list_for_owner(owner=OWNER)
    assert listing.sessions[0].source.source_id == NO_TOKEN.source_id
    assert listing.sessions[0].source.name == "No token"
    assert listing.sessions[0].source.has_token is False


@pytest.mark.asyncio
async def test_a_reused_request_id_from_another_source_opens_a_new_session():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])

    first = await h.send(source=CI)
    second = await h.send(source=OTHER)

    assert first.session_id != second.session_id
    assert second.status == "created"
    assert h.store.records[second.session_id].source_id == OTHER.source_id


@pytest.mark.asyncio
async def test_reach_is_checked_after_reserve_too():
    h = make_harness()

    async def foreign(record):
        return record.model_copy(update={"source_id": OTHER.source_id})

    h.store.reserve = foreign
    with pytest.raises(AppError) as exc_info:
        await h.send(source=CI)
    assert exc_info.value.code == "session_not_found"
    assert h.engine.executed == []


@pytest.mark.asyncio
async def test_owner_payloads_carry_the_source():
    h = make_harness(on_schedule=[STARTED, settled("completed", final_message_id="f1")])
    first = await h.send(source=CI, client_label=None)

    listing = await h.facade.list_for_owner(owner=OWNER)
    record = await h.facade.record_for_owner(first.session_id, owner=OWNER)

    for view in (listing.sessions[0], record):
        assert view.client_label is None
        assert view.source.model_dump() == {
            "source_id": CI.source_id,
            "name": "CI bot",
            "has_token": True,
        }


@pytest.mark.asyncio
async def test_an_unknown_stored_source_is_shown_by_its_id():
    h = make_harness()
    h.store.records["s"] = SessionRecord(
        session_id="s",
        owner_user_id=OWNER.user_id,
        source_id="src_gone",
        opened_at=NOW,
        last_activity_at=NOW,
    )

    record = await h.facade.record_for_owner("s", owner=OWNER)

    assert record.source.model_dump() == {
        "source_id": "src_gone",
        "name": "src_gone",
        "has_token": False,
    }


@pytest.mark.asyncio
async def test_the_owner_closes_any_source_session():
    h = make_harness(on_schedule=[STARTED, WAITING])
    first = await h.send(source=CI)

    closed = await h.facade.close_for_owner(first.session_id, owner=OWNER)

    assert closed.status == "closed"
    assert h.store.records[first.session_id].status == "closed"
    other = SessionOwner(user_id="someone-else", name="x")
    with pytest.raises(AppError) as exc_info:
        await h.facade.close_for_owner(first.session_id, owner=other)
    assert exc_info.value.code == "session_not_found"
