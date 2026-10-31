from pydantic import Field, JsonValue

from common.dto.base import FrozenDTO


class AgentInfo(FrozenDTO):
    agent_id: str
    name: str | None = None
    description: str | None = None
    url: str | None = None
    provider_id: str | None = None
    status: str = "active"
    capabilities: list[str] = Field(default_factory=list)
    source: str = "cloud"
    # Set while a bundled agent is gated inactive by an unmet requirement.
    inactive_reason: str | None = None
    is_public: bool = True
    public_url: str | None = None
    rate_limit_per_user_per_hour: int | None = None
    rate_limit_system_per_hour: int | None = None
    call_count: int = 0
    raw_card: dict[str, JsonValue] = Field(default_factory=dict)


class AgentCardInterface(FrozenDTO):
    """Callable transport binding advertised by an agent card."""

    url: str
    protocol_binding: str | None = None
    protocol_version: str | None = None
    tenant: str | None = None


class AgentCardSnapshot(FrozenDTO):
    agent_id: str
    url: str
    name: str | None = None
    description: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    raw_card: dict[str, JsonValue] = Field(default_factory=dict)
    interfaces: list[AgentCardInterface] = Field(default_factory=list)


class AgentMatchResult(FrozenDTO):
    agent_id: str
    score: float = 0.0
    reason: str | None = None
    agent: AgentInfo | None = None


class LocalAgentUpsertResult(FrozenDTO):
    agent_id: str
    managed: bool
    added: bool = False
    reactivated: bool = False


class AgentSkillView(FrozenDTO):
    """One advertised agent skill, as the public agent routes return it."""

    id: str
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCapabilitiesView(FrozenDTO):
    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False
    extensions: bool = False


class AgentCardView(FrozenDTO):
    """The public part of an agent card. Transport URLs are never included."""

    name: str
    description: str | None = None
    version: str | None = None
    provider_organization: str | None = None
    icon_url: str | None = None
    documentation_url: str | None = None
    skills: list[AgentSkillView] = Field(default_factory=list)
    capabilities: AgentCapabilitiesView = Field(default_factory=AgentCapabilitiesView)
    default_input_modes: list[str] = Field(default_factory=list)
    default_output_modes: list[str] = Field(default_factory=list)


class AgentView(AgentCardView):
    """An agent as the public agent routes return it."""

    agent_id: str
    provider_id: str | None = None
    provider_name: str | None = None
    status: str = "active"
    source: str = "cloud"
    # `provider_not_configured` or `image_model_not_configured` while a bundled
    # agent waits for that setup; null otherwise.
    inactive_reason: str | None = None
    is_public: bool = True
    public_url: str | None = None
    call_count: int = 0
    rate_limit_per_user_per_hour: int | None = None
    rate_limit_system_per_hour: int | None = None


class AgentRouteResponse(FrozenDTO):
    """Envelope for the ``/agent/*`` routes."""

    agent: AgentView | None = None
    agents: list[AgentView] | None = None
    card: AgentCardView | None = None
    success: bool
    error: str | None = None
    # Machine-readable reason for a failed request, such as a 409 conflict code.
    error_code: str | None = None
    status_code: int = 200


__all__ = [
    "AgentCapabilitiesView",
    "AgentCardInterface",
    "AgentCardSnapshot",
    "AgentCardView",
    "AgentInfo",
    "AgentRouteResponse",
    "AgentSkillView",
    "AgentView",
    "AgentMatchResult",
    "LocalAgentUpsertResult",
]
