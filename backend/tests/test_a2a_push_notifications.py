"""C12: per-call A2A push registration and ledger-authenticated push ingress."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.proto_utils import to_stream_response
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from a2a_adapter.orchestrator_direct_client import (
    OrchestratorDirectA2AClient,
    endpoint_scope_digest,
)
from a2a_adapter.webhook_payloads import parse_push_notification_payload
from common.config.loader import Settings, validate_backend
from execution.orchestrator.a2a_runtime.dispatch import DirectA2ADispatchAdapter
from execution.orchestrator.a2a_runtime.errors import (
    AgentCardContractError,
    RecoverableAdapterError,
    RecoverableTransportError,
)
from execution.orchestrator.a2a_runtime.hitl import InMemoryHITLApplicationPort
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryRoomEpochStore,
)
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    A2AObservationProcessor,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import (
    ownership_alias_keys,
    transition_call,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    A2AOwnershipAlias,
    A2APushNotificationConfig,
    A2ARuntimePolicy,
    AgentCallLedgerRecord,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.push import (
    A2APushNotificationIngress,
    A2APushRegistrar,
    LedgerPushTokenAuthenticator,
    push_token_digest,
)
from execution.orchestrator.a2a_runtime.recovery import A2ACallRecoveryService
from execution.ports import A2APushReceipt
from tests._a2a_task_builders import build_completed_text_task
from tests._orchestrator_a2a_helpers import invocation, ledger_record
from tests._orchestrator_helpers import NOW, NeverCancelled
from tests.test_orchestrator_a2a_ingress import Artifacts, Checkpoints, Outcomes, Sink
from tests.test_orchestrator_a2a_runtime import Dispatch, setup

ENDPOINT = "https://agent.example/a2a"
SCOPE = endpoint_scope_digest(ENDPOINT)
TOKEN = "push-token-" + "x" * 40


def _card(*, push: bool) -> dict:
    return {
        "name": "Agent",
        "version": "1.0.0",
        "capabilities": {"pushNotifications": push},
        "supportedInterfaces": [
            {"url": ENDPOINT, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
    }


def _push_body(event) -> bytes:
    """Serialize exactly as the SDK BasePushNotificationSender does."""
    return json.dumps(MessageToDict(to_stream_response(event))).encode()


def _completed_task(*, task_id: str = "task-1", context_id: str = "ctx-1") -> Task:
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(
            state=TaskState.TASK_STATE_COMPLETED,
            message=Message(
                message_id="m-1", role=Role.ROLE_AGENT, parts=[Part(text="done")]
            ),
        ),
        artifacts=[Artifact(artifact_id="a-1", parts=[Part(text="result")])],
    )


def _status_update(state, *, task_id: str = "task-1") -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id=task_id, context_id="ctx-1", status=TaskStatus(state=state)
    )


def _working_call(*, digest: str | None = push_token_digest(TOKEN)):
    base = ledger_record()
    aliases = [
        A2AOwnershipAlias(kind="task", value="task-1", binding_scope=SCOPE),
        A2AOwnershipAlias(kind="context", value="ctx-1", binding_scope=SCOPE),
    ]
    record = base.model_copy(update={"endpoint_scope_digest": SCOPE})
    record = transition_call(record, to_state="ready_to_dispatch", updated_at=NOW)
    record = transition_call(
        record,
        to_state="dispatching",
        updated_at=NOW,
        push_token_digest=digest,
    )
    record = transition_call(
        record,
        to_state="working",
        updated_at=NOW,
        ownership_aliases=aliases,
        ownership_alias_keys=ownership_alias_keys(aliases),
        a2a_task_id="task-1",
        a2a_context_id="ctx-1",
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return AgentCallLedgerRecord.model_validate(record.model_dump())


def _direct_client(*, fetch_remote_task=None, call_resolver=None):
    return OrchestratorDirectA2AClient(
        send_message=AsyncMock(),
        stream_message=AsyncMock(),
        cancel_remote_task=AsyncMock(),
        fetch_remote_task=fetch_remote_task or AsyncMock(return_value=None),
        fetch_agent_card=AsyncMock(return_value=_card(push=True)),
        receipt_factory=A2ADispatchReceipt,
        observation_factory=NormalizedA2AObservation,
        recoverable_transport_error_factory=RecoverableTransportError,
        agent_card_contract_error_factory=AgentCardContractError,
        call_resolver=call_resolver,
    )


async def _push_setup(*, call: AgentCallLedgerRecord | None = None, policy=None):
    ledger = InMemoryAgentCallLedgerStore()
    call = call or _working_call()
    await ledger.insert(call)
    inbox = InMemoryObservationInboxStore()
    conflicts = InMemoryObservationConflictStore()
    observations = A2AObservationIngress(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    sink = Sink()
    processor = A2AObservationProcessor(
        inbox=inbox,
        conflicts=conflicts,
        ledger=ledger,
        room_epochs=epochs,
        artifacts=Artifacts(),
        hitl=InMemoryHITLApplicationPort(),
        sink=sink,
        checkpoint_reader=Checkpoints(),
        outcome_reader=Outcomes(),
    )
    client = _direct_client()
    receiver = A2APushNotificationIngress(
        ledger=ledger,
        authenticator=LedgerPushTokenAuthenticator(ledger),
        observations=observations,
        processor=processor,
        read_push=client.read_push_notification,
        policy=policy,
    )
    return receiver, ledger, inbox, conflicts, sink, observations, epochs, call


# -- configuration ----------------------------------------------------------


def test_push_callback_base_url_defaults_to_disabled():
    assert Settings().a2a_push_callback_base_url == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hybro.example.com/", "https://hybro.example.com"),
        ("http://host.docker.internal:8000", "http://host.docker.internal:8000"),
        ("https://hybro.example.com/backend", "https://hybro.example.com/backend"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.2:8000/", "http://127.0.0.2:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://10.1.2.3", "http://10.1.2.3"),
        ("http://172.20.0.5:8000", "http://172.20.0.5:8000"),
        ("http://192.168.1.10", "http://192.168.1.10"),
    ],
)
def test_push_callback_base_url_accepts_https_and_private_http(raw, expected):
    settings = validate_backend({"a2a_push_callback_base_url": raw})
    assert settings.a2a_push_callback_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://hybro.example.com",
        "http://hybro.example.com",
        "http://8.8.8.8",
        "http://172.32.0.1",
        "http://169.254.169.254",
        "http://[2001:db8::1]",
        "https://user:pass@hybro.example.com",
        "https://hybro.example.com/?token=x",
        "https://hybro.example.com/#frag",
        "hybro.example.com",
        "https://hybro example.com",
    ],
)
def test_push_callback_base_url_rejects_unsafe_values(raw):
    with pytest.raises((ValueError, ValidationError)):
        validate_backend({"a2a_push_callback_base_url": raw})


# -- registration -----------------------------------------------------------


def test_registrar_issues_fresh_tokens_and_keeps_only_digests():
    registrar = A2APushRegistrar(
        callback_base_url="https://hybro.example.com", api_prefix="/api/v1"
    )

    first = registrar.issue("call-abc")
    second = registrar.issue("call-abc")

    assert first.config.url == "https://hybro.example.com/api/v1/a2a/push/call-abc"
    token = first.config.token.get_secret_value()
    assert len(token) >= 40
    assert first.token_digest == sha256(token.encode()).hexdigest()
    assert token != second.config.token.get_secret_value()
    assert token not in repr(first) and token not in str(first.config)
    assert token not in first.config.model_dump_json()


async def test_dispatch_registers_push_and_persists_only_the_token_digest():
    dispatch = Dispatch()
    runtime, ledger, _, _, _ = await setup(dispatch=dispatch)
    runtime.push_registrar = A2APushRegistrar(
        callback_base_url="https://hybro.example.com", api_prefix="/api/v1"
    )
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)

    await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())

    [command] = dispatch.commands
    push = command.push_notification
    assert push is not None
    record = await ledger.load("run-1", "call-1")
    assert push.url.endswith(f"/api/v1/a2a/push/{record.call_record_id}")
    token = push.token.get_secret_value()
    assert record.push_token_digest == push_token_digest(token)
    assert token not in record.model_dump_json()
    assert token not in json.dumps(record.model_dump(mode="json"))


async def test_dispatch_without_callback_base_url_registers_no_push():
    dispatch = Dispatch()
    runtime, ledger, _, _, _ = await setup(dispatch=dispatch)
    tool_invocation = invocation()
    accepted = await runtime.accept(tool_invocation)

    await runtime.execute(tool_invocation, accepted, signal=NeverCancelled())

    assert dispatch.commands[0].push_notification is None
    assert (await ledger.load("run-1", "call-1")).push_token_digest is None


class _RecordingSdk:
    def __init__(self, *, push: bool):
        self.card = _card(push=push)
        self.calls: list[dict] = []

    async def fetch_agent_card(self, url, **kwargs):
        return self.card

    async def send_message(self, card, message, **kwargs):
        self.calls.append(kwargs)
        task = build_completed_text_task(task_id="task-1", text="ok")
        return {"kind": "task", "result": task.model_dump(mode="json", by_alias=True)}

    def stream_message(self, card, message, **kwargs):
        self.calls.append(kwargs)

        async def frames():
            if False:  # pragma: no cover - structural async generator
                yield None

        return frames()


def _sdk_client(sdk: _RecordingSdk) -> OrchestratorDirectA2AClient:
    return OrchestratorDirectA2AClient(
        send_message=sdk.send_message,
        stream_message=sdk.stream_message,
        cancel_remote_task=AsyncMock(),
        fetch_remote_task=AsyncMock(),
        fetch_agent_card=sdk.fetch_agent_card,
        receipt_factory=A2ADispatchReceipt,
        observation_factory=NormalizedA2AObservation,
        recoverable_transport_error_factory=RecoverableTransportError,
        agent_card_contract_error_factory=AgentCardContractError,
    )


def _command(*, push: bool, mode: str = "sync"):
    from execution.orchestrator.a2a_runtime.models import A2ADispatchCommand

    return A2ADispatchCommand(
        command_id="command-1",
        call_record_id="call-1",
        invocation_id="inv-1",
        message_id="message-1",
        binding_id="binding-1",
        agent_id="agent-1",
        endpoint_scope=ENDPOINT,
        transport_kind="direct",
        direct_mode=mode,
        task="do work",
        materialized_resources=[],
        conversation_id="room-1",
        room_epoch=1,
        deadline_at=NOW + timedelta(seconds=30),
        push_notification=(
            A2APushNotificationConfig(
                url="https://hybro.example.com/api/v1/a2a/push/call-1",
                token=SecretStr(TOKEN),
            )
            if push
            else None
        ),
    )


@pytest.mark.parametrize(
    ("card_push", "command_push", "registered"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
@pytest.mark.parametrize("method", ["send", "start_poll", "open_stream"])
async def test_push_config_is_sent_only_when_card_and_configuration_allow(
    card_push, command_push, registered, method
):
    sdk = _RecordingSdk(push=card_push)
    client = _sdk_client(sdk)

    await getattr(client, method)(_command(push=command_push))

    config = sdk.calls[0]["push_notification_config"]
    if registered:
        assert config == {
            "url": "https://hybro.example.com/api/v1/a2a/push/call-1",
            "token": TOKEN,
        }
    else:
        assert config is None


def test_push_payload_parser_accepts_sdk_frames_and_rejects_messages():
    frame, task = parse_push_notification_payload(
        json.loads(_push_body(_completed_task()))
    )
    assert (frame, task.id, task.context_id) == ("task", "task-1", "ctx-1")
    frame, task = parse_push_notification_payload(
        json.loads(_push_body(_status_update(TaskState.TASK_STATE_WORKING)))
    )
    assert (frame, task.id) == ("status_update", "task-1")
    artifact = TaskArtifactUpdateEvent(
        task_id="task-1", artifact=Artifact(artifact_id="a", parts=[Part(text="x")])
    )
    frame, _ = parse_push_notification_payload(json.loads(_push_body(artifact)))
    assert frame == "artifact_update"
    with pytest.raises(ValueError):
        parse_push_notification_payload({"message": {"messageId": "m", "parts": []}})
    with pytest.raises(ValueError):
        parse_push_notification_payload({"statusUpdate": {"status": {}}})


# -- ingress ----------------------------------------------------------------


async def test_valid_push_records_terminal_task_and_feeds_the_sink():
    receiver, ledger, inbox, _, sink, _, _, call = await _push_setup()

    receipt = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_completed_task())
    )

    assert receipt.outcome == "accepted"
    assert receipt.observation_id is not None
    row = await inbox.load(receipt.observation_id)
    assert row.source_kind == "webhook"
    assert row.call_record_id == call.call_record_id
    assert row.observation.status == "completed"
    # Recorded before processing; the ledger is only changed by the processor.
    assert (await ledger.load_by_record_id(call.call_record_id)).state == "working"

    await receiver.process(receipt.observation_id)

    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "completed"
    assert [run_id for run_id, _ in sink.values] == ["run-1"]
    assert sink.values[0][1].outcome == persisted.terminal_result


@pytest.mark.parametrize("token", [None, "", "wrong-token", TOKEN + "x", TOKEN.upper()])
async def test_wrong_or_missing_token_is_unauthorized(token):
    receiver, _, inbox, *_ = await _push_setup()
    call = _working_call()

    receipt = await receiver.receive(
        call.call_record_id, token=token, body=_push_body(_completed_task())
    )

    assert receipt == A2APushReceipt("unauthorized")
    assert await inbox.list_due(due_at=datetime.now(UTC), limit=10) == []


async def test_unknown_call_and_legacy_call_without_digest_are_unauthorized():
    receiver, *_ = await _push_setup(call=_working_call(digest=None))
    legacy = _working_call(digest=None)

    for call_record_id in ("call-unknown", legacy.call_record_id, ""):
        receipt = await receiver.receive(
            call_record_id, token=TOKEN, body=_push_body(_completed_task())
        )
        assert receipt == A2APushReceipt("unauthorized")


async def test_terminal_call_is_unauthorized_like_an_unknown_one():
    receiver, ledger, _, _, _, _, _, call = await _push_setup()
    first = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_completed_task())
    )
    await receiver.process(first.observation_id)
    assert (await ledger.load_by_record_id(call.call_record_id)).state == "completed"

    again = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_completed_task())
    )
    unknown = await receiver.receive(
        "call-unknown", token=TOKEN, body=_push_body(_completed_task())
    )

    assert again == unknown == A2APushReceipt("unauthorized")


@pytest.mark.parametrize(
    "event",
    [
        _completed_task(task_id="task-other"),
        _completed_task(context_id="ctx-other"),
        _status_update(TaskState.TASK_STATE_COMPLETED, task_id="task-other"),
    ],
)
async def test_mismatched_task_or_context_is_rejected_before_recording(event):
    receiver, ledger, inbox, _, _, _, _, call = await _push_setup()

    receipt = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(event)
    )

    assert receipt == A2APushReceipt("invalid")
    assert (await ledger.load_by_record_id(call.call_record_id)) == call
    assert await inbox.list_due(due_at=datetime.now(UTC), limit=10) == []


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"message": {}}', b"{}"])
async def test_malformed_body_is_invalid(body):
    receiver, *_ = await _push_setup()
    call = _working_call()

    receipt = await receiver.receive(call.call_record_id, token=TOKEN, body=body)

    assert receipt == A2APushReceipt("invalid")


async def test_oversized_body_is_rejected_before_authentication():
    receiver, ledger, *_ = await _push_setup(
        policy=A2ARuntimePolicy(max_callback_body_bytes=64)
    )
    ledger.load_by_record_id = AsyncMock(side_effect=AssertionError("no lookup"))

    receipt = await receiver.receive("call-1", token=TOKEN, body=b"x" * 65)

    assert receipt == A2APushReceipt("too_large")


async def test_ingress_authenticate_checks_path_and_token_only():
    receiver, _, _, _, _, _, _, call = await _push_setup()

    assert await receiver.authenticate(call.call_record_id, token=TOKEN) == (
        A2APushReceipt("accepted")
    )
    for call_record_id, token in (
        (call.call_record_id, "wrong"),
        (call.call_record_id, None),
        ("call-unknown", TOKEN),
    ):
        assert await receiver.authenticate(call_record_id, token=token) == (
            A2APushReceipt("unauthorized")
        )


@pytest.mark.parametrize("site", ["ledger_load", "inbox_insert", "ledger_cas"])
async def test_non_transient_store_errors_report_unavailable(site):
    from pymongo.errors import OperationFailure

    receiver, ledger, inbox, _, _, _, _, call = await _push_setup()
    error = OperationFailure("not transient")
    body = _push_body(_completed_task())
    if site == "ledger_load":
        ledger.load_by_record_id = AsyncMock(side_effect=error)
        assert await receiver.authenticate(call.call_record_id, token=TOKEN) == (
            A2APushReceipt("unavailable")
        )
    elif site == "inbox_insert":
        inbox.insert = AsyncMock(side_effect=error)
    else:
        ledger.cas = AsyncMock(side_effect=error)
        body = _push_body(_status_update(TaskState.TASK_STATE_COMPLETED))

    receipt = await receiver.receive(call.call_record_id, token=TOKEN, body=body)

    assert receipt == A2APushReceipt("unavailable")


async def test_store_outage_reports_unavailable():
    receiver, ledger, *_ = await _push_setup()
    ledger.load_by_record_id = AsyncMock(side_effect=RecoverableAdapterError("down"))

    receipt = await receiver.receive(
        "call-1", token=TOKEN, body=_push_body(_completed_task())
    )

    assert receipt == A2APushReceipt("unavailable")


@pytest.mark.parametrize(
    "state",
    [TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED],
)
async def test_status_update_expedites_the_poll_without_recording(state):
    receiver, ledger, inbox, _, sink, _, _, call = await _push_setup()

    receipt = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_status_update(state))
    )

    assert receipt == A2APushReceipt("accepted")
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "working"
    assert persisted.next_attempt_at <= datetime.now(UTC)
    assert persisted.terminal_result is None
    assert await inbox.list_due(due_at=datetime.now(UTC), limit=10) == []
    assert sink.values == []


async def test_working_and_artifact_updates_are_acknowledged_no_ops():
    receiver, ledger, _, _, _, _, _, call = await _push_setup()
    artifact = TaskArtifactUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        artifact=Artifact(artifact_id="a", parts=[Part(text="x")]),
    )

    for event in (_status_update(TaskState.TASK_STATE_WORKING), artifact):
        receipt = await receiver.receive(
            call.call_record_id, token=TOKEN, body=_push_body(event)
        )
        assert receipt == A2APushReceipt("accepted")

    assert (await ledger.load_by_record_id(call.call_record_id)) == call


# -- idempotency with polling ------------------------------------------------


async def test_duplicate_push_replays_the_same_inbox_row():
    receiver, _, inbox, conflicts, *_ = await _push_setup()
    call = _working_call()
    body = _push_body(_completed_task())

    first = await receiver.receive(call.call_record_id, token=TOKEN, body=body)
    second = await receiver.receive(call.call_record_id, token=TOKEN, body=body)

    assert first == second
    rows = await inbox.list_due(due_at=datetime.now(UTC), limit=10)
    assert [row.observation_id for row in rows] == [first.observation_id]
    assert await conflicts.list_for_source(rows[0].source_identity) == []


class _RecoveryCheckpoints:
    async def is_acceptance_checkpointed(self, *args):
        return True

    async def is_suspension_checkpointed(self, *args):
        return False


async def _make_due(ledger, call_record_id):
    record = await ledger.load_by_record_id(call_record_id)
    now = datetime.now(UTC)
    due = record.model_copy(
        update={
            "next_attempt_at": now,
            "updated_at": now,
            "state_version": record.state_version + 1,
        }
    )
    assert await ledger.cas(due, expected_state_version=record.state_version) == (
        "accepted"
    )
    return due


async def _poll_recovery(ledger, observations, epochs):
    from a2a_adapter.webhook_payloads import task_from_task_frame

    async def fetch_remote_task(card, task_id, **kwargs):
        return task_from_task_frame(
            MessageToDict(to_stream_response(_completed_task()))["task"]
        )

    async def resolve(call_record_id):
        record = await ledger.load_by_record_id(call_record_id)
        return {
            "task_id": record.a2a_task_id,
            "context_id": record.a2a_context_id,
            "endpoint_scope": record.dispatch_snapshot.endpoint_scope,
            "agent_id": record.agent_id,
        }

    client = _direct_client(fetch_remote_task=fetch_remote_task, call_resolver=resolve)
    return A2ACallRecoveryService(
        ledger=ledger,
        checkpoints=_RecoveryCheckpoints(),
        room_epochs=epochs,
        dispatch=DirectA2ADispatchAdapter(client, observations=observations),
        observations=observations,
        recover_dispatch=AsyncMock(),
        hitl=InMemoryHITLApplicationPort(),
    )


async def test_poll_scheduled_before_an_applied_push_is_a_no_op():
    (
        receiver,
        ledger,
        _,
        conflicts,
        sink,
        observations,
        epochs,
        call,
    ) = await _push_setup()
    recovery = await _poll_recovery(ledger, observations, epochs)
    stale = await _make_due(ledger, call.call_record_id)

    receipt = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_completed_task())
    )
    await receiver.process(receipt.observation_id)
    applied = await ledger.load_by_record_id(call.call_record_id)

    assert await recovery.recover_call(stale, now=datetime.now(UTC)) is False
    assert await ledger.load_by_record_id(call.call_record_id) == applied
    assert len(sink.values) == 1


async def test_push_processed_after_poll_applied_the_same_result_is_a_no_op():
    (
        receiver,
        ledger,
        inbox,
        conflicts,
        _,
        observations,
        epochs,
        call,
    ) = await _push_setup()
    recovery = await _poll_recovery(ledger, observations, epochs)

    current = await _make_due(ledger, call.call_record_id)
    receipt = await receiver.receive(
        call.call_record_id, token=TOKEN, body=_push_body(_completed_task())
    )
    assert await recovery.recover_call(current, now=datetime.now(UTC)) is True
    polled = await ledger.load_by_record_id(call.call_record_id)
    assert polled.state == "completed"

    await receiver.process(receipt.observation_id)

    after = await ledger.load_by_record_id(call.call_record_id)
    assert after.terminal_result_digest == polled.terminal_result_digest
    assert after.terminal_result == polled.terminal_result
    row = await inbox.load(receipt.observation_id)
    assert row.state == "completed"
    assert await conflicts.list_for_source(row.source_identity) == []


# -- route ------------------------------------------------------------------


class _FakeReceiver:
    def __init__(self, receipt: A2APushReceipt, *, max_body_bytes: int = 1024):
        self.receipt = receipt
        self._max = max_body_bytes
        self.received: list[tuple[str, str | None, bytes]] = []
        self.processed: list[str] = []
        self.auth = A2APushReceipt("accepted")
        self.authenticated: list[tuple[str, str | None]] = []

    @property
    def max_body_bytes(self) -> int:
        return self._max

    async def authenticate(self, call_record_id, *, token):
        self.authenticated.append((call_record_id, token))
        return self.auth

    async def receive(self, call_record_id, *, token, body):
        self.received.append((call_record_id, token, body))
        return self.receipt

    async def process(self, observation_id):
        self.processed.append(observation_id)


def _app(receiver) -> FastAPI:
    from api_gateway.routes.a2a_push_routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    if receiver is not None:
        app.state.a2a_push_receiver = receiver
    return app


async def _post(app, body: bytes = b"{}", **headers):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/v1/a2a/push/call-1", content=body, headers=headers
        )


async def test_route_passes_token_and_schedules_processing():
    receiver = _FakeReceiver(A2APushReceipt("accepted", "observation-1"))

    response = await _post(
        _app(receiver), b'{"task": {}}', **{"X-A2A-Notification-Token": TOKEN}
    )

    assert response.status_code == 202
    assert receiver.received == [("call-1", TOKEN, b'{"task": {}}')]
    assert receiver.processed == ["observation-1"]


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("unauthorized", 401), ("invalid", 400), ("too_large", 413), ("unavailable", 503)],
)
async def test_route_maps_rejections_uniformly(outcome, status):
    receiver = _FakeReceiver(A2APushReceipt(outcome))

    response = await _post(_app(receiver))

    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    assert receiver.processed == []


async def test_route_enforces_the_body_limit_before_receiving():
    receiver = _FakeReceiver(A2APushReceipt("accepted"), max_body_bytes=8)

    response = await _post(_app(receiver), b"x" * 9)

    assert response.status_code == 413
    assert receiver.received == []


def _counting_receive(app):
    """Wrap an ASGI app and count request-body messages it pulls."""
    reads = {"body": 0}

    async def wrapped(scope, receive, send):
        async def counted():
            message = await receive()
            if message["type"] == "http.request":
                reads["body"] += 1
            return message

        await app(scope, counted, send)

    return wrapped, reads


async def test_wrong_token_with_large_body_is_rejected_without_reading_it():
    receiver = _FakeReceiver(A2APushReceipt("accepted"), max_body_bytes=4096)
    receiver.auth = A2APushReceipt("unauthorized")
    app, reads = _counting_receive(_app(receiver))

    response = await _post(app, b"x" * 4000, **{"X-A2A-Notification-Token": "bad"})

    assert response.status_code == 401
    assert receiver.authenticated == [("call-1", "bad")]
    assert reads["body"] == 0
    assert receiver.received == []


async def test_declared_oversized_body_is_rejected_before_authentication():
    receiver = _FakeReceiver(A2APushReceipt("accepted"), max_body_bytes=8)

    response = await _post(_app(receiver), b"x" * 9)

    assert response.status_code == 413
    assert receiver.authenticated == []


async def test_authenticated_push_reads_the_body():
    receiver = _FakeReceiver(A2APushReceipt("accepted"))
    app, reads = _counting_receive(_app(receiver))

    response = await _post(app, b'{"task": {}}', **{"X-A2A-Notification-Token": TOKEN})

    assert response.status_code == 202
    assert reads["body"] >= 1
    assert receiver.received == [("call-1", TOKEN, b'{"task": {}}')]


def test_push_path_has_a_concurrency_cap():
    from common.middleware.request_concurrency import (
        RequestConcurrencyLimitMiddleware,
    )
    from main import A2A_PUSH_MAX_CONCURRENT, app

    caps = [
        middleware.kwargs
        for middleware in app.user_middleware
        if middleware.cls is RequestConcurrencyLimitMiddleware
        and middleware.kwargs["path"] == "/api/v1/a2a/push"
    ]
    assert caps == [
        {
            "path": "/api/v1/a2a/push",
            "max_concurrent": A2A_PUSH_MAX_CONCURRENT,
            "include_subpaths": True,
        }
    ]


async def test_concurrency_cap_applies_to_push_subpaths():
    import asyncio

    from common.middleware.request_concurrency import (
        RequestConcurrencyLimitMiddleware,
    )

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestConcurrencyLimitMiddleware(
        slow, path="/api/v1/a2a/push", max_concurrent=1, include_subpaths=True
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post("/api/v1/a2a/push/call-1"))
        await asyncio.wait_for(entered.wait(), 5)
        second = await client.post("/api/v1/a2a/push/call-2")
        other = asyncio.create_task(client.post("/api/v1/a2a/pushy"))
        release.set()
        assert (await first).status_code == 202
        assert (await other).status_code == 202
    assert second.status_code == 429


async def test_route_without_bound_receiver_is_unavailable():
    response = await _post(_app(None))

    assert response.status_code == 503


def test_route_is_registered_with_push_token_policy():
    from fastapi.routing import APIRoute

    from api_gateway.registry import resolve_declared_owner, route_group_for_path
    from main import app

    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name == "receive_a2a_push"
    )
    assert route.path == "/api/v1/a2a/push/{call_record_id}"
    assert route.methods == {"POST"}
    assert route.status_code == 202
    assert route_group_for_path(route.path) == "a2a_push"
    assert "a2a_push" in route.tags
    assert resolve_declared_owner(route) == "api_gateway.routes.a2a_push_routes"
    assert "get_current_user" not in {
        dependency.call.__name__ for dependency in route.dependant.dependencies
    }
