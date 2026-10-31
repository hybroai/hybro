"""Explicit canonical config/credential fixtures; never load developer state."""

import base64
import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

from common.config.runtime_config import (
    ApiKeyCredential,
    OAuthCredential,
    ResolvedCredential,
    RuntimeConfig,
    RuntimeModels,
    RuntimeProvider,
)
from common.config.runtime_store import RuntimeConfigStore, RuntimeState

if TYPE_CHECKING:
    from llm_gateway.provider_runtime import ProviderRuntime


def oauth_credential(
    account="fixture-account", *, expires_at=None, refresh="fixture-refresh"
) -> OAuthCredential:
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return OAuthCredential(
        access_token=SecretStr("fixture." + payload + ".signature"),
        refresh_token=SecretStr(refresh),
        account_id=account,
        expires_at=float(expires_at if expires_at is not None else time.time() + 3600),
    )


def oauth_config() -> RuntimeConfig:
    return RuntimeConfig(
        provider=RuntimeProvider(id="openai", auth="oauth"),
        models=RuntimeModels(text="gpt-6-sol"),
    )


def runtime_state(provider="openai", model="gpt-4o-mini") -> RuntimeState:
    return RuntimeState(
        RuntimeConfig(
            provider=RuntimeProvider(id=provider, auth="api_key"),
            models=RuntimeModels(text=model),
        ),
        ResolvedCredential(
            "stored",
            ApiKeyCredential(provider=provider, api_key=SecretStr("test-only-key")),
        ),
    )


def provider_runtime(state: RuntimeState) -> "ProviderRuntime":
    """A holder over a fresh private store that contains exactly ``state``."""
    from llm_gateway.provider_runtime import ProviderRuntime
    from tests.fakes.config_bootstrap import home

    store = RuntimeConfigStore(Path(tempfile.mkdtemp(dir=home)) / "runtime")
    store.save(state.config, state.authentication.credential, {})
    return ProviderRuntime(store)
