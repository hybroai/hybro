"""Root API Gateway router for all `/api/v1/*` traffic."""

from fastapi import APIRouter, Depends

from api_gateway.routes import (
    agent_group_routes,
    agent_routes,
    files_routes,
    hitl_routes,
    inspection_routes,
    room_routes,
    sse_routes,
    webhook_routes,
)
from common.auth import get_current_user


def build_api_gateway_router() -> APIRouter:
    gateway_router = APIRouter()

    gateway_router.include_router(agent_routes.router, tags=["agent"])
    gateway_router.include_router(
        inspection_routes.router,
        tags=["inspection"],
        dependencies=[Depends(get_current_user)],
    )
    gateway_router.include_router(room_routes.router, tags=["room"])
    gateway_router.include_router(hitl_routes.router, tags=["hitl"])
    gateway_router.include_router(sse_routes.router, tags=["sse"])
    gateway_router.include_router(agent_group_routes.router, tags=["agent_group"])
    gateway_router.include_router(files_routes.router, tags=["files"])
    gateway_router.include_router(webhook_routes.router, tags=["webhooks"])

    return gateway_router


router = build_api_gateway_router()
