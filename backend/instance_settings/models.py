"""Settings API request and response DTOs.

The frontend Settings page consumes these shapes; they may still change before
that page ships.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from common.dto.base import FrozenDTO
from instance_settings.registry import FIELDS

RestartState = Literal["idle", "requested", "restarting", "completed", "failed"]
NoticeCode = Literal["removed_fields", "instances_ignored", "unlisted_fields"]
# Values are JSON scalars typed strictly, so "12" is never coerced to 12 nor
# true to 1 before the loader validates them.
FieldValue = StrictBool | StrictInt | StrictFloat | StrictStr | None
SubmittedValue = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[StrictStr, StringConstraints(max_length=2048)]
    | None
)


class _SettingsDTO(FrozenDTO):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RestartStatus(_SettingsDTO):
    available: bool
    state: RestartState
    error: str | None = None
    updated_at: float | None = None


class SettingsGroup(_SettingsDTO):
    id: str
    label: str


class SettingsField(_SettingsDTO):
    key: str
    group: str
    level: Literal["basic", "advanced"]
    apply: Literal["live", "restart"]
    label: str
    description: str
    kind: Literal["boolean", "integer", "number", "url", "enum"]
    options: tuple[str, ...] | None
    nullable: bool
    minimum: float | None
    maximum: float | None
    exclusive_minimum: float | None
    default: FieldValue
    value: FieldValue
    source: Literal["config", "default"]
    # The file's value differs from the running one until a restart applies it.
    restart_pending: bool


class SettingsNotice(_SettingsDTO):
    code: NoticeCode
    # Setting keys only, never values.
    fields: tuple[str, ...]
    message: str


class SystemInfo(_SettingsDTO):
    version: str
    deployment: Literal["image", "checkout"]
    port: int | None
    listen: Literal["loopback", "public"] | None
    auth_mode: Literal["mock", "clerk"]
    mcp_available: bool | None
    config_location: str


class SettingsView(_SettingsDTO):
    # settings_revision of config.json; None when the file is invalid.
    revision: str | None
    config_error: str | None
    restart_pending: bool
    restart: RestartStatus
    groups: tuple[SettingsGroup, ...]
    fields: tuple[SettingsField, ...]
    notices: tuple[SettingsNotice, ...]
    system: SystemInfo


class SettingsUpdate(_SettingsDTO):
    revision: str = Field(min_length=1, max_length=128)
    # Registry keys; null removes the key from config.json (back to default).
    values: dict[str, SubmittedValue] = Field(min_length=1, max_length=len(FIELDS))


class SettingsSaved(_SettingsDTO):
    # Keys whose stored value changed; a no-op edit lists none.
    changed: tuple[str, ...]
    restart_required: bool
    settings: SettingsView


class SettingsFieldError(_SettingsDTO):
    # A registry key, or None when the error is not about one submitted key.
    field: str | None
    code: str
    message: str


class SettingsErrorBody(_SettingsDTO):
    detail: str
    code: str
    errors: tuple[SettingsFieldError, ...] = ()


__all__ = [
    "RestartState",
    "RestartStatus",
    "SettingsErrorBody",
    "SettingsField",
    "SettingsFieldError",
    "SettingsGroup",
    "SettingsNotice",
    "SettingsSaved",
    "SettingsUpdate",
    "SettingsView",
    "SystemInfo",
]
