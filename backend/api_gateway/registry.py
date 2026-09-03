"""Route inventory and ownership helpers for the API Gateway."""

from __future__ import annotations

from collections.abc import Iterable
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

    if (
        matches("/a2a-tasks")
        or matches("/users/me/a2a-tasks")
        or (matches("/rooms") and normalized.endswith("/a2a-tasks"))
    ):
        return "a2a_task"
    if matches("/local-agents"):
        return "agent"
    if matches("/api-keys"):
        return "discovery_api_key"
    if matches("/agentGroups"):
        return "agent_group"
    if matches("/agent"):
        return "agent"
    if matches("/discovery/api-keys"):
        return "discovery_api_key"
    if matches("/discovery"):
        return "discovery"
    if matches("/files"):
        return "files"
    if matches("/gateway"):
        return "platform_gateway"
    if matches("/inspectionCenter"):
        return "inspection"
    if matches("/roomCenter"):
        return "room"
    if matches("/rooms") and "/agent-calls" in normalized:
        return "room"
    if matches("/rooms") and "/hitl" in normalized:
        return "hitl"
    if matches("/sse"):
        return "sse"
    if matches("/webhooks"):
        return "webhook"
    return "unknown"


def expected_owner_for_group(group: str) -> str:
    return f"api_gateway.routes.{group}_routes"


def open_cors_path_prefixes(api_prefix: str) -> tuple[str, ...]:
    from api_gateway.policies import open_cors_groups

    suffixes = {
        "discovery": "/discovery",
        "platform_gateway": "/gateway",
    }
    return tuple(
        f"{api_prefix}{suffixes[group]}"
        for group in sorted(open_cors_groups())
        if group in suffixes
    )


def route_groups_for_paths(paths: Iterable[str]) -> set[str]:
    return {route_group_for_path(path) for path in paths}
