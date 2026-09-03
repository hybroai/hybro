"""Gateway-owned FastAPI dependency context."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from fastapi import Depends, Request

from agent.protocols import (
    AgentCapabilityIssueStore,
    AgentCenterCompatibility,
    AgentGroupStoreCompatibility,
    AgentInspection,
    AgentLivenessChecker,
    AgentSuggestionService,
)
from common.protocols import (
    AgentRegistry,
    ExecutionEngine,
    FileStorage,
    HITLManager,
    RoomOwnershipReader,
    RoomRouteReader,
    SSERouteTransport,
    SSEStateReader,
)
from local_agents.protocols import LocalAgentDiscovery
from room.protocols import RoomCenterCompatibility


@dataclass(frozen=True, slots=True)
class APIGatewayDeps:
    agent_center: AgentCenterCompatibility
    agent_service: AgentRegistry
    capability_issue_service: AgentCapabilityIssueStore
    agent_liveness_checker: AgentLivenessChecker
    agent_group_store: AgentGroupStoreCompatibility
    file_storage: FileStorage
    room_ownership_reader: RoomOwnershipReader
    hitl_manager: HITLManager
    inspection_center: AgentInspection
    room_center: RoomCenterCompatibility
    room_store: RoomRouteReader
    agent_selection_service: AgentSuggestionService
    execution_engine: ExecutionEngine
    sse_store: SSEStateReader
    sse_transport: SSERouteTransport
    local_agent_discovery: LocalAgentDiscovery | None = None


def missing_required_deps(deps: APIGatewayDeps | None) -> list[str]:
    if deps is None:
        return ["app.state.api_gateway_deps"]

    optional_fields = {"local_agent_discovery"}

    return [
        field.name
        for field in fields(APIGatewayDeps)
        if getattr(deps, field.name) is None and field.name not in optional_fields
    ]


def bind_api_gateway_deps(app: Any, deps: APIGatewayDeps) -> None:
    missing = missing_required_deps(deps)
    if missing:
        raise RuntimeError("APIGatewayDeps incomplete - missing: " + ", ".join(missing))

    app.state.api_gateway_deps = deps


def get_api_gateway_deps(request: Request) -> APIGatewayDeps:
    deps = getattr(request.app.state, "api_gateway_deps", None)
    if deps is None:
        raise RuntimeError("APIGatewayDeps not bound - startup incomplete")
    return deps


_API_GATEWAY_DEPS_DEPENDENCY = Depends(get_api_gateway_deps)


def is_bound(app: Any) -> bool:
    deps = getattr(app.state, "api_gateway_deps", None)
    return deps is not None and not missing_required_deps(deps)


def get_agent_center(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentCenterCompatibility:
    return deps.agent_center


def get_agent_service(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentRegistry:
    return deps.agent_service


def get_capability_issue_service(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentCapabilityIssueStore:
    return deps.capability_issue_service


def get_agent_liveness_checker(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentLivenessChecker:
    return deps.agent_liveness_checker


def get_agent_group_store(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentGroupStoreCompatibility:
    return deps.agent_group_store


def get_local_agent_discovery(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> LocalAgentDiscovery | None:
    return deps.local_agent_discovery


def get_file_storage(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> FileStorage:
    return deps.file_storage


def get_room_ownership_reader(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> RoomOwnershipReader:
    return deps.room_ownership_reader


def get_hitl_manager(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> HITLManager:
    return deps.hitl_manager


def get_inspection_center(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentInspection:
    return deps.inspection_center


def get_room_center(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> RoomCenterCompatibility:
    return deps.room_center


def get_room_store(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> RoomRouteReader:
    return deps.room_store


def get_agent_selection_service(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentSuggestionService:
    return deps.agent_selection_service


def get_execution_engine(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> ExecutionEngine:
    return deps.execution_engine


def get_sse_store(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> SSEStateReader:
    return deps.sse_store


def get_sse_transport(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> SSERouteTransport:
    return deps.sse_transport
