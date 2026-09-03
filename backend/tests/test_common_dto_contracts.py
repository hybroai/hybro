from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from common.dto import (
    AgentCardSnapshot,
    AgentEvent,
    AgentInfo,
    AgentMessageFinal,
    AgentMessagePartial,
    AgentRegistered,
    ArtifactUpdateEvent,
    CancellationEvent,
    CompactionResult,
    ContextBlock,
    CreateRoomRequest,
    DeliveryEnvelope,
    EmbeddingResult,
    ErrorEvent,
    ExecutionAck,
    ExecutionRequest,
    ExecutionResult,
    FileMetadata,
    HITLRequest,
    HITLRequestEvent,
    HITLResolvedEvent,
    HITLResponse,
    InternalDomainEvent,
    LLMRequest,
    LLMResponse,
    MembershipSeed,
    MembershipUpdateRequest,
    MemorySearchResult,
    MessageRecord,
    ModelInfo,
    NotificationPayload,
    PaginationParams,
    ProcessingStatusEvent,
    QueryFilter,
    RoomCreated,
    RoomCreationParams,
    RoomInfo,
    RoomMembership,
    RoomSummary,
    RunEventNotification,
    RunInfo,
    RunState,
    SavedUserMessage,
    SortOrder,
    SSEEvent,
    TaskSubmittedEvent,
    TaskUpdateEvent,
    WorkflowState,
)


def test_common_dtos_can_be_instantiated():
    now = datetime.now(UTC)

    AgentInfo(agent_id="a1", name="Agent", status="active")
    AgentCardSnapshot(agent_id="a1", url="http://agent", name="Agent", raw_card={})
    RoomSummary(
        room_id="r1",
        room_name="Room",
        owner_id="u1",
        owner_name="User",
        created_at=now,
    )
    RoomMembership(room_id="r1", agent_ids=["a1"])
    MessageRecord(
        room_id="r1",
        message_id="m1",
        message_type="user",
        content={},
        created_at=now,
    )
    seed = MembershipSeed(mode="manual", agent_ids=["a1"])
    MembershipUpdateRequest(add_agent_ids=["a2"], remove_agent_ids=["a1"])
    RoomInfo(
        room_id="r1",
        room_name="Room",
        owner_id="u1",
        owner_name="User",
        agent_ids=["a1"],
        created_at=now,
    )
    CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=seed,
    )
    RoomCreationParams(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=seed,
    )
    SavedUserMessage(
        room_id="r1",
        message_id="m1",
        user_id="u1",
        user_name="User",
        message={},
    )
    ExecutionRequest(
        room_id="r1", message_text="hello", sender_id="u1", sender_name="User"
    )
    ExecutionAck(
        room_id="r1",
        message_id="m1",
        user_id="u1",
        user_name="User",
        message={},
    )
    ExecutionResult(success=True)
    WorkflowState(run_id="run1", room_id="r1", state="queued", updated_at=now)
    RunInfo(run_id="run1", room_id="r1", state=RunState.PROCESSING)
    HITLRequest(
        request_id="hitl1",
        room_id="r1",
        user_message_id="m1",
        prompt="Continue?",
        source="agent",
    )
    HITLResponse(request_id="hitl1", response_text="yes", responder_id="u1")
    AgentEvent(room_id="r1", agent_id="a1", message_id="m1", event_type="partial")
    ContextBlock(block_id="b1", room_id="r1", content="context", token_count=3)
    CompactionResult(room_id="r1", compacted_count=1, tokens_saved=10)
    MemorySearchResult(
        room_id="r1",
        content="memory",
        keyword_score=0.5,
        relevance_score=0.5,
        temporal_decay_factor=1.0,
    )
    DeliveryEnvelope(room_id="r1", event_type="processing_status", payload={})
    SSEEvent(event="message", data={})
    ProcessingStatusEvent(room_id="r1", message_id="m1", status="processing")
    RunEventNotification(
        room_id="r1",
        event_id="e1",
        run_id="run1",
        seq=1,
        run_event_type="agent_started",
    )
    AgentMessagePartial(
        room_id="r1",
        message_id="m1",
        agent_id="a1",
        content_delta="hello",
    )
    AgentMessageFinal(
        room_id="r1",
        message_id="m1",
        agent_id="a1",
        content={"text": "done"},
    )
    CancellationEvent(room_id="r1", message_id="m1")
    HITLRequestEvent(
        room_id="r1",
        request_id="h1",
        prompt="Continue?",
        prompt_type="text",
        source="agent",
        message_id="m1",
    )
    HITLResolvedEvent(room_id="r1", request_id="h1", message_id="m1", source="agent")
    TaskSubmittedEvent(
        room_id="r1",
        message_id="m1",
        task_id="t1",
        agent_name="Agent",
    )
    TaskUpdateEvent(room_id="r1", message_id="m1", status="working")
    ArtifactUpdateEvent(room_id="r1", message_id="m1", agent_id="a1", artifact={})
    ErrorEvent(room_id="r1", error="failed")
    NotificationPayload(room_id="r1", message="notice")
    LLMRequest(messages=[{"role": "user", "content": "hi"}])
    LLMResponse(content="ok", model="test")
    EmbeddingResult(text="hi", embedding=[0.1])
    ModelInfo(
        model_id="m1",
        logical_name="test",
        provider="openai",
        capabilities=[],
        max_context_tokens=1,
    )
    FileMetadata(
        file_id="f1",
        room_id="r1",
        owner_id="u1",
        source="user_upload",
        mime_type="text/plain",
        file_name="x.txt",
        size_bytes=1,
        sha256="0" * 64,
        status="ready",
    )
    QueryFilter(criteria={"room_id": "r1"})
    PaginationParams(page=1, limit=10)
    SortOrder(field="created_at", direction="desc")
    InternalDomainEvent(timestamp=now)
    AgentRegistered(agent_id="a1", timestamp=now)
    RoomCreated(room_id="r1", owner_id="u1", timestamp=now)


def test_removed_gateway_discovery_and_rate_limit_dtos_are_not_exported():
    import common.dto as dto

    removed_names = {
        "GatewayDiscoveryAgentResult",
        "GatewayDiscoveryResponse",
        "GatewayRequest",
        "GatewayResponse",
        "GatewayRoute",
        "RateLimitInfo",
        "RateLimitResult",
    }

    assert removed_names.isdisjoint(dto.__all__)
    assert removed_names.isdisjoint(vars(dto))
    assert dto.FileInfo
    assert dto.FileMetadata


def test_room_creation_params_default_seed_does_not_weaken_create_request():
    params = RoomCreationParams(owner_id="u1", owner_name="User", room_name="Room")

    assert isinstance(params, CreateRoomRequest)
    assert params.membership_seed == MembershipSeed(mode="manual")
    assert "membership_seed" in params.model_dump(exclude_unset=True)
    with pytest.raises(PydanticValidationError) as exc_info:
        CreateRoomRequest(owner_id="u1", owner_name="User", room_name="Room")
    assert exc_info.value.errors()[0]["loc"] == ("membership_seed",)


def test_room_creation_params_compare_like_create_request_payloads():
    seed = MembershipSeed(mode="manual")
    params = RoomCreationParams(
        owner_id="u1", owner_name="User", room_name="Room", membership_seed=seed
    )
    request = CreateRoomRequest(
        owner_id="u1", owner_name="User", room_name="Room", membership_seed=seed
    )

    assert params == request
    assert request == params
    with pytest.raises(TypeError):
        hash(params)
    with pytest.raises(TypeError):
        hash(request)


def test_room_creation_params_and_create_request_export_room_creation_contracts():
    seed = MembershipSeed(mode="manual")
    request = CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=seed,
    )
    params = RoomCreationParams(owner_id="u1", owner_name="User", room_name="Room")

    assert request.membership_seed == seed
    assert params.membership_seed == MembershipSeed(mode="manual")
    assert "membership_seed" in CreateRoomRequest.model_fields
    assert "membership_seed" in RoomCreationParams.model_fields


def test_create_room_request_is_unhashable_to_avoid_any_payload_hash_contract():
    request = CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=MembershipSeed(mode="manual"),
        extend_info={"a": 1},
    )

    with pytest.raises(TypeError):
        hash(request)


def test_create_room_request_payloads_are_frozen():
    request = CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=MembershipSeed(mode="manual"),
        extend_info={"a": 1},
    )

    assert type(request.extend_info).__name__ == "FrozenDict"
    with pytest.raises(TypeError):
        request.extend_info["a"] = 2


def test_frozen_containers_support_deepcopy_without_losing_immutability():
    """Deepcopy must not raise through FrozenDict/FrozenList mutators."""
    from copy import deepcopy

    from common.dto.base import FrozenDict, FrozenList

    frozen_dict = FrozenDict({"nested": FrozenList([1, 2])})
    copied = deepcopy(frozen_dict)
    assert copied == frozen_dict
    assert isinstance(copied, FrozenDict)
    assert isinstance(copied["nested"], FrozenList)
    assert copied is not frozen_dict
    assert copied["nested"] is not frozen_dict["nested"]
    with pytest.raises(TypeError):
        copied["nested"].append(3)


def test_frozen_dto_deepcopy_is_isolated_and_still_immutable():
    """Frozen DTO deep copies must succeed (pydantic model_copy(deep=True))
    and produce an equal, independent, still-immutable instance."""
    from copy import deepcopy

    request = CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=MembershipSeed(mode="manual"),
        extend_info={"a": 1},
    )
    copied = deepcopy(request)
    assert copied is not request
    assert copied == request
    assert type(copied.extend_info).__name__ == "FrozenDict"
    with pytest.raises(TypeError):
        copied.extend_info["a"] = 2
    with pytest.raises(PydanticValidationError):
        copied.owner_id = "u2"


def test_frozen_dto_model_copy_deep_true_does_not_raise():
    """pydantic invokes __deepcopy__() without a memo argument; deep copies of
    FrozenDTOs must keep working through model_copy(deep=True)."""
    request = CreateRoomRequest(
        owner_id="u1",
        owner_name="User",
        room_name="Room",
        membership_seed=MembershipSeed(mode="manual"),
        extend_info={"a": 1},
    )

    copied = request.model_copy(deep=True)

    assert copied is not request
    assert copied == request
    assert type(copied.extend_info).__name__ == "FrozenDict"
    with pytest.raises(TypeError):
        copied.extend_info["a"] = 2
