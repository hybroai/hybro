import ast
import inspect
import json
import tomllib
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from common.dto import (
    AgentInfo,
    AgentMessageFinal,
    AgentMessagePartial,
    ArtifactUpdateEvent,
    CancellationEvent,
    DeliveryEnvelope,
    DeliveryEvent,
    DeliveryEventBase,
    ErrorEvent,
    HITLRequestEvent,
    HITLResolvedEvent,
    InternalDomainEvent,
    ProcessingStatusEvent,
    RoomInfo,
    RunEventNotification,
    RunInfo,
    RunState,
    TaskSubmittedEvent,
    TaskUpdateEvent,
)
from common.errors import AppError, NotFoundError, ValidationError


def test_frozen_dto_is_immutable():
    agent = AgentInfo(agent_id="a1", name="Agent", status="active")

    with pytest.raises(PydanticValidationError):
        agent.name = "Changed"


def test_frozen_dto_container_fields_are_immutable():
    agent = AgentInfo(agent_id="a1", capabilities=["search"])
    delivery = DeliveryEnvelope(room_id="r1", event_type="message", payload={"x": 1})

    with pytest.raises(TypeError):
        agent.capabilities += ("write",)

    with pytest.raises(TypeError):
        delivery.payload["x"] = 2

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert '"capabilities":["search"]' in agent.model_dump_json()
        assert '"payload":{"x":1}' in delivery.model_dump_json()


def test_common_foundation_subpackages_are_packaged():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    packages = set(pyproject["tool"]["setuptools"]["packages"])

    assert {
        "common.config",
        "common.dto",
        "common.errors",
        "common.observability",
        "common.protocols",
    }.issubset(packages)


def test_common_a2a_helpers_do_not_perform_storage_signing():
    source = Path("common/utils/a2a_helpers.py").read_text()
    storage_markers = (
        "bind_a2a_storage_dependencies",
        "_require_s3_service",
        ".upload_file(",
        ".generate_presigned_url(",
    )

    assert not any(marker in source for marker in storage_markers)

    manifest = json.loads(
        Path("tests/fixtures/phase9_cleanup_manifest.json").read_text()
    )
    blockers = [
        entry
        for entry in manifest["blocked_cleanup"]
        if entry.get("path") == "common/utils/a2a_helpers.py"
        and entry.get("contract") == "a2a_storage_signing"
    ]

    assert not blockers


def test_common_utils_dependency_seams_are_protocol_typed_not_any_globals():
    seams = {
        Path("common/utils/a2a_helpers.py"): {
            "a2a_artifact_storage": "A2AArtifactFiles | None"
        },
    }
    violations: list[str] = []

    for path, expected in seams.items():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(
                node.target, ast.Name
            ):
                continue
            if node.target.id not in expected:
                continue
            annotation = ast.unparse(node.annotation)
            if annotation != expected[node.target.id] or "Any" in annotation:
                violations.append(
                    f"{path}:{node.lineno}: {node.target.id}: {annotation}"
                )

    context_source = Path("common/utils/context_utils.py").read_text()
    if "turn_notes_llm_provider" in context_source:
        violations.append("common/utils/context_utils.py: turn_notes_llm_provider")
    if "def bind_context_llm_provider" in context_source:
        violations.append("common/utils/context_utils.py: bind_context_llm_provider")

    assert not violations, (
        "Common utility dependency seams are broad globals:\n" + "\n".join(violations)
    )


def test_common_card_resolver_keeps_sdk_agent_card_validation(monkeypatch):
    from common.client.card_resolver import A2ACardResolver
    from common.types import A2AClientJSONError

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "name": "Incomplete",
                "url": "https://agent.example",
                "version": "1.0.0",
                "capabilities": {},
                "skills": [],
            }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return Response()

    monkeypatch.setattr("httpx.Client", Client)

    with pytest.raises(A2AClientJSONError, match="description"):
        A2ACardResolver("https://agent.example").get_agent_card()


def test_common_types_expose_sdk_free_task_parts():
    from pydantic import TypeAdapter

    from common.types import DataPart, FileContent, FilePart, Part, TaskState, TextPart

    assert TextPart.__module__ == "common.types"
    assert FilePart.__module__ == "common.types"
    assert DataPart.__module__ == "common.types"
    assert TaskState.__module__ == "common.types"
    assert TaskState.completed.value == "completed"

    parsed = TypeAdapter(Part).validate_python(
        {"kind": "file", "file": {"uri": "s3://bucket/key"}}
    )
    assert isinstance(parsed, Part)
    assert isinstance(parsed.root, FilePart)
    assert isinstance(parsed.root.file, FileContent)


def test_agent_capabilities_ignore_unknown_fields():
    from common.types import AgentCapabilities

    capabilities = AgentCapabilities(
        streaming=True,
        pushNotifications=False,
        stateTransitionHistory=True,
        stremaing=True,
    )

    assert "stremaing" not in capabilities.model_dump()
    assert not capabilities.model_extra or "stremaing" not in capabilities.model_extra


def test_agent_card_ignores_unknown_fields():
    from common.types import AgentCapabilities, AgentCard, AgentSkill

    card = AgentCard(
        name="agent",
        description="desc",
        url="https://agent.example",
        version="1.0.0",
        capabilities=AgentCapabilities(),
        skills=[AgentSkill(id="skill", name="Skill")],
        versoin="typo",
    )

    assert "versoin" not in card.model_dump()
    assert not card.model_extra or "versoin" not in card.model_extra


def test_agent_card_preserves_known_sdk_extension_fields_with_aliases():
    from common.types import AgentCapabilities, AgentCard, AgentSkill

    card = AgentCard(
        name="agent",
        description="desc",
        url="https://agent.example",
        version="1.0.0",
        capabilities=AgentCapabilities(extensions=[{"uri": "urn:capability:example"}]),
        skills=[AgentSkill(id="skill", name="Skill")],
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        additionalInterfaces=[{"url": "https://agent.example/a2a"}],
        security=[{"bearer": []}],
        securitySchemes={"bearer": {"type": "http"}},
        signatures=[{"protected": "header", "signature": "sig"}],
        supportsAuthenticatedExtendedCard=True,
    )

    dumped = card.model_dump(mode="json")

    assert dumped["protocolVersion"] == "0.3.0"
    assert dumped["capabilities"]["extensions"] == [{"uri": "urn:capability:example"}]
    assert dumped["security"] == [{"bearer": []}]
    assert dumped["signatures"] == [{"protected": "header", "signature": "sig"}]
    assert dumped["supportsAuthenticatedExtendedCard"] is True
    assert "supports_authenticated_extended_card" not in dumped


def test_file_content_accepts_snake_case_mime_type_but_dumps_a2a_field():
    from common.types import FileContent

    content = FileContent(uri="s3://bucket/file.png", mime_type="image/png")

    assert content.mimeType == "image/png"
    assert content.model_dump(mode="json")["mimeType"] == "image/png"
    assert "mime_type" not in content.model_dump(mode="json")


@pytest.mark.asyncio
async def test_auth_config_binds_authorized_parties(monkeypatch):
    import common.auth as auth

    captured = {}

    def fake_authenticate_request(request, options):
        captured["authorized_parties"] = options.authorized_parties
        captured["secret_key"] = options.secret_key
        return SimpleNamespace(
            is_signed_in=True,
            payload={"sub": "user-1", "sid": "session-1"},
        )

    monkeypatch.setattr(auth, "authenticate_request", fake_authenticate_request)

    auth.bind_auth_config(
        clerk_secret_key_value="secret",
        authorized_parties=("https://test.example",),
    )
    user = await auth.verify_clerk_token_from_request(MagicMock())

    assert user.user_id == "user-1"
    assert captured["secret_key"] == "secret"
    assert captured["authorized_parties"] == ("https://test.example",)
    assert "AUTHORIZED_PARTIES" not in Path("common/auth.py").read_text()


def test_delivery_dtos_accept_optional_trace_and_correlation_fields():
    envelope = DeliveryEnvelope(
        room_id="room-1",
        event_type="processing_status",
        payload={},
        trace_id="trace-1",
    )
    base = ProcessingStatusEvent(
        room_id="room-1",
        message_id="msg-1",
        status="processing",
        trace_id="trace-2",
    )
    run_event = RunEventNotification(
        room_id="room-1",
        event_id="evt-1",
        run_id="run-1",
        seq=1,
        run_event_type="agent_started",
        correlation_id="cr-1",
    )
    omitted = RunEventNotification(
        room_id="room-1",
        event_id="evt-2",
        run_id="run-1",
        seq=2,
        run_event_type="agent_finished",
    )

    assert envelope.trace_id == "trace-1"
    assert base.trace_id == "trace-2"
    assert run_event.correlation_id == "cr-1"
    assert omitted.correlation_id is None


def test_room_info_preserves_legacy_membership_status_default():
    room = RoomInfo(room_id="r1", room_name="Room", owner_id="u1")

    assert room.membership_origin_status == "active"


def test_protocols_are_runtime_checkable():
    import common.protocols as protocols

    for name in protocols.__all__:
        obj = getattr(protocols, name)
        if inspect.isclass(obj):
            assert getattr(obj, "_is_runtime_protocol", False), name


def test_common_json_aliases_are_protocol_safe():
    import subprocess
    import sys
    from typing import get_args

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import common.json_types; "
                "import common.protocols; "
                "assert '"
                "app_"
                "shell"
                ".bound' not in sys.modules"
            ),
        ],
        check=True,
    )

    import common.protocols as protocols
    from common.json_types import JsonMap, JsonScalar, JsonValue

    assert protocols.JsonScalar is JsonScalar
    assert protocols.JsonValue is JsonValue
    assert protocols.JsonMap is JsonMap

    assert set(get_args(JsonScalar)) == {str, int, float, bool, type(None)}
    json_value_args = set(get_args(JsonValue))
    assert {str, int, float, bool, type(None)}.issubset(json_value_args)
    assert list["JsonValue"] in json_value_args
    assert dict[str, "JsonValue"] in json_value_args
    assert get_args(JsonMap) == (str, JsonValue)


def test_event_exports_are_distinct():
    assert DeliveryEvent is not InternalDomainEvent
    assert InternalDomainEvent.__name__ == "InternalDomainEvent"


def test_delivery_event_schemas_match_design_doc():
    expected_fields = {
        DeliveryEventBase: {"room_id", "timestamp", "trace_id"},
        ProcessingStatusEvent: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "status",
            "agent_id",
            "details",
            "related_message_id",
            "client_request_id",
            "agents",
            "delivery_id",
        },
        RunEventNotification: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "event_id",
            "delivery_id",
            "run_id",
            "seq",
            "run_event_type",
            "payload",
            "correlation_id",
        },
        AgentMessagePartial: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "agent_id",
            "content_delta",
        },
        AgentMessageFinal: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "agent_id",
            "content",
            "delivery_id",
        },
        CancellationEvent: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "reason",
        },
        HITLRequestEvent: {
            "room_id",
            "run_id",
            "timestamp",
            "trace_id",
            "event_type",
            "request_id",
            "message_id",
            "source",
            "prompt",
            "prompt_type",
            "choices",
            "agent_id",
            "agent_name",
            "agent_label",
            "source_step_id",
            "interaction_id",
            "interaction_status",
            "interaction_version",
            "application_status",
            "question_count",
            "question_index",
            "related_message_id",
            "related_user_message_id",
            "client_request_id",
        },
        HITLResolvedEvent: {
            "room_id",
            "run_id",
            "timestamp",
            "trace_id",
            "event_type",
            "request_id",
            "message_id",
            "source",
            "status",
            "interaction_id",
            "interaction_status",
            "interaction_version",
            "application_status",
            "question_count",
            "question_index",
            "error_message",
            "answer_ref",
            "related_message_id",
            "related_user_message_id",
            "client_request_id",
        },
        TaskSubmittedEvent: {
            "room_id",
            "run_id",
            "opaque_public_call_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "task_id",
            "agent_name",
            "agent_id",
            "status",
            "related_message_id",
            "created_at",
            "step_number",
            "total_steps",
            "task_content",
            "client_request_id",
        },
        TaskUpdateEvent: {
            "room_id",
            "run_id",
            "opaque_public_call_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "status",
            "delivery_id",
            "content",
            "error",
            "requires_input",
            "requires_auth",
            "status_message",
            "agent_name",
            "agent_id",
            "related_message_id",
            "created_at",
            "step_number",
            "total_steps",
            "task_content",
            "parts",
            "client_request_id",
        },
        ArtifactUpdateEvent: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "agent_id",
            "artifact",
            "append",
            "last_chunk",
            "client_request_id",
        },
        ErrorEvent: {
            "room_id",
            "timestamp",
            "trace_id",
            "event_type",
            "error",
            "error_type",
            "message_id",
            "agent_id",
            "retry_after_seconds",
            "user_requests_used",
            "user_requests_limit",
            "system_requests_used",
            "system_requests_limit",
            "client_request_id",
        },
    }

    for dto, fields in expected_fields.items():
        assert set(dto.model_fields) == fields

    expected_required_fields = {
        DeliveryEventBase: {"room_id"},
        ProcessingStatusEvent: {"room_id", "message_id", "status"},
        RunEventNotification: {
            "room_id",
            "event_id",
            "run_id",
            "seq",
            "run_event_type",
        },
        AgentMessagePartial: {"room_id", "message_id", "agent_id", "content_delta"},
        AgentMessageFinal: {"room_id", "message_id", "agent_id"},
        CancellationEvent: {"room_id", "message_id"},
        HITLRequestEvent: {
            "room_id",
            "request_id",
            "prompt",
            "prompt_type",
            "source",
            "message_id",
        },
        HITLResolvedEvent: {"room_id", "request_id", "message_id", "source"},
        TaskSubmittedEvent: {"room_id", "message_id", "task_id", "agent_name"},
        TaskUpdateEvent: {"room_id", "message_id", "status"},
        ArtifactUpdateEvent: {"room_id", "message_id", "agent_id", "artifact"},
        ErrorEvent: {"room_id", "error"},
    }

    for dto, fields in expected_required_fields.items():
        required_fields = {
            name for name, field in dto.model_fields.items() if field.is_required()
        }
        assert required_fields == fields


def _public_protocol_methods(protocol):
    return {
        name
        for name, member in protocol.__dict__.items()
        if inspect.isfunction(member)
        and (
            not name.startswith("_") or name in {"__aenter__", "__aexit__", "__call__"}
        )
    }


def _assert_methods(protocol, expected):
    assert _public_protocol_methods(protocol) == set(expected)


def _assert_params(method, expected):
    assert list(inspect.signature(method).parameters) == expected


def test_protocol_methods_match_design_doc():
    from common import protocols

    expected_methods = {
        protocols.AgentRegistry: {
            "get_agent",
            "get_agent_card",
            "get_agent_by_url",
            "get_agents_by_ids",
            "is_agent_healthy",
            "is_directly_callable",
        },
        protocols.AgentMatcher: {"match_agents"},
        protocols.AgentMessageMatcher: {"match_for_message"},
        protocols.AgentExclusionReader: {"get_excluded_agent_ids"},
        protocols.AgentManagement: {
            "register_agent",
            "delete_agent",
            "update_agent",
            "list_agents",
            "list_public_agents",
        },
        protocols.AgentRegistryWriter: {
            "upsert_local_agent",
            "list_local_agent_ids",
            "mark_local_agents_inactive",
        },
        protocols.RoomRegistry: {"get_room", "get_room_agents", "get_room_owner"},
        protocols.RoomManagement: {
            "create_room",
            "delete_room",
            "update_room",
            "update_membership",
        },
        protocols.RoomMessageStore: {
            "save_user_message",
            "save_agent_message",
            "update_agent_message_status",
            "get_message",
        },
        protocols.RoomHistoryReader: {
            "get_messages_for_room",
            "get_messages_by_ids",
            "get_message_thread",
        },
        protocols.RoomOwnershipReader: {
            "get_room_owner",
            "verify_room_agent_membership",
        },
        protocols.ContextAssemblyPort: {
            "assemble_supervisor_context_from_memory",
            "assemble_agent_execution_context_from_memory",
        },
        protocols.MemorySearchPort: {"search_memory"},
        protocols.ProjectionPort: {
            "project_message_for_event",
            "run_compaction",
        },
        protocols.CompactionPort: {
            "should_compact",
            "compact_if_needed",
            "compact_room_memory",
        },
        protocols.RoomMemoryCleanupPort: {"delete_room_memory"},
        protocols.ExecutionEngine: {
            "execute",
            "start_orchestration",
            "schedule_orchestration",
            "cancel",
            "get_run",
            "get_runs_for_room",
            "get_latest_runs_for_rooms",
            "cancel_inflight_tasks",
            "heal_diverged_runs",
        },
        protocols.HITLManager: {
            "resolve_hitl_batch",
            "get_pending_hitl",
            "cancel_hitl_interaction",
        },
        protocols.A2ATaskStatusReader: {
            "get_room_agent_message_by_message_id",
            "get_task_messages_for_room",
            "get_pending_task_messages_for_user",
        },
        protocols.RoomRouteReader: {"get_room_by_room_id"},
        protocols.SSEStateReader: {"get_room_user_message_by_message_id"},
        protocols.EventPublisher: {"emit"},
        protocols.SSETransport: {
            "connect",
            "disconnect",
            "broadcast_frame_to_room",
            "set_draining",
        },
        protocols.SSEConnectionLike: {"get_message"},
        protocols.SSERouteTransport: {
            "add_connection",
            "remove_connection",
            "get_room_status",
        },
        protocols.WebhookReceiver: {"authenticate_webhook", "handle_webhook"},
        protocols.RoomDistributedLock: {"acquire", "renew", "release"},
        protocols.RoomMembershipSeedSource: {
            "get_saved_group",
            "list_current_agents",
        },
        protocols.APIKeyStore: {
            "add_api_key",
            "deactivate_api_key",
            "get_api_key_by_id",
            "get_api_keys_by_user",
        },
        protocols.APIKeyValidationStore: {
            "get_api_key_by_hash",
            "update_api_key_usage",
        },
        protocols.APIKeyRateLimiter: {"check_rate_limit", "record_request"},
        protocols.AttachmentCleanupPort: {"delete_for_room"},
        protocols.AttachmentMetadataReader: {"get_for_room_file"},
        protocols.AttachmentContentReader: {"get_bytes"},
        protocols.EmbeddingServiceProtocol: {"get_embedding"},
        protocols.RequiredEmbeddingServiceProtocol: {"get_embedding"},
        protocols.ModelSelectableEmbeddingServiceProtocol: {"get_embedding"},
        protocols.LLMTextGateway: {"generate"},
        protocols.LLMStructuredGateway: {"generate_structured"},
        protocols.LLMEmbeddingGateway: {"embed", "embed_batch"},
        protocols.LLMStreamGateway: {"generate_stream"},
        protocols.LLMStreamingProvider: {"generate_stream"},
        protocols.LLMProviderAdapter: {
            "embed",
            "embed_batch",
            "generate",
            "generate_structured",
        },
        protocols.QuoteRepository: {
            "delete_by_id",
            "delete_for_room",
            "get_by_id",
            "insert",
        },
        protocols.RuntimeAgentRoomStore: {
            "add_agent_group",
            "delete_agent_group",
            "get_agent_by_agent_id",
            "get_agent_group_by_id",
            "get_agent_groups_by_owner",
            "get_agent_name_by_agent_id",
            "get_agents_with_conditions",
            "get_all_active_agents",
            "get_room_by_room_id",
            "get_rooms_by_room_owner_id",
            "increment_agent_call_count",
            "update_agent_group",
            "update_room_by_room_id",
        },
        protocols.RuntimeHITLLifecycleStore: {
            "attach_interaction_request",
            "claim_interaction_application",
            "claim_run_answer_projection",
            "claim_resume_command",
            "create_resume_command",
            "ensure_hitl_lifecycle_indexes",
            "get_interaction",
            "get_interaction_for_request_strict",
            "get_interaction_strict",
            "get_resume_command_for_interaction_strict",
            "get_resume_command_strict",
            "iter_active_interactions",
            "iter_due_interactions",
            "iter_due_resume_commands",
            "iter_materializing_interactions",
            "iter_stale_applications",
            "iter_unreconciled_terminal_interactions",
            "iter_unreconciled_terminal_requests",
            "mark_interaction_application_state",
            "mark_interaction_terminal_reconciled",
            "mark_resume_command_aggregate_applied",
            "mark_resume_command_state",
            "mark_run_answer_projection",
            "materialize_interaction",
            "record_interaction_answer",
            "reclaim_stale_resume_command",
            "renew_interaction_application",
            "renew_resume_command",
            "renew_run_answer_projection",
            "resume_uncertain_interaction",
            "terminalize_interaction",
        },
        protocols.RuntimeHITLStore: {
            "cas_update_hitl_request",
            "cas_update_hitl_request_strict",
            "claim_hitl_open_projection",
            "claim_hitl_request",
            "count_hitl_requests_for_message",
            "complete_hitl_open_projection",
            "create_or_reuse_pending_hitl_request",
            "create_hitl_request",
            "ensure_hitl_indexes",
            "fenced_update_hitl_request",
            "find_pending_hitl_request_for_agent_message",
            "get_hitl_request",
            "get_pending_hitl_requests",
            "get_pending_hitl_requests_strict",
            "get_pending_hitl_requests_for_message",
            "get_pending_hitl_requests_for_message_strict",
            "iter_stale_processing_hitl_requests",
            "persist_hitl_request_id_on_message",
            "persist_pending_hitl_on_agent_message",
            "persist_hitl_interaction_metadata",
            "persist_hitl_user_answer",
            "release_hitl_open_projection",
            "update_agent_message_task_state",
            "update_hitl_request",
        },
        protocols.RuntimeMemoryStore: {
            "get_room_memory_by_room_id",
            "update_turn_notes",
        },
        protocols.RuntimeMessageStore: {
            "accumulate_artifact_on_message",
            "add_room_agent_message",
            "add_room_user_message",
            "cancel_agent_messages_by_ids",
            "cancel_descendants",
            "project_descendant_terminal_state",
            "claim_or_reclaim_user_message",
            "claim_user_message_for_processing",
            "delete_room_agent_message_by_message_id",
            "get_room_agent_message_by_message_id",
            "get_room_agent_messages_by_related_message_id",
            "get_room_agent_messages_by_related_message_id_strict",
            "get_room_agent_messages_by_room_id",
            "get_room_user_message_by_message_id",
            "get_room_user_message_by_message_id_strict",
            "get_room_user_messages_by_room_id",
            "get_stale_claimed_orchestration_messages",
            "refresh_processing_claim",
            "reset_last_notified_state",
            "set_system_task_terminal_state",
            "set_turn_completion_kind",
            "turn_exists",
            "unclaim_user_message",
            "update_last_notified_state",
            "update_orchestration_projection_if_status",
            "update_room_agent_message_by_message_id",
            "update_room_agent_message_with_new_message_content_by_message_id",
            "update_room_user_message_by_message_id",
            "update_task_state_on_message",
            "update_task_state_on_message_if_not_terminal",
            "upsert_room_agent_message",
        },
        protocols.RuntimeTaskLifecycleStore: {
            "check_task_limits",
            "enable_task_tracking_on_message",
            "find_stale_non_terminal_runs",
            "generate_webhook_token",
            "get_active_runs_by_room_id",
            "get_and_clear_continuation_on_message",
            "get_and_clear_continuation_on_user_message",
            "get_expired_task_messages",
            "get_non_tracked_stale_task_messages",
            "get_orphaned_agent_messages",
            "get_pending_continuation_on_message",
            "get_pending_task_messages_for_user",
            "get_room_ids_with_non_terminal_runs",
            "get_stale_task_messages",
            "get_task_messages_for_room",
            "hash_webhook_token",
            "is_message_cancelled",
            "is_message_cancelled_strict",
            "resolve_client_request_id_for_agent_message",
            "resolve_client_request_id_for_message_id",
            "save_continuation_on_message",
            "save_continuation_on_user_message",
            "touch_task_message",
            "update_task_on_message",
            "update_webhook_token_hash_on_message",
            "verify_webhook_token",
            "verify_webhook_token_for_task",
            "verify_webhook_token_on_message",
        },
        protocols.AgentCallCounter: {"increment_agent_call_count"},
        protocols.MessageCancellationReader: {"is_message_cancelled"},
        protocols.GatewayService: {
            "discover_agents",
            "get_agent_card",
            "prepare_stream",
            "send_message",
            "stream_message",
        },
        protocols.GatewayDiscoveryProvider: {"discover_agents"},
        protocols.RateLimiter: {"check", "check_global"},
        protocols.FileStorage: {
            "upload",
            "get_url",
            "delete",
            "list_for_room",
            "get_ready_file",
            "prepare_download",
            "stream",
        },
        protocols.PreparedFileStream: {"aclose"},
        protocols.AgentTransport: {"send_message", "stream_message"},
        protocols.APIKeyPrincipal: set(),
        protocols.APIKeyAuthenticator: {"validate_api_key"},
        protocols.AgentCardResolver: {
            "resolve_card",
            "supports_push_notifications",
            "supports_streaming",
        },
        protocols.LLMProvider: {
            "generate",
            "generate_structured",
            "embed",
            "embed_batch",
        },
        protocols.ModelRegistry: {
            "get_model",
            "supports_capability",
            "list_models",
        },
        protocols.MongoDAL: {"collection", "connect", "close", "ping"},
        protocols.MongoCollection: {
            "find_one",
            "find",
            "find_one_and_update",
            "insert_one",
            "insert_many",
            "update_one",
            "replace_one",
            "update_many",
            "delete_one",
            "delete_many",
            "count",
            "aggregate",
            "create_index",
            "create_indexes",
            "index_information",
            "drop_index",
            "bulk_write",
            "distinct",
            "find_one_by_stable_or_native_id",
            "watch",
        },
        protocols.RedisKV: {
            "get",
            "set",
            "delete",
            "compare_delete",
            "compare_set",
            "increment",
            "setnx",
            "exists",
            "ping",
            "close",
        },
        protocols.RedisPubSub: {"publish", "subscribe", "ping", "close"},
        protocols.RedisStreams: {"xadd", "xread", "ping", "close"},
        protocols.DistributedLock: {"acquire", "release", "renew"},
        protocols.LeaderElector: {"try_acquire", "renew", "release", "release_all"},
        protocols.IndexRegistry: {"register", "ensure_all"},
        protocols.AgentRepository: {
            "get_by_id",
            "get_by_ids",
            "get_by_provider",
            "get_by_source",
            "get_public",
            "upsert",
            "delete",
            "update_health",
            "mark_agents_inactive",
            "increment_agent_call_count",
            "find_by_normalized_url",
            "list_visible",
            "update",
            "public_url_exists",
            "activate_agents",
            "text_search",
        },
        protocols.RoomRepository: {
            "get_by_id",
            "get_by_owner",
            "get_history_by_owner",
            "create",
            "update",
            "update_fields",
            "touch_activity",
            "set_membership",
            "delete",
        },
        protocols.MessageRepository: {
            "save_user_message",
            "get_user_message_by_idempotency_key",
            "insert_user_message_idempotently",
            "save_agent_message",
            "update_user_message",
            "update_agent_message",
            "update_agent_message_if_not_terminal",
            "count_agent_messages",
            "get_user_message_by_id",
            "get_agent_message_by_id",
            "is_message_cancelled",
            "get_by_id",
            "get_by_ids",
            "get_for_room",
            "get_timeline_page",
            "get_thread",
            "update_status",
            "delete_for_room",
            "get_user_messages_for_room",
            "get_agent_messages_for_room",
            "get_agent_messages_by_related_message_id",
            "get_agent_task_messages_for_user",
            "get_task_messages_for_room",
            "get_pending_task_messages_for_user",
        },
        protocols.RunRepository: {
            "find",
            "find_one",
            "create",
            "get_by_id",
            "get_for_room",
            "get_latest_for_rooms",
            "insert_one",
            "update_one",
            "update_state",
            "get_diverged",
        },
        protocols.RunEventRepository: {
            "find",
            "find_one",
            "find_one_and_update",
            "insert_one",
            "update_one",
            "append",
            "get_for_run",
            "get_latest",
        },
        protocols.HITLRepository: {
            "create",
            "get_by_id",
            "get_pending_for_room",
            "resolve",
        },
        protocols.MemoryRepository: {
            "get_room_memory",
            "upsert_room_memory",
            "delete_room_memory",
            "create_room_memory",
            "ensure_room_memory",
            "push_and_trim_conversation_turn",
            "push_and_trim_conversation_turn_if_absent",
            "update_turn_notes",
            "get_room_summary_projection",
            "update_room_summary_atomic",
            "compact_turns_bulk",
            "list_room_ids_with_memory",
        },
        protocols.ContentStorageRepository: {
            "upsert_full_content",
            "get_content_by_document_id",
            "get_content_by_turn_id",
            "delete_content_by_turn_id",
            "delete_content_by_room_id",
            "get_content_stats_for_room",
            "text_search",
            "scan_text_search",
            "hydrate_turn_content",
        },
    }

    for protocol, methods in expected_methods.items():
        _assert_methods(protocol, methods)

    assert not hasattr(protocols, "RoomActiveRunReader")

    protocol_exports = {
        getattr(protocols, name)
        for name in protocols.__all__
        if inspect.isclass(getattr(protocols, name))
        and getattr(getattr(protocols, name), "_is_protocol", False)
    }
    marker_protocols = {
        protocols.APIKeyPrincipal,
        protocols.APIKeyRecord,
        protocols.A2ATaskStatusMessage,
        protocols.HealthCheck,
        protocols.LLMGateway,
        protocols.MongoChangeStream,
        protocols.RoomRouteRecord,
        protocols.SSEUserMessageRecord,
    }
    missing_coverage = protocol_exports - set(expected_methods) - marker_protocols
    assert not missing_coverage, {protocol.__name__ for protocol in missing_coverage}

    assert not hasattr(protocols, "CrudRepository")
    assert not hasattr(protocols, "TaskRepository")
    _assert_params(
        protocols.AgentMatcher.match_agents,
        [
            "self",
            "query",
            "limit",
            "filter_ids",
            "respect_visibility",
            "requesting_user_id",
        ],
    )
    _assert_params(
        protocols.AgentMessageMatcher.match_for_message,
        [
            "self",
            "query",
            "limit",
            "filter_ids",
            "requesting_user_id",
            "required_input_modes",
        ],
    )
    _assert_params(protocols.RoomManagement.create_room, ["self", "request"])
    _assert_params(
        protocols.ExecutionEngine.cancel,
        ["self", "room_id", "message_id", "requested_by_user_id"],
    )
    _assert_params(protocols.SSEConnectionLike.get_message, ["self", "timeout"])
    _assert_params(
        protocols.SSERouteTransport.add_connection,
        ["self", "room_id"],
    )
    _assert_params(
        protocols.SSERouteTransport.remove_connection,
        ["self", "room_id", "connection_id"],
    )
    _assert_params(
        protocols.SSERouteTransport.get_room_status,
        ["self", "room_id"],
    )
    _assert_params(
        protocols.WebhookReceiver.authenticate_webhook,
        ["self", "message_id", "token"],
    )
    _assert_params(
        protocols.WebhookReceiver.handle_webhook,
        ["self", "message_id", "payload", "token"],
    )
    _assert_params(protocols.MongoCollection.find, ["self", "query", "kwargs"])
    _assert_params(protocols.DistributedLock.acquire, ["self", "key", "owner", "ttl"])


def test_run_state_contract_matches_persisted_values():
    persisted_values = {
        "queued",
        "processing",
        "awaiting_input",
        "completed",
        "failed",
        "canceled",
    }

    assert {state.value for state in RunState} == persisted_values
    RunInfo(run_id="run1", room_id="r1", state=RunState.PROCESSING)


def test_settings_class_loads_from_env(monkeypatch):
    monkeypatch.setenv("MONGODB_DB_NAME", "common_foundation_test_db")
    from common.config.settings import Settings

    settings = Settings()

    assert settings.mongodb_db_name == "common_foundation_test_db"


def test_common_settings_package_exports_settings_singleton():
    from common.config import settings as common_settings
    from common.config.settings import settings as exported_settings

    assert exported_settings is common_settings


def test_error_hierarchy():
    err = NotFoundError("Agent", "a1")

    assert isinstance(err, AppError)
    assert err.code == "NOT_FOUND"
    assert err.details["entity_type"] == "Agent"

    validation = ValidationError("Invalid input", details={"field": "name"})
    assert str(validation) == "Invalid input"
    assert validation.details == {"field": "name"}
