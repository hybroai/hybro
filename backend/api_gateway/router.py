"""Root API Gateway router for all `/api/v1/*` traffic."""

from fastapi import APIRouter, Depends

from api_gateway.owner_session import get_current_user
from api_gateway.routes import (
    a2a_push_routes,
    agent_group_routes,
    agent_network_routes,
    agent_routes,
    auth_routes,
    diagnostics_routes,
    files_routes,
    hitl_routes,
    inspection_routes,
    llm_proxy_routes,
    machine_routes,
    room_routes,
    rule_routes,
    session_routes,
    settings_routes,
    setup_routes,
    source_routes,
    sse_routes,
)


def build_api_gateway_router() -> APIRouter:
    gateway_router = APIRouter()

    gateway_router.include_router(agent_routes.router, tags=["agent"])
    gateway_router.include_router(agent_network_routes.router, tags=["agent"])
    gateway_router.include_router(
        inspection_routes.router,
        tags=["inspection"],
        dependencies=[Depends(get_current_user)],
    )
    gateway_router.include_router(room_routes.router, tags=["room"])
    gateway_router.include_router(hitl_routes.router, tags=["hitl"])
    gateway_router.include_router(session_routes.router, tags=["session"])
    gateway_router.include_router(sse_routes.router, tags=["sse"])
    gateway_router.include_router(agent_group_routes.router, tags=["agent_group"])
    gateway_router.include_router(files_routes.router, tags=["files"])
    gateway_router.include_router(llm_proxy_routes.router, tags=["internal_llm"])
    gateway_router.include_router(a2a_push_routes.router, tags=["a2a_push"])
    gateway_router.include_router(setup_routes.router, tags=["setup"])
    gateway_router.include_router(settings_routes.router, tags=["settings"])
    gateway_router.include_router(diagnostics_routes.router, tags=["diagnostics"])
    gateway_router.include_router(auth_routes.router, tags=["auth"])
    gateway_router.include_router(machine_routes.router, tags=["machine"])
    gateway_router.include_router(source_routes.router, tags=["sources"])
    gateway_router.include_router(rule_routes.router, tags=["rules"])

    return gateway_router


router = build_api_gateway_router()
