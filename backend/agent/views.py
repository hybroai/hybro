"""Public agent views returned by the ``/agent/*`` routes.

Views are built from the stored A2A card (camelCase JSON) and never carry
transport URLs or security data.
"""

from __future__ import annotations

from common.dto import (
    AgentCapabilitiesView,
    AgentCardView,
    AgentInfo,
    AgentSkillView,
    AgentView,
)
from common.protocols.json_types import JsonValue


def agent_card_view(
    card: dict[str, JsonValue],
    *,
    name: str | None = None,
    description: str | None = None,
) -> AgentCardView:
    return AgentCardView(**_card_fields(card, name=name, description=description))


def agent_view(info: AgentInfo) -> AgentView:
    return AgentView(
        **_card_fields(info.raw_card, name=info.name, description=info.description),
        agent_id=info.agent_id,
        provider_id=info.provider_id,
        status=info.status,
        source=info.source,
        inactive_reason=info.inactive_reason,
        is_public=info.is_public,
        public_url=info.public_url,
        call_count=info.call_count,
        rate_limit_per_user_per_hour=info.rate_limit_per_user_per_hour,
        rate_limit_system_per_hour=info.rate_limit_system_per_hour,
    )


def _card_fields(
    card: dict[str, JsonValue],
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    provider = _dict(card.get("provider"))
    capabilities = _dict(card.get("capabilities"))
    return {
        "name": _text(card.get("name")) or name or "",
        "description": _text(card.get("description")) or description or "",
        "version": _text(card.get("version")) or "1.0.0",
        "provider_organization": _text(provider.get("organization")),
        "icon_url": _text(card.get("iconUrl")),
        "documentation_url": _text(card.get("documentationUrl")),
        "skills": [
            AgentSkillView(
                id=_text(skill.get("id")),
                name=_text(skill.get("name")),
                description=_text(skill.get("description")),
                tags=_texts(skill.get("tags")),
                examples=_texts(skill.get("examples")),
            )
            for skill in map(_dict, _list(card.get("skills")))
            if _text(skill.get("id")) and _text(skill.get("name"))
        ],
        "capabilities": AgentCapabilitiesView(
            streaming=bool(capabilities.get("streaming")),
            push_notifications=bool(capabilities.get("pushNotifications")),
            state_transition_history=bool(capabilities.get("stateTransitionHistory")),
            extensions=bool(capabilities.get("extensions")),
        ),
        "default_input_modes": _texts(card.get("defaultInputModes", ["text"])),
        "default_output_modes": _texts(card.get("defaultOutputModes", ["text"])),
    }


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _texts(value: JsonValue) -> list[str]:
    return [item for item in _list(value) if isinstance(item, str)]


__all__ = ["agent_card_view", "agent_view"]
