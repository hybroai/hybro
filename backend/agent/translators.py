from __future__ import annotations

from datetime import datetime
from typing import Any

from common.dto.agent import AgentCardSnapshot, AgentInfo


def agent_info_from_doc(doc: dict[str, Any]) -> AgentInfo:
    card = _card(doc)
    return AgentInfo(
        agent_id=doc["agent_id"],
        name=card.get("name"),
        description=card.get("description"),
        url=doc.get("normalized_url") or card.get("url") or doc.get("public_url"),
        provider_id=doc.get("provider_id"),
        status=_status_value(doc.get("agent_status", "active")) or "active",
        capabilities=list(doc.get("capabilities") or _card_capabilities(card)),
        source=doc.get("source", "cloud"),
        inactive_reason=doc.get("inactive_reason"),
        is_public=doc["is_public"],
        public_url=doc.get("public_url"),
        rate_limit_per_user_per_hour=doc.get("rate_limit_per_user_per_hour"),
        rate_limit_system_per_hour=doc.get("rate_limit_system_per_hour"),
        call_count=doc.get("call_count", 0),
        raw_card=dict(card),
    )


def agent_card_from_doc(doc: dict[str, Any]) -> AgentCardSnapshot:
    card = _card(doc)
    return AgentCardSnapshot(
        agent_id=doc["agent_id"],
        name=card.get("name"),
        description=card.get("description"),
        url=doc.get("normalized_url") or card.get("url") or doc.get("public_url") or "",
        capabilities=list(doc.get("capabilities") or _card_capabilities(card)),
        raw_card=dict(card),
    )


def registration_doc_from_card(
    *,
    agent_id: str,
    provider_id: str,
    card: AgentCardSnapshot,
    normalized_url: str | None,
    public_url: str | None,
    now: datetime,
    is_public: bool = True,
    rate_limit_per_user_per_hour: int | None = None,
    rate_limit_system_per_hour: int | None = None,
) -> dict[str, Any]:
    raw_card = dict(card.raw_card)
    raw_card.setdefault("name", card.name)
    raw_card.setdefault("description", card.description)
    raw_card.setdefault("url", card.url)
    return {
        "agent_id": agent_id,
        "provider_id": provider_id,
        "agent_card": raw_card,
        "normalized_url": normalized_url,
        "public_url": public_url,
        "agent_status": "active",
        "is_public": is_public,
        "source": "cloud",
        "capabilities": list(card.capabilities),
        "rate_limit_per_user_per_hour": rate_limit_per_user_per_hour,
        "rate_limit_system_per_hour": rate_limit_system_per_hour,
        "call_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def _card(doc: dict[str, Any]) -> dict[str, Any]:
    return dict(doc.get("agent_card") or {})


def _card_capabilities(card: dict[str, Any]) -> list[str]:
    skills = card.get("skills")
    if not isinstance(skills, list):
        return []
    return [
        str(skill.get("name") or skill.get("id"))
        for skill in skills
        if isinstance(skill, dict) and (skill.get("name") or skill.get("id"))
    ]


def _status_value(status: Any, *, default: str | None = "active") -> str | None:
    value = getattr(status, "value", status)
    return str(value) if value is not None else default
