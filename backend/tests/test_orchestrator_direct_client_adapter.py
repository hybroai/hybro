from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from google.protobuf.json_format import MessageToDict

from a2a_adapter.client_facade import A2AClientFacadeError
from a2a_adapter.orchestrator_direct_client import (
    FREE_TEXT_INTERACTION_PREFIX,
    FREE_TEXT_QUESTION_ID,
    DirectCallAddress,
    OrchestratorDirectA2AClient,
    _agent_error,
    _extract_interaction_spec,
    endpoint_scope_digest,
)
from common.a2a_constants import (
    HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE,
    HYBRO_A2A_ERROR_METADATA_KEY,
    HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY,
    HYBRO_A2A_INTERACTION_METADATA_KEY,
    HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE,
    HYBRO_A2A_PART_PROVENANCE_METADATA_KEY,
    HYBRO_A2A_SELECTED_SKILL_METADATA_KEY,
)
from common.dto.hitl import (
    HITLAuthorizationResultAnswer,
    HITLConfirmationAnswer,
    HITLMultiChoiceAnswer,
    HITLPolicyDecision,
    HITLPolicyDecisionAnswer,
    HITLQuestionAnswer,
    HITLSingleChoiceAnswer,
    HITLTextAnswer,
)
from common.types import (
    Artifact,
    DataPart,
    Message,
    MessageRole,
    Task,
    TaskState,
    TextPart,
)
from execution.orchestrator.a2a_runtime.errors import (
    AgentCardContractError,
    RecoverableAdapterError,
    RecoverableTransportError,
    StaleRoomEpochError,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ACancellationCommand,
    A2AContinuationCommand,
    A2ADispatchCommand,
    A2ADispatchReceipt,
    A2AModelReplyCommand,
    MaterializedResourcePart,
    NormalizedA2AObservation,
)
from execution.orchestrator.models import ToolResult
from tests._a2a_task_builders import (
    build_completed_text_task,
    build_failed_text_task,
    build_task_status,
)

NOW = datetime.now(UTC)


def _card() -> dict:
    return {
        "name": "Agent",
        "version": "1.0.0",
        "capabilities": {},
        "supportedInterfaces": [
            {
                "url": "https://agent.example/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
    }


def _task_dict(task: Task) -> dict:
    return task.model_dump(mode="json", by_alias=True)


def _file_task_result(name: str, encoded_b64: str) -> dict:
    """A completed task frame as ``client_facade`` yields it (ProtoJSON)."""
    return {
        "id": "task-1",
        "contextId": "ctx-1",
        "kind": "task",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {
                "artifactId": "art-1",
                "name": name,
                "parts": [
                    {"raw": encoded_b64, "filename": name, "mediaType": "image/png"}
                ],
            }
        ],
    }


def _dispatch_command(**updates) -> A2ADispatchCommand:
    values = {
        "command_id": "command-1",
        "call_record_id": "call-1",
        "invocation_id": "inv-1",
        "message_id": "message-1",
        "binding_id": "binding-1",
        "agent_id": "agent-1",
        "endpoint_scope": "https://agent.example/a2a",
        "transport_kind": "direct",
        "direct_mode": "sync",
        "task": "do work",
        "materialized_resources": [],
        "room_id": "room-1",
        "room_epoch": 1,
        "deadline_at": NOW + timedelta(seconds=30),
    }
    values.update(updates)
    return A2ADispatchCommand(**values)


def _continuation_command() -> A2AContinuationCommand:
    return A2AContinuationCommand(
        command_id="continuation-1",
        transport_kind="direct",
        call_record_id="call-1",
        interaction_id="interaction-1",
        interaction_revision=1,
        answer_digest="answer",
        answers=[],
        binding_id="binding-1",
        binding_digest="binding-digest",
        requesting_subject_digest=sha256(b"user-1").hexdigest(),
        task_id="task-1",
        context_id="context-1",
        conversation_id="room-1",
        room_epoch=1,
        created_at=NOW,
    )


def _model_reply_command() -> A2AModelReplyCommand:
    return A2AModelReplyCommand(
        command_id="model-reply-1",
        transport_kind="direct",
        call_record_id="call-1",
        binding_id="binding-1",
        binding_digest="binding-digest",
        requesting_subject_digest=sha256(b"user-1").hexdigest(),
        task_id="task-1",
        context_id="context-1",
        conversation_id="room-1",
        room_epoch=1,
        message_text="continue",
        created_at=NOW,
    )


def _cancellation_command() -> A2ACancellationCommand:
    return A2ACancellationCommand(
        command_id="cancel-1",
        transport_kind="direct",
        call_record_id="call-1",
        reason="stop",
        created_at=NOW,
    )


class FakeSdk:
    def __init__(
        self, *, send=None, fetch=None, cancel=None, stream=None, fetch_card=None
    ):
        self.send = send
        self.fetch = fetch
        self.cancel = cancel
        self.stream = stream
        self.fetch_card = fetch_card
        self.send_calls = []
        self.fetch_calls = []
        self.cancel_calls = []
        self.card_calls = []

    async def send_message(self, card, message, **kwargs):
        self.send_calls.append((card, message, kwargs))
        return await self.send(card, message, kwargs)

    async def fetch_remote_task(self, card, task_id, **kwargs):
        self.fetch_calls.append((card, task_id, kwargs))
        return await self.fetch(card, task_id, kwargs)

    async def cancel_remote_task(self, card, task_id, **kwargs):
        self.cancel_calls.append((card, task_id, kwargs))
        return await self.cancel(card, task_id, kwargs)

    async def fetch_agent_card(self, url, **kwargs):
        self.card_calls.append((url, kwargs))
        if self.fetch_card is not None:
            return await self.fetch_card(url, kwargs)
        return _card()

    def stream_message(self, card, message, **kwargs):
        return self.stream(card, message, kwargs)


def _client(sdk: FakeSdk, **kwargs) -> OrchestratorDirectA2AClient:
    return OrchestratorDirectA2AClient(
        send_message=sdk.send_message,
        stream_message=sdk.stream_message,
        cancel_remote_task=sdk.cancel_remote_task,
        fetch_remote_task=sdk.fetch_remote_task,
        fetch_agent_card=sdk.fetch_agent_card,
        receipt_factory=A2ADispatchReceipt,
        observation_factory=NormalizedA2AObservation,
        recoverable_transport_error_factory=RecoverableTransportError,
        agent_card_contract_error_factory=AgentCardContractError,
        **kwargs,
    )


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
async def test_card_fetch_retryable_status_is_sanitized_transport_error(status_code):
    async def fetch_card(url, kwargs):
        raise A2AClientFacadeError(
            f"provider failure at {url}", status_code=status_code
        )

    sdk = FakeSdk(fetch_card=fetch_card)
    with pytest.raises(RecoverableTransportError) as caught:
        await _client(sdk).send(_dispatch_command())

    assert str(caught.value) == "Agent Card is temporarily unavailable."
    assert "agent.example" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert sdk.send_calls == []


async def test_card_fetch_no_status_network_signature_is_retryable():
    async def fetch_card(url, kwargs):
        raise A2AClientFacadeError(f"All connection attempts failed for {url}")

    with pytest.raises(RecoverableTransportError, match="temporarily unavailable"):
        await _client(FakeSdk(fetch_card=fetch_card)).send(_dispatch_command())


@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_card_fetch_contract_status_is_nonretryable_and_sanitized(status_code):
    async def fetch_card(url, kwargs):
        raise A2AClientFacadeError(
            f"provider failure at {url}", status_code=status_code
        )

    with pytest.raises(AgentCardContractError) as caught:
        await _client(FakeSdk(fetch_card=fetch_card)).send(_dispatch_command())

    assert str(caught.value) == "Agent Card could not be resolved."
    assert "agent.example" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_invalid_card_is_nonretryable_contract_error():
    async def fetch_card(url, kwargs):
        return {
            "name": "missing required card fields",
            "secret": "provider-secret-value",
        }

    with pytest.raises(AgentCardContractError, match="Agent Card is invalid") as caught:
        await _client(FakeSdk(fetch_card=fetch_card)).send(_dispatch_command())

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "provider-secret-value" not in repr(caught.value)


@pytest.mark.parametrize(
    "operation",
    [
        "send",
        "start_poll",
        "open_stream",
        "inspect",
        "continue_task",
        "send_model_reply",
        "inspect_continuation",
        "cancel",
        "inspect_cancellation",
    ],
)
async def test_all_direct_paths_share_retryable_card_resolution(operation):
    async def fetch_card(url, kwargs):
        raise A2AClientFacadeError("temporary provider outage", status_code=503)

    sdk = FakeSdk(fetch_card=fetch_card)
    client = _client(sdk)
    dispatch_command = _dispatch_command()
    client._remember(dispatch_command, task_id="task-1", context_id="context-1")
    command_by_operation = {
        "send": dispatch_command,
        "start_poll": dispatch_command,
        "open_stream": dispatch_command,
        "inspect": dispatch_command,
        "continue_task": _continuation_command(),
        "send_model_reply": _model_reply_command(),
        "inspect_continuation": _continuation_command(),
        "cancel": _cancellation_command(),
        "inspect_cancellation": _cancellation_command(),
    }

    with pytest.raises(RecoverableTransportError):
        await getattr(client, operation)(command_by_operation[operation])

    assert len(sdk.card_calls) == 1
    assert sdk.send_calls == []
    assert sdk.fetch_calls == []
    assert sdk.cancel_calls == []


async def test_send_terminal_builds_terminal_receipt_and_observation():
    task = build_completed_text_task(task_id="task-1", text="done", context_id="ctx-1")

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    sdk = FakeSdk(send=send)
    receipt = await _client(sdk).send(_dispatch_command())

    assert isinstance(receipt, A2ADispatchReceipt)
    assert receipt.outcome == "terminal"
    assert receipt.task_id == "task-1"
    assert receipt.terminal_observation is not None
    obs = receipt.terminal_observation
    assert obs.event_kind == "terminal"
    assert obs.status == "completed"
    assert obs.binding_scope == endpoint_scope_digest("https://agent.example/a2a")


async def test_send_rematerialized_data_preserves_payload_metadata():
    task = build_completed_text_task(task_id="task-1", text="done", context_id="ctx-1")

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    sdk = FakeSdk(send=send)
    await _client(sdk).send(
        _dispatch_command(
            materialized_resources=[
                MaterializedResourcePart(
                    ref_id="art_inline",
                    kind="data",
                    content_digest="digest",
                    payload={"value": 42},
                    mime_type="application/vnd.hybro.result+json",
                    metadata={
                        "mime_type": "application/vnd.hybro.result+json",
                        "schema": "v1",
                    },
                )
            ]
        )
    )

    sent_message = sdk.send_calls[0][1]
    # 1.0 parts are one unified message; the set member identifies the payload.
    data_part = next(
        part for part in sent_message.parts if part.WhichOneof("content") == "data"
    )
    assert MessageToDict(data_part.data) == {"value": 42.0}
    assert MessageToDict(data_part.metadata) == {
        "mime_type": "application/vnd.hybro.result+json",
        "schema": "v1",
    }


async def test_send_tags_instruction_context_and_selected_skill_for_sdk_round_trip():
    task = build_completed_text_task(task_id="task-1", text="done", context_id="ctx-1")

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    sdk = FakeSdk(send=send)
    await _client(sdk).send(
        _dispatch_command(
            skill_id="skill-review",
            materialized_resources=[
                MaterializedResourcePart(
                    ref_id="ctx:message:user-1",
                    kind="text",
                    content_digest="context-digest",
                    payload="durable user evidence",
                    mime_type="text/plain",
                    metadata={
                        "owner": "context-memory",
                        HYBRO_A2A_PART_PROVENANCE_METADATA_KEY: {
                            "schema_version": 99,
                            "role": "forged",
                        },
                    },
                ),
                MaterializedResourcePart(
                    ref_id="file-1",
                    kind="file",
                    content_digest="file-digest",
                    payload={
                        "name": "input.pdf",
                        "mime_type": "application/pdf",
                        "bytes": base64.b64encode(b"pdf").decode(),
                    },
                    mime_type="application/pdf",
                    metadata={"authorized": True},
                ),
            ],
        )
    )

    sent_message = sdk.send_calls[0][1]
    assert sent_message.metadata == {
        "agent_id": "agent-1",
        HYBRO_A2A_SELECTED_SKILL_METADATA_KEY: {
            "schema_version": 1,
            "skill_id": "skill-review",
        },
    }
    instruction, context, file_part = list(sent_message.parts)
    assert instruction.WhichOneof("content") == "text"
    assert context.WhichOneof("content") == "text"
    assert MessageToDict(instruction.metadata) == {
        HYBRO_A2A_PART_PROVENANCE_METADATA_KEY: {
            "schema_version": 1,
            "role": HYBRO_A2A_ORCHESTRATOR_INSTRUCTION_ROLE,
        }
    }
    assert MessageToDict(context.metadata) == {
        "owner": "context-memory",
        HYBRO_A2A_PART_PROVENANCE_METADATA_KEY: {
            "schema_version": 1,
            "role": HYBRO_A2A_DURABLE_USER_CONTEXT_ROLE,
        },
    }
    assert file_part.WhichOneof("content") == "raw"
    assert file_part.raw == b"pdf"
    assert file_part.media_type == "application/pdf"
    assert file_part.filename == "input.pdf"
    assert MessageToDict(file_part.metadata) == {"authorized": True}

    private_metadata = json.dumps(
        {
            "message": MessageToDict(sent_message.metadata),
            "parts": [
                MessageToDict(part.metadata) if part.HasField("metadata") else None
                for part in sent_message.parts
            ],
        },
        sort_keys=True,
    )
    for forbidden in (
        "https://agent.example/a2a",
        "room-1",
        "binding-1",
        "call-1",
        "inv-1",
        "presentation_id",
    ):
        assert forbidden not in private_metadata


async def test_send_nonterminal_returns_accepted_with_task_identity():
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=build_task_status(TaskState.working),
        artifacts=None,
    )

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    receipt = await _client(FakeSdk(send=send)).send(_dispatch_command())
    assert receipt.outcome == "accepted"
    assert receipt.task_id == "task-1"
    assert receipt.context_id == "ctx-1"


async def test_send_input_required_builds_interaction_receipt():
    """An immediate input-required answer is the invocation's durable result:
    the receipt carries the Agent's request so the kernel can decide whether
    to satisfy it from context instead of polling the still-open task."""
    status = build_task_status(TaskState.input_required)
    status.message = Message(
        role=MessageRole.AGENT,
        parts=[TextPart(text="I need the client name and coverage limit.")],
        message_id="msg-1",
    )
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=status,
        artifacts=None,
    )

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    receipt = await _client(FakeSdk(send=send)).send(_dispatch_command())

    assert receipt.outcome == "interaction"
    assert receipt.task_id == "task-1"
    assert receipt.terminal_observation is None
    observation = receipt.interaction_observation
    assert observation is not None
    assert observation.event_kind == "input_required"
    assert observation.status is None
    assert observation.content[0].text == ("I need the client name and coverage limit.")
    spec = observation.interaction_spec
    assert spec is not None
    assert spec["interaction_id"].startswith(FREE_TEXT_INTERACTION_PREFIX)
    assert spec["questions"] == [
        {
            "question_id": FREE_TEXT_QUESTION_ID,
            "interaction_kind": "questionnaire",
            "prompt": "I need the client name and coverage limit.",
            "answer_kind": "text",
            "required": True,
        }
    ]


async def test_send_inline_data_artifact_reaches_observation_with_text():
    """Agents attach structured documents (JSON data parts) alongside a text
    summary; both must reach the kernel or it cannot read the real document
    and re-dispatches the same Agent until the budget runs out."""
    status = build_task_status(TaskState.completed)
    status.message = Message(
        role=MessageRole.AGENT,
        parts=[TextPart(text="Quote ready, see attached document.")],
        message_id="msg-1",
    )
    artifact = Artifact(
        artifact_id="artifact-1",
        name="cyber_quote",
        parts=[
            DataPart(
                data={"premium": "USD 35,700", "limit": 5000000},
                metadata={"mime_type": "application/vnd.hybro.quote+json"},
            )
        ],
    )
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=status,
        artifacts=[artifact],
    )

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    receipt = await _client(FakeSdk(send=send)).send(_dispatch_command())

    observation = receipt.terminal_observation
    assert observation is not None
    texts = [part.text for part in observation.content if part.kind == "text"]
    assert texts == ["Quote ready, see attached document."]
    data = [part.data for part in observation.content if part.kind == "data"]
    assert data == [{"premium": "USD 35,700", "limit": 5000000}]
    assert len(observation.inline_artifacts) == 1
    descriptor = observation.inline_artifacts[0]
    assert descriptor.ref_id.startswith("art_")
    assert descriptor.artifact_id == "artifact-1"
    assert descriptor.artifact_name == "cyber_quote"
    assert descriptor.content_index == 0
    assert descriptor.mime_type == "application/vnd.hybro.quote+json"
    assert observation.content[0].metadata == {
        "mime_type": "application/vnd.hybro.quote+json"
    }
    assert descriptor.ref_id in observation.artifact_refs


async def test_start_poll_requests_nonblocking_and_returns_accepted():
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=build_task_status(TaskState.submitted),
        artifacts=None,
    )

    async def send(card, message, kwargs):
        assert kwargs["blocking"] is False
        return {"kind": "task", "result": _task_dict(task)}

    receipt = await _client(FakeSdk(send=send)).start_poll(_dispatch_command())
    assert receipt.outcome == "accepted"
    assert receipt.task_id == "task-1"


async def test_open_stream_yields_normalized_observations():
    completed = build_completed_text_task(
        task_id="task-1", text="done", context_id="ctx-1"
    )

    async def stream(card, message, kwargs):
        yield {"result": {"task": _task_dict(completed)}}

    client = _client(FakeSdk(stream=stream))
    stream_obj = await client.open_stream(_dispatch_command())
    events = [event async for event in stream_obj]
    assert len(events) == 1
    assert isinstance(events[0], NormalizedA2AObservation)
    assert events[0].event_kind == "terminal"
    await stream_obj.close(reason="terminal")


async def test_open_stream_parses_standard_discriminated_interaction_frames():
    async def stream(card, message, kwargs):
        yield {
            "result": {
                "kind": "artifact-update",
                "taskId": "task-1",
                "contextId": "ctx-1",
                "append": False,
                "lastChunk": True,
                "artifact": {
                    "artifactId": "artifact-1",
                    "name": "draft",
                    "parts": [{"kind": "text", "text": "Draft ready."}],
                },
            }
        }
        yield {
            "result": {
                "kind": "status-update",
                "taskId": "task-1",
                "contextId": "ctx-1",
                "final": True,
                "status": {
                    "state": "input-required",
                    "message": {
                        "kind": "message",
                        "messageId": "message-2",
                        "role": "agent",
                        "parts": [{"kind": "text", "text": "Which island?"}],
                        "metadata": {
                            HYBRO_A2A_INTERACTION_METADATA_KEY: {
                                "schema_version": 1,
                                "interaction_id": "interaction-1",
                                "questions": [
                                    {
                                        "question_id": "question-1",
                                        "interaction_kind": "questionnaire",
                                        "prompt": "Which island?",
                                        "answer_kind": "text",
                                        "required": True,
                                    }
                                ],
                            }
                        },
                    },
                },
            }
        }

    client = _client(FakeSdk(stream=stream))
    command = _dispatch_command()
    stream_obj = await client.open_stream(command)
    events = [event async for event in stream_obj]
    await stream_obj.close(reason="interaction")

    assert [event.event_kind for event in events] == ["working", "input_required"]
    assert all(event.task_id == "task-1" for event in events)
    assert all(event.context_id == "ctx-1" for event in events)
    assert events[0].content[0].text == "Draft ready."
    assert events[1].content[0].text == "Which island?"
    assert events[1].interaction_spec == {
        "schema_version": 1,
        "interaction_id": "interaction-1",
        "questions": [
            {
                "question_id": "question-1",
                "interaction_kind": "questionnaire",
                "prompt": "Which island?",
                "answer_kind": "text",
                "required": True,
                "choices": None,
            }
        ],
    }
    assert client._addresses[command.call_record_id].task_id == "task-1"
    assert client._addresses[command.call_record_id].context_id == "ctx-1"


async def test_open_stream_assigns_per_frame_identity_and_registers_address():
    working = Task(
        id="task-1",
        context_id="ctx-1",
        status=build_task_status(TaskState.working),
        artifacts=None,
    )

    async def stream(card, message, kwargs):
        yield {"result": {"task": _task_dict(working)}}
        yield {"result": {"task": _task_dict(working)}}

    client = _client(FakeSdk(stream=stream))
    command = _dispatch_command()
    stream_obj = await client.open_stream(command)
    events = [event async for event in stream_obj]
    await stream_obj.close(reason="done")

    assert len(events) == 2
    assert events[0].observation_id != events[1].observation_id
    assert events[0].source_identity != events[1].source_identity
    assert events[0].cursor == "1"
    assert events[1].cursor == "2"
    assert client._addresses[command.call_record_id].task_id == "task-1"
    assert client._addresses[command.call_record_id].context_id == "ctx-1"


async def test_open_stream_accumulates_artifact_refs_into_terminal_tool_result():
    """artifact-update materializes bytes; terminal status-update must keep refs."""
    image_bytes = b"png-binary-for-stream-accumulation"
    encoded_b64 = base64.b64encode(image_bytes).decode()

    class EpochOwner:
        async def commit(self, **kwargs):
            return "/api/v1/files/stream-file-1/content"

    async def stream(card, message, kwargs):
        yield {
            "result": {
                "append": False,
                "artifact": {
                    "artifactId": "art-img",
                    "name": "cover.png",
                    "parts": [
                        {
                            "raw": encoded_b64,
                            "filename": "cover.png",
                            "mediaType": "image/png",
                        }
                    ],
                },
                "contextId": "ctx-1",
                "kind": "artifact-update",
                "lastChunk": True,
                "taskId": "task-1",
            }
        }
        yield {
            "result": {
                "contextId": "ctx-1",
                "final": True,
                "kind": "status-update",
                "status": {
                    "message": {
                        "parts": [{"kind": "text", "text": "cover ready"}],
                        "role": "agent",
                    },
                    "state": "completed",
                },
                "taskId": "task-1",
            }
        }

    client = _client(FakeSdk(stream=stream), epoch_owner=EpochOwner())
    stream_obj = await client.open_stream(_dispatch_command())
    events = [event async for event in stream_obj]
    await stream_obj.close(reason="terminal")

    assert len(events) == 2
    assert events[0].artifact_refs == ["/api/v1/files/stream-file-1/content"]
    assert events[1].event_kind == "terminal"
    assert events[1].artifact_refs == ["/api/v1/files/stream-file-1/content"]

    tool_result = ToolResult(
        call_id="inv-1",
        tool_name="agent_tool",
        status="completed",
        content=list(events[1].content or []),
        artifact_refs=list(events[1].artifact_refs or []),
        error_code=None,
        error_message=None,
    )
    assert tool_result.artifact_refs == ["/api/v1/files/stream-file-1/content"]


async def test_send_reraises_recoverable_materialization_errors():
    encoded_b64 = base64.b64encode(b"png-bytes").decode()
    task = _file_task_result("cover.png", encoded_b64)

    class StaleEpochOwner:
        async def commit(self, **kwargs):
            raise StaleRoomEpochError("artifact Room epoch is no longer active")

    async def send(card, message, kwargs):
        return {"kind": "task", "result": task}

    client = _client(FakeSdk(send=send), epoch_owner=StaleEpochOwner())
    with pytest.raises(StaleRoomEpochError, match="no longer active"):
        await client.send(_dispatch_command())

    class TransientOwner:
        async def commit(self, **kwargs):
            raise RecoverableAdapterError(
                "Room artifact owner is temporarily unavailable"
            )

    client = _client(FakeSdk(send=send), epoch_owner=TransientOwner())
    with pytest.raises(RecoverableAdapterError, match="temporarily unavailable"):
        await client.send(_dispatch_command())


async def test_send_oversized_artifact_stays_terminal_failure(monkeypatch, caplog):
    from a2a_adapter import artifact_storage

    monkeypatch.setattr(artifact_storage, "MAX_FILE_RAW_BYTES", 4)
    task = _file_task_result(
        "broken.png", base64.b64encode(b"more than four bytes").decode()
    )

    class EpochOwner:
        async def commit(self, **kwargs):
            raise AssertionError("commit must not run for an oversized file")

    async def send(card, message, kwargs):
        return {"kind": "task", "result": task}

    caplog.set_level(logging.ERROR, logger="a2a_adapter.orchestrator_direct_client")
    receipt = await _client(FakeSdk(send=send), epoch_owner=EpochOwner()).send(
        _dispatch_command()
    )
    assert receipt.outcome == "terminal"
    assert receipt.terminal_observation is not None
    assert receipt.terminal_observation.status == "failed"
    assert receipt.terminal_observation.error_code == "artifact_materialization_failed"
    [record] = [
        r
        for r in caplog.records
        if r.getMessage() == "task_artifacts_materialization_failed"
    ]
    assert record.levelno == logging.ERROR
    assert record.operation == "send"
    assert record.call_record_id == "call-1"
    assert record.error_type


async def test_inspect_terminal_uses_resolved_task_identity():
    task = build_failed_text_task(
        task_id="task-1", error_text="boom", context_id="ctx-1"
    )

    async def fetch(card, task_id, kwargs):
        return task

    client = _client(FakeSdk(fetch=fetch))
    client._remember(_dispatch_command(), task_id="task-1", context_id="ctx-1")
    receipt = await client.inspect(_dispatch_command())
    assert receipt.outcome == "terminal"
    assert receipt.terminal_observation.status == "failed"


async def test_cancel_acknowledged_returns_accepted():
    async def cancel(card, task_id, kwargs):
        return True

    client = _client(FakeSdk(cancel=cancel))
    client._remember(_dispatch_command(), task_id="task-1", context_id="ctx-1")
    receipt = await client.cancel(_cancellation_command())
    assert receipt.outcome == "accepted"
    assert receipt.task_id == "task-1"


async def test_continuation_message_preserves_all_typed_answers_and_digest():
    answers = [
        HITLQuestionAnswer(question_id="text", answer=HITLTextAnswer(text="Ada")),
        HITLQuestionAnswer(
            question_id="single", answer=HITLSingleChoiceAnswer(choice="one")
        ),
        HITLQuestionAnswer(
            question_id="multi", answer=HITLMultiChoiceAnswer(choices=["a", "b"])
        ),
        HITLQuestionAnswer(
            question_id="confirm", answer=HITLConfirmationAnswer(confirmed=True)
        ),
        HITLQuestionAnswer(
            question_id="auth",
            answer=HITLAuthorizationResultAnswer(
                authorization_reference="authref:proof-1"
            ),
        ),
        HITLQuestionAnswer(
            question_id="policy",
            answer=HITLPolicyDecisionAnswer(
                decision=HITLPolicyDecision.APPROVE, reason="approved"
            ),
        ),
    ]
    command = _continuation_command().model_copy(
        update={"answer_digest": "durable-digest", "answers": answers}
    )
    client = _client(FakeSdk())
    message = client._build_continuation_message(
        command,
        address=DirectCallAddress(
            call_record_id="call-1",
            task_id="task-1",
            context_id="context-1",
            endpoint_scope="https://agent.example/a2a",
            agent_id="agent-1",
        ),
    )

    envelope = message["metadata"][HYBRO_A2A_INTERACTION_ANSWER_METADATA_KEY]
    assert envelope == {
        "schema_version": 1,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "answer_digest": "durable-digest",
        "answers": [answer.model_dump(mode="json") for answer in answers],
    }
    assert message["parts"] == [
        {
            "kind": "text",
            "text": (
                "text: Ada\nsingle: one\nmulti: a, b\nconfirm: True\n"
                "auth: authref:proof-1\npolicy: approve"
            ),
        }
    ]


async def test_continue_task_uses_command_task_and_context():
    task = build_completed_text_task(
        task_id="task-1", text="continued", context_id="ctx-1"
    )

    async def send(card, message, kwargs):
        assert message.message_id == "continuation-1"
        assert message.task_id == "task-1"
        assert message.context_id == "context-1"
        return {"kind": "task", "result": _task_dict(task)}

    client = _client(FakeSdk(send=send))
    client._remember(
        _dispatch_command(),
        task_id="task-1",
        context_id="ctx-1",
    )
    receipt = await client.continue_task(_continuation_command())
    assert receipt.outcome == "terminal"


async def test_continue_task_input_required_builds_interaction_receipt():
    status = build_task_status(TaskState.input_required)
    status.message = Message(
        role=MessageRole.AGENT,
        parts=[TextPart(text="How many days will you stay?")],
        message_id="msg-1",
    )
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=status,
        artifacts=None,
    )

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    client = _client(FakeSdk(send=send))
    client._remember(
        _dispatch_command(),
        task_id="task-1",
        context_id="ctx-1",
    )
    receipt = await client.continue_task(_continuation_command())

    assert receipt.outcome == "interaction"
    observation = receipt.interaction_observation
    assert observation is not None
    assert observation.event_kind == "input_required"
    assert observation.content[0].text == "How many days will you stay?"


async def test_continue_task_distinguishes_repeated_typed_interaction_rounds():
    send_count = 0

    async def send(card, message, kwargs):
        nonlocal send_count
        send_count += 1
        interaction_id = f"interaction-{send_count}"
        status = build_task_status(TaskState.input_required)
        status.message = Message(
            role=MessageRole.AGENT,
            parts=[TextPart(text=f"Question for round {send_count}?")],
            message_id=f"msg-{send_count}",
            metadata={
                HYBRO_A2A_INTERACTION_METADATA_KEY: {
                    "schema_version": 1,
                    "interaction_id": interaction_id,
                    "questions": [
                        {
                            "question_id": f"question-{send_count}",
                            "interaction_kind": "questionnaire",
                            "prompt": f"Question for round {send_count}?",
                            "answer_kind": "text",
                            "required": True,
                        }
                    ],
                }
            },
        )
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=status,
            artifacts=None,
        )
        return {"kind": "task", "result": _task_dict(task)}

    client = _client(FakeSdk(send=send))
    client._remember(_dispatch_command(), task_id="task-1", context_id="ctx-1")

    first = await client.continue_task(_continuation_command())
    second = await client.continue_task(_continuation_command())
    first_observation = first.interaction_observation
    second_observation = second.interaction_observation

    assert first_observation is not None
    assert second_observation is not None
    assert first_observation.observation_id != second_observation.observation_id
    assert first_observation.source_identity != second_observation.source_identity
    assert first_observation.interaction_spec["interaction_id"] == "interaction-1"
    assert second_observation.interaction_spec["interaction_id"] == "interaction-2"


async def test_extract_interaction_spec_requires_status_message_metadata():
    """History-only metadata must not be treated as the live challenge.

    After HITL continuation the prior status message moves into history. Using
    that stale interaction_id would re-park / resend the answered challenge
    instead of waiting for the Agent's next typed status.message.
    """
    from a2a_adapter.orchestrator_direct_client import _extract_interaction_spec
    from common.a2a_constants import HYBRO_A2A_INTERACTION_METADATA_KEY

    interaction = {
        "schema_version": 1,
        "interaction_id": "travel-planner:hist-1",
        "questions": [
            {
                "question_id": "travel-details:hist-1",
                "interaction_kind": "questionnaire",
                "prompt": "How many days?",
                "answer_kind": "text",
                "required": True,
            }
        ],
    }
    history_message = Message(
        role=MessageRole.AGENT,
        parts=[TextPart(text="How many days?")],
        message_id="hist-msg",
        metadata={HYBRO_A2A_INTERACTION_METADATA_KEY: interaction},
    )
    status = build_task_status(TaskState.input_required)
    status.message = None
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=status,
        history=[history_message],
        artifacts=None,
    )

    assert _extract_interaction_spec(task, event_kind="input_required") is None


async def test_continue_task_preserves_endpoint_scope_for_later_rounds():
    """Continuation commands have no endpoint_scope. Remembering them must not
    wipe the dispatch address, or the next HITL round returns delivery_uncertain.
    """
    task = build_completed_text_task(
        task_id="task-1", text="continued", context_id="ctx-1"
    )
    send_count = 0

    async def send(card, message, kwargs):
        nonlocal send_count
        send_count += 1
        return {"kind": "task", "result": _task_dict(task)}

    client = _client(FakeSdk(send=send))
    client._remember(
        _dispatch_command(),
        task_id="task-1",
        context_id="ctx-1",
    )
    first = await client.continue_task(_continuation_command())
    second = await client.continue_task(_continuation_command())

    assert first.outcome == "terminal"
    assert second.outcome == "terminal"
    assert send_count == 2


async def test_continue_task_refreshes_poisoned_address_from_resolver():
    task = build_completed_text_task(
        task_id="task-1", text="continued", context_id="ctx-1"
    )

    async def send(card, message, kwargs):
        return {"kind": "task", "result": _task_dict(task)}

    async def resolve(call_record_id: str):
        assert call_record_id == "call-1"
        return {
            "task_id": "task-1",
            "context_id": "ctx-1",
            "endpoint_scope": "https://agent.example/a2a",
            "agent_id": "agent-1",
        }

    client = _client(FakeSdk(send=send), call_resolver=resolve)
    client._addresses["call-1"] = DirectCallAddress(
        call_record_id="call-1",
        task_id="task-1",
        context_id="ctx-1",
        endpoint_scope=None,
        agent_id=None,
    )
    receipt = await client.continue_task(_continuation_command())
    assert receipt.outcome == "terminal"


async def test_unknown_task_identity_returns_delivery_uncertain():
    async def cancel(card, task_id, kwargs):
        return True

    client = _client(FakeSdk(cancel=cancel))
    receipt = await client.cancel(_cancellation_command())
    assert receipt.outcome == "delivery_uncertain"


def _status_task(state, *, text=None, message_id="msg-1", metadata=None) -> Task:
    status = build_task_status(state)
    status.message = (
        Message(
            role=MessageRole.AGENT,
            parts=[TextPart(text=text)] if text else [],
            message_id=message_id,
            metadata=metadata,
        )
        if text is not None or metadata is not None
        else None
    )
    return Task(id="task-1", context_id="ctx-1", status=status, artifacts=None)


def test_free_text_spec_is_stable_per_question_and_skipped_without_text():
    first = _extract_interaction_spec(
        _status_task(TaskState.input_required, text="Which tone?"),
        event_kind="input_required",
    )
    again = _extract_interaction_spec(
        _status_task(TaskState.input_required, text="Which tone?"),
        event_kind="input_required",
    )
    next_round = _extract_interaction_spec(
        _status_task(TaskState.input_required, text="Which tone?", message_id="m2"),
        event_kind="input_required",
    )
    empty = _extract_interaction_spec(
        _status_task(TaskState.input_required, text=""),
        event_kind="input_required",
    )
    auth = _extract_interaction_spec(
        _status_task(TaskState.auth_required, text="Sign in"),
        event_kind="auth_required",
    )

    assert first == again
    assert first["interaction_id"] != next_round["interaction_id"]
    assert empty is None
    assert auth is None


def test_free_text_continuation_is_a_plain_a2a_reply():
    command = _continuation_command().model_copy(
        update={
            "interaction_id": f"{FREE_TEXT_INTERACTION_PREFIX}abc",
            "answers": [
                HITLQuestionAnswer(
                    question_id=FREE_TEXT_QUESTION_ID,
                    answer=HITLTextAnswer(text="Formal, please"),
                )
            ],
        }
    )
    message = _client(FakeSdk())._build_continuation_message(
        command,
        address=DirectCallAddress(
            call_record_id="call-1",
            task_id="task-1",
            context_id="context-1",
            endpoint_scope="https://agent.example/a2a",
            agent_id="agent-1",
        ),
    )

    assert message["parts"] == [{"kind": "text", "text": "Formal, please"}]
    assert message["metadata"] == {"agent_id": "agent-1"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"code": "llm_quota_exceeded", "message": "Quota exceeded"},
            ("llm_quota_exceeded", "Quota exceeded"),
        ),
        ({"code": "bad code!", "message": "x"}, (None, None)),
        ({"code": "ok", "message": "m" * 900}, ("ok", "m" * 500)),
        ("not-a-mapping", (None, None)),
    ],
)
def test_agent_error_reads_only_bounded_documented_fields(raw, expected):
    task = _status_task(
        TaskState.failed,
        text="failed",
        metadata={HYBRO_A2A_ERROR_METADATA_KEY: raw},
    )

    assert _agent_error(task) == expected
