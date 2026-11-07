"""The curated settings the Settings page may read and edit.

Only these keys are returned or written; everything else in ``config.json``
(infrastructure, internal tuning, cloud auth, credentials) is never exposed.
Defaults and bounds are read from the loader's pydantic models, so the form and
startup share one set of rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from annotated_types import Ge, Gt, Le
from pydantic import BaseModel

from common.config.loader import FrontendSettings, Settings, image_defaults

Section = Literal["backend", "frontend"]
Level = Literal["basic", "advanced"]
Apply = Literal["live", "restart"]
Kind = Literal["boolean", "integer", "number", "url", "enum"]
Scalar = bool | int | float | str | None


@dataclass(frozen=True, slots=True)
class GroupSpec:
    id: str
    label: str


GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec("runs_agents", "Runs & agents"),
    GroupSpec("discovery", "Local agent discovery"),
    GroupSpec("chat_memory", "Chat & memory"),
    GroupSpec("logging", "Logging"),
    GroupSpec("advanced", "Advanced"),
)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMATS = ("auto", "json", "logfmt")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    section: Section
    name: str
    group: str
    level: Level
    apply: Apply
    label: str
    description: str
    kind: Kind
    options: tuple[str, ...] | None
    nullable: bool
    minimum: float | None
    maximum: float | None
    exclusive_minimum: float | None


def _model(section: Section) -> type[BaseModel]:
    return Settings if section == "backend" else FrontendSettings


def _spec(
    section: Section,
    name: str,
    group: str,
    level: Level,
    kind: Kind,
    label: str,
    description: str,
    *,
    apply: Apply = "restart",
    options: tuple[str, ...] | None = None,
) -> FieldSpec:
    info = _model(section).model_fields[name]
    minimum = maximum = exclusive_minimum = None
    for constraint in info.metadata:
        if isinstance(constraint, Ge):
            minimum = constraint.ge
        elif isinstance(constraint, Gt):
            exclusive_minimum = constraint.gt
        elif isinstance(constraint, Le):
            maximum = constraint.le
    # validate_backend rejects negative numbers for every backend key.
    if kind in ("integer", "number") and minimum is None and exclusive_minimum is None:
        minimum = 0
    return FieldSpec(
        key=f"{section}.{name}",
        section=section,
        name=name,
        group=group,
        level=level,
        apply=apply,
        label=label,
        description=description,
        kind=kind,
        options=options,
        nullable=info.default is None,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
    )


def _profile_fields(profile: Literal["fast", "ultimate"]) -> tuple[FieldSpec, ...]:
    title = profile.capitalize()
    prefix = f"orchestrator_{profile}"
    return (
        _spec(
            "backend",
            f"{prefix}_max_model_turns",
            "advanced",
            "advanced",
            "integer",
            f"{title}: model turns per Run",
            f"Most model turns one {title} Run may take.",
        ),
        _spec(
            "backend",
            f"{prefix}_max_agent_calls",
            "advanced",
            "advanced",
            "integer",
            f"{title}: agent calls per Run",
            f"Most agent calls one {title} Run may make.",
        ),
        _spec(
            "backend",
            f"{prefix}_max_parallel_calls",
            "advanced",
            "advanced",
            "integer",
            f"{title}: parallel agent calls",
            f"Most agent calls a {title} Run makes at once; at most the agent "
            "calls per Run.",
        ),
        _spec(
            "backend",
            f"{prefix}_deadline_seconds",
            "advanced",
            "advanced",
            "number",
            f"{title}: Run deadline (seconds)",
            f"Active time one {title} Run may take; a paused Run's clock stops.",
        ),
    )


FIELDS: tuple[FieldSpec, ...] = (
    _spec(
        "backend",
        "approval_deadline_hours",
        "runs_agents",
        "basic",
        "number",
        "Approval deadline (hours)",
        "How long a Run waits for your approval; empty means no deadline.",
    ),
    _spec(
        "backend",
        "orchestrator_call_wait_ceiling_hours",
        "runs_agents",
        "basic",
        "number",
        "Remote agent wait limit (hours)",
        "How long an accepted remote agent call is awaited.",
    ),
    _spec(
        "backend",
        "a2a_push_callback_base_url",
        "runs_agents",
        "basic",
        "url",
        "Push callback base URL",
        "Public address at which remote agents reach this instance for push "
        "notifications; empty uses polling only.",
    ),
    _spec(
        "backend",
        "agent_health_check_enabled",
        "runs_agents",
        "basic",
        "boolean",
        "Agent health checks",
        "Check registered agents periodically.",
    ),
    _spec(
        "backend",
        "agent_health_check_interval",
        "runs_agents",
        "basic",
        "integer",
        "Health check interval (seconds)",
        "Time between agent health checks.",
    ),
    _spec(
        "backend",
        "local_agent_discovery_enabled",
        "discovery",
        "basic",
        "boolean",
        "Discover local agents",
        "Find A2A agents running on this computer's ports.",
    ),
    _spec(
        "backend",
        "local_agent_discovery_port_start",
        "discovery",
        "basic",
        "integer",
        "First port",
        "First port scanned for local agents.",
    ),
    _spec(
        "backend",
        "local_agent_discovery_port_end",
        "discovery",
        "basic",
        "integer",
        "Last port",
        "Last port scanned for local agents; not below the first port.",
    ),
    _spec(
        "frontend",
        "max_message_length",
        "chat_memory",
        "basic",
        "integer",
        "Maximum message length",
        "Most characters in one chat message.",
    ),
    _spec(
        "backend",
        "compaction_enabled",
        "chat_memory",
        "basic",
        "boolean",
        "Compact long conversations",
        "Keep older turns as references so long conversations fit the model.",
    ),
    _spec(
        "backend",
        "compaction_content_ttl_days",
        "chat_memory",
        "basic",
        "integer",
        "Compacted content retention (days)",
        "How long compacted content is kept; 0 keeps it forever.",
    ),
    _spec(
        "backend",
        "log_level",
        "logging",
        "basic",
        "enum",
        "Log level",
        "Least severe backend log lines written. Applies immediately.",
        apply="live",
        options=LOG_LEVELS,
    ),
    _spec(
        "backend",
        "log_format",
        "logging",
        "basic",
        "enum",
        "Log format",
        "Backend log line format; auto picks logfmt in development.",
        options=LOG_FORMATS,
    ),
    *_profile_fields("fast"),
    *_profile_fields("ultimate"),
    _spec(
        "backend",
        "orchestrator_passthrough_deadline_seconds",
        "advanced",
        "advanced",
        "number",
        "Direct call deadline (seconds)",
        "Active time a Run that calls one mentioned agent may take.",
    ),
    _spec(
        "backend",
        "orchestrator_passthrough_max_transport_retries_per_call",
        "advanced",
        "advanced",
        "integer",
        "Direct call transport retries",
        "Retries of a failed connection to the mentioned agent.",
    ),
    _spec(
        "backend",
        "session_run_start_grace_seconds",
        "advanced",
        "advanced",
        "number",
        "Session send wait (seconds)",
        "How long a session send waits for its Run to settle or pause.",
    ),
    _spec(
        "backend",
        "llm_gateway_max_attempts",
        "advanced",
        "advanced",
        "integer",
        "Model call attempts",
        "Attempts per model request, including the first.",
    ),
    _spec(
        "backend",
        "llm_gateway_request_timeout_seconds",
        "advanced",
        "advanced",
        "number",
        "Model request timeout (seconds)",
        "Time limit of one model request; allow for slow reasoning models.",
    ),
    _spec(
        "backend",
        "llm_gateway_stream_timeout_seconds",
        "advanced",
        "advanced",
        "number",
        "Model stream timeout (seconds)",
        "Time limit of one streamed model response.",
    ),
    _spec(
        "backend",
        "local_agent_discovery_interval_seconds",
        "advanced",
        "advanced",
        "integer",
        "Discovery interval (seconds)",
        "Time between local agent scans.",
    ),
    _spec(
        "frontend",
        "inspection_timeout_ms",
        "advanced",
        "advanced",
        "integer",
        "Agent inspection timeout (ms)",
        "Time limit of an agent inspection request from the browser.",
    ),
)

_BY_KEY = {spec.key: spec for spec in FIELDS}


def field_spec(key: str) -> FieldSpec:
    """The spec of a registry key; ``KeyError`` for any other key."""
    return _BY_KEY[key]


def is_field(key: str) -> bool:
    return key in _BY_KEY


def field_default(spec: FieldSpec, *, image: bool) -> Scalar:
    """The model default, overridden by the image defaults in the image."""
    if image and spec.section == "backend":
        defaults = image_defaults()
        if spec.name in defaults:
            return defaults[spec.name]  # type: ignore[return-value]
    return _model(spec.section).model_fields[spec.name].default


def json_type_matches(spec: FieldSpec, value: object) -> bool:
    """Whether a submitted JSON value has the kind's type, before validation.

    The loader's ``log_level``/``log_format`` validators coerce any input with
    ``str(value or default)``, so this check keeps ``false`` or ``""`` out.
    """
    if spec.kind == "boolean":
        return isinstance(value, bool)
    if spec.kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec.kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


__all__ = [
    "FIELDS",
    "GROUPS",
    "FieldSpec",
    "GroupSpec",
    "field_default",
    "field_spec",
    "is_field",
    "json_type_matches",
]
