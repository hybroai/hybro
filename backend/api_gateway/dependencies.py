"""Gateway-owned FastAPI dependency context."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from agent.network import AgentNetworkService
from agent.protocols import (
    AgentCapabilityIssueStore,
    AgentCatalog,
    AgentGroupStore,
    AgentInspection,
    AgentNetworkAccess,
    AgentSuggestionService,
)
from chat.protocols import ChatRooms
from common.auth import ClerkUser, get_current_user
from common.protocols import (
    AgentMatcher,
    AgentRegistry,
    ExecutionEngine,
    FileStorage,
    HITLManager,
    RoomOwnershipReader,
    RoomRouteReader,
    SSERouteTransport,
    SSEStateReader,
)
from execution.ports import A2APushNotificationReceiver
from local_agents.protocols import LocalAgentDiscovery
from session.access import BoundSessionAccess
from session.protocols import SessionAccess, SessionOwner, SessionService


@dataclass(frozen=True, slots=True)
class APIGatewayDeps:
    agent_catalog: AgentCatalog
    agent_service: AgentRegistry
    agent_matcher: AgentMatcher
    capability_issue_service: AgentCapabilityIssueStore
    agent_group_store: AgentGroupStore
    file_storage: FileStorage
    room_ownership_reader: RoomOwnershipReader
    hitl_manager: HITLManager
    inspection_center: AgentInspection
    chat_rooms: ChatRooms
    room_store: RoomRouteReader
    agent_selection_service: AgentSuggestionService
    execution_engine: ExecutionEngine
    sse_store: SSEStateReader
    sse_transport: SSERouteTransport
    session_facade: SessionService
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


def get_agent_catalog(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentCatalog:
    return deps.agent_catalog


def get_agent_service(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentRegistry:
    return deps.agent_service


def get_capability_issue_service(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentCapabilityIssueStore:
    return deps.capability_issue_service


def get_agent_group_store(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentGroupStore:
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


def get_chat_rooms(
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> ChatRooms:
    return deps.chat_rooms


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


def get_a2a_push_receiver(request: Request) -> A2APushNotificationReceiver:
    """Execution's push ingress, bound after the orchestrator runtime is built."""
    receiver = getattr(request.app.state, "a2a_push_receiver", None)
    if receiver is None:
        raise HTTPException(status_code=503, detail="Push ingress is unavailable")
    return receiver


A2APushReceiverDependency = Annotated[
    A2APushNotificationReceiver, Depends(get_a2a_push_receiver)
]


def get_agent_network(
    user: Annotated[ClerkUser, Depends(get_current_user)],
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> AgentNetworkAccess:
    return AgentNetworkService(
        deps.agent_service,
        deps.agent_group_store,
        deps.agent_matcher,
        requesting_user_id=user.user_id,
    )


AgentNetworkDependency = Annotated[AgentNetworkAccess, Depends(get_agent_network)]


def get_session_access(
    user: Annotated[ClerkUser, Depends(get_current_user)],
    deps: APIGatewayDeps = _API_GATEWAY_DEPS_DEPENDENCY,
) -> SessionAccess:
    """Bind the session facade to the authenticated owner for one request."""
    return BoundSessionAccess(
        deps.session_facade,
        owner=SessionOwner(
            user_id=user.user_id,
            name=getattr(user, "username", None)
            or getattr(user, "email", None)
            or user.user_id,
        ),
        resolver=AgentNetworkService(
            deps.agent_service,
            deps.agent_group_store,
            deps.agent_matcher,
            requesting_user_id=user.user_id,
        ),
    )


SessionAccessDependency = Annotated[SessionAccess, Depends(get_session_access)]
