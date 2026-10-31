"""Setup-only selection/verification orchestration, independent of CLI and SDKs.

Catalog entries must already satisfy the runtime's text/image eligibility rules.
The host entry point supplies production bindings from the canonical runtime.
Verification checks the exact credential, endpoint, and selected text model;
image eligibility is checked locally without billable image requests. Failures
must never log credentials or raw Provider responses.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import SecretStr

from common.config.loader import validate_openai_base_url
from common.config.runtime_config import (
    ApiKeyCredential,
    OAuthCredential,
    ResolvedCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeProvider,
    StoredCredential,
    resolve_credential,
)
from common.config.runtime_store import RuntimeConfigStore


class SetupError(ValueError):
    """Safe setup failure suitable for terminal output."""


class StoredCredentialUnreadable(SetupError):
    """``auth.json`` exists but is not valid JSON or does not have mode 0600.

    A save cannot replace it safely, because the store reads it to keep its
    service credentials and to detect a concurrent change.
    """

    def __init__(self) -> None:
        super().__init__(
            "auth.json in the configuration directory cannot be read; it must "
            "be valid JSON with mode 0600. Fix the file, then save again."
        )


@dataclass(frozen=True, slots=True)
class ModelChoices:
    """Already-eligible model IDs; first text entry is the recommendation."""

    text: tuple[str, ...]
    image: tuple[str, ...] = ()

    def validate(self, config: RuntimeConfig) -> None:
        if config.models.text not in self.text or (
            config.models.image is not None and config.models.image not in self.image
        ):
            raise SetupError("Selected model is not eligible for this Provider/auth.")


ModelCatalog = Callable[[RuntimeProvider], ModelChoices]
CredentialVerifier = Callable[
    [RuntimeConfig, ResolvedCredential, str | None], Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class _PreparedAuthentication:
    provider: RuntimeProvider
    previous: StoredCredential | None
    credential: StoredCredential | None
    resolved: ResolvedCredential
    environment: Mapping[str, str] = field(repr=False)
    base_url: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class SetupResult:
    changed: bool
    revision: str
    source: Literal["environment", "stored"]


def _openai_base_url(value: str | None) -> str | None:
    try:
        return validate_openai_base_url(value)
    except ValueError:
        raise SetupError(
            "Invalid OPENAI_BASE_URL; use an HTTP(S) URL with a hostname and no userinfo."
        ) from None


class SetupService:
    def __init__(
        self,
        store: RuntimeConfigStore,
        catalog: ModelCatalog,
        verifier: CredentialVerifier,
    ) -> None:
        self.store = store
        self._catalog = catalog
        self._verifier = verifier

    def choices(self, provider: RuntimeProvider) -> ModelChoices:
        try:
            choices = self._catalog(provider)
        except Exception:
            raise SetupError(
                "Cannot load eligible models for this Provider/auth."
            ) from None
        if not choices.text:
            raise SetupError("No eligible text models for this Provider/auth.")
        return choices

    def authenticate(
        self,
        provider: RuntimeProvider,
        environment: Mapping[str, str],
        *,
        non_interactive: bool,
        read_secret: Callable[[], SecretStr],
        report_source: Callable[[str], None],
    ) -> _PreparedAuthentication:
        """Acquire authentication before model input; never persist a candidate."""
        environment = dict(environment)
        if (self.store.home / "config.json").exists():
            saved_url = self.store.read_config().backend.get("openai_base_url")
            if isinstance(saved_url, str) and saved_url:
                environment.setdefault("OPENAI_BASE_URL", saved_url)
        base_url = (
            _openai_base_url(environment.get("OPENAI_BASE_URL"))
            if provider.id == "openai" and provider.auth == "api_key"
            else None
        )
        previous = self.store.read_stored_credential()
        credential = self._select_credential(
            provider, previous, environment, non_interactive, read_secret, report_source
        )
        resolved = ResolvedCredential(
            "stored" if credential is not None else "environment",
            credential
            if credential is not None
            else ApiKeyCredential(
                provider=provider.id,
                api_key=SecretStr(environment[f"{provider.id.upper()}_API_KEY"]),
            ),
        )
        report_source(
            "OAuth authenticated; pending verification and save."
            if provider.auth == "oauth"
            else f"API key acquired (source: {resolved.source}); pending verification and save."
        )
        return _PreparedAuthentication(
            provider, previous, credential, resolved, environment, base_url
        )

    def save(
        self,
        config: RuntimeConfig,
        prepared: _PreparedAuthentication,
        *,
        begin_commit: Callable[[], None],
    ) -> SetupResult:
        """Verify once, then enter the CLI's non-cancelable CAS save boundary."""
        if config.provider != prepared.provider:
            raise SetupError("Authentication selection changed; rerun setup.")
        self.choices(config.provider).validate(config)
        resolved = resolve_credential(config, prepared.credential, prepared.environment)
        if resolved != prepared.resolved:
            raise SetupError("Authentication source changed; rerun setup.")
        asyncio.run(self._verify(config, resolved, prepared.base_url))
        begin_commit()
        changed = self.store.save(
            config,
            prepared.credential,
            prepared.environment,
            expected_stored=prepared.previous,
        )
        return SetupResult(changed, self.store.read_config().revision, resolved.source)

    async def save_verified(
        self, config: RuntimeConfig, credential: StoredCredential
    ) -> SetupResult:
        """Verify once and save, inside a running event loop (the setup API).

        The credential comes from the request (an API key or a finished OAuth
        login), never from the environment. Store I/O runs in worker threads.
        The stored credential read before verification guards the save, as in
        the CLI, so a concurrent change rejects it instead of being overwritten.
        """
        if config.provider is None:
            raise SetupError("Select a Provider and models to save.")
        if (credential.provider, credential.auth) != (
            config.provider.id,
            config.provider.auth,
        ):
            raise SetupError("Credential does not match the selected Provider/auth.")
        self.choices(config.provider).validate(config)
        saved = await asyncio.to_thread(self.store.read_config)
        base_url = None
        if config.provider.id == "openai" and config.provider.auth == "api_key":
            value = saved.backend.get("openai_base_url")
            base_url = _openai_base_url(value if isinstance(value, str) else None)
        try:
            previous = await asyncio.to_thread(self.store.read_stored_credential)
        except RuntimeConfigurationError:
            raise StoredCredentialUnreadable from None
        await self._verify(config, ResolvedCredential("stored", credential), base_url)
        changed = await asyncio.to_thread(
            self.store.save, config, credential, {}, expected_stored=previous
        )
        revision = (await asyncio.to_thread(self.store.read_config)).revision
        return SetupResult(changed, revision, "stored")

    async def _verify(
        self, config: RuntimeConfig, resolved: ResolvedCredential, base_url: str | None
    ) -> None:
        failure = None
        try:
            await self._verifier(config, resolved, base_url)
        except Exception as exc:
            # Cleanup failures must not replace an already-requested cancellation.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            from llm_gateway._diagnostics import _exception_diagnostic

            failure = _exception_diagnostic(exc)
        if failure is not None:
            # Outside the handler: no raw exception remains in __context__ either.
            raise SetupError(
                "Provider credential/model verification failed; "
                + failure.describe()
                + " No configuration saved."
            )
        # Honor SIGINT even if a verifier returns synchronously or catches the
        # task's CancelledError. No synchronous persistence runs inside the runner.
        await asyncio.sleep(0)
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError

    @staticmethod
    def _select_credential(
        selected: RuntimeProvider,
        previous: StoredCredential | None,
        environment: Mapping[str, str],
        non_interactive: bool,
        read_secret: Callable[[], SecretStr],
        report_source: Callable[[str], None],
    ) -> StoredCredential | None:
        if selected.auth == "oauth":
            if non_interactive:
                raise SetupError(
                    "OAuth requires interactive setup on your browser's host."
                )
            if (
                isinstance(previous, OAuthCredential)
                and previous.expires_at > time.time() + 300
            ):
                return previous
            from llm_gateway.openai_oauth import login

            return asyncio.run(login(report_source))
        env_key = environment.get(f"{selected.id.upper()}_API_KEY", "").strip()
        same_identity = previous is not None and (previous.provider, previous.auth) == (
            selected.id,
            selected.auth,
        )
        if (
            env_key
            and same_identity
            and isinstance(previous, ApiKeyCredential)
            and previous.api_key.get_secret_value() != env_key
        ):
            raise RuntimeConfigurationError(
                "Both environment and stored credentials are configured; remove one."
            )
        if env_key:
            return None
        if non_interactive:
            raise SetupError(
                "Non-interactive setup requires the selected environment key."
            )
        if same_identity:
            return previous
        return ApiKeyCredential(provider=selected.id, api_key=read_secret())
