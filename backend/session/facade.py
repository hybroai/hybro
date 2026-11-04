"""Session facade: client sends, checks, and closes; owner lists and reads.

A session sends each client message to one named agent through the
passthrough execution mode (no orchestrator model turns), records every turn
in a conversation container owned by the Session product, and lets the owner
decide agent interactions in the Hybro app. The client sees only the session
state; it never sees the container id or the approval question.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from common.dto import (
    CreateRoomRequest,
    ExecutionAck,
    ExecutionRequest,
    MembershipSeed,
    MembershipUpdateRequest,
    RunInfo,
)
from common.errors import AppError
from common.idempotency import (
    MAX_CLIENT_REQUEST_ID_LENGTH,
    normalize_client_request_id,
)
from common.observability import events as E
from common.protocols import (
    AgentNetworkTargetResolver,
    ExecutionEngine,
    HITLManager,
    RoomHistoryReader,
)
from common.utils.logger import get_logger
from common.utils.time import utcnow
from models.room import MAX_MESSAGE_LENGTH
from models.session import (
    RECEIPT_TIP,
    SessionAgent,
    SessionClosed,
    SessionHistoryEntry,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionOwnerRecord,
    SessionReceiptStatus,
    SessionState,
    SessionStatus,
    SessionSummary,
)
from session.models import SessionRecord, SessionRequest, new_session_id
from session.projection import (
    busy_state,
    final_message_id,
    resolve_state,
)
from session.protocols import (
    RoomManagement,
    RoomMessageStore,
    RoomRegistry,
    SessionOwner,
    SessionStore,
)
from session.waiter import CorrelatedOutcome, SessionWaiter

logger = get_logger(__name__)

# The engine's 409 acks carry no error code; the fingerprint conflict is
# recognised by its fixed text (conversation runtime) and pinned by a test.
FINGERPRINT_CONFLICT_ERROR = (
    "The client_request_id was already used for a different request"
)
_RECEIPT_MESSAGES: dict[str, str] = {
    "created": "Created.",
    "accepted": "Received.",
    "busy": (
        "Not recorded: an earlier message in this session is still active. "
        "Send it again once that request finishes."
    ),
}
_ROOM_NAME_WORDS = 8
_ROOM_NAME_MAX = 80
_TITLE_MAX = 120
_LIST_LIMIT = 100


def _error(code: str, message: str) -> AppError:
    return AppError(message, code=code)


def _state_value(run: RunInfo) -> str:
    return str(getattr(run.state, "value", run.state))


class SessionFacade:
    def __init__(
        self,
        *,
        store: SessionStore,
        rooms: RoomManagement,
        registry: RoomRegistry,
        history: RoomHistoryReader,
        messages: RoomMessageStore,
        engine: ExecutionEngine,
        approvals: HITLManager,
        waiter: SessionWaiter,
        now: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], str] = new_session_id,
    ) -> None:
        self._store = store
        self._rooms = rooms
        self._registry = registry
        self._history = history
        self._messages = messages
        self._engine = engine
        self._approvals = approvals
        self._waiter = waiter
        self._now = now
        self._id_factory = id_factory

    # -- Client --------------------------------------------------------------

    async def send(
        self,
        request: SessionMessageRequest,
        *,
        owner: SessionOwner,
        resolver: AgentNetworkTargetResolver,
    ) -> SessionMessageResponse:
        client_request_id = _validated_request_id(request.client_request_id)
        text = _validated_text(request.text)
        await _resolve_agent(resolver, request.agent_id, request.subnet_id)
        record = await self._session_for_send(request, owner, client_request_id, text)
        record = await self._ensure_container(record, owner, text)
        conversation_id = record.conversation_id
        assert conversation_id is not None

        is_retry = record.last_client_request_id == client_request_id
        # A retry of an earlier request replays it; it is not a new send.
        earlier = None if is_retry else record.request(client_request_id)
        if not is_retry and earlier is None:
            runs = await self._engine.get_runs_for_room(conversation_id)
            if runs:
                # A concurrent send of this request may have recorded it and
                # started its run since this send read the session.
                current = await self._store.get(record.session_id) or record
                if current.request(client_request_id) is None:
                    return await self._busy(
                        record, runs, client_request_id, request.agent_id
                    )
                record = current
                is_retry = record.last_client_request_id == client_request_id
                earlier = None if is_retry else record.request(client_request_id)

        seq_before = await self._waiter.latest_seq(conversation_id)
        execution_request = ExecutionRequest(
            conversation_id=conversation_id,
            sender_id=owner.user_id,
            sender_name=record.client_label,
            # The chat send shape; the server assigns the message id.
            message={
                "conversation_id": conversation_id,
                "message_id": "",
                "message_type": "user",
                "message_content": {"message_text": text},
            },
            client_request_id=client_request_id,
            mode="passthrough",
            agent_scope={"source": "mention", "agent_ids": [request.agent_id]},
        )
        ack = await self._engine.execute(execution_request)
        if not ack.success:
            return await self._refused(record, ack, client_request_id, request.agent_id)
        if not ack.message_id:
            raise _error("execution_error", "The message was not recorded")

        if earlier is not None:
            # The engine replays a recorded request; no run starts.
            return await self._earlier_receipt(record, earlier, request.agent_id)
        lost_to: SessionRequest | None = None
        if is_retry:
            after = record.last_send_event_seq
        else:
            after = seq_before
            record, lost_to = await self._record_send(
                record, client_request_id, ack.message_id, seq_before
            )
        # Only the engine call that created the message may start its run, and
        # it can still lose the record to a faster replay of the same request.
        if ack.should_start_orchestration:
            self._engine.schedule_orchestration(execution_request, ack)
            record = await self._accept_agent(record, request.agent_id)
        if lost_to is not None:
            # Report the recorded request, not this send's view of the log.
            return await self._earlier_receipt(record, lost_to, request.agent_id)

        # The receipt returns at once; the client queries for the responses.
        outcome = await self._waiter.scan(
            conversation_id, client_request_id, after=after
        )
        state, _reason = self._resolve(
            outcome, sent_at=record.last_activity_at, message_id=ack.message_id
        )
        return self._receipt(
            record,
            client_request_id,
            request.agent_id,
            status=(
                "created"
                if record.creation_request_id == client_request_id
                else "accepted"
            ),
            state=state,
        )

    async def check(self, session_id: str, *, owner: SessionOwner) -> SessionStatus:
        record = await self._owned(session_id, owner)
        return await self._client_status(record)

    async def close(self, session_id: str, *, owner: SessionOwner) -> SessionClosed:
        record = await self._owned(session_id, owner)
        if record.status == "closed":
            return SessionClosed(session_id=record.session_id, status="closed")
        if record.conversation_id:
            await self._stop_activity(record.conversation_id, owner)
        closed = await self._store.mark_closed(record.session_id, self._now())
        return SessionClosed(
            session_id=record.session_id,
            status=closed.status if closed is not None else "closed",
        )

    # -- Owner ---------------------------------------------------------------

    async def list_for_owner(self, *, owner: SessionOwner) -> SessionListResponse:
        records = await self._with_waiting(
            await self._store.list_for_owner(owner.user_id, _LIST_LIMIT), owner
        )
        statuses = await asyncio.gather(*(self._owner_record(r) for r in records))
        return SessionListResponse(
            sessions=[
                SessionSummary(
                    session_id=record.session_id,
                    client_label=record.client_label,
                    title=record.title,
                    agents=status.agents,
                    state=status.state,
                    paused_agent_id=status.paused_agent_id,
                    conversation_id=record.conversation_id,
                    opened_at=record.opened_at,
                    last_activity_at=status.last_activity_at,
                    closed_at=record.closed_at,
                )
                for record, status in zip(records, statuses, strict=True)
            ]
        )

    async def record_for_owner(
        self, session_id: str, *, owner: SessionOwner
    ) -> SessionOwnerRecord:
        record = await self._owned(session_id, owner)
        return await self._owner_record(record)

    # -- Internals -------------------------------------------------------------

    async def _with_waiting(
        self, records: list[SessionRecord], owner: SessionOwner
    ) -> list[SessionRecord]:
        """Add open sessions waiting on the owner that fell outside the page.

        The page is the most recently active sessions, and a run parking on
        the owner does not touch the session record.
        """
        open_rooms = await self._store.list_open_conversations(owner.user_id)
        if not open_rooms:
            return records
        runs = await self._engine.get_latest_runs_for_rooms(list(open_rooms))
        listed = {record.session_id for record in records}
        missing = [
            open_rooms[conversation_id]
            for conversation_id, run in runs.items()
            if _state_value(run) == "awaiting_input"
            and open_rooms.get(conversation_id) not in listed
        ]
        found = await asyncio.gather(*(self._store.get(sid) for sid in missing))
        return [record for record in found if record is not None] + records

    async def _session_for_send(
        self,
        request: SessionMessageRequest,
        owner: SessionOwner,
        client_request_id: str,
        text: str,
    ) -> SessionRecord:
        if request.session_id is None:
            label = (request.client_label or "").strip()
            if not label:
                raise _error(
                    "invalid_request", "client_label is required to open a session"
                )
            record = await self._open(owner, label, client_request_id, text)
        else:
            record = await self._owned(request.session_id, owner)
        if record.status == "closed":
            raise _error("session_closed", "The session is closed")
        return record

    async def _open(
        self, owner: SessionOwner, label: str, client_request_id: str, text: str
    ) -> SessionRecord:
        now = self._now()
        return await self._store.reserve(
            SessionRecord(
                session_id=self._id_factory(),
                owner_user_id=owner.user_id,
                client_label=label,
                title=_title(text),
                creation_request_id=client_request_id,
                opened_at=now,
                last_activity_at=now,
            )
        )

    async def _owned(self, session_id: str, owner: SessionOwner) -> SessionRecord:
        record = await self._store.get(session_id)
        if record is None or record.owner_user_id != owner.user_id:
            raise _error("session_not_found", "Session not found")
        return record

    async def _ensure_container(
        self, record: SessionRecord, owner: SessionOwner, text: str
    ) -> SessionRecord:
        if record.conversation_id:
            return record
        room = await self._rooms.create_room(
            CreateRoomRequest(
                owner_id=owner.user_id,
                owner_name=owner.name or owner.user_id,
                room_name=_room_name(record.client_label, text),
                # Agents join the container only after an accepted send.
                membership_seed=MembershipSeed(mode="manual", agent_ids=[]),
                owner_product="session",
            )
        )
        current = await self._store.set_conversation_if_unset(
            record.session_id, room.conversation_id
        )
        if current is None:
            raise _error("session_not_found", "Session not found")
        if current.conversation_id != room.conversation_id:
            # A concurrent request created the container first.
            try:
                await self._rooms.delete_room(room.conversation_id, owner.user_id)
            except Exception as exc:
                logger.emit(
                    E.SESSION_ORPHAN_CONTAINER_DELETE_FAILED,
                    exc=exc,
                    session_id=record.session_id,
                    conversation_id=room.conversation_id,
                )
        return current

    async def _accept_agent(
        self, record: SessionRecord, agent_id: str
    ) -> SessionRecord:
        conversation_id = record.conversation_id
        assert conversation_id is not None
        try:
            members = await self._registry.get_room_agents(conversation_id)
            if agent_id not in members:
                await self._rooms.update_membership(
                    conversation_id,
                    MembershipUpdateRequest(add_agent_ids=[agent_id]),
                )
        except Exception as exc:
            # The run is already scheduled; membership is presentation only.
            logger.emit(
                E.SESSION_MEMBER_ADD_FAILED,
                exc=exc,
                session_id=record.session_id,
                agent_id=agent_id,
            )
        return (
            await self._store.record_accepted_agent(record.session_id, agent_id)
            or record
        )

    async def _record_send(
        self,
        record: SessionRecord,
        client_request_id: str,
        user_message_id: str,
        seq_before: int,
    ) -> tuple[SessionRecord, SessionRequest | None]:
        """Record a new send; also return the request if another send won."""
        stored = (
            await self._store.record_send(
                record.session_id,
                client_request_id=client_request_id,
                user_message_id=user_message_id,
                event_seq=seq_before,
                at=self._now(),
            )
            or record
        )
        recorded = stored.request(client_request_id)
        if recorded is not None and (
            stored.last_client_request_id != client_request_id
            or stored.last_send_event_seq != seq_before
        ):
            # A concurrent send of this request recorded it first.
            return stored, recorded
        return stored, None

    async def _earlier_receipt(
        self, record: SessionRecord, earlier: SessionRequest, agent_id: str
    ) -> SessionMessageResponse:
        assert record.conversation_id is not None
        outcomes = await self._waiter.history(record.conversation_id)
        state, _reason = self._resolve(
            outcomes.get(earlier.client_request_id) or CorrelatedOutcome(),
            sent_at=earlier.sent_at,
            message_id=earlier.user_message_id,
        )
        return self._receipt(
            record,
            earlier.client_request_id,
            agent_id,
            status=(
                "created"
                if record.creation_request_id == earlier.client_request_id
                else "accepted"
            ),
            state=state,
        )

    async def _busy(
        self,
        record: SessionRecord,
        runs: list[RunInfo],
        client_request_id: str,
        agent_id: str,
    ) -> SessionMessageResponse:
        return self._receipt(
            record,
            client_request_id,
            agent_id,
            status="busy",
            state=busy_state([_state_value(run) for run in runs]),
        )

    async def _refused(
        self,
        record: SessionRecord,
        ack: ExecutionAck,
        client_request_id: str,
        agent_id: str,
    ) -> SessionMessageResponse:
        message = ack.error or "The message was not accepted"
        if ack.status_code == 409:
            if message == FINGERPRINT_CONFLICT_ERROR:
                raise _error("request_conflict", message)
            # The engine saw an active run after the busy check: a race.
            runs = await self._engine.get_runs_for_room(record.conversation_id or "")
            return await self._busy(record, runs, client_request_id, agent_id)
        if ack.status_code == 404:
            raise _error("not_found", message)
        if 400 <= ack.status_code < 500:
            raise _error("request_rejected", message)
        raise _error("execution_error", message)

    def _resolve(
        self,
        outcome: CorrelatedOutcome | None,
        *,
        sent_at: datetime,
        message_id: str | None,
        closed: bool = False,
    ) -> tuple[SessionState, str | None]:
        """Client state, giving a request without run events time to start.

        The run starts in a background task that can outlast the grace under
        load; while that task runs, the request is still working.
        """
        if (
            outcome is not None
            and not outcome.seen
            and not closed
            and (
                (self._now() - sent_at).total_seconds()
                < self._waiter.start_grace_seconds
                or (
                    message_id is not None
                    and self._engine.is_orchestration_pending(message_id)
                )
            )
        ):
            return "working", None
        return resolve_state(outcome, closed=closed)

    def _receipt(
        self,
        record: SessionRecord,
        client_request_id: str,
        agent_id: str,
        *,
        status: SessionReceiptStatus,
        state: SessionState,
    ) -> SessionMessageResponse:
        message = _RECEIPT_MESSAGES[status]
        return SessionMessageResponse(
            session_id=record.session_id,
            client_request_id=client_request_id,
            agent_id=agent_id,
            status=status,
            state=state,
            message=f"{message} {RECEIPT_TIP}",
            created_at=record.opened_at,
            updated_at=record.last_activity_at,
        )

    async def _outcome(self, record: SessionRecord) -> CorrelatedOutcome | None:
        if not record.conversation_id or not record.last_client_request_id:
            return None
        return await self._waiter.scan(
            record.conversation_id,
            record.last_client_request_id,
            after=record.last_send_event_seq,
        )

    def _updated_at(
        self, record: SessionRecord, outcomes: list[CorrelatedOutcome | None]
    ) -> datetime:
        times = [record.last_activity_at]
        times.extend(o.last_event_at for o in outcomes if o and o.last_event_at)
        return max(times)

    async def _owner_record(self, record: SessionRecord) -> SessionOwnerRecord:
        outcome = await self._outcome(record)
        closed = record.status == "closed"
        state, reason = self._resolve(
            outcome,
            sent_at=record.last_activity_at,
            message_id=record.last_user_message_id,
            closed=closed,
        )
        return SessionOwnerRecord(
            session_id=record.session_id,
            client_label=record.client_label,
            title=record.title,
            state=state,
            reason=reason,
            paused_agent_id=(
                await self._paused_agent_id(record)
                if state == "waiting_on_owner"
                else None
            ),
            agents=await self._agents(record),
            status=record.status,
            conversation_id=record.conversation_id,
            opened_at=record.opened_at,
            last_activity_at=self._updated_at(record, [outcome]),
            closed_at=record.closed_at,
        )

    async def _client_status(self, record: SessionRecord) -> SessionStatus:
        outcome = await self._outcome(record)
        closed = record.status == "closed"
        state, reason = self._resolve(
            outcome,
            sent_at=record.last_activity_at,
            message_id=record.last_user_message_id,
            closed=closed,
        )
        agents = await self._agents(record)
        history = await self._client_history(record, agents)
        return SessionStatus(
            session_id=record.session_id,
            state=state,
            reason=reason,
            agents=agents,
            created_at=record.opened_at,
            updated_at=max(
                [self._updated_at(record, [outcome])]
                + [entry.replied_at for entry in history if entry.replied_at]
            ),
            history=history,
        )

    async def _client_history(
        self, record: SessionRecord, agents: list[SessionAgent]
    ) -> list[SessionHistoryEntry]:
        """The client's messages and the agents' final replies, oldest first.

        Agents' questions to the owner and the owner's decisions stay out.
        """
        if not record.conversation_id:
            return []
        outcomes = await self._waiter.history(record.conversation_id)
        names = {agent.agent_id: agent.name for agent in agents}
        reply_ids = [
            reply_id
            for request in record.requests
            if (reply_id := final_message_id(outcomes.get(request.client_request_id)))
            is not None
        ]
        messages = {
            message.message_id: message
            for message in await self._history.get_messages_by_ids(
                list(
                    dict.fromkeys(
                        [request.user_message_id for request in record.requests]
                        + reply_ids
                    )
                )
            )
        }
        closed = record.status == "closed"
        entries: list[SessionHistoryEntry] = []
        for request in record.requests:
            outcome = outcomes.get(request.client_request_id)
            user = messages.get(request.user_message_id)
            reply = messages.get(final_message_id(outcome) or "")
            # Only the latest request can be closed.
            latest = request.client_request_id == record.last_client_request_id
            state, reason = self._resolve(
                outcome or CorrelatedOutcome(),
                sent_at=request.sent_at,
                message_id=request.user_message_id,
                closed=closed and latest,
            )
            agent_id = _target_agent(user)
            entries.append(
                SessionHistoryEntry(
                    client_request_id=request.client_request_id,
                    text=_message_text(user),
                    sent_at=(
                        user.created_at
                        if user is not None and user.created_at
                        else request.sent_at
                    ),
                    agent_id=agent_id,
                    agent_name=(names.get(agent_id) if agent_id else None)
                    or _agent_name(outcome),
                    state=state,
                    reason=reason,
                    reply=_message_text(reply) or None,
                    replied_at=reply.created_at if reply is not None else None,
                )
            )
        return entries

    async def _agents(self, record: SessionRecord) -> list[SessionAgent]:
        if not record.conversation_id:
            return []
        room = await self._registry.get_room(record.conversation_id)
        if room is None:
            return []
        return [
            SessionAgent(agent_id=agent_id, name=room.agent_set.get(agent_id))
            for agent_id in room.agent_ids
        ]

    async def _paused_agent_id(
        self, record: SessionRecord, runs: list[RunInfo] | None = None
    ) -> str | None:
        """The agent of the run awaiting the owner, from its trigger message."""
        if runs is None and record.conversation_id:
            runs = await self._engine.get_runs_for_room(record.conversation_id)
        awaiting = next(
            (run for run in runs or [] if _state_value(run) == "awaiting_input"),
            None,
        )
        if awaiting is not None and awaiting.trigger_message_id:
            message = await self._messages.get_message(awaiting.trigger_message_id)
            extend_info = message.metadata.get("extend_info") if message else None
            candidates = (
                extend_info.get("candidate_agent_ids")
                if isinstance(extend_info, dict)
                else None
            )
            if isinstance(candidates, list) and candidates:
                return str(candidates[0])
        return record.last_accepted_agent_id

    async def _stop_activity(self, conversation_id: str, owner: SessionOwner) -> None:
        """Best-effort stop: cancel a pending approval, else the active run.

        If no pending approval could be canceled, the run is canceled instead,
        so a closed session never leaves its run going.
        """
        try:
            pending = await self._approvals.get_pending_hitl(conversation_id)
        except Exception as exc:
            logger.emit(
                E.SESSION_CLOSE_PENDING_LOOKUP_FAILED,
                exc=exc,
                conversation_id=conversation_id,
            )
            pending = []
        interactions = {
            request.interaction_id: request.interaction_version
            for request in pending
            if request.interaction_id and request.interaction_version is not None
        }
        canceled_any = False
        if interactions:
            for interaction_id, version in interactions.items():
                try:
                    await self._approvals.cancel_hitl_interaction(
                        conversation_id, interaction_id, version
                    )
                    canceled_any = True
                except Exception as exc:
                    logger.emit(
                        E.SESSION_CLOSE_INTERACTION_CANCEL_FAILED,
                        exc=exc,
                        conversation_id=conversation_id,
                        interaction_id=interaction_id,
                    )
            if canceled_any:
                return
        for run in await self._engine.get_runs_for_room(conversation_id):
            if not run.trigger_message_id:
                continue
            try:
                await self._engine.cancel(
                    conversation_id,
                    run.trigger_message_id,
                    requested_by_user_id=owner.user_id,
                )
            except Exception as exc:
                # Pending local agent-call cancellation and reconciliation are
                # tolerated; the session is closed regardless.
                logger.emit(
                    E.SESSION_CLOSE_RUN_CANCEL_FAILED,
                    exc=exc,
                    conversation_id=conversation_id,
                    run_id=run.run_id,
                )


def _validated_request_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise _error("invalid_request", "client_request_id is required")
    normalized = normalize_client_request_id(value)
    if not normalized:
        raise _error("invalid_request", "client_request_id is required")
    if len(normalized) > MAX_CLIENT_REQUEST_ID_LENGTH:
        raise _error(
            "invalid_request",
            "client_request_id exceeds maximum length of "
            f"{MAX_CLIENT_REQUEST_ID_LENGTH} characters",
        )
    return normalized


def _validated_text(text: str | None) -> str:
    if not isinstance(text, str) or not text.strip():
        raise _error("invalid_request", "text is required")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise _error(
            "invalid_request",
            f"text exceeds maximum length of {MAX_MESSAGE_LENGTH} characters",
        )
    return text


async def _resolve_agent(
    resolver: AgentNetworkTargetResolver, agent_id: str, subnet_id: str | None
) -> None:
    try:
        await resolver.resolve_target(agent_id, subnet_id)
    except AppError as exc:
        if exc.code == "NOT_FOUND":
            entity = exc.details.get("entity_type")
            code = "subnet_not_found" if entity == "AgentGroup" else "agent_not_found"
            raise _error(code, exc.message) from exc
        if exc.code == "CONFLICT":
            raise _error("agent_unavailable", exc.message) from exc
        if exc.code == "VALIDATION":
            raise _error("invalid_request", exc.message) from exc
        if exc.code == "FORBIDDEN":
            raise _error("forbidden", exc.message) from exc
        raise


def _message_text(message: object | None) -> str:
    content = getattr(message, "content", None)
    text = content.get("message_text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else ""


def _target_agent(message: object | None) -> str | None:
    """The agent a client message addressed, from its routing metadata."""
    metadata = getattr(message, "metadata", None)
    extend_info = metadata.get("extend_info") if isinstance(metadata, dict) else None
    candidates = (
        extend_info.get("candidate_agent_ids")
        if isinstance(extend_info, dict)
        else None
    )
    if isinstance(candidates, list) and candidates:
        return str(candidates[0])
    return None


def _agent_name(outcome: CorrelatedOutcome | None) -> str | None:
    label = outcome.agent_label if outcome is not None else None
    return label.split(" - ", 1)[0] if label else None


def _title(text: str) -> str | None:
    """The owner-facing session name: the first message on one short line."""
    line = " ".join(text.split())
    if len(line) <= _TITLE_MAX:
        return line or None
    return line[: _TITLE_MAX - 1].rstrip() + "…"


def _room_name(client_label: str, text: str) -> str:
    words = " ".join(text.split()[:_ROOM_NAME_WORDS])
    name = f"{client_label}: {words}" if words else client_label
    return name if len(name) <= _ROOM_NAME_MAX else name[: _ROOM_NAME_MAX - 3] + "..."


__all__ = ["FINGERPRINT_CONFLICT_ERROR", "SessionFacade", "SessionOwner"]
