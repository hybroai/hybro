from __future__ import annotations

from typing import Any

from execution.hitl.service import BoundHITLServiceProxy, HITLService


def create_hitl_service(**kwargs: Any) -> HITLService:
    allowed = {
        "persistence",
        "delivery",
        "agent_reply",
        "continuation",
        "task_notifications",
        "terminal_lifecycle",
        "lifecycle",
        "application",
        "room_files",
        "canonical_control_publisher",
        "lifecycle_family_reader",
        "canonical_run_state_reader",
        "supervisor_resume",
        "canonical_cancellation_requester",
        "public_secret_values",
    }
    legacy_aliases = {
        "store",
        "database" + "_service",
        "db" + "_service",
        "a2a" + "_" + "service",
    }
    for name in kwargs:
        if name in legacy_aliases:
            raise TypeError(f"create_hitl_service no longer accepts {name!r}")
        if name not in allowed:
            raise TypeError(f"create_hitl_service got unexpected argument {name!r}")

    service = HITLService(
        continuation=kwargs.get("continuation"),
        task_notifications=kwargs.get("task_notifications"),
        terminal_lifecycle=kwargs.get("terminal_lifecycle"),
        lifecycle=kwargs.get("lifecycle"),
        application=kwargs.get("application"),
        room_files=kwargs.get("room_files"),
        canonical_control_publisher=kwargs.get("canonical_control_publisher"),
        lifecycle_family_reader=kwargs.get("lifecycle_family_reader"),
        canonical_run_state_reader=kwargs.get("canonical_run_state_reader"),
        supervisor_resume=kwargs.get("supervisor_resume"),
        canonical_cancellation_requester=kwargs.get("canonical_cancellation_requester"),
        public_secret_values=kwargs.get("public_secret_values", ()),
    )
    service._persistence = kwargs.get("persistence")
    service._delivery = kwargs.get("delivery")
    service._agent_reply = kwargs.get("agent_reply")
    service._continuation = kwargs.get("continuation")
    service._task_notifications = kwargs.get("task_notifications")
    return service


__all__ = ["BoundHITLServiceProxy", "create_hitl_service"]
