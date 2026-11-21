"""
Agent Group API Endpoints

Provides CRUD operations for user-created agent groups.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Query, Request

from agent.protocols import AgentGroupStore
from api_gateway.dependencies import get_agent_group_store
from api_gateway.owner_session import get_current_user
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import ClerkUser
from models.agent_group import (
    BUILTIN_GROUP_ALL_AGENTS,
    BUILTIN_GROUP_ROOM_TEAM,
    AgentGroup,
)

router = APIRouter()


def _current_user_id(user: ClerkUser | None) -> str | None:
    return user.user_id if user else None


def _forbidden(message: str) -> dict[str, Any]:
    return {"success": False, "error": message, "status_code": 403}


def _owns_group(group: AgentGroup, user_id: str | None) -> bool:
    return user_id is None or group.owner_id == user_id


async def _prepare_preset_group(
    db: AgentGroupStore,
    owner_id: str,
    preset_key: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    if preset_key is None:
        return None, None
    if not isinstance(preset_key, str) or not preset_key.strip():
        return None, {
            "success": False,
            "error": "Preset key must be a non-empty string",
            "status_code": 400,
        }

    group_id = uuid5(NAMESPACE_URL, f"hybro-agent-group:{owner_id}:{preset_key}").hex
    existing_group = await db.get_agent_group_by_id(group_id)
    if existing_group is None:
        return group_id, None
    if existing_group.owner_id != owner_id:
        return group_id, _forbidden("Cannot access another owner's agent group")
    return group_id, {
        "success": True,
        "group": existing_group.model_dump(mode="json"),
        "status_code": 200,
    }


@router.post("/agentGroups")
async def create_agent_group(
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    db: AgentGroupStore = Depends(get_agent_group_store),
):
    """
    Create a new agent group.
    """
    request_data = await request.json()
    name = request_data.get("name")
    description = request_data.get("description")
    requested_owner_id = request_data.get("owner_id")
    user_id = _current_user_id(user)
    owner_id = user_id or requested_owner_id
    agents = request_data.get("agents", [])
    preset_key = request_data.get("preset_key")

    if not name:
        return {"success": False, "error": "Group name is required", "status_code": 400}

    if not owner_id:
        return {"success": False, "error": "Owner ID is required", "status_code": 400}
    if user_id and requested_owner_id and requested_owner_id != user_id:
        return _forbidden("Cannot create an agent group for another owner")
    group_id, preset_response = await _prepare_preset_group(db, owner_id, preset_key)
    if preset_response is not None:
        return preset_response

    agent_group = AgentGroup(
        **({"group_id": group_id} if group_id else {}),
        name=name,
        description=description,
        type="user",
        owner_id=owner_id,
        agents=agents,
    )

    success = await db.add_agent_group(agent_group)

    if success:
        return {
            "success": True,
            "group": agent_group.model_dump(mode="json"),
            "status_code": 200,
        }

    # A concurrent request with the same preset key may have won the unique
    # group_id insert. Return that owner-scoped group instead of surfacing an
    # error or creating another team.
    if group_id:
        existing_group = await db.get_agent_group_by_id(group_id)
        if existing_group is not None and existing_group.owner_id == owner_id:
            return {
                "success": True,
                "group": existing_group.model_dump(mode="json"),
                "status_code": 200,
            }

    return {
        "success": False,
        "error": "Failed to create agent group",
        "status_code": 500,
    }


@router.get("/agentGroups")
async def list_agent_groups(
    owner_id: str | None = Query(default=None),
    user: ClerkUser = Depends(get_current_user),
    db: AgentGroupStore = Depends(get_agent_group_store),
):
    """
    List all agent groups for a user.
    Returns both built-in groups and user-created groups.
    """
    user_id = _current_user_id(user)
    effective_owner_id = user_id or owner_id
    if not effective_owner_id:
        return {"success": False, "error": "Owner ID is required", "status_code": 400}
    if user_id and owner_id and owner_id != user_id:
        return _forbidden("Cannot list another owner's agent groups")

    # Get user's custom groups
    user_groups = await db.get_agent_groups_by_owner(effective_owner_id)

    # Add built-in groups at the beginning
    builtin_groups = [
        {
            "group_id": BUILTIN_GROUP_ALL_AGENTS,
            "name": "All Agents",
            "description": "Search the entire agent network for the best match",
            "type": "builtin",
            "owner_id": None,
            "agents": [],
        },
        {
            "group_id": BUILTIN_GROUP_ROOM_TEAM,
            "name": "Room Team",
            "description": "Use agents assigned to this room",
            "type": "builtin",
            "owner_id": None,
            "agents": [],
        },
    ]

    all_groups = builtin_groups + [g.model_dump(mode="json") for g in user_groups]

    return {"success": True, "groups": all_groups, "status_code": 200}


@router.get("/agentGroups/{group_id}")
async def get_agent_group(
    group_id: str,
    user: ClerkUser = Depends(get_current_user),
    db: AgentGroupStore = Depends(get_agent_group_store),
):
    """
    Get a specific agent group by ID.
    """
    if not group_id:
        return {"success": False, "error": "Group ID is required", "status_code": 400}

    # Check for built-in groups
    if group_id == BUILTIN_GROUP_ALL_AGENTS:
        return {
            "success": True,
            "group": {
                "group_id": BUILTIN_GROUP_ALL_AGENTS,
                "name": "All Agents",
                "description": "Search the entire agent network for the best match",
                "type": "builtin",
                "owner_id": None,
                "agents": [],
            },
            "status_code": 200,
        }

    if group_id == BUILTIN_GROUP_ROOM_TEAM:
        return {
            "success": True,
            "group": {
                "group_id": BUILTIN_GROUP_ROOM_TEAM,
                "name": "Room Team",
                "description": "Use agents assigned to this room",
                "type": "builtin",
                "owner_id": None,
                "agents": [],
            },
            "status_code": 200,
        }
    group = await db.get_agent_group_by_id(group_id)

    if group:
        if not _owns_group(group, _current_user_id(user)):
            return _forbidden("Cannot access another owner's agent group")
        return {
            "success": True,
            "group": group.model_dump(mode="json"),
            "status_code": 200,
        }
    else:
        return {"success": False, "error": "Agent group not found", "status_code": 404}


@router.put("/agentGroups/{group_id}")
async def update_agent_group(
    group_id: str,
    request: Request,
    user: ClerkUser = Depends(get_current_user),
    db: AgentGroupStore = Depends(get_agent_group_store),
):
    """
    Update an agent group.
    """
    if not group_id:
        return {"success": False, "error": "Group ID is required", "status_code": 400}

    # Cannot update built-in groups
    if group_id in [BUILTIN_GROUP_ALL_AGENTS, BUILTIN_GROUP_ROOM_TEAM]:
        return {
            "success": False,
            "error": "Cannot update built-in groups",
            "status_code": 400,
        }

    request_data = await request.json()

    # Build updates dict
    updates = {}
    if "name" in request_data:
        updates["name"] = request_data["name"]
    if "description" in request_data:
        updates["description"] = request_data["description"]
    if "agents" in request_data:
        updates["agents"] = request_data["agents"]

    if not updates:
        return {"success": False, "error": "No updates provided", "status_code": 400}
    existing_group = await db.get_agent_group_by_id(group_id)
    if not existing_group:
        return {"success": False, "error": "Agent group not found", "status_code": 404}
    if not _owns_group(existing_group, _current_user_id(user)):
        return _forbidden("Cannot update another owner's agent group")

    updated = await db.update_agent_group(group_id, updates)

    if updated:
        # Fetch updated group
        updated_group = (
            updated
            if isinstance(updated, AgentGroup)
            else await db.get_agent_group_by_id(group_id)
        )
        return {
            "success": True,
            "group": updated_group.model_dump(mode="json") if updated_group else None,
            "status_code": 200,
        }
    else:
        return {
            "success": False,
            "error": "Failed to update agent group",
            "status_code": 500,
        }


@router.delete("/agentGroups/{group_id}")
async def delete_agent_group(
    group_id: str,
    user: ClerkUser = Depends(get_current_user),
    db: AgentGroupStore = Depends(get_agent_group_store),
):
    """
    Delete an agent group.
    """
    if not group_id:
        return {"success": False, "error": "Group ID is required", "status_code": 400}

    # Cannot delete built-in groups
    if group_id in [BUILTIN_GROUP_ALL_AGENTS, BUILTIN_GROUP_ROOM_TEAM]:
        return {
            "success": False,
            "error": "Cannot delete built-in groups",
            "status_code": 400,
        }
    existing_group = await db.get_agent_group_by_id(group_id)
    if not existing_group:
        return {"success": False, "error": "Agent group not found", "status_code": 404}
    if not _owns_group(existing_group, _current_user_id(user)):
        return _forbidden("Cannot delete another owner's agent group")
    success = await db.delete_agent_group(group_id)

    if success:
        return {"success": True, "status_code": 200}
    else:
        return {
            "success": False,
            "error": "Failed to delete agent group",
            "status_code": 500,
        }


_mark_declared_owner(router, __name__)
