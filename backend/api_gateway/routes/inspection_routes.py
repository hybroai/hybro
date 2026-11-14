from fastapi import APIRouter, Depends

from agent.protocols import AgentInspection
from api_gateway.dependencies import get_inspection_center
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.observability import events as E
from common.observability import get_logger
from models.request import InspectionCenterRequest

# The gateway router adds the auth dependency for every route in this module.
router = APIRouter()
logger = get_logger(__name__)


@router.post("/inspectionCenter/inspectAgentCard")
async def inspect_agent(
    body: InspectionCenterRequest,
    center: AgentInspection = Depends(get_inspection_center),
):
    logger.emit(E.AGENT_CARD_INSPECTION_RECEIVED, agent_url=body.agent_url)
    return await center.inspect_agent_card(body)


@router.post("/inspectionCenter/inspectA2AConnection")
async def inspect_a2a_connection(
    body: InspectionCenterRequest,
    center: AgentInspection = Depends(get_inspection_center),
):
    logger.emit(E.AGENT_CONNECTION_INSPECTION_RECEIVED, agent_url=body.agent_url)
    return await center.inspect_a2a_connection(body)


_mark_declared_owner(router, __name__)
