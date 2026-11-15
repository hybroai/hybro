"""Normalize orchestration candidate scope snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from common.utils.a2a_file_modes import agent_input_modes, agent_supports_any_file
from common.utils.time import utcnow

AuthorizationKind = Literal[
    "room_member",
    "saved_group_member",
    "explicit_selection",
    "mention",
    "all_active_agents",
]


class AuthorizationBasis(BaseModel):
    kind: AuthorizationKind
    conversation_id: str | None = None
    group_id: str | None = None
    selected_by_user_id: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


class CandidateAgentSnapshot(BaseModel):
    agent_id: str
    name: str | None = None
    role: str | None = None
    capability_summary: str = ""
    status: str | None = None
    source: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ["text"])
    output_modes: list[str] = Field(default_factory=list)
    supports_file_upload: bool = False
    success_rate: float | None = None


class CandidateScopeSnapshot(BaseModel):
    snapshot_id: str
    revision: int = 1
    source: str
    conversation_id: str
    group_id: str | None = None
    agent_ids: list[str]
    agents: list[CandidateAgentSnapshot] = Field(default_factory=list)
    room_membership_version: str | None = None
    group_version: str | None = None
    resolved_at: datetime = Field(default_factory=utcnow)
    authorization_basis: AuthorizationBasis

    @field_validator("revision")
    @classmethod
    def _revision_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("revision must be at least 1")
        return value


_AUTHORIZATION_KIND_BY_SOURCE: dict[str, AuthorizationKind] = {
    "room_default": "room_member",
    "saved_group": "saved_group_member",
    "explicit_selection": "explicit_selection",
    "mention": "mention",
    "all_agents": "all_active_agents",
}
SUPPORTED_CANDIDATE_SCOPE_SOURCES = frozenset(_AUTHORIZATION_KIND_BY_SOURCE)


def normalize_candidate_scope(
    *,
    conversation_id: str,
    source: str,
    selected_agent_set: Mapping[str, Any] | Sequence[Any],
    group_id: str | None = None,
    selected_by_user_id: str | None = None,
    room_membership_version: str | None = None,
    group_version: str | None = None,
) -> CandidateScopeSnapshot:
    """Build a stable candidate scope snapshot from selected agent data."""

    agents = _candidate_agents_from_selected_set(selected_agent_set)
    return CandidateScopeSnapshot(
        snapshot_id=uuid4().hex,
        source=source,
        conversation_id=conversation_id,
        group_id=group_id,
        agent_ids=[agent.agent_id for agent in agents],
        agents=agents,
        room_membership_version=room_membership_version,
        group_version=group_version,
        authorization_basis=AuthorizationBasis(
            kind=_authorization_kind(source),
            conversation_id=conversation_id,
            group_id=group_id,
            selected_by_user_id=selected_by_user_id,
        ),
    )


def candidate_scope_items(candidate_scope: Any) -> list[Any]:
    """Return canonical candidate items from envelope or snapshot scope shapes."""

    return _raw_candidate_items(candidate_scope)


def _candidate_agents_from_selected_set(
    selected_agent_set: Mapping[str, Any] | Sequence[Any],
) -> list[CandidateAgentSnapshot]:
    agents: list[CandidateAgentSnapshot] = []
    seen_ids: set[str] = set()
    for item in _raw_candidate_items(selected_agent_set):
        agent = _candidate_agent_snapshot(item)
        if agent is None or agent.agent_id in seen_ids:
            continue
        seen_ids.add(agent.agent_id)
        agents.append(agent)
    return agents


def _raw_candidate_items(
    selected_agent_set: Mapping[str, Any] | Sequence[Any],
) -> list[Any]:
    if isinstance(selected_agent_set, str):
        return [selected_agent_set]

    if isinstance(selected_agent_set, Mapping):
        return _raw_candidate_items_from_mapping(selected_agent_set)

    if _looks_like_scope_object(selected_agent_set):
        return _raw_candidate_items_from_scope_object(selected_agent_set)

    if not isinstance(selected_agent_set, Sequence):
        return [selected_agent_set]

    return list(selected_agent_set)


def _raw_candidate_items_from_mapping(
    selected_agent_set: Mapping[str, Any],
) -> list[Any]:
    scope_items = _candidate_items_from_agents_and_ids(
        _first_mapping_value(selected_agent_set, "agents", "candidate_agents"),
        _first_mapping_value(selected_agent_set, "agent_ids", "candidate_agent_ids"),
    )
    if scope_items is not None:
        return scope_items
    if _looks_like_agent_mapping(selected_agent_set):
        return [selected_agent_set]
    if _looks_like_scope_mapping(selected_agent_set):
        return []

    items: list[Any] = []
    for agent_id, value in selected_agent_set.items():
        if not isinstance(agent_id, str):
            continue
        if isinstance(value, str):
            items.append({"agent_id": agent_id, "name": value})
        elif isinstance(value, Mapping):
            items.append({"agent_id": agent_id, **value})
        else:
            items.append(value)
    return items


def _raw_candidate_items_from_scope_object(selected_agent_set: Any) -> list[Any]:
    return (
        _candidate_items_from_agents_and_ids(
            _first_attr_value(selected_agent_set, "agents", "candidate_agents"),
            _first_attr_value(selected_agent_set, "agent_ids", "candidate_agent_ids"),
        )
        or []
    )


def _candidate_items_from_agents_and_ids(
    raw_agents: Any,
    raw_ids: Any,
) -> list[Any] | None:
    ids = list(raw_ids) if _is_non_string_sequence(raw_ids) else None
    if _is_non_string_sequence(raw_agents):
        items = list(raw_agents)
        if items:
            return _candidate_items_ordered_by_ids(items, ids) if ids else items
    return ids


def _is_non_string_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _candidate_items_ordered_by_ids(
    raw_items: Sequence[Any],
    raw_ids: Sequence[Any],
) -> list[Any]:
    items_by_id: dict[str, Any] = {}
    for item in raw_items:
        agent_id = _candidate_item_id(item)
        if agent_id is not None and agent_id not in items_by_id:
            items_by_id[agent_id] = item

    items: list[Any] = []
    seen_ids: set[str] = set()
    for raw_id in raw_ids:
        agent_id = _optional_str(raw_id)
        if agent_id is None or agent_id in seen_ids:
            continue
        seen_ids.add(agent_id)
        items.append(items_by_id.get(agent_id) or agent_id)
    return items


def _candidate_item_id(raw_item: Any) -> str | None:
    if isinstance(raw_item, str):
        return _optional_str(raw_item)
    if isinstance(raw_item, Mapping):
        return _optional_str(_first_mapping_value(raw_item, "agent_id", "id"))
    return _optional_str(_first_attr_value(raw_item, "agent_id", "id"))


def _candidate_agent_snapshot(  # noqa: C901
    raw_item: Any,
) -> CandidateAgentSnapshot | None:
    if isinstance(raw_item, str):
        agent_id = _optional_str(raw_item)
        if agent_id is None:
            return None
        return CandidateAgentSnapshot(agent_id=agent_id)

    if isinstance(raw_item, Mapping):
        agent_id = _optional_str(_first_mapping_value(raw_item, "agent_id", "id"))
        if agent_id is None:
            return None
        agent_card = _first_mapping_value(raw_item, "agent_card")
        name = _optional_str(_first_mapping_value(raw_item, "name", "agent_name"))
        role = _optional_str(_first_mapping_value(raw_item, "role"))
        capability_summary = _capability_summary_from_mapping(raw_item)
        if agent_card is not None:
            name = name or _optional_str(
                _first_card_value(agent_card, "name", "agent_name")
            )
            role = role or _optional_str(_first_card_value(agent_card, "role"))
            if not capability_summary:
                card_capabilities = _skill_list(_first_card_value(agent_card, "skills"))
                if card_capabilities:
                    capability_summary = ", ".join(card_capabilities)
                else:
                    capability_summary = _optional_str(
                        _first_card_value(agent_card, "capability_summary")
                    ) or _optional_str(_first_card_value(agent_card, "description"))
            input_modes = sorted(agent_input_modes(agent_card))
            output_modes = _string_list(
                _first_card_value(
                    agent_card, "default_output_modes", "defaultOutputModes"
                )
            )
            supports_file_upload = agent_supports_any_file(agent_card)
        else:
            input_modes = ["text"]
            output_modes = []
            supports_file_upload = False
        direct_input_modes = _candidate_modes_from_mapping(
            raw_item,
            "input_modes",
            "default_input_modes",
            "defaultInputModes",
            default=[],
        )
        if direct_input_modes:
            input_modes = direct_input_modes
        direct_output_modes = _candidate_modes_from_mapping(
            raw_item,
            "output_modes",
            "default_output_modes",
            "defaultOutputModes",
            default=[],
        )
        if direct_output_modes:
            output_modes = direct_output_modes
        direct_supports_file_upload = _first_mapping_value(
            raw_item,
            "supports_file_upload",
            "supportsFileUpload",
        )
        if direct_supports_file_upload is not None:
            supports_file_upload = bool(direct_supports_file_upload)
        capabilities = _string_list(
            _first_mapping_value(raw_item, "capabilities", "skills")
        )
        if not capabilities and agent_card is not None:
            capabilities = _skill_list(_first_card_value(agent_card, "skills"))
        return CandidateAgentSnapshot(
            agent_id=agent_id,
            name=name,
            role=role,
            capability_summary=capability_summary,
            status=_status_from_mapping(raw_item),
            source=_optional_str(_first_mapping_value(raw_item, "source")),
            capabilities=capabilities,
            input_modes=input_modes,
            output_modes=output_modes,
            supports_file_upload=supports_file_upload,
            success_rate=_optional_float(
                _first_mapping_value(raw_item, "success_rate")
            ),
        )

    agent_id = _optional_str(_first_attr_value(raw_item, "agent_id", "id"))
    if agent_id is None:
        return None

    agent_card = getattr(raw_item, "agent_card", None)
    name = _optional_str(_first_attr_value(raw_item, "name", "agent_name"))
    role = _optional_str(_first_attr_value(raw_item, "role"))
    capability_summary = _capability_summary_from_object(raw_item)
    if agent_card is not None:
        name = name or _optional_str(
            _first_card_value(agent_card, "name", "agent_name")
        )
        role = role or _optional_str(_first_card_value(agent_card, "role"))
        if not capability_summary:
            card_capabilities = _skill_list(_first_card_value(agent_card, "skills"))
            if card_capabilities:
                capability_summary = ", ".join(card_capabilities)
            else:
                capability_summary = _optional_str(
                    _first_card_value(agent_card, "capability_summary")
                ) or _optional_str(_first_card_value(agent_card, "description"))
        input_modes = sorted(agent_input_modes(agent_card))
        output_modes = _string_list(
            _first_card_value(agent_card, "default_output_modes", "defaultOutputModes")
        )
        supports_file_upload = agent_supports_any_file(agent_card)
    else:
        input_modes = ["text"]
        output_modes = []
        supports_file_upload = False
    direct_input_modes = _candidate_modes_from_object(
        raw_item,
        None,
        "input_modes",
        "default_input_modes",
        "defaultInputModes",
        default=[],
    )
    if direct_input_modes:
        input_modes = direct_input_modes
    direct_output_modes = _candidate_modes_from_object(
        raw_item,
        None,
        "output_modes",
        "default_output_modes",
        "defaultOutputModes",
        default=[],
    )
    if direct_output_modes:
        output_modes = direct_output_modes
    direct_supports_file_upload = _first_attr_value(
        raw_item,
        "supports_file_upload",
        "supportsFileUpload",
    )
    if direct_supports_file_upload is not None:
        supports_file_upload = bool(direct_supports_file_upload)
    capabilities = _string_list(_first_attr_value(raw_item, "capabilities", "skills"))
    if not capabilities and agent_card is not None:
        capabilities = _skill_list(_first_card_value(agent_card, "skills"))

    return CandidateAgentSnapshot(
        agent_id=agent_id,
        name=name,
        role=role,
        capability_summary=capability_summary or "",
        status=_status_from_object(raw_item),
        source=_optional_str(_first_attr_value(raw_item, "source")),
        capabilities=capabilities,
        input_modes=input_modes,
        output_modes=output_modes,
        supports_file_upload=bool(supports_file_upload),
        success_rate=_optional_float(_first_attr_value(raw_item, "success_rate")),
    )


def _candidate_modes_from_mapping(
    raw_item: Mapping[str, Any],
    *keys: str,
    default: list[str],
) -> list[str]:
    value = _first_mapping_value(raw_item, *keys)
    return list(default) if value is None else _string_list(value)


def _candidate_modes_from_object(
    raw_item: Any,
    agent_card: Any | None,
    *attrs: str,
    default: list[str],
) -> list[str]:
    value = _first_attr_value(raw_item, *attrs)
    if value is None and agent_card is not None:
        value = _first_attr_value(agent_card, *attrs)
    return list(default) if value is None else _string_list(value)


def _capability_summary_from_mapping(raw_item: Mapping[str, Any]) -> str:
    summary = _optional_str(_first_mapping_value(raw_item, "capability_summary"))
    if summary is not None:
        return summary

    description = _optional_str(_first_mapping_value(raw_item, "description"))
    if description is not None:
        return description

    capabilities = _first_mapping_value(raw_item, "capabilities", "skills")
    capability_list = _skill_list(capabilities)
    if capability_list:
        return ", ".join(capability_list)
    return ""


def _capability_summary_from_object(raw_item: Any) -> str:
    summary = _optional_str(_first_attr_value(raw_item, "capability_summary"))
    if summary is not None:
        return summary

    description = _optional_str(_first_attr_value(raw_item, "description"))
    if description is not None:
        return description

    capabilities = _first_attr_value(raw_item, "capabilities", "skills")
    capability_list = _skill_list(capabilities)
    if capability_list:
        return ", ".join(capability_list)
    return ""


def _status_from_mapping(raw_item: Mapping[str, Any]) -> str | None:
    status = _optional_str(_first_mapping_value(raw_item, "status", "agent_status"))
    if status is not None:
        return status
    return _status_from_health(_first_mapping_value(raw_item, "is_healthy", "healthy"))


def _status_from_object(raw_item: Any) -> str | None:
    raw_status = _first_attr_value(raw_item, "status", "agent_status")
    status_value = getattr(raw_status, "value", raw_status)
    status = _optional_str(status_value)
    if status is not None:
        return status
    return _status_from_health(_first_attr_value(raw_item, "is_healthy", "healthy"))


def _status_from_health(value: Any) -> str | None:
    if not isinstance(value, bool):
        return None
    return "active" if value else "inactive"


def _authorization_kind(source: str) -> AuthorizationKind:
    try:
        return _AUTHORIZATION_KIND_BY_SOURCE[source]
    except KeyError as exc:
        supported = ", ".join(sorted(_AUTHORIZATION_KIND_BY_SOURCE))
        raise ValueError(
            f"unsupported candidate scope source {source!r}; expected one of: "
            f"{supported}"
        ) from exc


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [
        item for item in (_optional_str(item) for item in value) if item is not None
    ]


def _skill_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            skill = _optional_str(item)
        else:
            skill = _optional_str(_first_attr_value(item, "id", "name"))
            if skill is None and isinstance(item, Mapping):
                skill = _optional_str(_first_mapping_value(item, "id", "name"))
        if skill is not None:
            result.append(skill)
    return result


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _looks_like_agent_mapping(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("agent_id", "id"))


def _looks_like_scope_mapping(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "source",
            "mode",
            "candidate_scope_mode",
            "group_id",
            "candidate_scope_group_id",
            "revision",
            "snapshot_version",
            "candidate_scope_snapshot_version",
        )
    )


def _looks_like_scope_object(value: Any) -> bool:
    return any(
        hasattr(value, attr)
        for attr in (
            "agents",
            "agent_ids",
            "candidate_agents",
            "candidate_agent_ids",
        )
    )


def _first_mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _first_attr_value(value: Any, *attrs: str) -> Any | None:
    for attr in attrs:
        if hasattr(value, attr):
            return getattr(value, attr)
    return None


def _first_card_value(card: Any, *keys: str) -> Any | None:
    if isinstance(card, Mapping):
        return _first_mapping_value(card, *keys)
    return _first_attr_value(card, *keys)
