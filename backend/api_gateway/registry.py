"""Route inventory and ownership helpers for the API Gateway."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

DECLARED_OWNER_ATTR = "__api_gateway_declared_owner__"


def mark_declared_owner(router: APIRouter, owner: str) -> None:
    """Attach gateway ownership metadata to every endpoint in a router."""
    for route in router.routes:
        setattr(route, DECLARED_OWNER_ATTR, owner)
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            setattr(endpoint, DECLARED_OWNER_ATTR, owner)


def resolve_declared_owner(route: Any) -> str:
    route_owner = getattr(route, DECLARED_OWNER_ATTR, None)
    if isinstance(route_owner, str) and route_owner:
        return route_owner

    endpoint = getattr(route, "endpoint", None)
    endpoint_owner = getattr(endpoint, DECLARED_OWNER_ATTR, None)
    if isinstance(endpoint_owner, str) and endpoint_owner:
        return endpoint_owner

    return getattr(endpoint, "__module__", "")


def route_group_for_path(path: str) -> str:
    normalized = path.removeprefix("/api/v1")

    def matches(prefix: str) -> bool:
        return normalized == prefix or normalized.startswith(f"{prefix}/")

    if matches("/internal/llm"):
        return "llm_proxy"
    if matches("/a2a/push"):
        return "a2a_push"
    if matches("/local-agents"):
        return "agent"
    if matches("/agentGroups"):
        return "agent_group"
    if matches("/agent") or matches("/agents"):
        return "agent"
    if matches("/files"):
        return "files"
    if matches("/inspectionCenter"):
        return "inspection"
    if matches("/rooms") and "/hitl" in normalized:
        return "hitl"
    if matches("/rooms") or matches("/agent-suggestions"):
        return "room"
    if matches("/sessions"):
        return "session"
    if matches("/sse"):
        return "sse"
    return "unknown"
