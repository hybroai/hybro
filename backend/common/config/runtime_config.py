"""Canonical runtime configuration and credential-source validation.

No environment, application settings, or Provider clients are loaded on import.
Callers supply the already-resolved deployment environment explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

ProviderId = Literal["openai", "deepseek", "anthropic"]
AuthMethod = Literal["api_key", "oauth"]
ModelId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

_PROVIDER_ENV_KEYS: dict[ProviderId, str] = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class RuntimeConfigurationError(ValueError):
    """Safe configuration failure: messages must never include credential input."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )


class RuntimeProvider(_StrictModel):
    id: ProviderId
    auth: AuthMethod

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.auth == "oauth" and self.id != "openai":
            raise ValueError("Only OpenAI supports OAuth configuration")
        return self


class RuntimeModels(_StrictModel):
    text: ModelId
    image: ModelId | None = None


class RuntimeConfig(_StrictModel):
    version: Literal[1] = 1
    # Both present or both absent; absent means no Provider is configured yet.
    provider: RuntimeProvider | None = None
    models: RuntimeModels | None = None
    backend: dict[str, object] = Field(default_factory=dict)
    frontend: dict[str, object] = Field(default_factory=dict)
    image_size: Literal["1024x1024", "1536x1024", "1024x1536", "auto"] = "1024x1024"
    _removed_backend_fields: tuple[str, ...] = PrivateAttr(default=())
    _had_instances: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def record_removed_backend_fields(cls, data: object, handler) -> Self:
        from common.config.loader import strip_removed_fields

        removed: tuple[str, ...] = ()
        had_instances = False
        if isinstance(data, Mapping):
            if isinstance(data.get("backend"), Mapping):
                _, removed = strip_removed_fields(dict(data["backend"]))
            # The 0.3 CLI's host ports; the 0.4 script takes --port instead.
            if "instances" in data:
                had_instances = True
                data = {k: v for k, v in data.items() if k != "instances"}
        config = handler(data)
        if removed:
            config._removed_backend_fields = removed
        config._had_instances = had_instances
        return config

    @property
    def removed_backend_fields(self) -> tuple[str, ...]:
        """Retired backend keys dropped while this document was validated."""
        return self._removed_backend_fields

    @property
    def had_instances(self) -> bool:
        """Whether the document still carried the retired top-level ``instances``."""
        return self._had_instances

    @field_validator("backend")
    @classmethod
    def validate_backend_settings(cls, value: dict[str, object]) -> dict[str, object]:
        from common.config.loader import strip_removed_fields, validate_backend

        pruned, _ = strip_removed_fields(value)
        validate_backend(pruned)
        return pruned

    @field_validator("frontend")
    @classmethod
    def validate_frontend_settings(cls, value: dict[str, object]) -> dict[str, object]:
        from common.config.loader import DERIVED_FRONTEND_FIELDS, FrontendSettings

        # The browser serves MCP from its own origin; none is a user setting.
        if DERIVED_FRONTEND_FIELDS.intersection(value):
            raise PydanticCustomError(
                "derived_mcp_url",
                "The MCP URL is the app's address plus /mcp; remove frontend.mcp_url",
            )
        FrontendSettings.model_validate(value)
        return value

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if (self.provider is None) != (self.models is None):
            raise ValueError("Provider and models must be set together")
        return self

    @property
    def configured(self) -> bool:
        return self.provider is not None

    @model_validator(mode="after")
    def validate_api_prefixes(self) -> Self:
        if "api_prefix" in self.frontend and self.frontend[
            "api_prefix"
        ] != self.backend.get("api_prefix", "/api/v1"):
            raise ValueError("Frontend and backend API prefixes must match")
        return self

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        # Literal[1] otherwise accepts True or 1.0 because Python equates them.
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @property
    def revision(self) -> str:
        canonical = json.dumps(
            self.model_dump(exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class ApiKeyCredential(_StrictModel):
    provider: ProviderId
    auth: Literal["api_key"] = "api_key"
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key must not be blank")
        return value


def account_id(token: str) -> str:
    """Extract unsigned account metadata; reject malformed/header-unsafe claims."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(token) > 32768:
            raise ValueError
        payload = parts[1]
        decoded = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(decoded)["https://api.openai.com/auth"]["chatgpt_account_id"]
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 256
            or not all(c.isascii() and (c.isalnum() or c in "_-") for c in value)
        ):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, RecursionError):
        raise RuntimeConfigurationError(
            "Invalid OAuth account metadata; rerun setup."
        ) from None


class OAuthCredential(_StrictModel):
    """Subscription credentials and unsigned account routing metadata."""

    provider: Literal["openai"] = "openai"
    auth: Literal["oauth"] = "oauth"
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    account_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"),
    ]

    @model_validator(mode="after")
    def validate_account(self) -> Self:
        if account_id(self.access_token.get_secret_value()) != self.account_id:
            raise ValueError("OAuth account metadata mismatch")
        return self

    @field_validator("access_token", "refresh_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("OAuth token must not be blank")
        return value


StoredCredential = Annotated[
    ApiKeyCredential | OAuthCredential, Field(discriminator="auth")
]
CREDENTIAL_ADAPTER: TypeAdapter[StoredCredential] = TypeAdapter(StoredCredential)


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    source: Literal["environment", "stored"]
    credential: ApiKeyCredential | OAuthCredential


def resolve_credential(
    config: RuntimeConfig,
    stored: StoredCredential | None,
    environment: Mapping[str, str],
) -> ResolvedCredential | None:
    """Resolve one source, never substitute a key for an OAuth session.

    An unconfigured document has no credential to resolve and returns None.
    """
    if config.provider is None:
        return None
    if stored is not None and (stored.provider, stored.auth) != (
        config.provider.id,
        config.provider.auth,
    ):
        raise RuntimeConfigurationError(
            "Configuration and stored credential identity differ; rerun setup."
        )
    if config.provider.auth == "oauth":
        if not isinstance(stored, OAuthCredential):
            raise RuntimeConfigurationError("OAuth credential missing; rerun setup.")
        return ResolvedCredential("stored", stored)
    key = environment.get(_PROVIDER_ENV_KEYS[config.provider.id], "")
    if key.strip() and stored is not None:
        raise RuntimeConfigurationError(
            "Both environment and stored credentials are configured; remove one."
        )
    if key.strip():
        credential = ApiKeyCredential(
            provider=config.provider.id, api_key=SecretStr(key)
        )
        return ResolvedCredential("environment", credential)
    if isinstance(stored, ApiKeyCredential):
        return ResolvedCredential("stored", stored)
    raise RuntimeConfigurationError("Provider API key missing; run setup.")
