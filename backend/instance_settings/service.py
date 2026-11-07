"""The instance settings owner behind the thin ``settings_routes`` adapter.

The view reads ``config.json`` on every call and compares it with what is
running: the backend's startup ``Settings`` (with the live log level), the
startup document's frontend section, and the revision the frontend and agents
were started with (``runtime.json`` in the image). Values of keys outside the
registry are never returned.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import BaseModel

from common.config.loader import FrontendSettings, Settings, validate_backend
from common.config.runtime_config import RuntimeConfig, RuntimeConfigurationError
from common.config.runtime_store import RuntimeConfigStore
from instance_settings.models import (
    RestartStatus,
    SettingsField,
    SettingsGroup,
    SettingsNotice,
    SettingsView,
    SystemInfo,
)
from instance_settings.registry import (
    FIELDS,
    GROUPS,
    FieldSpec,
    Scalar,
    field_default,
    is_field,
)

# Shown instead of a path: the backend cannot know the host's location.
CONFIG_LOCATION = "~/.hybro/config.json (or $HYBRO_HOME/config.json)"
_MAX_RUNTIME_FILE_BYTES = 64 * 1024
_NOTICE_MESSAGES = {
    "removed_fields": (
        "These retired settings are ignored and are dropped on the next save."
    ),
    "instances_ignored": (
        "The top-level instances section is ignored; choose the port with hybro --port."
    ),
    "unlisted_fields": (
        "These keys are set in config.json but cannot be edited on this page."
    ),
}


# Until the restart trigger is wired, the view reports it as unavailable.
_UNAVAILABLE_RESTART = RestartStatus(available=False, state="idle")


def _validated(section: Mapping[str, object], model: type[BaseModel]) -> BaseModel:
    if model is Settings:
        return validate_backend(dict(section))
    return model.model_validate(dict(section))


class _Values:
    """Effective values of one document's registry fields."""

    def __init__(self, config: RuntimeConfig, *, image: bool) -> None:
        self.config = config
        self.image = image
        self.models: dict[str, BaseModel] = {
            "backend": _validated(config.backend, Settings),
            "frontend": _validated(config.frontend, FrontendSettings),
        }

    def present(self, spec: FieldSpec) -> bool:
        return spec.name in getattr(self.config, spec.section)

    def value(self, spec: FieldSpec) -> Scalar:
        if not self.present(spec):
            return field_default(spec, image=self.image)
        return getattr(self.models[spec.section], spec.name)


class InstanceSettings:
    def __init__(
        self,
        *,
        store: RuntimeConfigStore,
        settings: Settings,
        startup_config: RuntimeConfig,
        can_configure: Callable[[str], bool],
        version: str,
    ) -> None:
        self._store = store
        self._settings = settings
        self._startup = startup_config
        self._can_configure = can_configure
        self._version = version
        self._runtime_file = (
            Path(settings.instance_runtime_file)
            if settings.instance_runtime_file
            else None
        )
        self._startup_frontend = FrontendSettings.model_validate(
            dict(startup_config.frontend)
        )
        # The running log level; a settings write applies and records it live.
        self._log_level = settings.log_level

    @property
    def image(self) -> bool:
        return self._runtime_file is not None

    def can_configure(self, user_id: str) -> bool:
        return self._can_configure(user_id)

    async def view(self) -> SettingsView:
        try:
            config: RuntimeConfig | None = await asyncio.to_thread(
                self._store.read_config
            )
            error = None
        except RuntimeConfigurationError as exc:
            # Store messages are safe: they never carry config values.
            config, error = None, str(exc)
        except OSError:
            config, error = None, "Cannot read config.json."
        return await self._view(config, error)

    async def _view(
        self, config: RuntimeConfig | None, error: str | None
    ) -> SettingsView:
        runtime, restart = await asyncio.to_thread(self._read_runtime)
        return self._build_view(config, error, runtime, restart)

    def _read_runtime(self) -> tuple[dict[str, object] | None, RestartStatus]:
        return self._runtime_record(), _UNAVAILABLE_RESTART

    def _runtime_record(self) -> dict[str, object] | None:
        """``runtime.json`` in the image; None in a checkout or when unreadable."""
        if self._runtime_file is None:
            return None
        try:
            with self._runtime_file.open("rb") as stream:
                data = stream.read(_MAX_RUNTIME_FILE_BYTES + 1)
            if len(data) > _MAX_RUNTIME_FILE_BYTES:
                return None
            record = json.loads(data)
        except (OSError, ValueError, RecursionError):
            return None
        return record if isinstance(record, dict) else None

    def _running(self, spec: FieldSpec) -> Scalar:
        if spec.section == "frontend":
            return getattr(self._startup_frontend, spec.name)
        if spec.name == "log_level":
            return self._log_level
        return getattr(self._settings, spec.name)

    def _build_view(
        self,
        config: RuntimeConfig | None,
        error: str | None,
        runtime: dict[str, object] | None,
        restart: RestartStatus,
    ) -> SettingsView:
        # An invalid file has no values: show the running ones instead.
        values = _Values(
            config if config is not None else self._startup, image=self.image
        )
        fields: list[SettingsField] = []
        for spec in FIELDS:
            running = self._running(spec)
            value = values.value(spec) if config is not None else running
            fields.append(
                SettingsField(
                    key=spec.key,
                    group=spec.group,
                    level=spec.level,
                    apply=spec.apply,
                    label=spec.label,
                    description=spec.description,
                    kind=spec.kind,
                    options=spec.options,
                    nullable=spec.nullable,
                    minimum=spec.minimum,
                    maximum=spec.maximum,
                    exclusive_minimum=spec.exclusive_minimum,
                    default=field_default(spec, image=self.image),
                    value=value,
                    source="config" if values.present(spec) else "default",
                    restart_pending=value != running,
                )
            )
        services_revision = self._startup.restart_revision
        if runtime is not None and isinstance(runtime.get("services_revision"), str):
            services_revision = runtime["services_revision"]  # type: ignore[assignment]
        return SettingsView(
            revision=config.settings_revision if config is not None else None,
            config_error=error,
            restart_pending=(
                config is not None and config.restart_revision != services_revision
            ),
            restart=restart,
            groups=tuple(SettingsGroup(id=g.id, label=g.label) for g in GROUPS),
            fields=tuple(fields),
            notices=_notices(config) if config is not None else (),
            system=self._system(runtime),
        )

    def _system(self, runtime: dict[str, object] | None) -> SystemInfo:
        port = runtime.get("public_port") if runtime is not None else None
        listen = runtime.get("listen") if runtime is not None else None
        if not isinstance(port, int) or isinstance(port, bool):
            port = None
        if listen not in ("loopback", "public"):
            listen = None
        return SystemInfo(
            version=self._version,
            deployment="image" if self.image else "checkout",
            port=port,
            listen=listen,
            auth_mode=self._settings.auth_mode,
            # MCP is served without sign-in, so only a loopback listener offers it.
            mcp_available=listen == "loopback" if listen is not None else None,
            config_location=CONFIG_LOCATION,
        )


def _notices(config: RuntimeConfig) -> tuple[SettingsNotice, ...]:
    notices: list[SettingsNotice] = []
    if config.removed_backend_fields:
        notices.append(
            _notice(
                "removed_fields",
                tuple(f"backend.{name}" for name in config.removed_backend_fields),
            )
        )
    if config.had_instances:
        notices.append(_notice("instances_ignored", ()))
    # Names the strict models accepted as known fields; never their values.
    unlisted = tuple(
        sorted(
            key
            for section in ("backend", "frontend")
            for key in (f"{section}.{name}" for name in getattr(config, section))
            if not is_field(key)
        )
    )
    if unlisted:
        notices.append(_notice("unlisted_fields", unlisted))
    return tuple(notices)


def _notice(code: str, fields: tuple[str, ...]) -> SettingsNotice:
    return SettingsNotice(code=code, fields=fields, message=_NOTICE_MESSAGES[code])


__all__ = ["InstanceSettings"]
