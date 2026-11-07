"""The instance settings owner behind the thin ``settings_routes`` adapter.

The view reads ``config.json`` on every call and compares it with what is
running: the backend's startup ``Settings`` (with the live log level), the
startup document's frontend section, and the revision the frontend and agents
were started with (``runtime.json`` in the image). Values of keys outside the
registry are never returned.

A write edits only registry keys under ``backend``/``frontend``, with the
startup validation rules, if config.json still has the revision the editor
read. ``backend.log_level`` applies live; everything else after a restart.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import BaseModel, ValidationError

from common.config.loader import (
    BackendFieldError,
    FrontendSettings,
    Settings,
    validate_backend,
)
from common.config.runtime_config import RuntimeConfig, RuntimeConfigurationError
from common.config.runtime_store import (
    RuntimeConfigStore,
    SettingsCandidateInvalid,
    SettingsFileInvalid,
    SettingsRevisionConflict,
)
from common.observability.logging import set_log_level
from instance_settings.models import (
    RestartAccepted,
    RestartStatus,
    SettingsField,
    SettingsFieldError,
    SettingsGroup,
    SettingsNotice,
    SettingsSaved,
    SettingsUpdate,
    SettingsView,
    SystemInfo,
)
from instance_settings.protocols import SettingsRequestError
from instance_settings.registry import (
    FIELDS,
    GROUPS,
    FieldSpec,
    Scalar,
    field_default,
    field_spec,
    is_field,
    json_type_matches,
    option_matches,
)
from instance_settings.restart import RestartControl

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
        restart: RestartControl,
    ) -> None:
        self._store = store
        self._settings = settings
        self._startup = startup_config
        self._can_configure = can_configure
        self._version = version
        self._restart = restart
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

    async def update(self, request: SettingsUpdate) -> SettingsSaved:
        unknown = sum(1 for key in request.values if not is_field(key))
        if unknown:
            # The client's keys are never echoed.
            message = (
                "1 field is not an editable setting."
                if unknown == 1
                else f"{unknown} fields are not editable settings."
            )
            raise _invalid(
                (SettingsFieldError(field=None, code="unknown_field", message=message),)
            )
        specs = tuple(field_spec(key) for key in request.values)
        _check_types(specs, request.values)
        edits = {(spec.section, spec.name): request.values[spec.key] for spec in specs}
        try:
            previous, current = await asyncio.to_thread(
                self._store.update_settings,
                edits,
                expected_revision=request.revision,
                validate=lambda document: _normalize(specs, document),
            )
        except SettingsFileInvalid as exc:
            raise SettingsRequestError("config_invalid", 409, str(exc)) from None
        except SettingsRevisionConflict as exc:
            raise SettingsRequestError("revision_conflict", 409, str(exc)) from None
        except SettingsCandidateInvalid as exc:
            raise SettingsRequestError("invalid_settings", 422, str(exc)) from None
        except (RuntimeConfigurationError, OSError):
            raise SettingsRequestError(
                "settings_unavailable", 503, "Cannot write config.json."
            ) from None
        before = _Values(previous, image=self.image)
        after = _Values(current, image=self.image)
        changed = tuple(
            spec.key
            for spec in specs
            if (before.present(spec), before.value(spec))
            != (after.present(spec), after.value(spec))
        )
        log_level = field_spec("backend.log_level")
        if log_level in specs:
            level = after.value(log_level)
            assert isinstance(level, str)
            if level != self._log_level:
                set_log_level(level)
                self._log_level = level
        view = await self._view(current, None)
        return SettingsSaved(
            changed=changed, restart_required=view.restart_pending, settings=view
        )

    async def restart(self) -> RestartAccepted:
        """Validate and record a restart; ``launch_restart`` starts it."""
        return RestartAccepted(restart=await self._restart.request())

    def launch_restart(self) -> None:
        self._restart.launch()

    async def _view(
        self, config: RuntimeConfig | None, error: str | None
    ) -> SettingsView:
        runtime, restart = await asyncio.to_thread(self._read_runtime)
        return self._build_view(config, error, runtime, restart)

    def _read_runtime(self) -> tuple[dict[str, object] | None, RestartStatus]:
        return self._runtime_record(), self._restart.status()

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


def _invalid(errors: tuple[SettingsFieldError, ...]) -> SettingsRequestError:
    return SettingsRequestError(
        "invalid_settings", 422, "Some settings are invalid.", errors=errors
    )


def _check_types(specs: tuple[FieldSpec, ...], values: Mapping[str, object]) -> None:
    """Refuse values of the wrong JSON type before the loader can coerce them."""
    errors: list[SettingsFieldError] = []
    for spec in specs:
        value = values[spec.key]
        if value is None:
            continue
        if not json_type_matches(spec, value):
            errors.append(
                SettingsFieldError(
                    field=spec.key,
                    code="type_mismatch",
                    message=f"Expected a value of type {spec.kind}.",
                )
            )
        elif spec.kind == "enum" and not option_matches(spec, str(value)):
            assert spec.options is not None
            errors.append(
                SettingsFieldError(
                    field=spec.key,
                    code="invalid_option",
                    message="Choose one of " + ", ".join(spec.options) + ".",
                )
            )
    if errors:
        raise _invalid(tuple(errors))


def _normalize(
    specs: tuple[FieldSpec, ...], document: dict[str, object]
) -> dict[str, object]:
    """Validate a candidate as startup does; write the validated values.

    Runs inside the store lock. Field errors name ``section.name`` and carry
    pydantic's message without the input.
    """
    errors: list[SettingsFieldError] = []
    validated: dict[str, BaseModel] = {}
    for section, model in (("backend", Settings), ("frontend", FrontendSettings)):
        values = document.get(section, {})
        assert isinstance(values, dict)
        try:
            validated[section] = _validated(values, model)
        except BackendFieldError as exc:
            errors.append(
                SettingsFieldError(
                    field=f"{section}.{exc.field}", code="value_error", message=str(exc)
                )
            )
        except ValidationError as exc:
            errors.extend(
                SettingsFieldError(
                    field=f"{section}.{error['loc'][0]}" if error["loc"] else None,
                    code=error["type"],
                    message=error["msg"],
                )
                for error in exc.errors(
                    include_input=False, include_url=False, include_context=False
                )
            )
    if errors:
        raise _invalid(tuple(errors))
    for spec in specs:
        values = document.get(spec.section)
        if isinstance(values, dict) and spec.name in values:
            values[spec.name] = getattr(validated[spec.section], spec.name)
    return document


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


__all__ = ["InstanceSettings", "SettingsRequestError"]
