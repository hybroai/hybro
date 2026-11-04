"""Count orchestrator agent calls toward each agent's call totals."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from common.observability import events as E
from common.utils.logger import get_logger

from .models import TERMINAL_AGENT_CALL_STATES, AgentCallLedgerRecord


@runtime_checkable
class AgentCallCounter(Protocol):
    async def increment_agent_call_count(
        self, agent_id: str, *, success: bool
    ) -> None: ...


logger = get_logger(__name__)

# A call in these states was never sent to the agent.
_NOT_DISPATCHED = frozenset({"accepted", "ready_to_dispatch"})


class CountingAgentCallLedgerStore:
    """Ledger store that counts each agent call once when it first finishes.

    Every terminal write goes through ``cas``. A call counts when this
    process's write is accepted and moves the call from a dispatched,
    unfinished state into a terminal one; success means it completed.
    Replays, updates to a finished call, lost races, and calls rejected
    before dispatch do not count. Counting is best effort and never fails
    the write.
    """

    def __init__(self, inner: Any, counter: AgentCallCounter) -> None:
        self._inner = inner
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def cas(
        self,
        record: AgentCallLedgerRecord,
        *,
        expected_state_version: int,
    ) -> str:
        prior = None
        if record.state in TERMINAL_AGENT_CALL_STATES:
            prior = await self._inner.load_by_record_id(record.call_record_id)
        outcome = await self._inner.cas(
            record, expected_state_version=expected_state_version
        )
        if (
            outcome == "accepted"
            and prior is not None
            and prior.state not in TERMINAL_AGENT_CALL_STATES
            and prior.state not in _NOT_DISPATCHED
        ):
            try:
                await self._counter.increment_agent_call_count(
                    record.agent_id, success=record.state == "completed"
                )
            except Exception as exc:
                logger.emit(
                    E.AGENT_CALL_COUNT_FAILED,
                    exc=exc,
                    agent_id=record.agent_id,
                    state=record.state,
                )
        return outcome


__all__ = ["AgentCallCounter", "CountingAgentCallLedgerStore"]
