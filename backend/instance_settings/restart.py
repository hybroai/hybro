"""The backend side of the Settings page's restart trigger.

In the image, ``runtime_entrypoint.py restart`` restarts the services through
supervisord, this backend among them, so it runs detached in its own session.
Before it starts, ``restart --check`` validates the configuration the restart
would apply. Both share ``restart.json``, next to ``runtime.json``:
``{"state", "error", "updated_at"}``; the helper refreshes ``updated_at``
while it works.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen

from instance_settings.models import RestartState, RestartStatus
from instance_settings.protocols import SettingsRequestError

# The helper heartbeats every 10 seconds; a status this old belongs to a
# helper that died.
STALE_SECONDS = 60.0
CHECK_TIMEOUT_SECONDS = 30.0
# The entrypoint scoped the backend's environment; the helper needs only these.
_HELPER_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "HYBRO_HOME",
    "HYBRO_CONTAINER",
)
_STATES: frozenset[str] = frozenset(
    {"idle", "requested", "restarting", "completed", "failed"}
)
_MAX_STATUS_BYTES = 16 * 1024
_MAX_ERROR_CHARS = 1000
_STALE_ERROR = "The restart did not finish; check `hybro logs`."
_CHECK_FAILED = "The restart check failed."


class RestartControl:
    def __init__(
        self,
        *,
        runtime_file: Path | None,
        python: str,
        entrypoint: Path,
        check_timeout: float = CHECK_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runtime_file = runtime_file
        self._python = python
        self.entrypoint = entrypoint
        self._check_timeout = check_timeout
        source = os.environ if environment is None else environment
        self._env = {key: source[key] for key in _HELPER_ENV_KEYS if key in source}
        # One uvicorn worker, so this serializes the check, the requested
        # status write, and the launch.
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Only the image runs the services under supervisord."""
        return self._runtime_file is not None and self._runtime_file.is_file()

    @property
    def _status_file(self) -> Path:
        assert self._runtime_file is not None
        return self._runtime_file.with_name("restart.json")

    def status(self) -> RestartStatus:
        if not self.available:
            return RestartStatus(available=False, state="idle")
        record = self._read_status()
        state = record.get("state")
        if state not in _STATES:
            return RestartStatus(available=True, state="idle")
        error = record.get("error")
        updated_at = record.get("updated_at")
        if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
            updated_at = None
        if state in ("requested", "restarting") and (
            updated_at is None or time.time() - updated_at > STALE_SECONDS
        ):
            return RestartStatus(
                available=True,
                state="failed",
                error=_STALE_ERROR,
                updated_at=updated_at,
            )
        return RestartStatus(
            available=True,
            state=state,
            error=error[:_MAX_ERROR_CHARS] if isinstance(error, str) else None,
            updated_at=updated_at,
        )

    async def check(self) -> str | None:
        """Run ``restart --check``; the helper's error message, or None."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._python,
                str(self.entrypoint),
                "restart",
                "--check",
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=DEVNULL,
                env=self._env,
            )
        except OSError:
            return "The restart check could not run."
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), self._check_timeout
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return "The restart check did not finish in time."
        lines = stdout.decode("utf-8", "replace").strip().splitlines()
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = None
        if not isinstance(result, dict):
            return _CHECK_FAILED
        if result.get("ok") is True and process.returncode == 0:
            return None
        error = result.get("error")
        return error[:_MAX_ERROR_CHARS] if isinstance(error, str) else _CHECK_FAILED

    async def request(self) -> RestartStatus:
        """Validate and record a restart request; ``launch`` starts it."""
        if not self.available:
            raise SettingsRequestError(
                "restart_unavailable",
                409,
                "Restart the backend, frontend and agents yourself; only the "
                "Hybro image restarts them from Settings.",
            )
        if self._lock.locked():
            raise _in_progress()
        async with self._lock:
            current = await asyncio.to_thread(self.status)
            if current.state in ("requested", "restarting"):
                raise _in_progress()
            error = await self.check()
            if error is not None:
                raise SettingsRequestError("config_invalid", 409, error)
            await asyncio.to_thread(self._write_status, "requested", None)
            return await asyncio.to_thread(self.status)

    def launch(self) -> None:
        """Start the helper in its own session, so stopping the backend spares it."""
        try:
            Popen(
                [self._python, str(self.entrypoint), "restart"],
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=self._env,
            )
        except OSError:
            with contextlib.suppress(OSError):
                self._write_status("failed", "The restart could not start.")

    def _read_status(self) -> dict[str, object]:
        try:
            with self._status_file.open("rb") as stream:
                data = stream.read(_MAX_STATUS_BYTES + 1)
            if len(data) > _MAX_STATUS_BYTES:
                return {}
            record = json.loads(data)
        except (OSError, ValueError, RecursionError):
            return {}
        return record if isinstance(record, dict) else {}

    def _write_status(self, state: RestartState, error: str | None) -> None:
        """Replace the status file with a fresh 0600 file in one step."""
        path = self._status_file
        staging = path.with_name(f".{path.name}.{os.getpid()}.new")
        staging.unlink(missing_ok=True)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"state": state, "error": error, "updated_at": time.time()}, handle
            )
        os.replace(staging, path)


def _in_progress() -> SettingsRequestError:
    return SettingsRequestError(
        "restart_in_progress", 409, "A restart is already in progress."
    )


__all__ = ["CHECK_TIMEOUT_SECONDS", "STALE_SECONDS", "RestartControl"]
