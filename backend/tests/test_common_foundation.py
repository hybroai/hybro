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
    delivery = AgentInfo(agent_id="a2", raw_card={"x": 1})

    with pytest.raises(TypeError):
        agent.capabilities += ("write",)

    with pytest.raises(TypeError):
        delivery.raw_card["x"] = 2

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert '"capabilities":["search"]' in agent.model_dump_json()
        assert '"raw_card":{"x":1}' in delivery.model_dump_json()


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


def test_agent_card_defaults_missing_skills_to_empty_list():
    """ProtoJSON omits an empty ``skills`` list from stored agent cards."""
    from common.types import AgentCard

    card = AgentCard.model_validate(
        {
            "name": "agent",
            "url": "https://agent.example",
            "version": "1.0.0",
            "capabilities": {},
        }
    )

    assert card.skills == []


def test_agent_model_loads_stored_card_without_skills():
    from models.agent import Agent, AgentStatus

    agent = Agent.model_validate(
        {
            "agent_id": "agent-without-skills",
            "agent_card": {
                "name": "agent",
                "description": "desc",
                "url": "https://agent.example",
                "version": "1.0.0",
                "capabilities": {"streaming": True},
                "defaultInputModes": ["text"],
                "defaultOutputModes": ["text"],
            },
            "agent_status": "active",
            "is_public": True,
        }
    )

    assert agent.agent_card.skills == []
    assert agent.agent_status is AgentStatus.active


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
    base = ProcessingStatusEvent(
        conversation_id="room-1",
        message_id="msg-1",
        status="processing",
        trace_id="trace-2",
    )
    run_event = RunEventNotification(
        conversation_id="room-1",
        event_id="evt-1",
        run_id="run-1",
        seq=1,
        run_event_type="turn_start",
        payload={"internal_turn_id": "turn-1", "attempt": 1},
        correlation_id="cr-1",
        trace_id="trace-3",
    )

    assert base.trace_id == "trace-2"
    assert run_event.correlation_id == "cr-1"
    assert run_event.trace_id == "trace-3"


def test_room_info_defaults_to_manual_membership_status():
    room = RoomInfo(conversation_id="r1", room_name="Room", owner_id="u1")

    assert room.membership_origin_status == "manual"


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
        DeliveryEventBase: {"conversation_id", "timestamp", "trace_id"},
        ProcessingStatusEvent: {
            "conversation_id",
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
            "conversation_id",
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
            "conversation_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "agent_id",
            "content_delta",
        },
        AgentMessageFinal: {
            "conversation_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "agent_id",
            "content",
            "delivery_id",
        },
        CancellationEvent: {
            "conversation_id",
            "timestamp",
            "trace_id",
            "event_type",
            "message_id",
            "reason",
        },
        HITLRequestEvent: {
            "conversation_id",
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
            "header",
            "required",
            "allow_other",
            "choice_options",
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
            "conversation_id",
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
            "answer_text",
            "related_message_id",
            "related_user_message_id",
            "client_request_id",
        },
        TaskSubmittedEvent: {
            "conversation_id",
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
            "conversation_id",
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
            "conversation_id",
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
            "conversation_id",
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
        DeliveryEventBase: {"conversation_id"},
        ProcessingStatusEvent: {"conversation_id", "message_id", "status"},
        RunEventNotification: {
            "conversation_id",
            "event_id",
            "run_id",
            "seq",
            "run_event_type",
            "payload",
        },
        AgentMessagePartial: {
            "conversation_id",
            "message_id",
            "agent_id",
            "content_delta",
        },
        AgentMessageFinal: {"conversation_id", "message_id", "agent_id"},
        CancellationEvent: {"conversation_id", "message_id"},
        HITLRequestEvent: {
            "conversation_id",
            "request_id",
            "prompt",
            "prompt_type",
            "source",
            "message_id",
        },
        HITLResolvedEvent: {"conversation_id", "request_id", "message_id", "source"},
        TaskSubmittedEvent: {"conversation_id", "message_id", "task_id", "agent_name"},
        TaskUpdateEvent: {"conversation_id", "message_id", "status"},
        ArtifactUpdateEvent: {"conversation_id", "message_id", "agent_id", "artifact"},
        ErrorEvent: {"conversation_id", "error"},
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
            "list_active_agents",
            "list_visible_agents",
            "is_agent_healthy",
            "is_directly_callable",
        },
        protocols.AgentMatcher: {"match_agents"},
        protocols.AgentNetworkTargetResolver: {"resolve_target"},
        protocols.AgentExclusionReader: {"get_excluded_agent_ids"},
        protocols.RoomHistoryReader: {
            "get_messages_for_room",
            "get_messages_by_ids",
            "get_message_thread",
        },
        protocols.RoomOwnershipReader: {
            "get_room_owner",
            "verify_room_agent_membership",
        },
        protocols.ExecutionEngine: {
            "execute",
            "start_orchestration",
            "schedule_orchestration",
            "is_orchestration_pending",
            "cancel",
            "get_runs_for_room",
            "get_latest_runs_for_rooms",
            "cancel_inflight_tasks",
        },
        protocols.HITLManager: {
            "resolve_hitl_batch",
            "get_pending_hitl",
            "cancel_hitl_interaction",
        },
        protocols.RoomRouteReader: {"get_room_by_room_id"},
        protocols.SSEStateReader: {"get_room_user_message_by_message_id"},
        protocols.EventPublisher: {"emit"},
        protocols.SSEConnectionLike: {"get_message"},
        protocols.SSERouteTransport: {
            "add_connection",
            "remove_connection",
            "get_room_status",
        },
        protocols.RoomDistributedLock: {"acquire", "renew", "release"},
        protocols.LLMTextGateway: {"generate"},
        protocols.LLMStructuredGateway: {"generate_structured"},
        protocols.LLMEmbeddingGateway: {"embed", "embed_batch"},
        protocols.LLMStreamGateway: {"generate_stream"},
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
        protocols.AgentCardResolver: {
            "resolve_card",
            "supports_push_notifications",
            "supports_streaming",
        },
        protocols.MongoDAL: {
            "collection",
            "connect",
            "close",
            "ping",
            "supports_transactions",
        },
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
        protocols.LeaderElector: {"try_acquire", "renew", "release", "release_all"},
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
    assert not hasattr(protocols, "A2ATaskStatusReader")
    assert not hasattr(protocols, "A2ATaskStatusMessage")
    for removed_name in (
        "APIKeyAuthenticator",
        "APIKeyPrincipal",
        "APIKeyRateLimiter",
        "APIKeyRecord",
        "APIKeyStore",
        "APIKeyValidationStore",
        "GatewayDiscoveryProvider",
        "GatewayService",
        "RateLimiter",
    ):
        assert not hasattr(protocols, removed_name)
    _assert_params(
        protocols.AgentRegistry.list_visible_agents,
        ["self", "user_id", "active_only", "query", "limit"],
    )
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
        protocols.ExecutionEngine.cancel,
        ["self", "conversation_id", "message_id", "requested_by_user_id"],
    )
    _assert_params(protocols.SSEConnectionLike.get_message, ["self", "timeout"])
    _assert_params(
        protocols.SSERouteTransport.add_connection,
        ["self", "conversation_id"],
    )
    _assert_params(
        protocols.SSERouteTransport.remove_connection,
        ["self", "conversation_id", "connection_id"],
    )
    _assert_params(
        protocols.SSERouteTransport.get_room_status,
        ["self", "conversation_id"],
    )
    _assert_params(protocols.MongoCollection.find, ["self", "query", "kwargs"])


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
    RunInfo(run_id="run1", conversation_id="r1", state=RunState.PROCESSING)


def test_settings_class_uses_explicit_values_not_environment(monkeypatch):
    monkeypatch.setenv("MONGODB_DB_NAME", "common_foundation_test_db")
    from common.config.loader import Settings

    assert Settings().mongodb_db_name == "hybro"
    settings = Settings(mongodb_db_name="common_foundation_test_db")
    assert settings.mongodb_db_name == "common_foundation_test_db"


def test_common_settings_package_exports_settings_singleton():
    from common.config import settings as common_settings
    from common.config.loader import settings as exported_settings

    assert exported_settings is common_settings


def test_error_hierarchy():
    err = NotFoundError("Agent", "a1")

    assert isinstance(err, AppError)
    assert err.code == "NOT_FOUND"
    assert err.details["entity_type"] == "Agent"

    validation = ValidationError("Invalid input", details={"field": "name"})
    assert str(validation) == "Invalid input"
    assert validation.details == {"field": "name"}
