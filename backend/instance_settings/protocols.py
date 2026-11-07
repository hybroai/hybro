"""The settings surface ``api_gateway`` consumes."""

from __future__ import annotations

from typing import Protocol

from instance_settings.models import SettingsErrorBody, SettingsFieldError, SettingsView


class SettingsRequestError(Exception):
    """A safe, coded settings failure the route returns as ``SettingsErrorBody``."""

    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        errors: tuple[SettingsFieldError, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.errors = errors

    def body(self) -> SettingsErrorBody:
        return SettingsErrorBody(detail=str(self), code=self.code, errors=self.errors)


class SettingsAccess(Protocol):
    def can_configure(self, user_id: str) -> bool: ...

    async def view(self) -> SettingsView: ...


__all__ = ["SettingsAccess", "SettingsRequestError"]
