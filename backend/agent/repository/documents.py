from __future__ import annotations

from common.observability import events as E
from common.observability import get_logger
from models.agent import Agent
from models.agent_group import AgentGroup

logger = get_logger(__name__)


def _safe_parse_agent_group(doc: dict | None) -> AgentGroup | None:
    if doc is None:
        return None
    try:
        return AgentGroup.model_validate(doc)
    except Exception as exc:
        logger.emit(
            E.AGENT_DOCUMENT_PARSE_FAILED,
            exc=exc,
            document_kind="agent_group",
            group_id=doc.get("group_id"),
        )
        return None


def _safe_parse_agent(doc: dict | None) -> Agent | None:
    if doc is None:
        return None
    try:
        return Agent.model_validate(doc)
    except Exception as exc:
        logger.emit(
            E.AGENT_DOCUMENT_PARSE_FAILED,
            exc=exc,
            document_kind="agent",
            agent_id=doc.get("agent_id"),
        )
        return None
