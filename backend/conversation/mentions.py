"""Agent mention parsing and canonical mention building for room messages."""

from __future__ import annotations

import re
from typing import Any

from common.observability import events as E
from common.observability import log_ids
from common.utils.logger import get_logger
from models.response import RoomCenterUserMessageResponse, ScopeResolutionError

logger = get_logger(__name__)

_MENTION_PATTERN = re.compile(r"<@([^|]+)\|([^>]+)>")


def parse_agent_mentions(message_text: str, room_agent_set: dict) -> list[dict]:
    """Parse ``<@agent_id|name>`` mentions of room members, in text order.

    The agent name comes from ``room_agent_set``; mentions of agents outside the
    room are dropped and counted in one log event; ids from message text are
    never logged.
    """
    mentions = []
    dropped_mention_count = 0
    for match in _MENTION_PATTERN.finditer(message_text):
        agent_id = match.group(1).strip()
        position = match.start()
        if agent_id in room_agent_set:
            mentions.append(
                {
                    "agent_id": agent_id,
                    "agent_name": room_agent_set[agent_id],
                    "mention_text": match.group(0),
                    "position": position,
                }
            )
        else:
            dropped_mention_count += 1
    if dropped_mention_count:
        logger.emit(
            E.MENTIONS_DROPPED,
            reason="not_room_member",
            dropped_mention_count=dropped_mention_count,
        )
    mentions.sort(key=lambda x: x["position"])
    return mentions


def canonical_mentions_or_error(
    agents: list[Any], invalid_ids: list[str]
) -> list[dict] | RoomCenterUserMessageResponse:
    """Build canonical mention dicts, or the 400 response for invalid targets."""
    canonical_mentions = [
        {
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_card.name,
            "mention_text": f"<@{agent.agent_id}|{agent.agent_card.name}>",
        }
        for agent in agents
    ]
    if invalid_ids:
        error_msg = f"Invalid or unauthorized mention targets: {', '.join(invalid_ids)}"
        logger.emit(
            E.TARGET_SCOPE_REJECTED,
            scope_source="mention",
            reason="unauthorized_targets",
            rejected_agent_count=len(invalid_ids),
            rejected_agent_ids=log_ids(invalid_ids),
        )
        return RoomCenterUserMessageResponse(
            message_id=None,
            message=None,
            success=False,
            error=error_msg,
            scope_resolution_error=ScopeResolutionError(
                code="unauthorized_mention",
                message=error_msg,
            ),
            status_code=400,
        )
    return canonical_mentions


__all__ = ["canonical_mentions_or_error", "parse_agent_mentions"]
