from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent.protocols import (
    AgentCapabilityIssueStore,
    AgentCenterCompatibility,
    AgentLivenessChecker,
)
from api_gateway.dependencies import (
    get_agent_center,
    get_agent_liveness_checker,
    get_agent_service,
    get_capability_issue_service,
    get_local_agent_discovery,
)
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import (
    ClerkUser,
    get_current_user,
    get_current_user_or_service,
    get_optional_user,
)
from common.protocols import AgentRegistry
from local_agents.models import DiscoveryTrigger, LocalAgentDiscoveryResult
from local_agents.protocols import LocalAgentDiscovery
from models.agent import IssueStatus
from models.response import AgentCenterResponse

router = APIRouter()


# ============= PROTECTED ENDPOINTS (Auth Required) =============


@router.post("/local-agents/discovery")
async def discover_local_agents(
    user: ClerkUser = Depends(get_current_user),
    discovery: LocalAgentDiscovery | None = Depends(get_local_agent_discovery),
) -> LocalAgentDiscoveryResult:
    """Discover A2A agents running on the Docker host."""
    del user
    if discovery is None:
        raise HTTPException(status_code=503, detail="Local agent discovery is disabled")
    try:
        return await discovery.request_discovery(DiscoveryTrigger.MANUAL)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/agent/registerAgent")
async def register_agent(
    request: Request,
    user: ClerkUser = Depends(get_current_user_or_service),
    center: AgentCenterCompatibility = Depends(get_agent_center),
):
    """Register a new agent - PROTECTED (Clerk user or default-agent registrar service token)"""
    request_data = await request.json()
    agent_url = request_data.get("agent_url")
    # we should use current user's clerk id as provider_id
    provider_id = user.user_id

    if not agent_url:
        raise HTTPException(status_code=400, detail="agent_url is required")
    agent_center_response = await center.register_agent_from_route(
        agent_url=agent_url,
        provider_id=provider_id,
    )

    # Handle duplicate error from service layer
    if not agent_center_response.success and agent_center_response.status_code == 400:
        raise HTTPException(
            status_code=400,
            detail=agent_center_response.error,
        )

    return agent_center_response


@router.get("/agent/getAgent/me")
async def get_agent_by_provider(
    user: ClerkUser = Depends(get_current_user),
    center: AgentCenterCompatibility = Depends(get_agent_center),
):
    """Get agents by provider id - PROTECTED (requires authentication)"""
    provider_id = user.user_id
    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id is required")

    return await center.get_agents_by_provider_for_route(
        provider_id=provider_id,
    )


@router.post("/agent/deleteAgent")
async def delete_agent(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    center: AgentCenterCompatibility = Depends(get_agent_center),
):
    """Delete an agent - PROTECTED (requires authentication and ownership)"""
    request_data = await request.json()
    agent_id = request_data.get("agent_id")

    if not agent_id or not agent_id.strip():
        raise HTTPException(status_code=400, detail="agent_id is required")
    agent_center_response = await center.delete_agent_from_route(
        agent_id=agent_id,
        provider_id=user.user_id,
    )
    if not agent_center_response.success and agent_center_response.status_code in {
        403,
        404,
    }:
        raise HTTPException(
            status_code=agent_center_response.status_code,
            detail=agent_center_response.error,
        )

    return agent_center_response


# ============= CAPABILITY ISSUE ENDPOINTS (Auth Required) =============


@router.get("/agent/{agent_id}/capability-issues")
async def get_capability_issues(
    agent_id: str,
    status: str | None = Query(None, description="Filter by status: open or resolved"),
    limit: int = Query(100, ge=1, le=500, description="Max issues to return"),
    offset: int = Query(0, ge=0, description="Number of issues to skip"),
    user: ClerkUser = Depends(get_current_user),
    agent_lookup: AgentRegistry = Depends(get_agent_service),
    issue_store: AgentCapabilityIssueStore = Depends(get_capability_issue_service),
):
    """Get capability issues for an agent - PROTECTED (requires ownership)"""
    existing_agent = await agent_lookup.get_agent(agent_id)
    if not existing_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if existing_agent.provider_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view issues for this agent",
        )

    issue_status = None
    if status:
        try:
            issue_status = IssueStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid status. Use 'open' or 'resolved'."
            ) from None

    issues = await issue_store.get_issues_for_agent(
        agent_id, status=issue_status, limit=limit, offset=offset
    )
    return {"issues": [issue.model_dump(mode="json") for issue in issues]}


@router.post("/agent/{agent_id}/capability-issues/resolve-all")
async def resolve_all_capability_issues(
    agent_id: str,
    user: ClerkUser = Depends(get_current_user),
    agent_lookup: AgentRegistry = Depends(get_agent_service),
    issue_store: AgentCapabilityIssueStore = Depends(get_capability_issue_service),
):
    """Bulk resolve all open capability issues for an agent - PROTECTED"""
    existing_agent = await agent_lookup.get_agent(agent_id)
    if not existing_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if existing_agent.provider_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to resolve issues for this agent",
        )

    count = await issue_store.resolve_all_for_agent(agent_id, user.user_id)
    return {"resolved_count": count}


@router.post("/agent/capability-issues/{issue_id}/resolve")
async def resolve_capability_issue(
    issue_id: str,
    user: ClerkUser = Depends(get_current_user),
    agent_lookup: AgentRegistry = Depends(get_agent_service),
    issue_store: AgentCapabilityIssueStore = Depends(get_capability_issue_service),
):
    """Resolve a single capability issue - PROTECTED (requires ownership)"""
    issue = await issue_store.get_issue_by_id(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    agent = await agent_lookup.get_agent(issue.agent_id)
    if not agent or agent.provider_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to resolve this issue",
        )

    result = await issue_store.resolve_issue(
        issue_id,
        user.user_id,
    )
    if not result:
        raise HTTPException(
            status_code=400, detail="Issue is already resolved or not found"
        )
    return {"issue": result.model_dump(mode="json")}


# ============= PUBLIC ENDPOINTS (No Auth Required) =============


@router.post("/agent/getAgentCardFromUrl")
async def get_agent_card_from_url(
    request: Request,
    center: AgentCenterCompatibility = Depends(get_agent_center),
):
    """Get agent card from URL - PUBLIC (no authentication required)"""
    request_data = await request.json()
    agent_url = request_data.get("agent_url")

    if not agent_url:
        raise HTTPException(status_code=400, detail="agent_url is required")
    return await center.get_agent_card_from_url_for_route(
        agent_url=agent_url,
    )


@router.get("/agent/getAgent/{agent_id}")
async def get_agent(
    agent_id: str,
    user: ClerkUser | None = Depends(get_optional_user),
    center: AgentCenterCompatibility = Depends(get_agent_center),
    liveness_checker: AgentLivenessChecker = Depends(get_agent_liveness_checker),
):
    """Get agent by ID - PUBLIC (authentication optional)"""
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")
    if not agent_id.strip():
        return AgentCenterResponse(
            agent_id=agent_id,
            success=False,
            error="agent_id is required",
            status_code=400,
        )

    user_id = user.user_id if user else None
    agent_center_response = await center.get_visible_agent_for_route(
        agent_id=agent_id,
        user_id=user_id,
    )

    if agent_center_response.success and agent_center_response.agent:
        agent_center_response.agent = await liveness_checker(
            agent_center_response.agent
        )

    return center.finalize_agent_response_for_route(agent_center_response)


@router.get("/agent/getAllAgents")
async def get_agent_list(
    active_only: bool = Query(
        False,
        description="Return only active agents when true",
    ),
    user: ClerkUser | None = Depends(get_optional_user),
    center: AgentCenterCompatibility = Depends(get_agent_center),
):
    """Get visible agents, optionally filtering to active agents."""
    user_id = user.user_id if user else None
    return await center.list_visible_agents_for_route(
        user_id=user_id,
        active_only=active_only,
    )


_mark_declared_owner(router, __name__)
