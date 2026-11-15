"""Each orchestrator agent call counts once toward the agent's call totals."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest

from execution.orchestrator.a2a_runtime.call_counting import (
    CountingAgentCallLedgerStore,
)
from execution.orchestrator.a2a_runtime.ledger import transition_call
from execution.orchestrator.models import TextPart, ToolResult
from tests.orchestrator_harness.a2a_in_memory import InMemoryAgentCallLedgerStore

from ._orchestrator_a2a_helpers import ledger_record

NOW = datetime(2026, 10, 15, tzinfo=UTC)


async def ledger_in(state: str):
    inner = InMemoryAgentCallLedgerStore()
    counter = AsyncMock()
    record = ledger_record(state=state)
    await inner.insert(record)
    return CountingAgentCallLedgerStore(inner, counter), counter, record


async def move(store, record, to_state: str):
    result = ToolResult(
        call_id=record.invocation_id,
        tool_name=record.tool_name,
        status="completed" if to_state == "completed" else "failed",
        content=[TextPart(text="done")],
        artifact_refs=[],
        error_code=None if to_state == "completed" else "agent_failed",
        error_message=None if to_state == "completed" else "failed",
    )
    moved = transition_call(
        record,
        to_state=to_state,
        updated_at=NOW,
        terminal_result=result,
        terminal_result_digest=sha256(result.model_dump_json().encode()).hexdigest(),
    )
    outcome = await store.cas(moved, expected_state_version=record.state_version)
    return moved, outcome


@pytest.mark.asyncio
async def test_a_completed_call_counts_once_as_a_success():
    store, counter, record = await ledger_in("working")

    completed, outcome = await move(store, record, "completed")
    replay = await store.cas(completed, expected_state_version=record.state_version)

    assert (outcome, replay) == ("accepted", "replayed")
    counter.increment_agent_call_count.assert_awaited_once_with(
        record.agent_id, success=True
    )


@pytest.mark.asyncio
async def test_a_failed_dispatch_counts_as_an_unsuccessful_call():
    store, counter, record = await ledger_in("dispatching")

    await move(store, record, "failed")

    counter.increment_agent_call_count.assert_awaited_once_with(
        record.agent_id, success=False
    )


@pytest.mark.asyncio
async def test_a_call_rejected_before_dispatch_is_not_counted():
    store, counter, record = await ledger_in("accepted")

    _, outcome = await move(store, record, "rejected")

    assert outcome == "accepted"
    counter.increment_agent_call_count.assert_not_awaited()


@pytest.mark.asyncio
async def test_updating_a_finished_call_does_not_count_again():
    store, counter, record = await ledger_in("working")
    failed, _ = await move(store, record, "failed")
    counter.increment_agent_call_count.reset_mock()

    tweaked = failed.model_copy(
        update={"state_version": failed.state_version + 1, "updated_at": NOW}
    )
    outcome = await store.cas(tweaked, expected_state_version=failed.state_version)

    assert outcome == "accepted"
    counter.increment_agent_call_count.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lost_race_does_not_count():
    store, counter, record = await ledger_in("working")
    await move(store, record, "completed")
    counter.increment_agent_call_count.reset_mock()

    _, outcome = await move(store, record, "failed")

    assert outcome == "conflict"
    counter.increment_agent_call_count.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_counter_failure_never_breaks_the_call():
    store, counter, record = await ledger_in("working")
    counter.increment_agent_call_count.side_effect = RuntimeError("mongo down")

    _, outcome = await move(store, record, "completed")

    assert outcome == "accepted"


@pytest.mark.asyncio
async def test_other_ledger_operations_pass_through():
    store, _, record = await ledger_in("working")
    assert await store.load_by_record_id(record.call_record_id) == record
