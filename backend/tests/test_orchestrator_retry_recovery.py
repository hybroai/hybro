"""Durable retry admission and canonical closeout across real Kernel restarts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from delivery.snapshot import RoomEventFold
from execution.model_loop.model_runtime import GatewayModelRuntime
from execution.orchestrator.kernel import OrchestratorKernel, UUIDFactory
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.models import ModelStreamEvent
from execution.orchestrator.public_projection import PublicProjectionTranslator
from tests._orchestrator_a2a_helpers import ledger_record
from tests._orchestrator_helpers import (
    NOW,
    NeverCancelled,
    final_events,
    make_kernel,
    make_run,
    tool_events,
)
from tests.test_orchestrator_a2a_runtime import _interaction, setup
from tests.test_orchestrator_model_first_kernel import (
    InteractionRuntime,
    _interaction_suspension,
)


class SimulatedRestart(RuntimeError):
    pass


def _provider_error():
    return [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="error", attempt=1, error_class="timeout", retryable=True
        ),
    ]


def _run_with_retries(count):
    run = make_run()
    return run.model_copy(
        update={
            "profile": run.profile.model_copy(
                update={"max_provider_retries_total": count}
            )
        }
    )


def _restart(kernel):
    return OrchestratorKernel(
        run_store=kernel.run_store,
        model_runtime=kernel.model_runtime,
        tool_runtime=kernel.tool_runtime,
        tool_catalog=kernel.tool_catalog,
        context_compiler=kernel.context_compiler,
        budget_policy=kernel.budget_policy,
        projection_driver=kernel.projection_driver,
        clock=kernel.clock,
        id_factory=UUIDFactory(),
        supervisor_hitl=kernel.supervisor_hitl,
        canonical_event_reader=kernel.canonical_event_reader,
    )


async def _public_history(kernel, run):
    """Use the production translator and durable event-ID append semantics."""
    records = {}
    translator = PublicProjectionTranslator()
    sequence = 0

    async def lifecycle(event_type, current, payload):
        nonlocal sequence
        sequence += 1
        if event_type in {"run_waiting_input", "run_resumed"}:
            event_id = f"{event_type}:{payload['interaction_id']}"
            records.setdefault(
                event_id,
                {
                    "room_id": current.conversation_id,
                    "room_seq": len(records) + 1,
                    "kind": "run_event",
                    "ts": NOW.isoformat(),
                    "payload_public": {
                        "event_id": event_id,
                        "run_id": current.run_id,
                        "seq": sequence,
                        "type": event_type,
                        "payload": payload,
                        "correlation_id": current.client_request_id,
                    },
                },
            )
            return
        projected = translator.translate(
            SessionEvent(
                event_type=event_type,
                session_id=current.session_id,
                run_id=current.run_id,
                causation_id=current.request.user_message_id,
                sequence=sequence,
                timestamp=NOW,
                payload=payload,
                conversation_id=current.conversation_id,
                user_message_id=current.request.user_message_id,
                client_request_id=current.client_request_id,
            ),
            catalog=current.tool_catalog,
        )
        if projected is None or projected.event_id in records:
            return
        records[projected.event_id] = {
            "room_id": current.conversation_id,
            "room_seq": len(records) + 1,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": projected.event_id,
                "run_id": projected.run_id,
                "seq": projected.seq,
                "type": projected.kind,
                "payload": projected.payload,
                "correlation_id": projected.client_request_id,
            },
        }

    async def read_events(_conversation_id, _run_id):
        return list(records.values())

    kernel.canonical_event_reader = read_events
    await lifecycle("run_started", run, {"mode": "ultimate"})
    return records, lifecycle


def _fold_settled(records, run):
    if run.status == "budget_exhausted":
        run = run.model_copy(update={"status": "failed"})
    fold = RoomEventFold()
    for record in records.values():
        assert fold.apply(record), record
    room_seq = len(records)
    if run.status == "completed":
        room_seq += 1
        assert fold.apply(
            {
                "room_id": run.conversation_id,
                "room_seq": room_seq,
                "kind": "agent_response",
                "ts": NOW.isoformat(),
                "payload_public": {
                    "message_id": run.proposed_final_message_id,
                    "content": "done",
                    "client_request_id": run.client_request_id,
                    "related_message_id": run.request.user_message_id,
                },
            }
        )
    room_seq += 1
    assert fold.apply(
        {
            "room_id": run.conversation_id,
            "room_seq": room_seq,
            "kind": "run_event",
            "ts": NOW.isoformat(),
            "payload_public": {
                "event_id": f"settled:{run.run_id}",
                "run_id": run.run_id,
                "seq": max(r["payload_public"].get("seq", 0) for r in records.values())
                + 1,
                "type": "run_settled",
                "correlation_id": run.client_request_id,
                "payload": {
                    "status": run.status,
                    "started_at": run.created_at.isoformat(),
                    "settled_at": run.updated_at.isoformat(),
                    "duration_ms": 0,
                    **(
                        {"final_message_id": run.proposed_final_message_id}
                        if run.status == "completed"
                        else {
                            "failure_code": "internal_error",
                            "error_summary": "The request could not be completed.",
                        }
                    ),
                },
            },
        }
    )
    folded = fold.state(room_seq=room_seq)["turns"][0]
    assert folded["state"] == run.status
    assert folded["active_interaction_id"] is None
    return folded


@pytest.mark.parametrize("max_retries", [0, 1, 3])
@pytest.mark.parametrize("new_kernel", [False, True])
async def test_every_scheduled_retry_restart_exhausts_durable_budget(
    max_retries, new_kernel
):
    run = _run_with_retries(max_retries)
    kernel, store, model, _ = await make_kernel(
        [_provider_error() for _ in range(max_retries + 2)], run=run
    )
    records, record_event = await _public_history(kernel, run)
    restarts = 0

    async def lifecycle(kind, current, payload):
        await record_event(kind, current, payload)
        if kind == "model_retry_scheduled":
            assert current.budget.provider_retries_used == len(model.requests)
            raise SimulatedRestart

    while True:
        try:
            result = await kernel.run(
                run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
            )
            break
        except SimulatedRestart:
            restarts += 1
            assert restarts <= max_retries
            if new_kernel:
                kernel = _restart(kernel)
    assert restarts == max_retries
    assert result.run.status == result.outcome == "failed"
    assert result.run.budget.provider_retries_used == max_retries
    assert len(model.requests) == max_retries + 1
    assert result.run.updated_at < result.run.budget.deadline_at
    assert await store.load(run.run_id) == result.run
    assert not any(
        record["payload_public"]["type"] in {"hitl_request", "run_waiting_input"}
        for record in records.values()
    )
    _fold_settled(records, result.run)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "boundary", ["budget", "closure", "message_end", "turn_end", "retry_event"]
)
async def test_retry_reservation_and_canonical_closure_replay_once(
    monkeypatch, boundary, side
):
    run = _run_with_retries(1)
    kernel, store, model, _ = await make_kernel(
        [_provider_error(), final_events()], run=run
    )
    records, record_event = await _public_history(kernel, run)
    crashed = False
    mutate = store.cas_mutate

    def crash_if(selected, at_side):
        nonlocal crashed
        if selected and side == at_side and not crashed:
            crashed = True
            raise SimulatedRestart

    async def cas(candidate, *, expected_state_version, command_id):
        selected = (
            boundary == "budget"
            and command_id.startswith("provider-attempt:kernel-retry:")
        ) or (boundary == "closure" and command_id.startswith("public-close-attempt:"))
        crash_if(selected, "before")
        result = await mutate(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        crash_if(selected, "after")
        return result

    async def lifecycle(kind, current, payload):
        selected = (
            (
                boundary == "message_end"
                and kind == "message_completed"
                and payload.get("disposition") in {"error", "aborted"}
            )
            or (boundary == "turn_end" and kind == "turn_completed")
            or (boundary == "retry_event" and kind == "model_retry_scheduled")
        )
        crash_if(selected, "before")
        await record_event(kind, current, payload)
        crash_if(selected, "after")

    monkeypatch.setattr(store, "cas_mutate", cas)
    with pytest.raises(SimulatedRestart):
        await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    interrupted = await store.load(run.run_id)
    assert interrupted.budget.provider_retries_used == (
        0 if boundary == "budget" and side == "before" else 1
    )
    result = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert result.outcome == "final_answer"
    assert result.run.budget.provider_retries_used == 1
    assert len(model.requests) == 2
    assert (
        sum(
            key.startswith("kernel-retry:")
            for key in result.run.budget.provider_attempt_keys
        )
        == 1
    )
    _fold_settled(records, result.run)


async def test_gateway_and_kernel_retries_share_budget_before_provider_io(monkeypatch):
    run = _run_with_retries(3)
    run = run.model_copy(
        update={
            "profile": run.profile.model_copy(
                update={
                    "model": run.profile.model.model_copy(
                        update={"max_provider_retries": 1}
                    )
                }
            )
        }
    )
    kernel, store, _, _ = await make_kernel([], run=run)
    charged_at_call = []
    mutate = store.cas_mutate

    async def slow_checkpoint(candidate, **kwargs):
        # Let an incorrectly early Gateway prefetch run before the store write.
        await asyncio.sleep(0.001)
        return await mutate(candidate, **kwargs)

    monkeypatch.setattr(store, "cas_mutate", slow_checkpoint)

    class FailingGateway:
        async def stream_turn_once(self, request, *, cancel_event):
            current = await store.load(run.run_id)
            charged_at_call.append(current.budget.provider_retries_used)
            assert current.active_assistant_message_id is not None
            key = f"{request.turn_id}:{current.active_assistant_message_id}:1"
            assert key in current.budget.provider_attempt_keys
            raise TimeoutError
            yield  # pragma: no cover - async iterator contract

    kernel.model_runtime = GatewayModelRuntime(
        FailingGateway(), sleep=AsyncMock(), random_value=lambda: 0, now=lambda: NOW
    )
    records, lifecycle = await _public_history(kernel, run)
    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "failed"
    assert charged_at_call == [0, 1, 2, 3]
    assert result.run.budget.provider_retries_used == 3
    assert (
        sum(
            key.startswith("kernel-retry:")
            for key in result.run.budget.provider_attempt_keys
        )
        == 1
    )
    _fold_settled(records, result.run)


async def test_successful_model_turns_and_decision_continuation_do_not_cost_retries():
    runtime = InteractionRuntime()
    runtime.suspensions["call-2"] = _interaction_suspension("call-2")
    kernel, store, model, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')),
            tool_events(("call-2", "fake_agent_pause", '{"status":"input_required"}')),
            final_events(),
        ],
        run=_run_with_retries(0),
        tool_runtime=runtime,
    )
    result = await kernel.run(next(iter(store.runs)), signal=NeverCancelled())
    assert result.outcome == "final_answer"
    assert len(model.requests) == 3
    assert result.run.budget.provider_retries_used == 0
    assert len(result.run.budget.provider_attempt_keys) == 3


@pytest.mark.parametrize("ending", ["final", "provider_error"])
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("boundary", ["child", "message_end", "tool_end"])
async def test_partial_real_child_closeout_recovers_every_owner_once(
    ending, side, boundary
):
    run = _run_with_retries(0 if ending == "final" else 1)
    a2a, ledger, _, dispatch, _ = await setup()
    runtime = InteractionRuntime()
    children = []
    calls = []
    for index in range(1, 4):
        call_id = f"call-{index}"
        interaction_id = f"interaction-{index}"
        child = ledger_record(
            run_id=run.run_id, call_id=call_id, state="input_required"
        ).model_copy(
            update={
                "tool_name": "fake_agent_pause",
                "source_index": index - 1,
                "pending_interaction_id": interaction_id,
                "interaction_revision": 1,
                "interaction_fingerprint": f"fp-{index}",
            }
        )
        assert await ledger.insert(child) == "accepted"
        await a2a.hitl.create_or_replay(
            call=child,
            interaction=_interaction("input_required").model_copy(
                update={"interaction_id": interaction_id}
            ),
            interaction_fingerprint=f"fp-{index}",
        )
        children.append(child)
        runtime.suspensions[call_id] = _interaction_suspension(
            call_id,
            call_record_id=child.call_record_id,
            interaction_id=interaction_id,
            fingerprint=f"fp-{index}",
        )
        calls.append((call_id, "fake_agent_pause", '{"status":"input_required"}'))
    crashed = False

    async def closeout(**kwargs):
        nonlocal crashed
        selected = (
            boundary == "child"
            and kwargs["call_record_id"] == children[1].call_record_id
            and not crashed
        )
        if selected and side == "before":
            crashed = True
            raise SimulatedRestart
        winner = await a2a.abandon_parked_interaction(**kwargs)
        if selected and side == "after":
            crashed = True
            raise SimulatedRestart
        return winner

    runtime.abandon_parked_interaction = closeout
    kernel, store, model, _ = await make_kernel(
        [tool_events(*calls)]
        + (
            [final_events()]
            if ending == "final"
            else [_provider_error(), _provider_error()]
        ),
        run=run,
        tool_runtime=runtime,
    )
    records, record_event = await _public_history(kernel, run)

    async def lifecycle(kind, current, payload):
        nonlocal crashed
        selected = not crashed and (
            (
                boundary == "message_end"
                and kind == "message_completed"
                and payload.get("disposition") in {"final", "error"}
            )
            or (boundary == "tool_end" and kind == "tool_execution_completed")
        )
        if selected and side == "before":
            crashed = True
            raise SimulatedRestart
        await record_event(kind, current, payload)
        if selected and side == "after":
            crashed = True
            raise SimulatedRestart

    with pytest.raises(SimulatedRestart):
        await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    first_winner = await ledger.load_by_record_id(children[0].call_record_id)
    assert first_winner.state == "canceled"
    assert a2a.hitl.is_abandoned_for_test("interaction-1")
    assert (await ledger.load_by_record_id(children[2].call_record_id)).state == (
        "input_required" if boundary == "child" else "canceled"
    )

    result = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert result.outcome == ("final_answer" if ending == "final" else "failed")
    assert len(model.requests) == (2 if ending == "final" else 3)
    assert result.run.budget.provider_retries_used == (0 if ending == "final" else 1)
    for child in children:
        winner = await ledger.load_by_record_id(child.call_record_id)
        assert winner.state == "canceled"
        assert winner.terminal_result is not None
        assert a2a.hitl.is_abandoned_for_test(child.pending_interaction_id)
    assert await ledger.load_by_record_id(children[0].call_record_id) == first_winner
    assert await a2a.hitl.get_eligible_interactions(run.conversation_id) == []
    assert await a2a.hitl.get_published_interactions(run.conversation_id) == []
    assert dispatch.commands == runtime.published == []
    entries = [entry for batch in result.run.tool_batches for entry in batch.entries]
    assert all(
        entry.state == "terminal"
        and entry.public_terminal_emitted
        and entry.result_flushed
        for entry in entries
    )
    ends = [
        record["payload_public"]["payload"]["tool_call_id"]
        for record in records.values()
        if record["payload_public"]["type"] == "tool_execution_end"
    ]
    assert len(ends) == len(set(ends)) == 3
    assert ends == [entry.opaque_public_call_id for entry in entries]
    _fold_settled(records, result.run)
    before = len(records)
    replay = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert replay.run == result.run
    assert len(records) == before


@pytest.mark.parametrize("extra_target", ["same", "distinct"])
@pytest.mark.parametrize(
    "boundary", [None, "visibility", "request", "waiting", "surface_checkpoint"]
)
async def test_surface_selects_one_of_two_durable_private_interactions(  # noqa: C901
    extra_target, boundary
):
    import json
    from types import SimpleNamespace

    from common.dto.hitl import HITLConfirmationAnswer, HITLQuestionAnswer
    from delivery.translator import to_sse_frame
    from execution.orchestrator.a2a_runtime.interaction_outcome import (
        emit_hitl_resolved_events,
    )
    from execution.orchestrator.a2a_runtime.ledger import (
        apply_observation,
        transition_call,
    )
    from execution.orchestrator.a2a_runtime.models import NormalizedA2AObservation
    from execution.orchestrator.models import (
        FrozenToolCatalogEntry,
        FrozenToolCatalogSnapshot,
        ToolObservation,
        ToolResultMessage,
    )
    from tests.orchestrator_harness.fake_tools import StaticFakeToolCatalog

    run = _run_with_retries(0)
    catalog = StaticFakeToolCatalog()
    tool = catalog.resolve(run, "fake_agent_pause")
    run = run.model_copy(
        update={
            "tool_catalog": FrozenToolCatalogSnapshot(
                catalog_id="catalog-1",
                created_at=NOW,
                entries=[
                    FrozenToolCatalogEntry(
                        definition=tool.definition, binding=tool.binding
                    )
                ],
            )
        }
    )
    a2a, ledger, _, _, _ = await setup()
    runtime = InteractionRuntime()
    calls = []
    parents = []
    for index in (1, 2):
        call_id = f"call-{index}"
        interaction_id = f"interaction-{index}"
        child = ledger_record(
            run_id=run.run_id, call_id=call_id, state="input_required"
        ).model_copy(
            update={
                "tool_name": "fake_agent_pause",
                "source_index": index - 1,
                "pending_interaction_id": interaction_id,
                "interaction_revision": 1,
                "interaction_fingerprint": f"fp-{index}",
            }
        )
        assert await ledger.insert(child) == "accepted"
        spec = _interaction("input_required")
        spec = spec.model_copy(
            update={
                "interaction_id": interaction_id,
                "questions": [
                    spec.questions[0],
                    spec.questions[0].model_copy(update={"question_id": "question-2"}),
                ],
            }
        )
        await a2a.hitl.create_or_replay(
            call=child,
            interaction=spec,
            interaction_fingerprint=f"fp-{index}",
        )
        parents.append(child)
        runtime.suspensions[call_id] = _interaction_suspension(
            call_id,
            call_record_id=child.call_record_id,
            interaction_id=interaction_id,
            fingerprint=f"fp-{index}",
        )
        calls.append((call_id, "fake_agent_pause", '{"status":"input_required"}'))
    runtime.publish_parked_interaction = a2a.publish_parked_interaction
    kernel, store, model, _ = await make_kernel(
        [tool_events(*calls)],
        run=run,
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    stream_turn = model.stream_turn
    selected = None

    async def select_from_private_schema(request, *, signal):
        nonlocal selected
        if len(model.requests) == 1:
            assert await a2a.hitl.get_published_interactions(run.conversation_id) == []
            tool = next(t for t in request.tools if t.name == "surface_agent_questions")
            targets = tool.input_schema["properties"]["presentation_id"]["enum"]
            assert len(targets) == 2
            selected = targets[1]
            model.scripts.append(
                tool_events(
                    (
                        "call-surface",
                        tool.name,
                        json.dumps({"presentation_id": selected}),
                    ),
                    (
                        "surface-extra",
                        tool.name,
                        json.dumps(
                            {
                                "presentation_id": selected
                                if extra_target == "same"
                                else targets[0]
                            }
                        ),
                    ),
                )
            )
        elif len(model.requests) == 2:
            current = await store.load(run.run_id)
            first, second = current.tool_batches[0].entries
            assert first.state == "input_required" and first.presented
            assert second.state == "terminal" and second.public_terminal_emitted
            assert current.tool_batches[1].entries[0].public_terminal_emitted
            assert any(
                isinstance(m, ToolResultMessage) and m.call_id == "call-2"
                for m in current.transcript
            )
            tool = next(t for t in request.tools if t.name == "surface_agent_questions")
            assert tool.input_schema["properties"] == {}
            model.scripts.append(tool_events(("surface-remaining", tool.name, "{}")))
        elif len(model.requests) == 3:
            model.scripts.append(final_events())
        async for event in stream_turn(request, signal=signal):
            yield event

    model.stream_turn = select_from_private_schema
    records, lifecycle = await _public_history(kernel, run)

    crashed = False
    mutate = store.cas_mutate

    async def cas(candidate, *, expected_state_version, command_id):
        nonlocal crashed
        result = await mutate(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if (
            boundary == "surface_checkpoint"
            and not crashed
            and command_id == "surface-questions:call-surface"
        ):
            crashed = True
            raise SimulatedRestart
        return result

    store.cas_mutate = cas

    def crash_at(at):
        nonlocal crashed
        if boundary == at and not crashed:
            crashed = True
            raise RuntimeError("publication acknowledgement lost")

    publish = a2a.hitl.publish

    async def publish_then_fail(*args, **kwargs):
        outcome = await publish(*args, **kwargs)
        crash_at("visibility")
        return outcome

    a2a.hitl.publish = publish_then_fail

    async def deliver(event):
        frame = to_sse_frame(event, timestamp=NOW)
        event_id = f"{frame['type']}:{event.interaction_id}:{event.request_id}"
        records.setdefault(
            event_id,
            {
                "room_id": run.conversation_id,
                "room_seq": len(records) + 1,
                "kind": frame["type"],
                "ts": frame["timestamp"],
                "payload_public": frame["data"],
            },
        )
        if frame["type"] == "hitl_request":
            crash_at("request")
        return True

    async def control(kind, run_id, interaction_id, request_ids):
        assert run_id == run.run_id
        payload = {"interaction_id": interaction_id}
        if kind == "run_waiting_input":
            payload.update(request_ids=request_ids, requested_at=NOW.isoformat())
        else:
            payload.update(resolved_request_ids=request_ids, resumed_at=NOW.isoformat())
        await lifecycle(kind, await store.load(run_id), payload)
        if kind == "run_waiting_input":
            crash_at("waiting")

    a2a.run_store = store
    a2a.hitl_delivery = SimpleNamespace(emit=deliver)
    a2a.canonical_hitl_control = control
    if boundary:
        from execution.orchestrator.a2a_runtime.errors import RecoverableCheckpointError

        with pytest.raises((RecoverableCheckpointError, SimulatedRestart)):
            await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
        interrupted = await store.load(run.run_id)
        assert interrupted.tool_batches[1].entries[0].state == (
            "input_required" if boundary == "surface_checkpoint" else "pending"
        )
        assert len(model.requests) == 2
        kernel = _restart(kernel)
    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "awaiting_user"
    assert len(model.requests) == 2
    rejected = result.run.tool_batches[1].entries[1]
    assert rejected.state == "terminal"
    assert rejected.opaque_public_call_id is None
    assert rejected.buffered_terminal_result.error_code == "surface_batch_limit"
    waiting_fold = RoomEventFold()
    for record in records.values():
        assert waiting_fold.apply(record), record
    assert (
        waiting_fold.state(room_seq=len(records))["turns"][0]["active_interaction_id"]
        == "interaction-2"
    )
    before = len(records)
    result = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert len(records) == before
    first, second = result.run.tool_batches[0].entries
    surface = result.run.tool_batches[1].entries[0]
    assert first.state == second.state == surface.state == "input_required"
    assert first.presentation_id != selected == second.presentation_id
    assert surface.surface_for_call_record_id == parents[1].call_record_id
    assert [
        interaction.interaction_id
        for interaction, _, _ in await a2a.hitl.get_published_interactions(
            run.conversation_id
        )
    ] == ["interaction-2"]
    assert await a2a.hitl.read_interaction("interaction-1") is not None
    assert not a2a.hitl.is_abandoned_for_test("interaction-1")
    assert (
        await ledger.load_by_record_id(parents[0].call_record_id)
    ).state == "input_required"
    assert selected not in repr(records)
    assert not any(
        record["payload_public"].get("type") == "message_end"
        and record["payload_public"]["payload"].get("disposition") == "final"
        for record in records.values()
    )

    for index, parent in enumerate(reversed(parents)):
        interaction_id = parent.pending_interaction_id
        spec, route, _ = await a2a.hitl.read_interaction(interaction_id)
        answer_digest = await a2a.hitl.answer(
            interaction_id=interaction_id,
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=[
                HITLQuestionAnswer(
                    question_id=question.question_id,
                    answer=HITLConfirmationAnswer(confirmed=True),
                )
                for question in spec.questions
            ],
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )
        assert (
            await a2a.hitl.read_answer_record(interaction_id, 1)
        ).answer_digest == answer_digest
        await emit_hitl_resolved_events(
            record=parent,
            interaction=spec,
            interaction_id=interaction_id,
            status="responded",
            answer_ref=f"answer-{index}",
            hitl_delivery=a2a.hitl_delivery,
            run_store=store,
            canonical_control=control,
        )
        resuming = transition_call(parent, to_state="resuming", updated_at=NOW)
        assert (
            await ledger.cas(resuming, expected_state_version=parent.state_version)
            == "accepted"
        )
        observation = NormalizedA2AObservation(
            observation_id=f"answer-result-{index}",
            call_record_id=parent.call_record_id,
            source_kind="direct",
            source_identity=f"direct:answer-{index}",
            binding_scope=parent.endpoint_scope_digest,
            event_kind="terminal",
            status="completed",
            observed_at=NOW,
        )
        terminal = apply_observation(
            resuming,
            observation,
            recent_limit=parent.runtime_policy.recent_observation_id_limit,
        )
        assert (
            await ledger.cas(terminal, expected_state_version=resuming.state_version)
            == "accepted"
        )
        await a2a.terminal_finalizer.finalize(terminal)
        result = await kernel.observe_tool(
            run.run_id,
            ToolObservation(
                observation_id=observation.observation_id,
                invocation_id=parent.invocation_id,
                outcome=terminal.terminal_result,
                observed_at=NOW,
            ),
            signal=NeverCancelled(),
            lifecycle=lifecycle,
        )
        if index == 0:
            assert result.outcome == "awaiting_user"
            assert len(model.requests) == 3
            assert (
                result.run.tool_batches[2].entries[0].surface_for_call_record_id
                == parents[0].call_record_id
            )
            assert [
                spec.interaction_id
                for spec, _, _ in await a2a.hitl.get_published_interactions(
                    run.conversation_id
                )
            ] == ["interaction-1"]
    assert result.outcome == "final_answer"
    assert len(model.requests) == 4
    assert result.run.budget.provider_retries_used == 0
    assert all(
        entry.state == "terminal"
        and (entry.public_terminal_emitted or entry.opaque_public_call_id is None)
        for batch in result.run.tool_batches
        for entry in batch.entries
    )
    assert await a2a.hitl.get_published_interactions(run.conversation_id) == []
    _fold_settled(records, result.run)


@pytest.mark.parametrize(
    "boundary", ["assistant_checkpoint", "message_end", "message_ack"]
)
@pytest.mark.parametrize("commentary", ["", "Checking the evidence."])
async def test_durable_tool_assistant_recovers_commentary_without_model_retry(
    monkeypatch, boundary, commentary
):
    run = _run_with_retries(0)
    script = tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}'))
    if commentary:
        script.insert(1, ModelStreamEvent(kind="text_delta", delta=commentary))
    kernel, store, model, tools = await make_kernel([script, final_events()], run=run)
    records, record_event = await _public_history(kernel, run)
    mutate = store.cas_mutate
    crashed = False

    async def cas(candidate, *, expected_state_version, command_id):
        nonlocal crashed
        result = await mutate(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if (
            boundary == "assistant_checkpoint"
            and command_id.startswith("assistant:")
            and not crashed
        ):
            crashed = True
            raise SimulatedRestart
        return result

    async def lifecycle(kind, current, payload):
        nonlocal crashed
        selected = (
            not crashed
            and kind == "message_completed"
            and payload.get("disposition") == "commentary"
        )
        if selected and boundary == "message_end":
            crashed = True
            raise SimulatedRestart
        await record_event(kind, current, payload)
        if selected and boundary == "message_ack":
            crashed = True
            raise SimulatedRestart

    monkeypatch.setattr(store, "cas_mutate", cas)
    with pytest.raises(SimulatedRestart):
        await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    interrupted = await store.load(run.run_id)
    assert len(model.requests) == 1
    assert tools.execute_log == []
    assert interrupted.tool_batches[0].entries[0].state == "pending"
    assistant_id = interrupted.active_assistant_message_id
    execute = tools.execute

    async def execute_before_next_model(*args, **kwargs):
        assert len(model.requests) == 1
        return await execute(*args, **kwargs)

    monkeypatch.setattr(tools, "execute", execute_before_next_model)
    result = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert result.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert len(model.requests) == 2  # Only the subsequent final needs another model.
    assert result.run.budget.provider_retries_used == 0
    events = [record["payload_public"] for record in records.values()]
    ends = [
        event["payload"]
        for event in events
        if event["type"] == "message_end"
        and event["payload"]["message_id"] == assistant_id
    ]
    assert len(ends) == 1
    assert ends[0]["disposition"] == "commentary"
    assert ends[0]["text"] == commentary
    assert not any(event["type"] == "retry_scheduled" for event in events)
    assert not any(
        event["type"] == "message_end" and event["payload"]["disposition"] == "aborted"
        for event in events
    )
    _fold_settled(records, result.run)
    before = len(records)
    await _restart(kernel).run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert len(records) == before


@pytest.mark.parametrize("conflict", ["deltas", "terminal_text", "final"])
async def test_checkpointed_tool_assistant_cannot_replace_conflicting_public_history(
    conflict,
):
    from execution.orchestrator.kernel import KernelConflict

    kernel, store, model, tools = await make_kernel(
        [tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}'))]
    )
    run = await store.load(next(iter(store.runs)))
    records, record_event = await _public_history(kernel, run)

    async def lifecycle(kind, current, payload):
        if kind == "message_completed" and payload.get("disposition") == "commentary":
            if conflict == "deltas":
                await record_event(
                    "message_updated",
                    current,
                    {
                        "internal_turn_id": current.active_internal_turn_id,
                        "message_id": current.active_assistant_message_id,
                        "content_index": 0,
                        "delta_index": 0,
                        "start_offset": 0,
                        "end_offset": 5,
                        "delta": "other",
                    },
                )
            else:
                await record_event(
                    kind,
                    current,
                    {
                        **payload,
                        **(
                            {"text": "other"}
                            if conflict == "terminal_text"
                            else {
                                "disposition": "final",
                                "stop_reason": "stop",
                            }
                        ),
                    },
                )
            raise SimulatedRestart
        await record_event(kind, current, payload)

    with pytest.raises(SimulatedRestart):
        await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    before = list(records.values())
    with pytest.raises(KernelConflict, match="contradicts"):
        await _restart(kernel).run(
            run.run_id, signal=NeverCancelled(), lifecycle=record_event
        )
    assert list(records.values()) == before
    assert len(model.requests) == 1
    assert tools.execute_log == []


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("active_identity_missing", [False, True])
async def test_recovery_does_not_adopt_an_older_tool_assistant(
    reader, active_identity_missing
):
    run = _run_with_retries(1)
    kernel, store, model, tools = await make_kernel(
        [tool_events(("call-1", "fake_agent_echo", '{"value":"ok"}')), final_events()],
        run=run,
    )
    records, record_event = await _public_history(kernel, run)
    if not reader:
        kernel.canonical_event_reader = None
    interrupted_id = None

    async def lifecycle(kind, current, payload):
        nonlocal interrupted_id
        await record_event(kind, current, payload)
        if (
            kind == "message_started"
            and len(model.requests) == 1
            and interrupted_id is None
        ):
            interrupted_id = current.active_assistant_message_id
            raise SimulatedRestart

    with pytest.raises(SimulatedRestart):
        await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    if active_identity_missing:
        current = await store.load(run.run_id)
        await store.cas_mutate(
            current.model_copy(
                update={
                    "active_assistant_message_id": None,
                    "state_version": current.state_version + 1,
                }
            ),
            expected_state_version=current.state_version,
            command_id="lose-active-identity",
        )
        if not reader:
            from execution.orchestrator.kernel import KernelConflict

            with pytest.raises(KernelConflict, match="no durable assistant message"):
                await _restart(kernel).run(
                    run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
                )
            # Neither advisory pointers nor public history prove a message owner.
            # The older committed tool Assistant must not be adopted as this one.
            assert len(model.requests) == 1
            assert tools.execute_log == ["call-1"]
            assert (
                len(
                    [
                        r
                        for r in records.values()
                        if r["payload_public"].get("type") == "message_end"
                    ]
                )
                == 1
            )
            return
    result = await _restart(kernel).run(
        run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert result.outcome == "final_answer"
    assert tools.execute_log == ["call-1"]
    assert len(model.requests) == 2
    assert result.run.budget.provider_retries_used == 1
    ends = [
        record["payload_public"]["payload"]
        for record in records.values()
        if record["payload_public"].get("type") == "message_end"
    ]
    assert [end["disposition"] for end in ends] == ["commentary", "aborted", "final"]
    assert ends[1]["message_id"] == interrupted_id
    _fold_settled(records, result.run)


@pytest.mark.parametrize(
    "failure",
    ["ledger", "owner", "hitl", "aggregate", "stale", "conflict", "absent", "error"],
)
async def test_kernel_rejects_unpublished_runtime_surface_without_phantom_hitl(
    monkeypatch, failure
):
    run = _run_with_retries(0)
    a2a, ledger, _, _, _ = await setup()
    call = ledger_record(run_id=run.run_id, state="input_required").model_copy(
        update={
            "tool_name": "fake_agent_pause",
            "pending_interaction_id": "interaction-1",
            "interaction_revision": 1,
            "interaction_fingerprint": "fp-1",
        }
    )
    if failure != "ledger":
        persisted = (
            call.model_copy(update={"pending_interaction_id": "other"})
            if failure == "owner"
            else call
        )
        assert await ledger.insert(persisted) == "accepted"
    owner = a2a.hitl
    if failure != "aggregate":
        await owner.create_or_replay(
            call=call,
            interaction=_interaction("input_required"),
            interaction_fingerprint="fp-1",
        )
    if failure == "hitl":
        a2a.hitl = None
    elif failure in {"stale", "conflict", "absent", "error"}:
        monkeypatch.setattr(owner, "publish", AsyncMock(return_value=failure))
    emit = AsyncMock()
    monkeypatch.setattr(a2a, "_emit_parked_hitl_events", emit)
    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension(
        "call-1", call_record_id=call.call_record_id
    )
    runtime.publish_parked_interaction = a2a.publish_parked_interaction
    kernel, store, model, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("surface", "surface_agent_questions", "{}")),
            final_events(),
        ],
        run=run,
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    records, record_event = await _public_history(kernel, run)
    decisions = []

    async def lifecycle(kind, current, payload):
        if kind == "model_decision":
            decisions.append(payload["decision"])
        await record_event(kind, current, payload)

    if failure in {"hitl", "error"}:
        from execution.orchestrator.a2a_runtime.errors import RecoverableCheckpointError

        with pytest.raises(RecoverableCheckpointError):
            await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
        current = await store.load(run.run_id)
        assert current.status == "running"
        assert current.tool_batches[1].entries[0].state == "pending"
        assert len(model.requests) == 2
        emit.assert_not_awaited()
        return
    result = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert result.outcome == "final_answer"
    assert (await store.load(run.run_id)).status == "completed"
    assert len(model.requests) == 3
    surface = result.run.tool_batches[1].entries[0]
    assert surface.state == "terminal"
    assert surface.buffered_terminal_result.error_code == "surface_publication_failed"
    assert surface.opaque_public_call_id is None
    assert "forwarded_to_user" not in decisions
    emit.assert_not_awaited()
    assert await owner.get_published_interactions(run.conversation_id) == []
    events = [record["payload_public"] for record in records.values()]
    assert not any(
        event["type"] in {"hitl_request", "run_waiting_input"} for event in events
    )
    assert (
        len([event for event in events if event["type"] == "tool_execution_start"]) == 1
    )
    _fold_settled(records, result.run)


@pytest.mark.parametrize("surface_remaining", [False, True])
async def test_surface_answer_waits_for_still_executing_sibling(
    monkeypatch, surface_remaining
):
    import json

    from execution.orchestrator.models import TextPart, ToolObservation, ToolResult

    runtime = InteractionRuntime()
    for index in (1, 2, 3):
        runtime.suspensions[f"call-{index}"] = _interaction_suspension(
            f"call-{index}",
            call_record_id=f"parent-{index}",
            interaction_id=f"interaction-{index}",
            fingerprint=f"fp-{index}",
        )
    run = _run_with_retries(0)
    kernel, store, model, _ = await make_kernel(
        [
            tool_events(
                *[
                    (f"call-{index}", "fake_agent_pause", '{"status":"input_required"}')
                    for index in (1, 2, 3)
                ]
            ),
        ],
        run=run,
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    stream = model.stream_turn

    async def select(request, *, signal):
        if len(model.requests) == 1:
            tool = next(t for t in request.tools if t.name == "surface_agent_questions")
            targets = tool.input_schema["properties"]["presentation_id"]["enum"]
            calls = [
                ("surface", tool.name, json.dumps({"presentation_id": targets[0]}))
            ]
            if surface_remaining:
                calls = [
                    (
                        "surface-c",
                        tool.name,
                        json.dumps({"presentation_id": targets[2]}),
                    )
                ]
            model.scripts.append(tool_events(*calls))
        async for event in stream(request, signal=signal):
            yield event

    monkeypatch.setattr(model, "stream_turn", select)
    records, lifecycle = await _public_history(kernel, run)
    waiting = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    # An already-presented sibling has since accepted a remote continuation.
    # Its outstanding observation is not an unanswered model decision.
    batches = list(waiting.run.tool_batches)
    entries = list(batches[0].entries)
    entries[1] = entries[1].model_copy(update={"state": "waiting_external"})
    batches[0] = batches[0].model_copy(update={"entries": entries})
    await store.cas_mutate(
        waiting.run.model_copy(
            update={
                "tool_batches": batches,
                "state_version": waiting.run.state_version + 1,
            }
        ),
        expected_state_version=waiting.run.state_version,
        command_id="remote-continuation-accepted",
    )

    def terminal(call_id):
        return ToolObservation(
            observation_id=f"result-{call_id}",
            invocation_id=call_id,
            outcome=ToolResult(
                call_id=call_id,
                tool_name="fake_agent_pause",
                status="completed",
                content=[TextPart(text="answered")],
                artifact_refs=[],
            ),
            observed_at=NOW,
        )

    still_waiting = await kernel.observe_tool(
        run.run_id, terminal("call-1"), signal=NeverCancelled(), lifecycle=lifecycle
    )
    assert (
        still_waiting.outcome
        == still_waiting.run.status
        == ("awaiting_user" if surface_remaining else "waiting_external")
    )
    assert len(model.requests) == 2
    assert still_waiting.run.tool_batches[0].entries[0].public_terminal_emitted
    if not surface_remaining:
        assert still_waiting.run.tool_batches[1].entries[0].public_terminal_emitted
    assert still_waiting.run.tool_batches[0].entries[1].state == "waiting_external"
    private_child = still_waiting.run.tool_batches[0].entries[2]
    assert private_child.state == "input_required" and private_child.presented
    assert still_waiting.run.tool_batches[1].results_flushed is not surface_remaining
    model.scripts.append(final_events())
    completed = await kernel.observe_tool(
        run.run_id, terminal("call-2"), signal=NeverCancelled(), lifecycle=lifecycle
    )
    if surface_remaining:
        # A real remaining public surface still needs an answer; private
        # presentation alone must not authorize a model turn in this case.
        assert completed.outcome == completed.run.status == "awaiting_user"
        assert len(model.requests) == 2
        completed = await kernel.observe_tool(
            run.run_id, terminal("call-3"), signal=NeverCancelled(), lifecycle=lifecycle
        )
    assert completed.outcome == "final_answer"
    assert completed.run.status == "completed"
    assert len(model.requests) == 3
    assert runtime.published == (
        [("parent-3", "interaction-3")]
        if surface_remaining
        else [("parent-1", "interaction-1")]
    )
    assert runtime.abandoned == (
        [] if surface_remaining else [("parent-3", "interaction-3")]
    )
    assert all(
        entry.state == "terminal" and entry.public_terminal_emitted
        for batch in completed.run.tool_batches
        for entry in batch.entries
    )
    _fold_settled(records, completed.run)


@pytest.mark.parametrize(
    "boundary",
    [
        None,
        "reservation",
        "message_end",
        "closure",
        "compaction_start",
        "compaction",
        "retry_event",
    ],
)
@pytest.mark.parametrize("max_compactions", [0, 1])
@pytest.mark.parametrize("ending", ["final", "overflow", "provider_error"])
async def test_overflow_uses_only_compaction_budget_across_restart(
    monkeypatch, boundary, max_compactions, ending
):
    from execution.model_loop.compaction import DeterministicFakeCompactor
    from execution.model_loop.context import CompiledContext
    from execution.orchestrator.models import ModelMessage, ModelTextPart

    class ShrinkingCompiler:
        def compile(self, run, *, tools, summary=None):
            return CompiledContext(
                kind="ready",
                messages=[
                    ModelMessage(role="user", content=[ModelTextPart(text="context")])
                ],
                estimated_input_tokens=100 if summary is None else 50,
                reserved_output_tokens=10,
                retained_transcript_indexes=(0,),
                compacted=summary is not None,
            )

    overflow = [
        ModelStreamEvent(kind="attempt_started", attempt=1),
        ModelStreamEvent(
            kind="attempt_failed",
            attempt=1,
            error_class="context_overflow",
            retryable=False,
        ),
        ModelStreamEvent(
            kind="error", attempt=1, error_class="context_overflow", retryable=False
        ),
    ]
    run = _run_with_retries(1 if ending == "provider_error" else 0)
    run = run.model_copy(
        update={
            "profile": run.profile.model_copy(
                update={"max_compactions": max_compactions}
            )
        }
    )
    kernel, store, model, _ = await make_kernel(
        [overflow]
        + (
            {
                "final": [final_events()],
                "overflow": [overflow],
                "provider_error": [_provider_error(), _provider_error()],
            }[ending]
        ),
        run=run,
    )
    kernel.context_compiler = ShrinkingCompiler()
    kernel.context_compactor = DeterministicFakeCompactor()
    records, record_event = await _public_history(kernel, run)
    mutate = store.cas_mutate
    crashed = False
    compact = kernel.context_compactor.compact
    compact_calls = 0

    async def compact_with_attempt(*args, **kwargs):
        nonlocal crashed, compact_calls
        compact_calls += 1
        await kwargs["on_event"](ModelStreamEvent(kind="attempt_started", attempt=1))
        if boundary == "compaction_start" and not crashed:
            crashed = True
            raise asyncio.CancelledError
        return await compact(*args, **kwargs)

    monkeypatch.setattr(kernel.context_compactor, "compact", compact_with_attempt)

    async def cas(candidate, *, expected_state_version, command_id):
        nonlocal crashed
        result = await mutate(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if (
            max_compactions
            and not crashed
            and (
                (
                    boundary == "closure"
                    and command_id.startswith("public-close-attempt:")
                )
                or (boundary == "compaction" and command_id.startswith("compaction:"))
                or (
                    boundary == "reservation"
                    and command_id.startswith("context-overflow:")
                )
            )
        ):
            crashed = True
            raise SimulatedRestart
        return result

    async def lifecycle(kind, current, payload):
        nonlocal crashed
        await record_event(kind, current, payload)
        if (
            max_compactions
            and not crashed
            and (
                (
                    boundary == "message_end"
                    and kind == "message_completed"
                    and payload.get("disposition") == "error"
                )
                or (
                    boundary == "retry_event"
                    and kind == "model_retry_scheduled"
                    and payload.get("error_class") == "context_overflow"
                )
            )
        ):
            crashed = True
            raise SimulatedRestart

    monkeypatch.setattr(store, "cas_mutate", cas)
    try:
        result = await kernel.run(
            run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
        )
    except (SimulatedRestart, asyncio.CancelledError):
        restarted = _restart(kernel)
        restarted.context_compactor = kernel.context_compactor
        result = await restarted.run(
            run.run_id, signal=NeverCancelled(), lifecycle=lifecycle
        )
    expected = (
        "budget_exhausted"
        if not max_compactions or ending == "overflow"
        else ("failed" if ending == "provider_error" else "final_answer")
    )
    assert result.outcome == expected
    assert result.run.budget.provider_retries_used == int(
        bool(max_compactions and ending == "provider_error")
    )
    assert result.run.budget.compactions_used == max_compactions
    assert len(model.requests) == 1 + max_compactions + int(
        bool(max_compactions and ending == "provider_error")
    )
    assert compact_calls == (
        0 if not max_compactions else (2 if boundary == "compaction_start" else 1)
    )
    assert crashed == bool(boundary and max_compactions)
    _fold_settled(records, result.run)


@pytest.mark.parametrize("failure", ["invalid_schema", "rejected_target"])
@pytest.mark.parametrize("restart", [False, True])
async def test_rejected_surface_does_not_consume_batch_slot(
    monkeypatch, failure, restart
):
    from execution.orchestrator.models import ToolObservation, ToolResult
    from execution.orchestrator.ports import InvalidParkedInteractionTarget

    runtime = InteractionRuntime()
    runtime.suspensions["call-1"] = _interaction_suspension("call-1")
    publish = runtime.publish_parked_interaction
    rejected = False

    async def publication(**kwargs):
        nonlocal rejected
        if failure == "rejected_target" and not rejected:
            rejected = True
            raise InvalidParkedInteractionTarget("target rejected")
        await publish(**kwargs)

    runtime.publish_parked_interaction = publication
    kernel, store, model, _ = await make_kernel(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(
                (
                    "rejected-surface",
                    "surface_agent_questions",
                    '{"unknown":true}' if failure == "invalid_schema" else "{}",
                ),
                ("accepted-surface", "surface_agent_questions", "{}"),
            ),
            final_events(),
        ],
        tool_runtime=runtime,
        supervisor_hitl=AsyncMock(),
    )
    run = await store.load(next(iter(store.runs)))
    records, lifecycle = await _public_history(kernel, run)
    mutate = store.cas_mutate
    crashed = False

    async def cas(candidate, *, expected_state_version, command_id):
        nonlocal crashed
        result = await mutate(
            candidate,
            expected_state_version=expected_state_version,
            command_id=command_id,
        )
        if (
            restart
            and not crashed
            and command_id
            in {
                "invalid-surface:rejected-surface",
                "surface-publication-failed:rejected-surface",
            }
        ):
            crashed = True
            raise SimulatedRestart
        return result

    monkeypatch.setattr(store, "cas_mutate", cas)
    if restart:
        with pytest.raises(SimulatedRestart):
            await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
        kernel = _restart(kernel)
    waiting = await kernel.run(run.run_id, signal=NeverCancelled(), lifecycle=lifecycle)
    assert waiting.outcome == "awaiting_user"
    first, second = waiting.run.tool_batches[1].entries
    assert first.state == "terminal" and first.opaque_public_call_id is None
    assert second.state == "input_required"
    assert runtime.published == [("parent-1", "interaction-1")]
    assert len(model.requests) == 2
    completed = await kernel.observe_tool(
        run.run_id,
        ToolObservation(
            observation_id="answer",
            invocation_id="call-1",
            outcome=ToolResult(
                call_id="call-1",
                tool_name="fake_agent_pause",
                status="completed",
                content=[],
                artifact_refs=[],
            ),
            observed_at=NOW,
        ),
        signal=NeverCancelled(),
        lifecycle=lifecycle,
    )
    assert completed.outcome == "final_answer"
    _fold_settled(records, completed.run)


@pytest.mark.parametrize("entrypoint", ["prompt", "observation"])
@pytest.mark.parametrize("answer_before_recovery", [False, True])
@pytest.mark.parametrize("mongo_schedule", [False, True])
async def test_host_schedules_partial_publication_and_drivers_apply_queued_answer(  # noqa: C901
    monkeypatch, answer_before_recovery, mongo_schedule, entrypoint
):
    import json
    from datetime import datetime, timedelta
    from hashlib import sha256
    from types import SimpleNamespace

    import orchestrator_composition as composition
    from approval.exceptions import HITLDeliveryUncertainError
    from delivery.translator import to_sse_frame
    from execution.adapters.hitl import DurableHITLApplicationPort
    from execution.adapters.session_host import RoomSessionHost
    from execution.orchestrator.a2a_runtime.errors import RecoverableCheckpointError
    from execution.orchestrator.a2a_runtime.models import NormalizedA2AObservation
    from execution.orchestrator.models import (
        FrozenToolCatalogEntry,
        FrozenToolCatalogSnapshot,
        ToolInteractionQuestion,
        ToolSuspension,
    )
    from execution.orchestrator.session import DefaultRunFactory
    from execution.orchestrator_routing import DualRuntimeRouter
    from execution.repository.orchestrator.run_store import MongoOrchestratorRunStore
    from tests._orchestrator_helpers import FixedIDs, ScriptedModelRuntime, user_message
    from tests.orchestrator_harness.a2a_in_memory import InMemoryHITLApplicationStore
    from tests.orchestrator_harness.fake_tools import StaticFakeToolCatalog
    from tests.orchestrator_harness.in_memory import InMemoryOrchestratorRunStore
    from tests.test_orchestrator_a2a_hitl_recovery_auth import (
        questionnaire_spec,
        setup_waiting,
    )
    from tests.test_orchestrator_a2a_mongo_parity import FakeCollection
    from tests.test_orchestrator_composition import _deps

    class Clock:
        value = NOW

        def now(self):
            return self.value

    clock = Clock()

    class DriverDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            clock.value += timedelta(microseconds=1)
            return clock.value

    monkeypatch.setattr(composition, "datetime", DriverDatetime)
    monkeypatch.setattr(
        "execution.orchestrator.a2a_runtime.hitl.datetime", DriverDatetime
    )
    monkeypatch.setattr(
        "execution.orchestrator.a2a_runtime.ingress.datetime", DriverDatetime
    )
    coordinator, ledger, _, _, dispatch, call, _ = await setup_waiting()
    spec = questionnaire_spec()
    spec = spec.model_copy(
        update={
            "questions": [
                spec.questions[0],
                spec.questions[0].model_copy(update={"question_id": "q2"}),
            ]
        }
    )
    fingerprint = sha256(
        json.dumps(
            spec.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    call = call.model_copy(
        update={"tool_name": "fake_agent_pause", "interaction_fingerprint": fingerprint}
    )
    if entrypoint == "observation":
        call = call.model_copy(
            update={
                "state": "working",
                "pending_interaction_id": None,
                "interaction_revision": None,
                "interaction_fingerprint": None,
            }
        )
    ledger = type(ledger)()
    await ledger.insert(call)
    owner = DurableHITLApplicationPort(hitl_store=InMemoryHITLApplicationStore())
    if entrypoint == "prompt":
        await owner.create_or_replay(
            call=call,
            interaction=spec,
            interaction_fingerprint=call.interaction_fingerprint,
        )
    a2a, _, _, _, _ = await setup(ledger=ledger, hitl=owner)
    tools = InteractionRuntime()
    tools.suspensions["call-1"] = _interaction_suspension(
        "call-1",
        call_record_id=call.call_record_id,
        fingerprint=call.interaction_fingerprint,
    )
    tools.suspensions["call-1"] = tools.suspensions["call-1"].model_copy(
        update={
            "questions": [
                ToolInteractionQuestion(
                    question_id=q.question_id,
                    prompt=q.prompt,
                    answer_kind="single_choice",
                    choices=list(q.choices),
                )
                for q in spec.questions
            ]
        }
    )
    if entrypoint == "observation":
        tools.suspensions["call-1"] = ToolSuspension(
            invocation_id=call.invocation_id,
            status="waiting_external",
            call_record_id=call.call_record_id,
        )
    tools.publish_parked_interaction = a2a.publish_parked_interaction
    store = (
        MongoOrchestratorRunStore(FakeCollection(), FakeCollection())
        if mongo_schedule
        else InMemoryOrchestratorRunStore()
    )
    model = ScriptedModelRuntime(
        [
            tool_events(("call-1", "fake_agent_pause", '{"status":"input_required"}')),
            tool_events(("surface", "surface_agent_questions", "{}")),
            final_events(),
        ]
    )
    seed = make_run()
    kernel, _, _, _ = await make_kernel([], run=seed)
    records, lifecycle = await _public_history(kernel, seed)

    async def listener(event):
        await lifecycle(event.event_type, await store.load(event.run_id), event.payload)

    failed = False

    async def deliver(event):
        nonlocal failed
        frame = to_sse_frame(event, timestamp=NOW)
        key = f"{frame['type']}:{event.interaction_id}:{event.request_id}"
        records.setdefault(
            key,
            {
                "room_id": seed.conversation_id,
                "room_seq": len(records) + 1,
                "kind": frame["type"],
                "ts": NOW.isoformat(),
                "payload_public": frame["data"],
            },
        )
        if frame["type"] == "hitl_request" and not failed:
            failed = True
            raise RuntimeError("request append acknowledgement lost")
        return True

    async def control(kind, run_id, interaction_id, request_ids):
        payload = {"interaction_id": interaction_id}
        if kind == "run_waiting_input":
            payload.update(
                request_ids=request_ids, requested_at=clock.now().isoformat()
            )
        else:
            payload.update(
                resolved_request_ids=request_ids, resumed_at=clock.now().isoformat()
            )
        await lifecycle(kind, await store.load(run_id), payload)

    deps = _deps()
    for name, instance in {
        "MongoOrchestratorRunStore": store,
        "MongoAgentCallLedgerStore": ledger,
        "MongoAgentToolBindingStore": coordinator.bindings,
        "MongoObservationInboxStore": coordinator.observations.inbox,
        "MongoObservationConflictStore": coordinator.observations.conflicts,
        "DurableHITLApplicationPort": owner,
        "MembershipAuthorizationRefresh": coordinator.authorization,
        "DirectA2ADispatchAdapter": dispatch,
        "GatewayModelRuntime": model,
        "A2AAgentToolRuntime": tools,
    }.items():
        monkeypatch.setattr(
            composition, name, lambda *args, _instance=instance, **kwargs: _instance
        )
    monkeypatch.setattr(
        composition,
        "DefaultRunFactory",
        lambda: DefaultRunFactory(clock=clock, id_factory=FixedIDs()),
    )
    monkeypatch.setattr(
        composition,
        "RoomSessionHost",
        lambda **kwargs: RoomSessionHost(**kwargs, clock=clock),
    )
    real_kernel = OrchestratorKernel
    constructed = []

    def create_kernel(**kwargs):
        result = real_kernel(**kwargs, clock=clock)
        constructed.append(result)
        return result

    monkeypatch.setattr(composition, "OrchestratorKernel", create_kernel)
    runtime = composition.create_orchestrator_runtime(
        mongo=deps.mongo,
        settings_obj=deps.settings,
        llm_gateway=deps.llm_gateway,
        model_registry=deps.model_registry,
        agent_registry=deps.agent_registry,
        exclusion_reader=deps.exclusion_reader,
        room_ownership_reader=deps.room_ownership_reader,
        epoch_store=coordinator.room_epochs,
        room_files=deps.room_files,
        message_writer=MagicMock(),
        session_listener=listener,
        canonical_event_reader=kernel.canonical_event_reader,
        hitl_delivery=SimpleNamespace(emit=deliver),
        canonical_hitl_control=control,
        supervisor_hitl=AsyncMock(),
    )
    a2a.run_store = store
    a2a.hitl_delivery = runtime.hitl_delivery
    a2a.canonical_hitl_control = control
    tool = StaticFakeToolCatalog().resolve(seed, "fake_agent_pause")
    snapshot = FrozenToolCatalogSnapshot(
        catalog_id="test",
        created_at=NOW,
        entries=[
            FrozenToolCatalogEntry(definition=tool.definition, binding=tool.binding)
        ],
    )
    host = runtime.session_host
    await host.create_session(
        conversation_id=seed.conversation_id,
        profile=seed.profile,
        candidate_scope=seed.candidate_scope,
        requesting_subject_id="user-1",
        frozen_catalog=snapshot,
    )
    if entrypoint == "prompt":
        with pytest.raises(RecoverableCheckpointError):
            await host.prompt(
                seed.conversation_id,
                user_message(),
                client_request_id=seed.client_request_id,
            )
    else:
        waiting = await host.prompt(
            seed.conversation_id,
            user_message(),
            client_request_id=seed.client_request_id,
        )
        assert waiting.outcome == waiting.run.status == "waiting_external"
        assert len(model.requests) == 1
        assert await owner.read_interaction(spec.interaction_id) is None
        # Run-addressed ingress must recover even after the hosted session is gone.
        host.drop_session(seed.conversation_id)
        outcome, inbox = await runtime.observation_ingress.record(
            NormalizedA2AObservation(
                observation_id="remote-questions",
                call_record_id=call.call_record_id,
                source_kind="direct",
                source_identity="direct:remote-questions",
                binding_scope=call.endpoint_scope_digest,
                event_kind="input_required",
                observed_at=clock.now(),
                task_id=call.a2a_task_id,
                context_id=call.a2a_context_id,
                interaction_spec=spec.model_dump(mode="json"),
            )
        )
        assert outcome == "accepted"
        assert await runtime.observation_processor.process(inbox.observation_id) == (
            "retryable"
        )
        assert await runtime.observation_processor.outcome_reader.has_processed_observation(
            seed.run_id, call.invocation_id, inbox.observation_id
        )
    interrupted = await store.load(seed.run_id)
    assert abs(
        interrupted.recovery_claim.next_attempt_at
        - (clock.now() + timedelta(seconds=5))
    ) < timedelta(milliseconds=1)  # Mongo scheduling uses BSON millisecond precision.
    assert len(model.requests) == 2
    assert interrupted.status == "running"
    assert len([row for row in records.values() if row["kind"] == "hitl_request"]) == 1
    assert not any(
        row["payload_public"].get("type") == "run_waiting_input"
        for row in records.values()
    )
    assert interrupted.tool_batches[1].entries[0].state == "pending"
    assert (await owner.get_published_interactions(seed.conversation_id))[0][
        0
    ].interaction_id == "interaction-1"
    _, route, _ = await owner.read_interaction("interaction-1")

    router = DualRuntimeRouter(runtime=runtime)

    async def answer():
        return await router.route_hitl_answer(
            interaction_id="interaction-1",
            answers=[
                {"request_id": q.question_id, "user_input": "a"} for q in spec.questions
            ],
            responder_id="user-1",
            conversation_id=seed.conversation_id,
        )

    if answer_before_recovery:
        with pytest.raises(HITLDeliveryUncertainError):
            await answer()
        assert await owner.read_answer_record("interaction-1", 1) is not None
        assert await owner.get_published_interactions(seed.conversation_id) == []
        assert (
            await ledger.load_by_record_id(call.call_record_id)
        ).state == "input_required"
        assert dispatch.commands == []
        assert not any(row["kind"] == "hitl_response" for row in records.values())
    assert (
        NOW
        < interrupted.recovery_claim.next_attempt_at
        < interrupted.budget.deadline_at
    )
    phases = dict(runtime.recovery_cycle.phases)
    before_recovery = len(constructed)
    assert before_recovery == (1 if entrypoint == "prompt" else 2)
    await phases["generic_runs"]()
    assert len(constructed) == before_recovery
    clock.value += timedelta(seconds=5)
    await phases["generic_runs"]()
    recovered = await store.load(seed.run_id)
    assert len(constructed) == before_recovery + 1
    assert len(model.requests) == 2
    assert recovered.status == "awaiting_user"
    assert recovered.tool_batches[1].entries[0].state == "input_required"
    assert [
        row["payload_public"]["request_id"]
        for row in records.values()
        if row["kind"] == "hitl_request"
    ] == [q.question_id for q in spec.questions]
    assert (
        len(
            [
                row
                for row in records.values()
                if row["payload_public"].get("type") == "run_waiting_input"
            ]
        )
        == 1
    )
    waiting_fold = RoomEventFold()
    for row in records.values():
        assert waiting_fold.apply(row), row
    assert (
        waiting_fold.state(room_seq=len(records))["turns"][0]["active_interaction_id"]
        == spec.interaction_id
    )
    assert dispatch.commands == []
    if not answer_before_recovery:
        assert await answer() == "working"
    else:
        await phases["continuation"]()
    assert len(dispatch.commands) == 1
    assert (await ledger.load_by_record_id(call.call_record_id)).state == "working"
    fold = RoomEventFold()
    for row in records.values():
        assert fold.apply(row), row
    assert (
        fold.state(room_seq=len(records))["turns"][0]["active_interaction_id"] is None
    )
    parent = await ledger.load_by_record_id(call.call_record_id)
    observation = NormalizedA2AObservation(
        observation_id="remote-completed",
        call_record_id=parent.call_record_id,
        source_kind="direct",
        source_identity="direct:remote-completed",
        binding_scope=parent.endpoint_scope_digest,
        event_kind="terminal",
        status="completed",
        observed_at=clock.now(),
        task_id=parent.a2a_task_id,
        context_id=parent.a2a_context_id,
    )
    outcome, terminal_inbox = await runtime.observation_ingress.record(observation)
    assert outcome == "accepted"
    assert await runtime.observation_processor.process(
        terminal_inbox.observation_id
    ) == ("accepted")
    completed = await store.load(seed.run_id)
    assert completed.status == "completed"
    assert completed.tool_batches[1].entries[0].state == "terminal"
    assert len(model.requests) == 3
    _fold_settled(records, completed)
    if entrypoint == "observation":
        # The failed processor claim expires later; redelivery is now audit-only.
        clock.value += timedelta(seconds=61)
        before_replay = (len(constructed), len(records))
        assert await runtime.observation_processor.process(inbox.observation_id) == (
            "accepted"
        )
        assert (len(constructed), len(records)) == before_replay
        assert (await runtime.observation_inbox.load(inbox.observation_id)).state == (
            "completed"
        )
