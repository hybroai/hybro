from fastapi import APIRouter, Depends, HTTPException, Request

from agent.protocols import AgentInspection
from api_gateway.dependencies import get_inspection_center
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.observability import events as E
from common.observability import get_logger
from models.request import InspectionCenterRequest

router = APIRouter()
logger = get_logger(__name__)


@router.post("/inspectionCenter/inspectAgentCard")
async def inspect_agent(
    request: Request,
    center: AgentInspection = Depends(get_inspection_center),
):
    request_data = await request.json()
    agent_url = request_data.get("agent_url")
    if not agent_url:
        raise HTTPException(status_code=400, detail="agent_url is required")
    logger.emit(E.AGENT_CARD_INSPECTION_RECEIVED, agent_url=agent_url)
    inspection_center_request = InspectionCenterRequest(agent_url=agent_url)
    inspection_center_response = await center.inspect_agent_card(
        inspection_center_request
    )
    return inspection_center_response


@router.post("/inspectionCenter/inspectA2AConnection")
async def inspect_a2a_connection(
    request: Request,
    center: AgentInspection = Depends(get_inspection_center),
):
    request_data = await request.json()
    agent_url = request_data.get("agent_url")
    if not agent_url:
        raise HTTPException(status_code=400, detail="agent_url is required")
    logger.emit(E.AGENT_CONNECTION_INSPECTION_RECEIVED, agent_url=agent_url)
    inspection_center_request = InspectionCenterRequest(agent_url=agent_url)
    inspection_center_response = await center.inspect_a2a_connection(
        inspection_center_request
    )
    return inspection_center_response


_mark_declared_owner(router, __name__)
