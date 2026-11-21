"""Harness for a check rule that answers "ask".

``RoomLog`` sends every kernel lifecycle event through the production
translator and the real ``EventPublisherImpl`` into an in-memory room event
store, then reads the Room back through ``SnapshotService``. The fold runs
when the snapshot is read, not when an event is stored, so a test must read
the snapshot to see an event the fold refuses.
"""

from __future__ import annotations

from common.dto import (
    AgentMessageFinal,
    DeliveryEmitStatus,
    RunEventNotification,
)
from delivery.snapshot import SnapshotService
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.models import AssistantMessage, TextPart
from execution.orchestrator.ports import AgentCallNeedsAnswer
from execution.orchestrator.public_projection import (
    PublicProjectionTranslator,
    canonical_settlement_payload,
)
from tests._orchestrator_helpers import NOW
from tests.fakes.room_events import InMemoryRoomEventStore
from tests.orchestrator_harness.fake_tools import RecordingFakeToolRuntime
from tests.test_delivery_event_publisher import make_publisher
from tests.test_orchestrator_passthrough import AgentRuntime

ACCEPTED = {DeliveryEmitStatus.DELIVERED, DeliveryEmitStatus.ALREADY_DELIVERED}


class RoomLog:
    """Durable Room history through the real publisher and snapshot."""

    def __init__(self) -> None:
        self.store = InMemoryRoomEventStore()
        self.publisher = make_publisher(room_events=self.store)
        self.translator = PublicProjectionTranslator()
        self.emitted: list[tuple[str, dict[str, object]]] = []
        self.statuses: list[DeliveryEmitStatus] = []
        self._sequence = 0
        # Raised once, by event type, to simulate a process dying before an
        # event is stored.
        self.die_before: set[str] = set()

    async def read(self, conversation_id: str, run_id: str) -> list[dict]:
        records = await self.store.read_range(
            conversation_id, after=-1, limit=10_000, include_skipped=False
        )
        return [record for record in records if record.get("run_id") == run_id]

    async def lifecycle(self, event_type, run, payload) -> None:
        if event_type in self.die_before:
            self.die_before.discard(event_type)
            raise ProcessDied
        self.emitted.append((event_type, dict(payload)))
        self._sequence += 1
        projected = self.translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=self._sequence,
                timestamp=NOW,
                payload=payload,
                conversation_id=run.conversation_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
            ),
            catalog=run.tool_catalog,
        )
        if projected is None:
            return
        status, _ = await self.publisher.emit_checked_identified(
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
        assert status in ACCEPTED, status
        self.statuses.append(status)

    async def settle(self, run) -> None:
        """Publish the final answer and the settlement, as the projection does."""
        if run.status == "completed":
            final = next(
                item
                for item in run.transcript
                if isinstance(item, AssistantMessage)
                and item.message_id == run.proposed_final_message_id
            )
            status, _ = await self.publisher.emit_checked_identified(
                AgentMessageFinal(
                    conversation_id=run.conversation_id,
                    message_id=final.message_id,
                    agent_id="system:hybro",
                    content={
                        "content": "".join(
                            part.text
                            for part in final.content
                            if isinstance(part, TextPart)
                        ),
                        "related_message_id": run.request.user_message_id,
                        "client_request_id": run.client_request_id,
                    },
                    delivery_id=f"orchestrator:{run.run_id}:final:{final.message_id}",
                )
            )
            assert status in ACCEPTED, status
        status, _ = await self.publisher.emit_checked_identified(
            RunEventNotification(
                conversation_id=run.conversation_id,
                event_id=f"public:{run.run_id}:run_settled",
                delivery_id=f"orchestrator:{run.run_id}:run_settled",
                run_id=run.run_id,
                seq=run.state_version,
                run_event_type="run_settled",
                payload=canonical_settlement_payload(run),
                correlation_id=run.client_request_id,
            )
        )
        assert status in ACCEPTED, status

    async def snapshot_turn(self, run) -> dict:
        state = await SnapshotService(store=self.store).snapshot(run.conversation_id)
        (turn,) = [item for item in state["turns"] if item["run_id"] == run.run_id]
        return turn

    async def public_types(self) -> list[str]:
        records = await self.store.read_range(
            "room-1", after=-1, limit=10_000, include_skipped=False
        )
        return [
            record["payload_public"].get("type") or record["kind"] for record in records
        ]

    async def tool_starts(self, public_call_id: str) -> int:
        records = await self.store.read_range(
            "room-1", after=-1, limit=10_000, include_skipped=False
        )
        return sum(
            1
            for record in records
            if record["payload_public"].get("type") == "tool_execution_start"
            and record["payload_public"]["payload"].get("tool_call_id")
            == public_call_id
        )


class ProcessDied(BaseException):
    """Simulates the process dying between two writes."""


class RecordingRuleQuestion:
    """The kernel's port that creates a check rule's permission Question."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("question store secret-detail")


class _AskingAccept:
    """``accept`` asks the owner for the named tools until the call is approved."""

    ask: dict[str, str]
    accept_calls: list[tuple[str, bool]]
    die_on_approved_accept: bool

    async def accept(self, invocation, *, rule_approved: bool = False):
        self.accept_calls.append((invocation.invocation_id, rule_approved))
        if rule_approved and self.die_on_approved_accept:
            self.die_on_approved_accept = False
            raise ProcessDied
        rule_id = self.ask.get(invocation.tool.definition.name)
        if rule_id is not None and not rule_approved:
            raise AgentCallNeedsAnswer(rule_id)
        return await super().accept(invocation)  # type: ignore[misc]


class AskingToolRuntime(_AskingAccept, RecordingFakeToolRuntime):
    def __init__(self, ask: dict[str, str], **kwargs) -> None:
        RecordingFakeToolRuntime.__init__(self, **kwargs)
        self.ask = ask
        self.accept_calls = []
        self.die_on_approved_accept = False


class AskingAgentRuntime(_AskingAccept, AgentRuntime):
    def __init__(self, ask: dict[str, str]) -> None:
        AgentRuntime.__init__(self)
        self.ask = ask
        self.accept_calls = []
        self.die_on_approved_accept = False
