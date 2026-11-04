"""The approval package emits catalog events with ids, never exception text."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError, OperationFailure

from approval.application import HITLApplicationCoordinator
from approval.exceptions import HITLRoutingFailedError
from approval.reconciler import HITLLifecycleReconciler
from approval.repository import HITLLifecycleStore, HITLRequestStore
from approval.service import MAX_HITL_ROUNDS, HITLService
from common.dto.hitl import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
    HITLRouteSnapshot,
)
from tests.test_hitl_aggregate_validation import _aggregate, _rows

SECRET = "secret-detail-must-not-be-logged"


class _Failed(RuntimeError):
    pass


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("approval.") and record.getMessage() == event
    ]


def _one(caplog, event: str) -> logging.LogRecord:
    [record] = _events(caplog, event)
    return record


def _assert_no_secret(caplog) -> None:
    for record in caplog.records:
        assert record.exc_info is None
        assert SECRET not in repr(record.__dict__)


def _assert_failure(record: logging.LogRecord, level: int) -> None:
    assert record.levelno == level
    assert record.error_type == "_Failed"


async def _raise(*_args, **_kwargs):
    raise _Failed(SECRET)


def _failing_collection() -> MagicMock:
    collection = MagicMock()
    for method in (
        "find",
        "find_one",
        "insert_one",
        "update_one",
        "find_one_and_update",
        "count",
        "create_index",
    ):
        setattr(collection, method, AsyncMock(side_effect=_Failed(SECRET)))
    return collection


def _request_store(collection) -> HITLRequestStore:
    return HITLRequestStore(hitl_requests=collection, message_writer=MagicMock())


# ── request store ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "call", "ids", "fallback"),
    [
        (
            "get_pending_hitl_requests_for_message",
            lambda s: s.get_pending_hitl_requests_for_message("msg-1"),
            {"user_message_id": "msg-1"},
            [],
        ),
        (
            "create_hitl_request",
            lambda s: s.create_hitl_request(
                {"request_id": "req-1", "room_id": "room-1", "prompt": SECRET}
            ),
            {"hitl_request_id": "req-1", "conversation_id": "room-1"},
            False,
        ),
        (
            "get_hitl_request",
            lambda s: s.get_hitl_request("req-1"),
            {"hitl_request_id": "req-1"},
            None,
        ),
        (
            "update_hitl_request",
            lambda s: s.update_hitl_request("req-1", user_input=SECRET),
            {"hitl_request_id": "req-1"},
            False,
        ),
        (
            "cas_update_hitl_request",
            lambda s: s.cas_update_hitl_request("req-1", "pending", prompt=SECRET),
            {"hitl_request_id": "req-1"},
            False,
        ),
        (
            "fenced_update_hitl_request",
            lambda s: s.fenced_update_hitl_request("req-1", "claim-1", status="x"),
            {"hitl_request_id": "req-1"},
            False,
        ),
        (
            "claim_hitl_request",
            lambda s: s.claim_hitl_request("req-1", status="answer_recorded"),
            {"hitl_request_id": "req-1"},
            None,
        ),
        (
            "get_pending_hitl_requests",
            lambda s: s.get_pending_hitl_requests("room-1"),
            {"conversation_id": "room-1"},
            [],
        ),
        (
            "count_hitl_requests_for_message",
            lambda s: s.count_hitl_requests_for_message("cont-1"),
            {"continuation_message_id": "cont-1"},
            0,
        ),
        (
            "find_pending_hitl_request_for_agent_message",
            lambda s: s.find_pending_hitl_request_for_agent_message(
                conversation_id="room-1",
                display_message_id="display-1",
                continuation_message_id="cont-1",
                agent_id="agent-1",
                a2a_task_id="task-1",
                a2a_context_id="ctx-1",
            ),
            {
                "conversation_id": "room-1",
                "display_message_id": "display-1",
                "continuation_message_id": "cont-1",
                "agent_id": "agent-1",
            },
            None,
        ),
        (
            "create_or_reuse_pending_hitl_request",
            lambda s: s.create_or_reuse_pending_hitl_request(
                {"request_id": "req-1", "room_id": "room-1", "prompt": SECRET}
            ),
            {"hitl_request_id": "req-1", "conversation_id": "room-1"},
            None,
        ),
    ],
)
async def test_request_store_failure_emits_hitl_store_operation_failed(
    caplog, operation, call, ids, fallback
):
    caplog.set_level(logging.DEBUG)

    assert await call(_request_store(_failing_collection())) == fallback

    record = _one(caplog, "hitl_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == operation
    for key, value in ids.items():
        assert getattr(record, key) == value
    _assert_no_secret(caplog)


def _duplicate_collection(find_one_results, error) -> MagicMock:
    collection = MagicMock()
    collection.insert_one = AsyncMock(side_effect=error)
    collection.find_one = AsyncMock(side_effect=list(find_one_results))
    return collection


_PENDING_DOC = {
    "request_id": "req-new",
    "room_id": "room-1",
    "status": "pending",
    "public_source": "agent",
    "display_message_id": "display-1",
    "continuation_message_id": "cont-1",
    "prompt": SECRET,
}


@pytest.mark.asyncio
async def test_ambiguous_duplicate_readback_emits_conflict_detected(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _duplicate_collection(
        [{"request_id": "req-display"}, {"request_id": "req-cont"}],
        DuplicateKeyError("E11000 uq_pending_hitl_display_message"),
    )

    assert (
        await _request_store(collection).create_or_reuse_pending_hitl_request(
            dict(_PENDING_DOC)
        )
        is None
    )

    record = _one(caplog, "pending_hitl_request_conflict_detected")
    assert record.levelno == logging.ERROR
    assert record.conversation_id == "room-1"
    assert record.display_message_id == "display-1"
    assert record.continuation_message_id == "cont-1"
    assert record.display_request_id == "req-display"
    assert record.continuation_request_id == "req-cont"
    assert record.lookup == "duplicate_readback"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_ambiguous_pending_lookup_emits_conflict_detected(caplog):
    caplog.set_level(logging.DEBUG)
    collection = MagicMock()
    collection.find_one = AsyncMock(
        side_effect=[{"request_id": "req-display"}, {"request_id": "req-cont"}]
    )

    assert (
        await _request_store(collection).find_pending_hitl_request_for_agent_message(
            conversation_id="room-1",
            display_message_id="display-1",
            continuation_message_id="cont-1",
            agent_id="agent-1",
            a2a_task_id="task-1",
            a2a_context_id="ctx-1",
        )
        is None
    )

    record = _one(caplog, "pending_hitl_request_conflict_detected")
    assert record.levelno == logging.ERROR
    assert record.lookup == "pending_lookup"
    assert record.display_request_id == "req-display"
    assert record.continuation_request_id == "req-cont"


@pytest.mark.asyncio
async def test_missing_duplicate_readback_emits_readback_failed(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _duplicate_collection(
        [None, None], DuplicateKeyError("E11000 duplicate key error")
    )

    assert (
        await _request_store(collection).create_or_reuse_pending_hitl_request(
            dict(_PENDING_DOC)
        )
        is None
    )

    record = _one(caplog, "pending_hitl_duplicate_readback_failed")
    assert record.levelno == logging.ERROR
    assert record.conversation_id == "room-1"
    assert record.hitl_request_id == "req-new"
    assert record.display_message_id == "display-1"
    assert record.continuation_message_id == "cont-1"
    _assert_no_secret(caplog)


def _index_collection(*, fail_names=(), conflict=False, recreate_fails=False):
    collection = MagicMock()
    attempts: dict[str | None, int] = {}

    async def create_index(keys, **kwargs):
        name = kwargs.get("name")
        attempts[name] = attempts.get(name, 0) + 1
        if name not in fail_names:
            return name
        if conflict and (attempts[name] == 1 or recreate_fails):
            raise OperationFailure(SECRET, code=85)
        if not conflict:
            raise _Failed(SECRET)
        return name

    collection.create_index = AsyncMock(side_effect=create_index)
    collection.drop_index = AsyncMock()
    return collection


@pytest.mark.asyncio
async def test_noncritical_index_failure_emits_index_creation_failed(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _index_collection(fail_names=(None,))

    await _request_store(collection).ensure_indexes()

    records = _events(caplog, "index_creation_failed")
    assert len(records) == 5
    for record in records:
        _assert_failure(record, logging.WARNING)
        assert record.collection_name == "hitl_requests"
        assert record.critical is False
    assert records[0].index_name == "request_id_1"
    assert records[1].index_name == "room_id_1_status_1"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_critical_index_failure_emits_index_creation_failed_and_raises(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _index_collection(fail_names=("uq_pending_hitl_display_message",))

    with pytest.raises(_Failed):
        await _request_store(collection).ensure_indexes()

    record = _one(caplog, "index_creation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.index_name == "uq_pending_hitl_display_message"
    assert record.critical is True
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_index_conflict_recreated_emits_conflict_detected_only(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _index_collection(
        fail_names=("uq_pending_hitl_display_message",), conflict=True
    )

    await _request_store(collection).ensure_indexes()

    record = _one(caplog, "index_conflict_detected")
    assert record.levelno == logging.WARNING
    assert record.collection_name == "hitl_requests"
    assert record.index_name == "uq_pending_hitl_display_message"
    assert record.critical is True
    assert _events(caplog, "index_recreation_failed") == []
    assert _events(caplog, "index_creation_failed") == []
    collection.drop_index.assert_awaited_once_with("uq_pending_hitl_display_message")


@pytest.mark.asyncio
async def test_index_conflict_recreate_failure_emits_recreation_failed(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _index_collection(
        fail_names=("uq_hitl_interaction_question",),
        conflict=True,
        recreate_fails=True,
    )

    await _request_store(collection).ensure_indexes()

    recreated = _one(caplog, "index_recreation_failed")
    assert recreated.levelno == logging.WARNING
    assert recreated.error_type == "OperationFailure"
    assert recreated.index_name == "uq_hitl_interaction_question"
    assert recreated.critical is False
    created = _one(caplog, "index_creation_failed")
    assert created.levelno == logging.WARNING
    assert created.index_name == "uq_hitl_interaction_question"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_critical_index_recreate_failure_emits_error_and_raises(caplog):
    caplog.set_level(logging.DEBUG)
    collection = _index_collection(
        fail_names=("uq_pending_hitl_display_message",),
        conflict=True,
        recreate_fails=True,
    )

    with pytest.raises(OperationFailure):
        await _request_store(collection).ensure_indexes()

    recreated = _one(caplog, "index_recreation_failed")
    assert recreated.levelno == logging.ERROR
    assert recreated.error_type == "OperationFailure"
    assert recreated.critical is True
    created = _one(caplog, "index_creation_failed")
    assert created.levelno == logging.ERROR
    assert created.critical is True
    _assert_no_secret(caplog)


# ── lifecycle store ──


@pytest.mark.asyncio
async def test_lifecycle_read_failure_emits_hitl_store_operation_failed(caplog):
    caplog.set_level(logging.DEBUG)
    store = HITLLifecycleStore(
        interactions=_failing_collection(),
        resume_commands=MagicMock(),
        hitl_requests=MagicMock(),
    )

    assert await store.get_interaction("interaction-1") is None

    record = _one(caplog, "hitl_store_operation_failed")
    _assert_failure(record, logging.ERROR)
    assert record.operation == "get_interaction"
    assert record.interaction_id == "interaction-1"
    _assert_no_secret(caplog)


# ── reconciler ──

_INTERACTION = {
    "interaction_id": "interaction-1",
    "room_id": "room-1",
    "orchestration_run_id": "run-1",
    "status": "open",
}


async def _yield(*items):
    for item in items:
        yield item


async def _raising_iter():
    raise _Failed(SECRET)
    yield  # pragma: no cover


def _reconciler(lifecycle=None, service=None, application=None):
    return HITLLifecycleReconciler(
        lifecycle=lifecycle or MagicMock(),
        service=service or MagicMock(),
        application=application or MagicMock(),
    )


def _empty_lifecycle() -> MagicMock:
    lifecycle = MagicMock()
    for name in (
        "iter_materializing_interactions",
        "iter_due_interactions",
        "iter_active_interactions",
        "iter_stale_applications",
        "iter_due_resume_commands",
        "iter_unreconciled_terminal_interactions",
        "iter_unreconciled_terminal_requests",
    ):
        getattr(lifecycle, name).side_effect = lambda *a, **k: _yield()
    return lifecycle


def _assert_interaction_ids(record: logging.LogRecord) -> None:
    assert record.interaction_id == "interaction-1"
    assert record.conversation_id == "room-1"
    assert record.run_id == "run-1"


@pytest.mark.asyncio
async def test_reconciliation_pass_failure_emits_pass_failed(caplog):
    caplog.set_level(logging.DEBUG)
    lifecycle = _empty_lifecycle()
    lifecycle.iter_due_interactions.side_effect = lambda *a, **k: _raising_iter()

    counts = await _reconciler(lifecycle=lifecycle).reconcile_lifecycle()

    assert counts["errors"] == 1
    record = _one(caplog, "hitl_reconciliation_pass_failed")
    _assert_failure(record, logging.ERROR)
    assert record.reconciliation_pass == "expired"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "level", "iterator", "configure"),
    [
        (
            "hitl_materialization_resume_deferred",
            logging.WARNING,
            "iter_materializing_interactions",
            lambda s, a: setattr(s, "resume_materializing_interaction", _raise),
        ),
        (
            "hitl_interaction_expiry_deferred",
            logging.ERROR,
            "iter_due_interactions",
            lambda s, a: setattr(a, "expire_interaction", _raise),
        ),
        (
            "hitl_answer_refs_repair_deferred",
            logging.WARNING,
            "iter_active_interactions",
            lambda s, a: setattr(a, "repair_persisted_answer_refs", _raise),
        ),
        (
            "hitl_application_resume_deferred",
            logging.WARNING,
            "iter_stale_applications",
            lambda s, a: setattr(a, "apply_interaction", _raise),
        ),
        (
            "hitl_terminal_interaction_reconcile_deferred",
            logging.WARNING,
            "iter_unreconciled_terminal_interactions",
            lambda s, a: setattr(s, "reconcile_terminal_interaction", _raise),
        ),
    ],
)
async def test_reconciler_interaction_failure_emits_event(
    caplog, event, level, iterator, configure
):
    caplog.set_level(logging.DEBUG)
    lifecycle = _empty_lifecycle()
    getattr(lifecycle, iterator).side_effect = lambda *a, **k: _yield(
        dict(_INTERACTION)
    )
    service, application = MagicMock(), MagicMock()
    configure(service, application)
    if iterator == "iter_active_interactions":
        # The open-projection pass shares this iterator; keep it quiet.
        service.recover_open_interaction_projection = AsyncMock(return_value=0)

    counts = await _reconciler(lifecycle, service, application).reconcile_lifecycle()

    assert counts["errors"] == 0
    record = _one(caplog, event)
    _assert_failure(record, level)
    _assert_interaction_ids(record)
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_open_projection_recovery_failure_emits_deferred(caplog):
    caplog.set_level(logging.DEBUG)
    lifecycle = _empty_lifecycle()
    lifecycle.iter_active_interactions.side_effect = lambda *a, **k: _yield(
        dict(_INTERACTION)
    )
    service, application = MagicMock(), MagicMock()
    service.recover_open_interaction_projection = _raise
    application.repair_persisted_answer_refs = AsyncMock(
        return_value=dict(_INTERACTION)
    )

    await _reconciler(lifecycle, service, application).reconcile_lifecycle()

    record = _one(caplog, "hitl_open_projection_recovery_deferred")
    _assert_failure(record, logging.WARNING)
    _assert_interaction_ids(record)
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_resume_command_failure_emits_reconcile_deferred(caplog):
    caplog.set_level(logging.DEBUG)
    lifecycle = _empty_lifecycle()
    lifecycle.iter_due_resume_commands.side_effect = lambda *a, **k: _yield(
        {
            "command_id": "command-1",
            "interaction_id": "interaction-1",
            "orchestration_run_id": "run-1",
        }
    )
    lifecycle.get_interaction_strict = _raise

    await _reconciler(lifecycle=lifecycle).reconcile_lifecycle()

    record = _one(caplog, "hitl_resume_command_reconcile_deferred")
    _assert_failure(record, logging.WARNING)
    assert record.command_id == "command-1"
    assert record.interaction_id == "interaction-1"
    assert record.run_id == "run-1"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_terminal_request_failure_emits_reconcile_deferred(caplog):
    caplog.set_level(logging.DEBUG)
    lifecycle = _empty_lifecycle()
    row = {**_rows()[0], "status": "canceled"}
    lifecycle.iter_unreconciled_terminal_requests.side_effect = lambda *a, **k: _yield(
        row
    )
    service = MagicMock()
    service._reconcile_terminal_request = _raise

    await _reconciler(lifecycle=lifecycle, service=service).reconcile_lifecycle()

    record = _one(caplog, "hitl_terminal_request_reconcile_deferred")
    _assert_failure(record, logging.WARNING)
    assert record.hitl_request_id == "request-1"
    assert record.conversation_id == "room-1"
    assert record.interaction_id == "interaction-1"
    _assert_no_secret(caplog)


# ── application ──


@pytest.mark.asyncio
async def test_unexpected_apply_failure_emits_hitl_answer_application_failed(
    caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    aggregate = {**_aggregate(), "status": "answers_recorded"}
    rows = [
        {**row, "status": "answer_recorded", "user_input": SECRET} for row in _rows()
    ]
    rows[0]["client_request_id"] = "client-1"
    lifecycle = MagicMock()
    lifecycle.claim_interaction_application = AsyncMock(return_value=aggregate)
    lifecycle.mark_interaction_application_state = AsyncMock(return_value=aggregate)
    application = HITLApplicationCoordinator(lifecycle=lifecycle)
    application.repair_persisted_answer_refs = AsyncMock(return_value=aggregate)
    application._request_rows = AsyncMock(return_value=rows)
    monkeypatch.setattr(
        "approval.application.validate_route_classifications", _raise_sync
    )
    service = MagicMock()
    service.canonical_run_allows_resume = AsyncMock(return_value=True)

    with pytest.raises(HITLRoutingFailedError):
        await application.apply_interaction(service, aggregate)

    record = _one(caplog, "hitl_answer_application_failed")
    _assert_failure(record, logging.ERROR)
    assert record.interaction_id == "interaction-1"
    assert record.conversation_id == "room-1"
    assert record.run_id == "run-1"
    assert record.client_request_id == "client-1"
    _assert_no_secret(caplog)


def _raise_sync(*_args, **_kwargs):
    raise _Failed(SECRET)


@pytest.mark.asyncio
async def test_finalize_emission_failure_emits_input_received_emission_failed(
    caplog,
):
    caplog.set_level(logging.DEBUG)
    aggregate = {**_aggregate(), "status": "applied", "terminal_reconciled": True}
    rows = [{**row, "status": "responded", "user_input": SECRET} for row in _rows()[:1]]
    application = HITLApplicationCoordinator(lifecycle=MagicMock())
    application._request_rows = AsyncMock(return_value=rows)
    service = MagicMock()
    service._emit_hitl_event = _raise
    service.persistence.update_hitl_request = AsyncMock(return_value=True)

    assert await application.finalize_applied(service, aggregate) is False

    record = _one(caplog, "hitl_input_received_emission_failed")
    _assert_failure(record, logging.WARNING)
    assert record.hitl_request_id == "request-1"
    assert record.interaction_id == "interaction-1"
    assert record.conversation_id == "room-1"
    _assert_no_secret(caplog)


# ── service ──


def _service(persistence=None, **kwargs) -> HITLService:
    service = HITLService(**kwargs)
    service._persistence = persistence or MagicMock()
    return service


def _member_kwargs(**overrides):
    snapshot = HITLRouteSnapshot(
        route=HITLApplicationRoute.SUPERVISOR_RUN,
        orchestration_run_id="run-1",
    )
    kwargs = dict(
        conversation_id="room-1",
        user_message_id="msg-1",
        application_route=HITLApplicationRoute.SUPERVISOR_RUN,
        public_source=HITLPublicSource.SUPERVISOR,
        evidence_origin=HITLEvidenceOrigin.SUPERVISOR,
        route_snapshot=snapshot,
        interaction_id="interaction-1",
        question_index=0,
        question_count=1,
        prompt=SECRET,
        orchestration_run_id="run-1",
    )
    kwargs.update(overrides)
    return kwargs


def _member_persistence() -> MagicMock:
    persistence = MagicMock()
    persistence.get_room_user_message_by_message_id = AsyncMock(return_value=None)
    persistence.resolve_client_request_id_for_message_id = AsyncMock(
        return_value="client-1"
    )
    persistence.get_hitl_request = AsyncMock(return_value=None)
    persistence.count_hitl_requests_for_message = AsyncMock(return_value=0)
    persistence.create_hitl_request = AsyncMock(return_value=False)
    persistence.create_or_reuse_pending_hitl_request = AsyncMock(return_value=None)
    persistence.cas_update_hitl_request = AsyncMock(return_value=False)
    return persistence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {
                "public_source": HITLPublicSource.AGENT,
                "agent_id": "agent-1",
                "a2a_task_id": None,
                "a2a_context_id": None,
                "prompt": "Which market should I use?",
            },
            "invalid_a2a_continuation",
        ),
        (
            {
                "public_source": HITLPublicSource.AGENT,
                "agent_id": "agent-1",
                "a2a_task_id": "task-1",
                "a2a_context_id": "ctx-1",
                "prompt": "Which market should I use?",
            },
            "display_message_id_missing",
        ),
    ],
)
async def test_invalid_agent_member_emits_hitl_request_rejected(
    caplog, overrides, reason
):
    caplog.set_level(logging.DEBUG)
    service = _service(_member_persistence())

    assert (
        await service._request_interaction_member(**_member_kwargs(**overrides)) is None
    )

    record = _one(caplog, "hitl_request_rejected")
    assert record.run_id == "run-1"
    assert record.levelno == logging.ERROR
    assert record.reason == reason
    assert record.conversation_id == "room-1"
    assert record.interaction_id == "interaction-1"
    assert record.agent_id == "agent-1"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_mismatched_supervisor_reuse_emits_hitl_request_rejected(caplog):
    caplog.set_level(logging.DEBUG)
    persistence = _member_persistence()
    persistence.get_hitl_request = AsyncMock(
        return_value={"request_id": "req-1", "interaction_id": "other"}
    )
    service = _service(persistence)

    assert (
        await service._request_interaction_member(**_member_kwargs(request_id="req-1"))
        is None
    )

    record = _one(caplog, "hitl_request_rejected")
    assert record.run_id == "run-1"
    assert record.levelno == logging.ERROR
    assert record.reason == "request_identity_mismatched"
    assert record.hitl_request_id == "req-1"
    assert record.interaction_id == "interaction-1"


@pytest.mark.asyncio
async def test_round_limit_emits_hitl_round_limit_reached(caplog):
    caplog.set_level(logging.DEBUG)
    persistence = _member_persistence()
    persistence.count_hitl_requests_for_message = AsyncMock(
        return_value=MAX_HITL_ROUNDS
    )
    service = _service(persistence)

    assert (
        await service._request_interaction_member(
            **_member_kwargs(continuation_message_id="cont-1")
        )
        is None
    )

    record = _one(caplog, "hitl_round_limit_reached")
    assert record.run_id == "run-1"
    assert record.levelno == logging.WARNING
    assert record.conversation_id == "room-1"
    assert record.interaction_id == "interaction-1"
    assert record.continuation_message_id == "cont-1"
    assert record.round_count == MAX_HITL_ROUNDS
    assert record.max_round_count == MAX_HITL_ROUNDS


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["supervisor", "agent"])
async def test_persist_failure_emits_hitl_request_persist_failed(caplog, source):
    caplog.set_level(logging.DEBUG)
    overrides = {}
    if source == "agent":
        overrides = {
            "public_source": HITLPublicSource.AGENT,
            "agent_id": "agent-1",
            "a2a_task_id": "task-1",
            "a2a_context_id": "ctx-1",
            "display_message_id": "display-1",
            "prompt": "Which market should I use?",
        }
    service = _service(_member_persistence())

    assert (
        await service._request_interaction_member(
            **_member_kwargs(request_id="req-1", **overrides)
        )
        is None
    )

    record = _one(caplog, "hitl_request_persist_failed")
    assert record.run_id == "run-1"
    assert record.levelno == logging.ERROR
    assert record.conversation_id == "room-1"
    assert record.interaction_id == "interaction-1"
    assert record.hitl_request_id == "req-1"
    assert record.public_source == source
    _assert_no_secret(caplog)


def _agent_member_kwargs():
    return _member_kwargs(
        public_source=HITLPublicSource.AGENT,
        evidence_origin=HITLEvidenceOrigin.AGENT,
        agent_id="agent-1",
        a2a_task_id="task-1",
        a2a_context_id="ctx-1",
        display_message_id="display-1",
        prompt="Which market should I use?",
    )


def _existing_agent_doc(**overrides):
    return {
        "request_id": "req-existing",
        "interaction_id": "interaction-1",
        "question_index": 0,
        "question_count": 1,
        "room_id": "room-1",
        "user_message_id": "msg-1",
        "orchestration_run_id": "run-1",
        "application_route": "supervisor_run",
        "public_source": "agent",
        "evidence_origin": "agent",
        "agent_id": "agent-1",
        "display_message_id": "display-1",
        "status": "pending",
        "prompt": "Old question?",
        **overrides,
    }


async def _reuse_agent_member(existing, *, repaired: bool = False):
    persistence = _member_persistence()
    persistence.create_or_reuse_pending_hitl_request = AsyncMock(
        return_value=(existing, False)
    )
    persistence.cas_update_hitl_request = AsyncMock(return_value=repaired)
    return await _service(persistence)._request_interaction_member(
        **_agent_member_kwargs()
    )


@pytest.mark.asyncio
async def test_mismatched_reused_agent_request_emits_hitl_request_rejected(caplog):
    caplog.set_level(logging.DEBUG)

    assert await _reuse_agent_member(_existing_agent_doc(question_index=1)) is None

    record = _one(caplog, "hitl_request_rejected")
    assert record.levelno == logging.ERROR
    assert record.reason == "reused_request_mismatched"
    assert record.hitl_request_id == "req-existing"
    assert record.interaction_id == "interaction-1"
    assert record.run_id == "run-1"
    assert _events(caplog, "reused_hitl_request_repair_failed") == []


@pytest.mark.asyncio
async def test_malformed_reused_agent_request_emits_hitl_request_rejected(
    caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "approval.service._is_actionable_agent_hitl_document", lambda *_: False
    )

    assert await _reuse_agent_member(_existing_agent_doc(), repaired=True) is None

    record = _one(caplog, "hitl_request_rejected")
    assert record.levelno == logging.ERROR
    assert record.reason == "persisted_request_malformed"
    assert record.hitl_request_id == "req-existing"
    assert record.conversation_id == "room-1"
    assert record.run_id == "run-1"


@pytest.mark.asyncio
async def test_reused_agent_repair_failure_emits_repair_failed(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "approval.service._same_agent_hitl_logical_request", lambda *_: True
    )

    assert await _reuse_agent_member(_existing_agent_doc()) is None

    record = _one(caplog, "reused_hitl_request_repair_failed")
    assert record.run_id == "run-1"
    assert record.levelno == logging.ERROR
    assert record.hitl_request_id == "req-existing"
    assert record.conversation_id == "room-1"
    assert record.interaction_id == "interaction-1"


@pytest.mark.asyncio
async def test_display_projection_failure_emits_event(caplog):
    caplog.set_level(logging.DEBUG)
    persistence = MagicMock()
    persistence.persist_hitl_user_answer = _raise
    service = _service(persistence)

    assert (
        await service._project_completed_hitl_display(
            display_message_id="display-1",
            user_input=SECRET,
            request_id="req-1",
            conversation_id="room-1",
        )
        is False
    )

    record = _one(caplog, "hitl_display_projection_failed")
    _assert_failure(record, logging.WARNING)
    assert record.display_message_id == "display-1"
    assert record.hitl_request_id == "req-1"
    assert record.conversation_id == "room-1"
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_incomplete_display_projection_emits_event(caplog):
    caplog.set_level(logging.DEBUG)
    persistence = MagicMock()
    persistence.persist_hitl_user_answer = AsyncMock(return_value=True)
    persistence.update_agent_message_task_state = AsyncMock(return_value=False)
    service = _service(persistence)

    assert (
        await service._project_completed_hitl_display(
            display_message_id="display-1",
            user_input=SECRET,
            request_id="req-1",
            conversation_id="room-1",
        )
        is False
    )

    record = _one(caplog, "hitl_display_projection_failed")
    assert record.levelno == logging.WARNING
    assert record.answer_projected is True
    assert record.state_projected is False
    assert not hasattr(record, "error_type")
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_client_request_id_lookup_failures_emit_resolution_failed(caplog):
    caplog.set_level(logging.DEBUG)
    persistence = MagicMock()
    persistence.get_room_user_message_by_message_id = _raise
    persistence.resolve_client_request_id_for_message_id = _raise
    service = _service(persistence)

    assert (
        await service._resolve_hitl_client_request_id(
            user_message_id="msg-1", message_id="display-1"
        )
        is None
    )

    records = _events(caplog, "client_request_id_resolution_failed")
    assert [record.message_id for record in records] == ["msg-1", "display-1"]
    assert [record.lookup for record in records] == ["user_message", "fallback"]
    for record in records:
        _assert_failure(record, logging.WARNING)
    _assert_no_secret(caplog)


@pytest.mark.asyncio
async def test_run_state_read_failure_emits_event(caplog):
    caplog.set_level(logging.DEBUG)
    service = _service(canonical_run_state_reader=_raise)

    assert await service._canonical_run_state("run-1") is None

    record = _one(caplog, "hitl_run_state_read_failed")
    _assert_failure(record, logging.WARNING)
    assert record.run_id == "run-1"
    _assert_no_secret(caplog)
