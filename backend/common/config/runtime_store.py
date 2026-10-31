"""Locked, private config/credential files shared by host setup and backend.

Filesystem operations are synchronous for the host CLI. Async callers must run
these bounded operations in a worker thread, not on the request event loop.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import ValidationError

from common.config.runtime_config import (
    CREDENTIAL_ADAPTER,
    ApiKeyCredential,
    OAuthCredential,
    ResolvedCredential,
    RuntimeConfig,
    RuntimeConfigurationError,
    StoredCredential,
    resolve_credential,
)

_MAX_FILE_BYTES = 64 * 1024
_OAUTH_REFRESH_TIMEOUT = 15


async def _store_io[T](operation: Callable[[], T]) -> T:
    # A canceled to_thread still runs. Wait before releasing the directory lock;
    # this also ensures a successful rotating refresh finishes its atomic save.
    return await _complete_before_cancel(
        asyncio.create_task(asyncio.to_thread(operation))
    )


async def _complete_before_cancel[T](task: asyncio.Task[T]) -> T:
    canceled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            canceled = True
        except Exception:
            if canceled:
                raise asyncio.CancelledError from None
            raise
    if canceled:
        raise asyncio.CancelledError
    return result


def runtime_home(environment: Mapping[str, str]) -> Path:
    """One host config location shared by setup and the gateway."""
    configured = environment.get("HYBRO_HOME")
    home = (
        Path(configured)
        if configured is not None
        else Path(environment.get("HOME") or Path.home()) / ".hybro"
    )
    if not home.is_absolute():
        raise RuntimeConfigurationError("Runtime directory must be absolute.")
    return home


def prepare_setup_directory(home: Path) -> bool:
    """Setup-only permission repair; never create a directory or inspect contents.

    lstat gives actionable diagnostics; only the no-follow directory FD authorizes
    repair, so a path replacement cannot redirect chmod through a symlink.
    Runtime readers/writers retain their strict validation without automatic repair.
    """
    try:
        metadata = home.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeConfigurationError(
            "Runtime directory is a symlink; set HYBRO_HOME to a real directory."
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeConfigurationError(
            "Runtime path is not a directory; set HYBRO_HOME to a private directory."
        )
    ownership_error = (
        "Runtime directory is owned by another user; choose a HYBRO_HOME "
        "you own or ask its administrator to correct ownership."
    )
    if metadata.st_uid != os.geteuid():
        raise RuntimeConfigurationError(ownership_error)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeConfigurationError(
                    "Runtime path is not a directory; choose a real HYBRO_HOME directory."
                )
            if metadata.st_uid != os.geteuid():
                raise RuntimeConfigurationError(ownership_error)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise RuntimeConfigurationError(
                    "Runtime directory changed during setup; rerun setup."
                )
            if stat.S_IMODE(metadata.st_mode) == 0o700:
                return False
            os.fchmod(fd, 0o700)
            return True
        finally:
            os.close(fd)
    except OSError:
        raise RuntimeConfigurationError(
            "Cannot secure runtime directory; check access and ensure HYBRO_HOME "
            "is a real directory you own, then rerun setup."
        ) from None


def current_store() -> RuntimeConfigStore:
    """Central process-location boundary shared by loaders and OAuth refresh."""
    return RuntimeConfigStore(runtime_home(os.environ))


class _ExpectedStored(Enum):
    UNSET = "unset"


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def _json_object(data: bytes) -> dict[str, object]:
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("File too large")
    value = json.loads(
        data,
        object_pairs_hook=_unique_json_pairs,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def parse_config(data: bytes) -> RuntimeConfig:
    try:
        return RuntimeConfig.model_validate(_json_object(data))
    except ValidationError as exc:
        from common.config.loader import FrontendSettings, Settings

        known = (
            set(Settings.model_fields)
            | set(FrontendSettings.model_fields)
            | {
                "version",
                "provider",
                "models",
                "id",
                "auth",
                "text",
                "image",
                "backend",
                "frontend",
                "image_size",
            }
        )
        errors = []
        for error in exc.errors(include_input=False, include_context=False)[:4]:
            path = (
                ".".join(
                    str(part) if isinstance(part, int) or part in known else "<unknown>"
                    for part in error["loc"]
                )
                or "config"
            )
            errors.append(f"{path}: {error['type']}")
        raise RuntimeConfigurationError(
            "Invalid config.json: " + "; ".join(errors)
        ) from None
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeConfigurationError(
            "Invalid config.json; expected bounded JSON with unique keys and finite values."
        ) from None


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


SERVICE_KEYS = frozenset(
    {
        "clerk_secret_key",
        "clerk_webhook_secret",
        "mongodb_url",
        "redis_url",
        "default_agent_llm_token",
    }
)


# Retired service keys that existing auth.json files may still carry. They are
# accepted on read, never offered by `config secret` or migration, and dropped
# the next time ensure_service_credentials rewrites the file. Remove this set in
# the next major release.
RETIRED_SERVICE_KEYS = frozenset(
    {"mongodb_password", "webhook_signing_key", "default_agent_registrar_token"}
)


def _auth_object(data: bytes | None) -> dict[str, object]:
    value = _json_object(data) if data is not None else {}
    services = value.get("services", {})
    if not isinstance(services, dict) or any(
        key not in SERVICE_KEYS | RETIRED_SERVICE_KEYS or not isinstance(secret, str)
        for key, secret in services.items()
    ):
        raise RuntimeConfigurationError("Invalid auth.json service credentials.")
    return value


def parse_credential(data: bytes) -> StoredCredential | None:
    try:
        value = _auth_object(data)
        value.pop("services", None)
        return CREDENTIAL_ADAPTER.validate_python(value) if value else None
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeConfigurationError("Invalid auth.json; rerun setup.") from None


def _credential_bytes(
    credential: StoredCredential | None, previous: bytes | None = None
) -> bytes:
    # Preserve service secrets when setup or OAuth rotates the Provider identity.
    services = _auth_object(previous).get("services", {})
    data = credential.model_dump(mode="json") if credential is not None else {}
    if isinstance(credential, ApiKeyCredential):
        data["api_key"] = credential.api_key.get_secret_value()
    elif isinstance(credential, OAuthCredential):
        data["access_token"] = credential.access_token.get_secret_value()
        data["refresh_token"] = credential.refresh_token.get_secret_value()
    if services:
        data["services"] = services
    encoded = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise RuntimeConfigurationError("Credential exceeds the file size limit.")
    return encoded


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """One consistent pair.

    ``authentication`` is None when no Provider is configured, or when a
    configured Provider's credential is missing or mismatched; the latter also
    carries the safe ``credential_error`` reason.
    """

    config: RuntimeConfig
    authentication: ResolvedCredential | None = None
    credential_error: str | None = None


# (inode, size, mtime_ns) of config.json and auth.json; None for a missing file.
# (inode, size, mtime_ns, mode) per file; the mode makes a chmod visible too.
_FileEntry = tuple[int, int, int, int]
FileSignature = tuple[_FileEntry | None, _FileEntry | None]


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """A loaded pair plus what identifies the files it was read from.

    ``signature`` is taken before reading, so a later write always differs from
    it. ``digest`` identifies the content, so a touch without a change does not
    count as a new pair.
    """

    state: RuntimeState
    signature: FileSignature
    digest: str


class RuntimeConfigStore:
    def __init__(self, home: Path) -> None:
        if not home.is_absolute():
            raise RuntimeConfigurationError("Runtime directory must be absolute.")
        self.home = home

    def _open_lock(self) -> int:
        try:
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = self.home.lstat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or stat.S_IMODE(directory.st_mode) != 0o700
            ):
                raise RuntimeConfigurationError(
                    "Runtime directory must be a real directory with mode 0700."
                )
            fd = os.open(
                self.home / ".runtime.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError:
            raise RuntimeConfigurationError(
                "Cannot open runtime directory lock."
            ) from None
        try:
            lock = os.fstat(fd)
            if not stat.S_ISREG(lock.st_mode) or stat.S_IMODE(lock.st_mode) != 0o600:
                raise RuntimeConfigurationError(
                    "Runtime lock must be a regular file with mode 0600."
                )
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = self._open_lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @asynccontextmanager
    async def _async_locked(self) -> AsyncIterator[None]:
        # Opening/validating files and flock never run on the event loop. A failed
        # nonblocking attempt closes its FD in the worker, so cancellation cannot
        # leave a worker waiting indefinitely or holding an orphaned lock.
        acquired: list[int] = []

        def attempt() -> None:
            fd = self._open_lock()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
            except BaseException:
                os.close(fd)
                raise
            else:
                acquired.append(fd)

        try:
            async with asyncio.timeout(30):
                while not acquired:
                    await _store_io(attempt)
                    if not acquired:
                        await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                await _store_io(lambda: os.close(acquired[0]))

    async def resolve_oauth(
        self,
        expected: RuntimeConfig,
        account: str,
        environment: Mapping[str, str],
        refresh: Callable[[OAuthCredential], Awaitable[OAuthCredential]],
    ) -> OAuthCredential:
        """Reread, serialize refresh and persist rotations without changing revision.

        ``expected`` is the caller's current pair. Only a Provider, model, or
        account change rejects the refresh; the caller then reloads that pair.
        Restart-only settings and another worker's token rotation for the same
        account are accepted.
        """

        def snapshot() -> tuple[bytes, bytes, OAuthCredential]:
            config_data, auth_data = self._read("config.json"), self._read("auth.json")
            if config_data is None or auth_data is None:
                raise RuntimeConfigurationError(
                    "OAuth configuration missing; rerun setup."
                )
            config = parse_config(config_data)
            resolved = resolve_credential(
                config, parse_credential(auth_data), environment
            )
            credential = resolved.credential if resolved is not None else None
            # Only the selection and account matter: settings that apply after a
            # restart, and edits from another instance, must not break refresh.
            if (
                (config.provider, config.models) != (expected.provider, expected.models)
                or not isinstance(credential, OAuthCredential)
                or credential.account_id != account
            ):
                raise RuntimeConfigurationError(
                    "OAuth selection/account changed; retry with the saved setup."
                )
            return config_data, auth_data, credential

        try:
            async with self._async_locked():
                config_data, auth_data, credential = await _store_io(snapshot)
                if credential.expires_at > time.time() + 300:
                    return credential

                async def rotate() -> OAuthCredential:
                    async with asyncio.timeout(_OAUTH_REFRESH_TIMEOUT):
                        updated = await refresh(credential)
                    if (
                        updated.account_id != account
                        or updated.expires_at <= time.time()
                    ):
                        raise RuntimeConfigurationError(
                            "Invalid refreshed OAuth identity or expiry."
                        )

                    def save_rotation() -> None:
                        current_config, current_auth, _ = snapshot()
                        if (current_config, current_auth) != (config_data, auth_data):
                            raise RuntimeConfigurationError(
                                "OAuth state changed during refresh; rerun setup."
                            )
                        self._replace(
                            "auth.json", _credential_bytes(updated, current_auth)
                        )

                    await _store_io(save_rotation)
                    return updated

                # A refresh can rotate remotely before its HTTP cleanup returns.
                # Once started, this bounded critical section owns refresh AND
                # persistence. Caller cancellation waits for it under the same
                # lock, then propagates, never discarding a received rotation.
                return await _complete_before_cancel(asyncio.create_task(rotate()))
        except (OSError, TimeoutError):
            raise RuntimeConfigurationError(
                "OAuth refresh/store unavailable; retry setup or restart backend."
            ) from None

    def _read(self, name: str) -> bytes | None:
        try:
            fd = os.open(self.home / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeConfigurationError("Runtime files must be regular files.")
            if name == "auth.json" and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise RuntimeConfigurationError("auth.json must have mode 0600.")
            data = stream.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise RuntimeConfigurationError("Runtime file exceeds the size limit.")
        return data

    def _replace(self, name: str, data: bytes | None) -> None:
        path = self.home / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.home)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        directory_fd = os.open(self.home, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def signature(self) -> FileSignature:
        """Cheap change check without the lock; a reader confirms by content."""

        def entry(name: str) -> _FileEntry | None:
            try:
                metadata = os.stat(self.home / name, follow_symlinks=False)
            except FileNotFoundError:
                return None
            return (
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_mode,
            )

        try:
            return entry("config.json"), entry("auth.json")
        except OSError:
            raise RuntimeConfigurationError(
                "Cannot read runtime configuration."
            ) from None

    def load(self, environment: Mapping[str, str]) -> RuntimeState:
        """Load one consistent pair; no Provider/network verification occurs here.

        A missing config.json is an unconfigured document. A configured Provider
        whose credential is missing or mismatched loads without a credential.
        """
        return self.load_snapshot().state

    def load_snapshot(self) -> RuntimeSnapshot:
        try:
            with self._locked():
                signature = self.signature()
                config_data = self._read("config.json")
                auth_data = self._read("auth.json")
                config = (
                    parse_config(config_data)
                    if config_data is not None
                    else RuntimeConfig()
                )
                stored = parse_credential(auth_data) if auth_data is not None else None
                try:
                    state = RuntimeState(config, resolve_credential(config, stored, {}))
                except RuntimeConfigurationError as exc:
                    state = RuntimeState(config, None, str(exc))
        except OSError:
            raise RuntimeConfigurationError(
                "Cannot read runtime configuration."
            ) from None
        digest = hashlib.sha256()
        for data in (config_data, auth_data):
            digest.update(b"-" if data is None else b"+%d:" % len(data) + data)
        return RuntimeSnapshot(state, signature, digest.hexdigest())

    def read_config(self) -> RuntimeConfig:
        with self._locked():
            data = self._read("config.json")
            return parse_config(data) if data is not None else RuntimeConfig()

    def read_service_credentials(self) -> dict[str, str]:
        with self._locked():
            data = self._read("auth.json")
            parse_credential(data) if data is not None else None
            services = _auth_object(data).get("services", {})
            return {k: v for k, v in services.items() if k in SERVICE_KEYS}

    def ensure_service_credentials(self) -> dict[str, str]:
        with self._locked():
            data = self._read("auth.json")
            credential = parse_credential(data) if data is not None else None
            value = _auth_object(data)
            services = {
                k: v
                for k, v in value.get("services", {}).items()
                if k not in RETIRED_SERVICE_KEYS
            }
            for key in ("default_agent_llm_token",):
                if not services.get(key):
                    services[key] = secrets.token_hex(32)
                if len(services[key].encode()) < 32:
                    raise RuntimeConfigurationError(
                        "Internal credentials must be at least 32 bytes."
                    )
            value["services"] = services
            updated = _credential_bytes(credential, json.dumps(value).encode())
            if data != updated:
                self._replace("auth.json", updated)
            return services

    def update_config(
        self,
        path: list[str],
        value: object,
        *,
        validate: Callable[[RuntimeConfig], None] | None = None,
    ) -> None:
        """Set one value; ``validate`` sees the candidate before it is written."""
        if not path or path[0] not in {"backend", "frontend", "image_size"}:
            raise RuntimeConfigurationError(
                "Change the Provider and models in Settings, Model provider."
            )
        with self._locked():
            previous = self._read("config.json")
            document = (
                _json_object(previous) if previous is not None else {"version": 1}
            )
            from common.config.loader import REMOVED_FIELDS, strip_removed_fields

            if path[0] == "backend" and len(path) == 2 and path[1] in REMOVED_FIELDS:
                raise RuntimeConfigurationError(
                    f"backend.{path[1]} was removed and is no longer a setting."
                )
            target = document
            for part in path[:-1]:
                child = target.setdefault(part, {})
                if not isinstance(child, dict):
                    raise RuntimeConfigurationError(
                        "Configuration path must refer to an object."
                    )
                target = child
            target[path[-1]] = value
            # A retired key is dropped on read, so it is not written back either.
            document.pop("instances", None)
            backend = document.get("backend")
            if isinstance(backend, dict):
                document["backend"], _ = strip_removed_fields(backend)
            encoded = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode()
            candidate = parse_config(encoded)
            if validate is not None:
                validate(candidate)
            self._replace("config.json", encoded)

    def read_stored_credential(self) -> StoredCredential | None:
        """Read setup's credential snapshot without creating an absent directory.

        A concurrent creator is checked by save's expected_stored comparison.
        Existing paths still go through the strict locked reader.
        """
        try:
            self.home.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise RuntimeConfigurationError("Cannot read stored credential.") from None
        try:
            with self._locked():
                data = self._read("auth.json")
                return parse_credential(data) if data is not None else None
        except OSError:
            raise RuntimeConfigurationError("Cannot read stored credential.") from None

    def save(
        self,
        config: RuntimeConfig,
        credential: StoredCredential | None,
        environment: Mapping[str, str],
        *,
        expected_stored: StoredCredential
        | None
        | _ExpectedStored = _ExpectedStored.UNSET,
    ) -> bool:
        """Persist an already-verified candidate; return False for semantic no-op.

        The caller owns Provider/model eligibility and live authentication checks.
        Environment keys are setup inputs only; persist the verified credential.
        Setup supplies expected_stored (including None for an absent credential)
        to reject changes since verification, before either writing or no-op.
        This is not a crash-atomic two-file transaction: a crash can leave a
        missing or identity-mismatched pair, which load reports as a credential
        error without a credential; saving setup again repairs it.
        """
        resolved = resolve_credential(config, credential, environment)
        if resolved is None:
            raise RuntimeConfigurationError("Select a Provider and models to save.")
        credential = resolved.credential
        try:
            with self._locked():
                old_config = self._read("config.json")
                old_auth = self._read("auth.json")
                if expected_stored is not _ExpectedStored.UNSET:
                    current = (
                        parse_credential(old_auth) if old_auth is not None else None
                    )
                    if current != expected_stored:
                        raise RuntimeConfigurationError(
                            "Stored credential changed during setup; rerun setup."
                        )
                if old_config is not None:
                    previous = parse_config(old_config)
                    config = config.model_copy(
                        update={
                            "backend": previous.backend,
                            "frontend": previous.frontend,
                            "image_size": previous.image_size,
                        }
                    )
                if (
                    config.provider is not None
                    and config.provider.id == "openai"
                    and config.provider.auth == "api_key"
                    and environment.get("OPENAI_BASE_URL")
                ):
                    config = config.model_copy(
                        update={
                            "backend": {
                                **config.backend,
                                "openai_base_url": environment["OPENAI_BASE_URL"],
                            }
                        }
                    )
                config_data = (
                    json.dumps(
                        config.model_dump(exclude_none=True), indent=2, allow_nan=False
                    )
                    + "\n"
                ).encode("utf-8")
                parse_config(config_data)
                auth_data = _credential_bytes(credential, old_auth)
                if self._unchanged(config, credential, old_config, old_auth):
                    return False
                self._save_pair(config_data, auth_data, old_config, old_auth)
                return True
        except OSError:
            raise RuntimeConfigurationError(
                "Cannot write runtime configuration."
            ) from None

    @staticmethod
    def _unchanged(
        config: RuntimeConfig,
        credential: StoredCredential | None,
        old_config: bytes | None,
        old_auth: bytes | None,
    ) -> bool:
        if old_config is None:
            return False
        try:
            return config == parse_config(old_config) and credential == (
                parse_credential(old_auth) if old_auth is not None else None
            )
        except RuntimeConfigurationError:
            return False

    def _save_pair(
        self,
        config: bytes,
        auth: bytes | None,
        old_config: bytes | None,
        old_auth: bytes | None,
    ) -> None:
        try:
            self._replace("auth.json", auth)
            self._replace("config.json", config)
        except OSError:
            try:
                self._replace("auth.json", old_auth)
                self._replace("config.json", old_config)
            except OSError:
                raise RuntimeConfigurationError(
                    "Configuration write and rollback failed; rerun setup before start."
                ) from None
            raise RuntimeConfigurationError(
                "Configuration write failed; previous files restored."
            ) from None
