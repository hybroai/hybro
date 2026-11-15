"""SDK-free card normalization and SDK card construction.

Card data reaches the adapter in several shapes: an internal ``AgentCard``, a
raw dict from persistence, or a card fetched from the wire. All of them are
converted to a plain dict here and then parsed by the SDK's own card parser,
which also maps the pre-1.0 ``url``/``preferredTransport``/``protocolVersion``
shape onto ``supportedInterfaces``. The SDK client then selects its 0.3
compatibility transport for a pre-1.0 interface, so legacy and 1.0 agents share
one code path instead of two hand-written variants.
"""

from __future__ import annotations

from typing import Any

from a2a.client.card_resolver import parse_agent_card
from a2a.types import AgentCard


def agent_card_dict(agent_card_data: Any) -> dict[str, Any]:
    """Return plain camelCase card data from an internal model or dict."""
    return _model_data(agent_card_data)


def build_agent_card(agent_card_data: Any) -> AgentCard:
    """Build an SDK agent card from internal models, dicts, or wire JSON.

    A card with no callable interface is rejected: every card in Hybro exists to
    be called, and a card that advertises no endpoint cannot be. This also keeps
    a truncated or malformed card from being registered as a working agent.
    """
    if isinstance(agent_card_data, AgentCard):
        return agent_card_data
    data = _model_data(agent_card_data)
    if not data:
        raise ValueError("agent card data is empty")
    card = parse_agent_card(data)
    if not _interface_urls(card):
        raise ValueError("agent card advertises no callable interface")
    return card


def _interface_urls(card: AgentCard) -> list[str]:
    return [interface.url for interface in card.supported_interfaces if interface.url]


def _model_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        # The SDK's compatibility parser mutates nested skill/security mappings.
        # Copy containers so frozen DTOs work without altering registered cards.
        return _mutable_json(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return {}


def _mutable_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_mutable_json(item) for item in value]
    return value


__all__ = ["agent_card_dict", "build_agent_card"]
