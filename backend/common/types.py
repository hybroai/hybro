"""SDK-free internal task and agent card schemas.

A2A SDK models are intentionally converted at adapter boundaries so common
modules can remain stable even when the external SDK surface changes.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_serializer,
    field_validator,
    model_validator,
)


class FileContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    mimeType: str | None = Field(
        default=None,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )
    bytes: str | None = None
    uri: str | None = None

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault("by_alias", False)
        return super().model_dump(*args, **kwargs)

    @property
    def mime_type(self) -> str | None:
        return self.mimeType

    @mime_type.setter
    def mime_type(self, value: str | None) -> None:
        self.mimeType = value

    @model_validator(mode="after")
    def check_content(self) -> Self:
        if not (self.bytes or self.uri):
            raise ValueError("Either 'bytes' or 'uri' must be present in the file data")
        if self.bytes and self.uri:
            raise ValueError(
                "Only one of 'bytes' or 'uri' can be present in the file data"
            )
        return self


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class RoomArtifactPart(BaseModel):
    """Application file projection; wire content is optional after materialization."""

    kind: Literal["file"] = "file"
    file: FileContent | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_reference(self) -> Self:
        file_id = self.metadata.get("file_id") if self.metadata else None
        if self.file is None and not file_id:
            raise ValueError("A file part requires wire content or metadata.file_id")
        return self


# Transport-facing alias. Persisted projections use the metadata-only shape
# accepted by RoomArtifactPart.
FilePart = RoomArtifactPart


class DataPart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None
    mimeType: str | None = Field(
        default=None,
        validation_alias=AliasChoices("mimeType", "mime_type", "media_type"),
        serialization_alias="mimeType",
    )

    @property
    def mime_type(self) -> str | None:
        return self.mimeType


class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    auth_required = "auth-required"
    policy_required = "policy-required"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"
    rejected = "rejected"
    expired = "expired"


PartUnion = Annotated[
    TextPart | RoomArtifactPart | DataPart,
    Field(discriminator="kind"),
]


class Part(RootModel[PartUnion]):
    """Wrapper that preserves .root access pattern used throughout the codebase."""

    pass


class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"


class Message(BaseModel):
    role: MessageRole
    kind: str = "message"
    message_id: str | None = Field(default=None, alias="messageId")
    context_id: str | None = Field(default=None, alias="contextId")
    task_id: str | None = Field(default=None, alias="taskId")
    parts: list[Part]
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class TaskStatus(BaseModel):
    state: TaskState
    message: Message | None = None
    timestamp: datetime | None = Field(default_factory=datetime.now)

    @field_serializer("timestamp")
    def serialize_dt(self, dt: datetime | None, _info):
        return dt.isoformat() if dt else None


class Artifact(BaseModel):
    artifact_id: str | None = Field(default=None, alias="artifactId")
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None
    index: int = 0
    append: bool | None = None
    lastChunk: bool | None = None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class Task(BaseModel):
    id: str
    kind: str = "task"
    sessionId: str | None = None
    context_id: str | None = Field(default=None, alias="contextId")
    status: TaskStatus
    artifacts: list[Artifact] | None = None
    history: list[Message] | None = None
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class TaskStatusUpdateEvent(BaseModel):
    id: str = Field(
        validation_alias=AliasChoices("id", "taskId"), serialization_alias="taskId"
    )
    kind: str = "status-update"
    context_id: str | None = Field(default=None, alias="contextId")
    status: TaskStatus
    final: bool = False
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    @property
    def task_id(self) -> str:
        return self.id


class TaskArtifactUpdateEvent(BaseModel):
    id: str = Field(
        validation_alias=AliasChoices("id", "taskId"), serialization_alias="taskId"
    )
    kind: str = "artifact-update"
    context_id: str | None = Field(default=None, alias="contextId")
    artifact: Artifact
    append: bool = False
    last_chunk: bool = Field(default=False, alias="lastChunk")
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    @field_validator("append", "last_chunk", mode="before")
    @classmethod
    def _default_nullable_flags(cls, value):
        return False if value is None else value

    @property
    def task_id(self) -> str:
        return self.id


class AgentInterface(BaseModel):
    """SDK-free projection of one callable A2A transport binding.

    Consumers use this to select a callable endpoint (url, binding, protocol
    version, tenant) without inspecting raw protocol cards or SDK objects.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str
    protocol_binding: str | None = Field(default=None, alias="protocolBinding")
    protocol_version: str | None = Field(default=None, alias="protocolVersion")
    tenant: str | None = None


class AgentProvider(BaseModel):
    organization: str
    url: str | None = None


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    streaming: bool | None = False
    pushNotifications: bool | None = False
    stateTransitionHistory: bool | None = False
    extensions: list[dict[str, Any]] | None = None

    @property
    def push_notifications(self) -> bool | None:
        return self.pushNotifications

    @push_notifications.setter
    def push_notifications(self, value: bool | None) -> None:
        self.pushNotifications = value

    @property
    def state_transition_history(self) -> bool | None:
        return self.stateTransitionHistory

    @state_transition_history.setter
    def state_transition_history(self, value: bool | None) -> None:
        self.stateTransitionHistory = value


class AgentAuthentication(BaseModel):
    schemes: list[str]
    credentials: str | None = None


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str | None = None
    tags: list[str] | None = None
    examples: list[str] | None = None
    inputModes: list[str] | None = None
    outputModes: list[str] | None = None


class AgentCard(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    description: str | None = None
    url: str
    provider: AgentProvider | None = None
    version: str
    documentationUrl: str | None = None
    iconUrl: str | None = None
    capabilities: AgentCapabilities
    authentication: AgentAuthentication | None = None
    defaultInputModes: list[str] = ["text"]
    defaultOutputModes: list[str] = ["text"]
    # A2A cards are protobuf messages; ProtoJSON omits an empty repeated
    # ``skills`` field, so a missing key means no skills.
    skills: list[AgentSkill] = Field(default_factory=list)
    protocolVersion: str | None = None
    preferredTransport: str | None = None
    additionalInterfaces: list[dict[str, Any]] | None = None
    supportedInterfaces: list[AgentInterface] | None = None
    security: list[dict[str, list[str]]] | None = None
    securitySchemes: dict[str, Any] | None = None
    signatures: list[dict[str, Any]] | None = None
    supports_authenticated_extended_card: bool | None = Field(
        default=None, alias="supportsAuthenticatedExtendedCard"
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_url_from_interfaces(cls, data):
        """Accept 1.0 cards, which carry interfaces instead of a top-level url.

        ``url`` is the agent's base URL, so it is derived from the advertised
        interfaces only when the data does not carry one at all. An explicit
        value always wins — including an empty string, which callers use to
        blank the field (route masking) and which must not be silently refilled
        with an interface endpoint.
        """
        if not isinstance(data, dict) or data.get("url") is not None:
            return data
        interfaces = data.get("supportedInterfaces") or data.get("additionalInterfaces")
        if isinstance(interfaces, list):
            for interface in interfaces:
                if isinstance(interface, dict) and interface.get("url"):
                    return {**data, "url": interface["url"]}
        return data

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)

    @property
    def documentation_url(self) -> str | None:
        return self.documentationUrl

    @documentation_url.setter
    def documentation_url(self, value: str | None) -> None:
        self.documentationUrl = value

    @property
    def default_input_modes(self) -> list[str]:
        return self.defaultInputModes

    @default_input_modes.setter
    def default_input_modes(self, value: list[str]) -> None:
        self.defaultInputModes = value

    @property
    def default_output_modes(self) -> list[str]:
        return self.defaultOutputModes

    @default_output_modes.setter
    def default_output_modes(self, value: list[str]) -> None:
        self.defaultOutputModes = value
