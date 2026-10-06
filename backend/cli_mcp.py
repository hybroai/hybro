"""Read-only host probe for the local MCP adapter. No runtime credentials."""

from typing import Literal

import httpx

MCP_URL = "http://127.0.0.1:8001/mcp"
MCPStatus = Literal["ready", "unavailable", "unsupported_auth"]
STATUS_LABELS: dict[MCPStatus, str] = {
    "ready": "Ready",
    "unavailable": "Unavailable; start services or check MCP logs.",
    "unsupported_auth": "Unavailable with Clerk authentication.",
}


def read_status() -> MCPStatus:
    try:
        with httpx.Client(
            timeout=4.0, trust_env=False, follow_redirects=False
        ) as client:
            response = client.get("http://127.0.0.1:8001/health")
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("service") == "hybro-mcp":
            status = body.get("status")
            if status in {"ready", "unavailable", "unsupported_auth"}:
                return status
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return "unavailable"
