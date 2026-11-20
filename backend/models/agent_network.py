"""Public payloads for authenticated, caller-visible agent network access."""

from typing import Annotated

from pydantic import BaseModel, Field, JsonValue, StringConstraints

NetworkIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class DiscoveredAgent(BaseModel):
    agent_id: str
    agent_card: dict[str, JsonValue]


class AgentDiscoveryResponse(BaseModel):
    agents: list[DiscoveredAgent]


class AgentGroupRef(BaseModel):
    group_id: str
    name: str


class AgentGroupSummary(AgentGroupRef):
    description: str | None = None
    agent_count: int


class AgentGroupListResponse(BaseModel):
    groups: list[AgentGroupSummary]


class AgentSkillSummary(BaseModel):
    id: str | None = None
    name: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class AgentSkillProfile(AgentSkillSummary):
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=list)
    output_modes: list[str] = Field(default_factory=list)


class AgentSearchMatch(BaseModel):
    """Compact consumer view used to choose an agent."""

    agent_id: str
    name: str | None = None
    description: str | None = None
    skills: list[AgentSkillSummary]
    input_modes: list[str]
    output_modes: list[str]
    groups: list[AgentGroupRef]
    score: float


class AgentSearchResponse(BaseModel):
    agents: list[AgentSearchMatch]


class AgentProfile(BaseModel):
    """Consumer view of a Card, without transport or security details."""

    agent_id: str
    status: str
    name: str | None = None
    description: str | None = None
    version: str | None = None
    provider: str | None = None
    streaming: bool = False
    input_modes: list[str]
    output_modes: list[str]
    skills: list[AgentSkillProfile]
    groups: list[AgentGroupRef]


class AgentNetworkErrorDetail(BaseModel):
    code: str
    message: str


class AgentNetworkErrorResponse(BaseModel):
    detail: AgentNetworkErrorDetail | list[dict[str, JsonValue]]
