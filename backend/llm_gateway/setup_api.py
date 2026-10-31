"""Browser model-provider setup behind the thin ``setup_routes`` adapter.

Status, model choices, API-key and OAuth saves, and the image size. A save
verifies the selection with one text request, writes ``config.json`` and
``auth.json`` through the shared store, and reloads the ``ProviderRuntime``, so
the saving request and every later model call see the new pair. Credentials
travel only in request bodies and the store; responses and errors never echo
them.

OAuth uses the registered ``http://localhost:1455/auth/callback`` redirect. One
login runs at a time. Its callback listener lives only while the login is
pending (at most ``LOGIN_TIMEOUT``); when the browser cannot reach it, the user
pastes the redirect address and ``complete`` checks it with the same strict
state check.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, ValidationError

from common.config.runtime_config import (
    ApiKeyCredential,
    AuthMethod,
    OAuthCredential,
    ProviderId,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeModels,
    RuntimeProvider,
    StoredCredential,
)
from common.dto.base import FrozenDTO
from common.observability import get_logger
from llm_gateway import openai_oauth
from llm_gateway.openai_oauth import (
    LOGIN_TIMEOUT,
    REFRESH_MARGIN,
    AuthorizationDenied,
    CallbackListener,
    authorization_flow,
    callback_code,
    exchange_code,
)
from llm_gateway.provider_runtime import ProviderPair, ProviderRuntime, ProviderState
from llm_gateway.setup_service import (
    SetupError,
    SetupService,
    StoredCredentialUnreadable,
)

logger = get_logger(__name__)

ImageSize = Literal["1024x1024", "1536x1024", "1024x1536", "auto"]
LoginState = Literal["pending", "authenticated", "failed", "expired"]


class _SetupDTO(FrozenDTO):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class SetupStatus(_SetupDTO):
    state: ProviderState
    provider: ProviderId | None = None
    auth: AuthMethod | None = None
    text_model: str | None = None
    image_model: str | None = None
    image_size: ImageSize
    # Safe code of the latest failure, such as oauth_refresh_failed.
    last_error: str | None = None


class SetupModels(_SetupDTO):
    """Eligible models; ``text[0]`` is the recommendation."""

    text: tuple[str, ...]
    image: tuple[str, ...]


class ApiKeySetup(_SetupDTO):
    provider: ProviderId
    # None reuses the saved key for this Provider, to change models without it.
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    text_model: str = Field(min_length=1, max_length=200)
    image_model: str | None = Field(default=None, min_length=1, max_length=200)


class SetupSaved(_SetupDTO):
    changed: bool
    status: SetupStatus


class OAuthLoginView(_SetupDTO):
    login_id: str
    state: LoginState
    # Present only while the login is pending.
    authorization_url: str | None = None
    # False when port 1455 could not be bound; the page asks for the pasted
    # redirect address instead.
    listener: bool
    expires_at: float
    error: str | None = None


class OAuthComplete(_SetupDTO):
    login_id: str = Field(min_length=1, max_length=64)
    redirect_url: str = Field(min_length=1, max_length=8192)


class OAuthModels(_SetupDTO):
    # None reuses the saved OAuth credential, to change models without a login.
    login_id: str | None = Field(default=None, min_length=1, max_length=64)
    text_model: str = Field(min_length=1, max_length=200)
    image_model: str | None = Field(default=None, min_length=1, max_length=200)


class ImageSizeUpdate(_SetupDTO):
    image_size: ImageSize


class ImageSizeSaved(_SetupDTO):
    image_size: ImageSize
    # The image size is read by agents at startup.
    restart_required: bool = True


class SetupErrorBody(_SetupDTO):
    detail: str
    code: str
    login_id: str | None = None


class SetupRequestError(Exception):
    """A safe, coded setup failure the route returns as ``SetupErrorBody``."""

    def __init__(
        self, code: str, status: int, message: str, *, login_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.login_id = login_id

    def body(self) -> SetupErrorBody:
        return SetupErrorBody(detail=str(self), code=self.code, login_id=self.login_id)


def _invalid(message: str) -> SetupRequestError:
    return SetupRequestError("invalid_selection", 400, message)


def _unreadable(exc: StoredCredentialUnreadable) -> SetupRequestError:
    return SetupRequestError("credential_unreadable", 409, str(exc))


def _conflict(exc: RuntimeConfigurationError) -> SetupRequestError:
    # Store messages are written to be safe (no credential or raw input).
    return SetupRequestError("config_conflict", 409, str(exc))


CodeExchange = Callable[[str, str], Awaitable[OAuthCredential]]


@dataclass(slots=True)
class _Login:
    login_id: str
    oauth_state: str = field(repr=False)
    verifier: str = field(repr=False)
    url: str
    deadline: float
    listener: CallbackListener | None = None
    status: LoginState = "pending"
    error: str | None = None
    credential: OAuthCredential | None = field(default=None, repr=False)
    exchange: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None


class OAuthLogins:
    """At most one ChatGPT login; pending state lives only in this process."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        timeout: float = LOGIN_TIMEOUT,
        exchange: CodeExchange | None = None,
    ) -> None:
        self._host = host
        self._timeout = timeout
        self._exchange = exchange or (
            lambda code, verifier: exchange_code(code, verifier)
        )
        self._login: _Login | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> OAuthLoginView:
        async with self._lock:
            return await self._open()

    async def _open(self) -> OAuthLoginView:
        """Start a login; the caller holds the lock."""
        current = self._login
        if current is not None and current.status == "pending":
            raise SetupRequestError(
                "login_pending",
                409,
                "A sign-in is already in progress; resume or cancel it.",
                login_id=current.login_id,
            )
        await self._drop()
        verifier, state, url = authorization_flow()
        login = _Login(
            login_id=secrets.token_urlsafe(16),
            oauth_state=state,
            verifier=verifier,
            url=url,
            deadline=time.monotonic() + self._timeout,
        )
        listener = CallbackListener(
            state,
            lambda code: self._accept(login, code),
            lambda: self._deny(login),
        )
        try:
            await listener.start(self._host)
            login.listener = listener
        except OSError:
            logger.info("oauth_callback_listener_unavailable")
        login.watchdog = asyncio.create_task(
            self._expire(login), name="oauth-login-expiry"
        )
        self._login = login
        return self._view(login)

    async def get(self, login_id: str) -> OAuthLoginView:
        return self._view(self._find(login_id))

    async def complete(self, login_id: str, redirect_url: str) -> OAuthLoginView:
        login = self._find(login_id)
        if login.status != "pending" or login.exchange is not None:
            return self._view(login)
        target = _callback_target(redirect_url)
        try:
            code = callback_code(target, login.oauth_state)
        except AuthorizationDenied:
            self._deny(login)
            return self._view(login)
        except RuntimeConfigurationError as exc:
            raise SetupRequestError("invalid_callback", 400, str(exc)) from None
        self._accept(login, code)
        exchange = login.exchange
        assert exchange is not None
        try:
            # The exchange belongs to the login, not to this request.
            await asyncio.shield(exchange)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if exchange.cancelled() and not (task is not None and task.cancelling()):
                raise SetupRequestError(
                    "login_not_found", 404, "The sign-in was canceled."
                ) from None
            raise
        return self._view(login)

    async def cancel(self, login_id: str) -> None:
        # Checked under the lock, so a concurrent start's new login survives.
        async with self._lock:
            self._find(login_id)
            await self._drop()

    def credential(self, login_id: str) -> OAuthCredential:
        login = self._find(login_id)
        if login.status == "expired":
            raise SetupRequestError(
                "login_expired", 409, "The sign-in expired; start again."
            )
        if login.status != "authenticated" or login.credential is None:
            raise SetupRequestError(
                "login_not_ready", 409, "The sign-in has not finished."
            )
        return login.credential

    async def consume(self, login_id: str) -> None:
        async with self._lock:
            if self._login is not None and self._login.login_id == login_id:
                await self._drop()

    async def aclose(self) -> None:
        async with self._lock:
            await self._drop()

    def _find(self, login_id: str) -> _Login:
        login = self._login
        if login is None or not secrets.compare_digest(login.login_id, login_id):
            raise SetupRequestError("login_not_found", 404, "Unknown sign-in.")
        return login

    def _accept(self, login: _Login, code: str) -> None:
        if login.exchange is None and login.status == "pending":
            login.exchange = asyncio.create_task(
                self._finish(login, code), name="oauth-code-exchange"
            )

    def _deny(self, login: _Login) -> None:
        # The listener has stopped accepting codes; expiry or the next start
        # releases the port.
        if login.status == "pending" and login.exchange is None:
            login.status = "failed"
            login.error = "OAuth authorization was denied."

    async def _finish(self, login: _Login, code: str) -> None:
        try:
            credential = await self._exchange(code, login.verifier)
        except RuntimeConfigurationError as exc:
            login.status, login.error = "failed", str(exc)
        except Exception as exc:
            logger.warning(
                "oauth_code_exchange_failed", extra={"error_type": type(exc).__name__}
            )
            login.status, login.error = "failed", "OAuth token exchange failed."
        else:
            if login.status == "pending":
                login.status, login.credential = "authenticated", credential
                # Time to choose models, measured from the sign-in itself.
                login.deadline = time.monotonic() + self._timeout
        # Let the listener's own handler finish its response.
        await self._close_listener(login, cancel=False)

    async def _expire(self, login: _Login) -> None:
        while True:
            # A finished sign-in moves the deadline; an exchange in flight ends
            # in its own state first.
            delay = login.deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif login.exchange is not None and not login.exchange.done():
                await asyncio.wait({login.exchange})
            else:
                break
        if login.status in {"pending", "authenticated"}:
            login.status, login.credential = "expired", None
        await self._close_listener(login, cancel=True)

    @staticmethod
    async def _close_listener(login: _Login, *, cancel: bool) -> None:
        listener, login.listener = login.listener, None
        if listener is not None:
            await listener.close(cancel_connections=cancel)

    async def _drop(self) -> None:
        login, self._login = self._login, None
        if login is None:
            return
        login.credential = None
        tasks = [
            task
            for task in (login.exchange, login.watchdog)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_listener(login, cancel=True)

    @staticmethod
    def _view(login: _Login) -> OAuthLoginView:
        return OAuthLoginView(
            login_id=login.login_id,
            state=login.status,
            authorization_url=login.url if login.status == "pending" else None,
            listener=login.listener is not None,
            expires_at=time.time() + max(0.0, login.deadline - time.monotonic()),
            error=login.error,
        )


def _callback_target(redirect_url: str) -> str:
    """The path and query of a pasted ``http://localhost:1455`` redirect."""
    try:
        parts = urlsplit(redirect_url.strip())
        port = parts.port
    except ValueError:
        port = None
        parts = None
    if (
        parts is None
        or parts.scheme != "http"
        or parts.hostname != "localhost"
        or port != openai_oauth.CALLBACK_PORT
        or parts.username is not None
        or parts.password is not None
    ):
        raise SetupRequestError(
            "invalid_callback",
            400,
            "Paste the full address that starts with "
            f"http://localhost:{openai_oauth.CALLBACK_PORT}/.",
        )
    return parts.path + ("?" + parts.query if parts.query else "")


@runtime_checkable
class SetupAccess(Protocol):
    """Route owner for browser model-provider setup."""

    def can_configure(self, user_id: str) -> bool: ...

    async def status(self) -> SetupStatus: ...

    def models(self, provider: ProviderId, auth: AuthMethod) -> SetupModels: ...

    async def save_api_key(self, request: ApiKeySetup) -> SetupSaved: ...

    async def start_oauth(self) -> OAuthLoginView: ...

    async def oauth_login(self, login_id: str) -> OAuthLoginView: ...

    async def complete_oauth(self, request: OAuthComplete) -> OAuthLoginView: ...

    async def cancel_oauth(self, login_id: str) -> None: ...

    async def save_oauth_models(self, request: OAuthModels) -> SetupSaved: ...

    async def save_image_size(self, request: ImageSizeUpdate) -> ImageSizeSaved: ...


class BrowserSetup:
    def __init__(
        self,
        runtime: ProviderRuntime,
        service: SetupService,
        logins: OAuthLogins,
        *,
        auth_mode: Literal["mock", "clerk"],
        admin_user_ids: frozenset[str],
    ) -> None:
        self._runtime = runtime
        self._service = service
        self._logins = logins
        self._auth_mode = auth_mode
        self._admins = admin_user_ids

    def can_configure(self, user_id: str) -> bool:
        # Mock mode trusts whoever reaches the API, as the loopback install does.
        return self._auth_mode == "mock" or user_id in self._admins

    async def status(self) -> SetupStatus:
        return self._status(await self._runtime.refresh())

    def models(self, provider: ProviderId, auth: AuthMethod) -> SetupModels:
        try:
            selected = RuntimeProvider(id=provider, auth=auth)
            choices = self._service.choices(selected)
        except (ValidationError, SetupError):
            raise _invalid("This Provider does not support that sign-in.") from None
        return SetupModels(text=choices.text, image=choices.image)

    async def save_api_key(self, request: ApiKeySetup) -> SetupSaved:
        config = self._selection(
            request.provider, "api_key", request.text_model, request.image_model
        )
        if request.api_key is None:
            return await self._save(config, await self._saved_key(request.provider))
        try:
            credential = ApiKeyCredential(
                provider=request.provider, api_key=request.api_key
            )
        except ValidationError:
            raise _invalid("Enter an API key.") from None
        return await self._save(config, credential)

    async def start_oauth(self) -> OAuthLoginView:
        return await self._logins.start()

    async def oauth_login(self, login_id: str) -> OAuthLoginView:
        return await self._logins.get(login_id)

    async def complete_oauth(self, request: OAuthComplete) -> OAuthLoginView:
        return await self._logins.complete(request.login_id, request.redirect_url)

    async def cancel_oauth(self, login_id: str) -> None:
        await self._logins.cancel(login_id)

    async def save_oauth_models(self, request: OAuthModels) -> SetupSaved:
        config = self._selection(
            "openai", "oauth", request.text_model, request.image_model
        )
        if request.login_id is not None:
            credential: OAuthCredential = self._logins.credential(request.login_id)
        else:
            credential = await self._saved_oauth()
        saved = await self._save(config, credential)
        if request.login_id is not None:
            await self._logins.consume(request.login_id)
        return saved

    async def save_image_size(self, request: ImageSizeUpdate) -> ImageSizeSaved:
        try:
            await asyncio.to_thread(
                self._runtime.store.update_config, ["image_size"], request.image_size
            )
        except RuntimeConfigurationError as exc:
            raise _conflict(exc) from None
        await self._runtime.reload()
        return ImageSizeSaved(image_size=request.image_size)

    async def aclose(self) -> None:
        await self._logins.aclose()

    def _selection(
        self,
        provider: ProviderId,
        auth: AuthMethod,
        text_model: str,
        image_model: str | None,
    ) -> RuntimeConfig:
        try:
            config = RuntimeConfig(
                provider=RuntimeProvider(id=provider, auth=auth),
                models=RuntimeModels(text=text_model, image=image_model),
            )
            assert config.provider is not None
            self._service.choices(config.provider).validate(config)
        except (ValidationError, SetupError):
            raise _invalid(
                "Selected model is not eligible for this Provider/sign-in."
            ) from None
        return config

    async def _stored(self) -> StoredCredential | None:
        try:
            return await asyncio.to_thread(self._runtime.store.read_stored_credential)
        except RuntimeConfigurationError:
            raise _unreadable(StoredCredentialUnreadable()) from None

    async def _saved_key(self, provider: ProviderId) -> ApiKeyCredential:
        stored = await self._stored()
        if not isinstance(stored, ApiKeyCredential) or stored.provider != provider:
            raise SetupRequestError(
                "key_required", 409, "Enter an API key to save these models."
            )
        return stored

    async def _saved_oauth(self) -> OAuthCredential:
        stored = await self._stored()
        if (
            not isinstance(stored, OAuthCredential)
            or stored.expires_at <= time.time() + REFRESH_MARGIN
        ):
            raise SetupRequestError(
                "login_required", 409, "Sign in with ChatGPT to save these models."
            )
        return stored

    async def _save(
        self, config: RuntimeConfig, credential: StoredCredential
    ) -> SetupSaved:
        try:
            result = await self._service.save_verified(config, credential)
        except StoredCredentialUnreadable as exc:
            raise _unreadable(exc) from None
        except SetupError as exc:
            raise SetupRequestError("verification_failed", 400, str(exc)) from None
        except RuntimeConfigurationError as exc:
            raise _conflict(exc) from None
        pair = await self._runtime.reload()
        return SetupSaved(changed=result.changed, status=self._status(pair))

    def _status(self, pair: ProviderPair) -> SetupStatus:
        config = pair.config
        return SetupStatus(
            state=pair.state,
            provider=config.provider.id if config.provider is not None else None,
            auth=config.provider.auth if config.provider is not None else None,
            text_model=config.models.text if config.models is not None else None,
            image_model=config.models.image if config.models is not None else None,
            image_size=config.image_size,
            last_error=self._runtime.last_error,
        )


__all__ = [
    "ApiKeySetup",
    "BrowserSetup",
    "ImageSizeSaved",
    "ImageSizeUpdate",
    "OAuthComplete",
    "OAuthLoginView",
    "OAuthLogins",
    "OAuthModels",
    "SetupAccess",
    "SetupErrorBody",
    "SetupModels",
    "SetupRequestError",
    "SetupSaved",
    "SetupStatus",
]
