"""Declarative traffic policies owned by the API Gateway."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutePolicy:
    auth: str
    tags: tuple[str, ...]
    cors: str = "default"
    api_key: bool = False
    deprecated: bool = False


ROUTE_POLICIES: dict[str, RoutePolicy] = {
    "agent": RoutePolicy(auth="mixed-route-level", tags=("agent",)),
    "agent_group": RoutePolicy(auth="clerk-route-level", tags=("agent_group",)),
    "files": RoutePolicy(auth="clerk-route-level", tags=("files",)),
    "hitl": RoutePolicy(auth="clerk-route-level", tags=("hitl",)),
    "inspection": RoutePolicy(auth="clerk-global", tags=("inspection",)),
    "room": RoutePolicy(auth="clerk-route-level", tags=("room",)),
    "sse": RoutePolicy(auth="query-token-supported", tags=("sse",)),
    "webhook": RoutePolicy(auth="bearer-token-route-level", tags=("webhooks",)),
}
