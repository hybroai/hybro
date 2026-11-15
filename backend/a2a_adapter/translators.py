from typing import Any

from common.dto import AgentCardInterface, AgentCardSnapshot
from common.types import Artifact, Message, Part, Task, TaskState, TaskStatus

from .message_factory import from_sdk_message, to_internal_part
from .webhook_payloads import (
    task_from_artifact_update,
    task_from_status_update,
    task_from_task_frame,
)


def a2a_card_to_snapshot(card: Any, agent_url: str) -> AgentCardSnapshot:
    base_url = _card_base_url(card, agent_url)
    raw_card = _to_raw_dict(card)
    # A 1.0 card states only its interfaces, so its ProtoJSON has no `url`. Any
    # consumer that rebuilds an internal card from `raw_card` would then fall
    # back to deriving one from an interface — the same wrong-base problem this
    # function exists to avoid. Carry the resolved base in `raw_card` so every
    # reader sees it.
    if base_url and not raw_card.get("url"):
        raw_card["url"] = base_url
    capabilities_data = _read(card, "capabilities") or {}
    input_modes = _read(card, "default_input_modes", "defaultInputModes", "input_modes")
    output_modes = _read(
        card,
        "default_output_modes",
        "defaultOutputModes",
        "output_modes",
    )
    return AgentCardSnapshot(
        agent_id=_first_non_empty(
            _read(card, "id"),
            _read(card, "agent_id"),
            _read(card, "name"),
            agent_url,
        ),
        url=base_url,
        name=_read(card, "name"),
        description=_read(card, "description"),
        capabilities=_normalize_capabilities(
            capabilities_data,
            input_modes or [],
            output_modes or [],
        ),
        raw_card=raw_card,
        interfaces=_card_interfaces(card),
    )


def _interface_url(interfaces: Any) -> str | None:
    """First advertised endpoint URL, or None when no interface declares one."""
    for item in _as_sequence(interfaces):
        url = _read(item, "url")
        if url:
            return str(url)
    return None


def _card_base_url(card: Any, agent_url: str) -> str:
    """Resolve the agent's base URL.

    ``AgentCardSnapshot.url`` is Hybro's notion of *where the agent lives*: it is
    the identity used for de-duplication, the target the health probe appends the
    well-known card path to, and the fallback call target. It is therefore the
    base the card was discovered at, not an interface endpoint — a 1.0 card may
    advertise interfaces on a subpath (``https://host/a2a/v1``), and treating one
    of those as the base makes the card unreachable at
    ``<base>/.well-known/agent-card.json``.

    Call endpoints are carried separately in ``interfaces``.
    """
    return _first_non_empty(
        agent_url,
        _interface_url(_read(card, "supported_interfaces", "supportedInterfaces")),
    )


def _card_interfaces(card: Any) -> list[AgentCardInterface]:
    """Project the card's ``supportedInterfaces`` without guessing endpoints.

    Consumers select a binding from this explicit list instead of re-deriving
    one from raw protocol fields.
    """
    declared = _as_sequence(_read(card, "supported_interfaces", "supportedInterfaces"))
    interfaces: list[AgentCardInterface] = []
    for item in declared:
        url = _read(item, "url")
        if not url:
            continue
        interfaces.append(
            AgentCardInterface(
                url=str(url),
                protocol_binding=_read(item, "protocol_binding", "protocolBinding"),
                protocol_version=_read(item, "protocol_version", "protocolVersion"),
                tenant=_read(item, "tenant"),
            )
        )
    return interfaces


def _as_sequence(value: Any) -> list[Any]:
    """Read a repeated field as a list.

    Protobuf repeated fields are not Python lists, so ``isinstance(x, list)``
    would silently drop every 1.0 interface and capability extension.
    """
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def coerce_parts(parts: list[Any] | None) -> list[Part]:
    """Map SDK parts, wire dicts, or internal parts onto internal parts."""
    return [to_internal_part(part) for part in parts or []]


def message_to_completed_task(
    message: Message,
    context_id: str,
    *,
    task_id: str,
    artifact_id: str,
) -> Task:
    """Convert a message response into a completed task artifact."""
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.completed),
        artifacts=[
            Artifact(
                artifact_id=artifact_id,
                name="response",
                parts=coerce_parts(message.parts),
            )
        ],
    )


def facade_result_to_model(response: dict[str, Any]) -> Message | Task:
    """Convert a normalized facade response into an internal Message or Task.

    The facade yields one 1.0 ``StreamResponse`` frame per call, tagged with the
    member that was present (``task`` / ``message``) or, for streaming calls,
    the event kind (``status_update`` / ``artifact_update``). Frame parsing is
    shared with direct-stream parsing in ``webhook_payloads`` so both agree on
    artifact semantics, status messages, and HITL metadata.
    """
    kind = response.get("kind")
    result = response.get("result") or {}
    if kind == "message":
        # A message frame *is* the message; there is no wrapping key. Callers
        # expect the internal Message here (they branch on its kind), so the
        # ProtoJSON body is mapped explicitly rather than re-validated.
        return from_sdk_message(result)
    if kind == "task":
        return task_from_task_frame(result)
    if kind == "status-update":
        return task_from_status_update(result, "")
    if kind == "artifact-update":
        return task_from_artifact_update(result)
    raise ValueError(str(response.get("error") or "Unknown A2A response"))


def _normalize_capabilities(
    capabilities_data: Any,
    input_modes: list[Any],
    output_modes: list[Any],
) -> list[str]:
    capabilities: set[str] = set()
    if isinstance(capabilities_data, list):
        capabilities.update(_string_value(item) for item in capabilities_data)
    elif isinstance(capabilities_data, dict):
        capabilities.update(
            _string_value(item)
            for item in capabilities_data.get("capabilities", [])
            if item
        )
        capabilities.update(_extension_names(capabilities_data.get("extensions")))
    else:
        capabilities.update(_extension_names(_read(capabilities_data, "extensions")))

    for key in ("streaming", "stream"):
        if bool(_read(capabilities_data, key)):
            capabilities.add("streaming")
    for key in ("push_notifications", "pushNotifications", "push-notifications"):
        if bool(_read(capabilities_data, key)):
            capabilities.add("push_notifications")

    capabilities.update(f"input:{_string_value(mode)}" for mode in input_modes)
    capabilities.update(f"output:{_string_value(mode)}" for mode in output_modes)
    return sorted(cap for cap in capabilities if cap)


def _extension_names(extensions: Any) -> set[str]:
    """Named extensions declared by a card's capabilities."""
    names: set[str] = set()
    for extension in _as_sequence(extensions):
        name = _read(extension, "name") or _read(extension, "uri")
        if name:
            names.add(_string_value(name))
    return names


def _to_raw_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _jsonable(value)
    to_dict = getattr(value, "model_dump", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict(mode="json", by_alias=True))
        except TypeError:
            return _jsonable(to_dict())
    if _is_protobuf_message(value):
        # 1.0 cards are protobuf; the wire JSON is its camelCase ProtoJSON form.
        from google.protobuf.json_format import MessageToDict

        return _jsonable(MessageToDict(value))
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return {}


def _is_protobuf_message(value: Any) -> bool:
    return hasattr(value, "DESCRIPTOR") and hasattr(value, "ListFields")


def _read(value: Any, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is not None and value != "":
            return _string_value(value)
    return ""


def _string_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_jsonable(item) for item in value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", by_alias=True)
        except TypeError:
            return value.model_dump()
    return value


__all__ = ["a2a_card_to_snapshot"]
