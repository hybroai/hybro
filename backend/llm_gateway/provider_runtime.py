"""The single owner of the current Provider pair, reloaded when its files change.

Every model call, and a background timer, compare the config/auth file
signatures with the held pair and reload on a change. A save in another process
that shares the config directory therefore applies without a restart. Callers
take one ``ProviderPair`` per call and use only that object, so a concurrent
swap never mixes the old and new Provider, models, or credential.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from common.config.runtime_config import RuntimeConfig, RuntimeConfigurationError
from common.config.runtime_store import (
    FileSignature,
    RuntimeConfigStore,
    RuntimeSnapshot,
    RuntimeState,
)
from common.observability import get_logger
from llm_gateway.catalog import TextModel, validate_models
from llm_gateway.errors import ProviderNotConfigured

ProviderState = Literal["configured", "unconfigured", "credential_invalid"]
# Failures of the files themselves; a later successful read clears them.
_FILE_ERRORS = frozenset({"config_invalid", "config_unreadable"})
# How long an explicit reload waits for listener work, such as agent gating.
_LISTENER_SETTLE_SECONDS = 5.0
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderPair:
    state: ProviderState
    runtime: RuntimeState
    digest: str

    @property
    def config(self) -> RuntimeConfig:
        return self.runtime.config

    def require(self) -> RuntimeState:
        """The working pair, or ``ProviderNotConfigured`` with the state's code."""
        if self.state == "unconfigured":
            raise ProviderNotConfigured("provider_not_configured")
        if self.state == "credential_invalid":
            raise ProviderNotConfigured("credential_invalid")
        return self.runtime


@dataclass(frozen=True, slots=True)
class TextSelection:
    """One Provider pair and its catalog text model, read once for one request."""

    pair: ProviderPair
    model: TextModel


def _pair(snapshot: RuntimeSnapshot) -> ProviderPair:
    state = snapshot.state
    if not state.config.configured:
        return ProviderPair("unconfigured", state, snapshot.digest)
    # An ineligible model is a configuration error, like invalid JSON.
    validate_models(state.config)
    kind: ProviderState = (
        "configured" if state.authentication is not None else "credential_invalid"
    )
    return ProviderPair(kind, state, snapshot.digest)


class ProviderRuntime:
    def __init__(
        self, store: RuntimeConfigStore, *, interval_seconds: float = 10.0
    ) -> None:
        """Load synchronously at boot; invalid configuration raises here."""
        self.store = store
        self._interval = interval_seconds
        snapshot = store.load_snapshot()
        self._pair = _pair(snapshot)
        self._signature = snapshot.signature
        self._failed_signature: FileSignature | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[ProviderPair], Awaitable[None] | None]] = []

    @property
    def current(self) -> ProviderPair:
        """The held pair, without checking the files."""
        return self._pair

    @property
    def last_error(self) -> str | None:
        """Safe code of the latest failure since the pair last changed."""
        return self._last_error

    def record_failure(self, code: str) -> None:
        self._last_error = code

    def add_listener(
        self, listener: Callable[[ProviderPair], Awaitable[None] | None]
    ) -> None:
        """Call ``listener`` with the new pair after each swap.

        Listeners run inside the reload, so they only schedule their work and
        may return it as a task. A swap found by ``refresh`` leaves that work
        running; ``reload`` (a setup save) waits for it, so the save response
        already reflects it.
        """
        self._listeners.append(listener)

    async def refresh(self) -> ProviderPair:
        """Reload when a file signature differs from the held pair's."""
        try:
            signature = await asyncio.to_thread(self.store.signature)
        except RuntimeConfigurationError:
            self._last_error = "config_unreadable"
            return self._pair
        if self._last_error == "config_unreadable":
            self._last_error = None
        if signature == self._signature or signature == self._failed_signature:
            return self._pair
        return await self._reload(signature)

    async def reload(self) -> ProviderPair:
        """Reload unconditionally, for example right after a setup save."""
        work: list[Awaitable[None]] = []
        pair = await self._reload(None, work)
        if work:
            await asyncio.wait(
                [asyncio.ensure_future(item) for item in work],
                timeout=_LISTENER_SETTLE_SECONDS,
            )
        return pair

    async def _reload(
        self,
        seen: FileSignature | None,
        work: list[Awaitable[None]] | None = None,
    ) -> ProviderPair:
        async with self._lock:
            if seen is not None and seen == self._signature:
                return self._pair  # Another caller reloaded these files.
            try:
                snapshot = await asyncio.to_thread(self.store.load_snapshot)
                pair = _pair(snapshot)
            except RuntimeConfigurationError as exc:
                # Keep serving the last valid pair, as a restart-only setting
                # would, and do not re-read the same broken files on every call.
                if seen is None or seen != self._failed_signature:
                    logger.warning(
                        "provider_config_reload_failed", extra={"reason": str(exc)}
                    )
                if seen is not None:
                    self._failed_signature = seen
                # Forget the good signature too: files restored to exactly
                # those bytes and metadata (a reverted chmod) must reload to
                # clear the error.
                self._signature = None
                self._last_error = "config_invalid"
                return self._pair
            self._signature = snapshot.signature
            self._failed_signature = None
            if pair.digest != self._pair.digest:
                self._pair = pair
                self._last_error = None
                logger.info(
                    "provider_config_reloaded", extra={"provider_state": pair.state}
                )
                for listener in self._listeners:
                    try:
                        result = listener(pair)
                        if result is not None and work is not None:
                            work.append(result)
                    except Exception as exc:  # One listener must not block a swap.
                        logger.warning(
                            "provider_listener_failed",
                            extra={"error_type": type(exc).__name__},
                        )
            elif self._last_error in _FILE_ERRORS:
                self._last_error = None
            return self._pair

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="provider-runtime-refresh"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.refresh()
            except Exception as exc:  # The timer must survive one bad cycle.
                logger.warning(
                    "provider_config_refresh_failed",
                    extra={"error_type": type(exc).__name__},
                )


__all__ = ["ProviderPair", "ProviderRuntime", "ProviderState", "TextSelection"]
