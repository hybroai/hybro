from __future__ import annotations

import json
import multiprocessing
import os
import stat
import traceback
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from common.config.runtime_config import (
    ApiKeyCredential,
    OAuthCredential,
    ProviderId,
    RuntimeConfig,
    RuntimeConfigurationError,
    resolve_credential,
)
from common.config.runtime_store import (
    RuntimeConfigStore,
    parse_config,
    parse_credential,
)
from tests.fakes.llm_runtime import oauth_credential


def config(provider: ProviderId = "openai", auth: str = "api_key") -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "version": 1,
            "provider": {"id": provider, "auth": auth},
            "models": {"text": "test-text"},
        }
    )


def key(
    provider: ProviderId = "openai", value: str = "test-secret"
) -> ApiKeyCredential:
    return ApiKeyCredential(provider=provider, api_key=SecretStr(value))


def oauth() -> OAuthCredential:
    return oauth_credential(expires_at=2000000000.0, refresh="test-refresh")


@pytest.mark.parametrize(
    "document",
    [
        b"version: 2\nprovider: {id: openai, auth: api_key}\nmodels: {text: test}",
        b"version: true\nprovider: {id: openai, auth: api_key}\nmodels: {text: test}",
        b"version: 1.0\nprovider: {id: openai, auth: api_key}\nmodels: {text: test}",
        b"version: 1\nprovider: {id: openai, auth: api_key}\nmodels: {text: '  '}",
        b"provider: {id: openai, auth: api_key}\nmodels: {text: 12}",
        b"provider: {id: openai, auth: api_key}\nmodels: {text: test, typo: value}",
        b"provider: {id: openai, auth: api_key, typo: value}\nmodels: {text: test}",
        b"provider: {id: openai, auth: api_key}\nmodels: {text: test}\ntypo: value",
        b"provider: {id: deepseek, auth: oauth}\nmodels: {text: test}",
        b"provider: {id: other, auth: api_key}\nmodels: {text: test}",
        b"provider: {id: openai, id: deepseek, auth: api_key}\nmodels: {text: test}",
        b"provider: {id: openai, auth: api_key}\nmodels: {text: first, text: last}",
        b"provider: &p {id: openai, auth: api_key}\nmodels: *p",
        b"!!python/object/apply:os.system ['do not run']",
        b"---\n{}\n---\n{}",
        b"- not-a-mapping",
        b"",
        b"\xff",
        b" " * (64 * 1024 + 1),
        (b"[" * 100) + b"0" + (b"]" * 100),
    ],
)
def test_legacy_yaml_and_malformed_json_fail_with_safe_error(document: bytes) -> None:
    with pytest.raises(RuntimeConfigurationError, match="Invalid config.json"):
        parse_config(document)


@pytest.mark.parametrize(
    "document",
    [
        b'{"provider":"openai","auth":"api_key","api_key":"first","api_key":"last"}',
        b'{"provider":"openai","auth":"api_key","api_key":"x","unknown":"secret"}',
        b'{"provider":"openai","auth":"api_key","api_key":"   "}',
        b'{"provider":"openai","auth":"oauth","access_token":"x"}',
        b'{"provider":"openai","auth":"api_key","api_key":12}',
        b'{"provider":"anthropic","auth":"oauth"}',
        b"[",
        b"\xff",
        b" " * (64 * 1024 + 1),
    ],
)
def test_invalid_credentials_fail_closed(document: bytes) -> None:
    with pytest.raises(RuntimeConfigurationError, match="Invalid auth.json"):
        parse_credential(document)


def test_serialization_redacts_secrets() -> None:
    credential = oauth()
    assert "test-access" not in repr(credential)
    assert "test-refresh" not in credential.model_dump_json()
    assert "test-secret" not in repr(resolve_credential(config(), key(), {}))
    invalid = (
        b'{"provider":"openai","auth":"api_key","api_key":"SECRET","extra":"SECRET"}'
    )
    with pytest.raises(RuntimeConfigurationError) as error:
        parse_credential(invalid)
    assert "SECRET" not in "".join(traceback.format_exception(error.value))


def test_revision_is_canonical_and_excludes_credentials() -> None:
    first = config()
    reordered = parse_config(
        b'{"models":{"image":null,"text":"test-text"},"provider":{"auth":"api_key","id":"openai"},"version":1}'
    )
    assert first.revision == reordered.revision
    assert first.revision != config("deepseek").revision
    assert first.revision != config(auth="oauth").revision
    assert (
        first.revision
        != RuntimeConfig.model_validate(
            {**first.model_dump(), "models": {"text": "different"}}
        ).revision
    )
    with pytest.raises(ValidationError):
        first.version = 1


@pytest.mark.parametrize("provider", ["openai", "deepseek", "anthropic"])
def test_credential_sources(provider: ProviderId) -> None:
    settings = config(provider)
    stored = key(provider)
    env = {f"{provider.upper()}_API_KEY": "environment-secret"}
    assert resolve_credential(settings, stored, {}).source == "stored"
    resolved = resolve_credential(settings, None, env)
    assert resolved.source == "environment"
    assert isinstance(resolved.credential, ApiKeyCredential)
    assert resolved.credential.api_key.get_secret_value() == "environment-secret"
    with pytest.raises(RuntimeConfigurationError, match="remove one"):
        resolve_credential(settings, stored, env)
    with pytest.raises(RuntimeConfigurationError, match="missing"):
        resolve_credential(settings, None, {})


def test_oauth_never_falls_back_to_api_key() -> None:
    settings = config(auth="oauth")
    resolved = resolve_credential(settings, oauth(), {})
    assert resolved.source == "stored"
    assert isinstance(resolved.credential, OAuthCredential)
    assert (
        resolve_credential(
            settings, oauth(), {"OPENAI_API_KEY": "independent-embedding-key"}
        )
        == resolved
    )
    with pytest.raises(RuntimeConfigurationError, match="missing"):
        resolve_credential(settings, None, {"OPENAI_API_KEY": "not-oauth"})


def test_credential_identity_must_match() -> None:
    with pytest.raises(RuntimeConfigurationError, match="identity differ"):
        resolve_credential(config("deepseek"), key(), {})
    with pytest.raises(RuntimeConfigurationError, match="identity differ"):
        resolve_credential(config(auth="oauth"), key(), {})
    with pytest.raises(RuntimeConfigurationError, match="missing"):
        resolve_credential(config("deepseek"), None, {"OPENAI_API_KEY": "other-key"})


def test_roundtrip_and_noop(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    store = RuntimeConfigStore(home)
    assert store.save(config(), key(), {})
    state = store.load({})
    assert state.config == config()
    assert state.authentication.credential == key()
    before = {
        name: (home / name).stat().st_mtime_ns for name in ("config.json", "auth.json")
    }
    assert not store.save(config(), key(), {})
    assert before == {name: (home / name).stat().st_mtime_ns for name in before}
    assert "test-secret" not in (home / "config.json").read_text()
    assert "image" not in json.loads((home / "config.json").read_text())["models"]
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    for name in ("auth.json", "config.json", ".runtime.lock"):
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600


def test_semantic_noop_preserves_user_json_formatting(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    path = tmp_path / "config.json"
    original = json.dumps(json.loads(path.read_text()), indent=4) + "\n"
    path.write_text(original)
    assert not store.save(config(), key(), {})
    assert path.read_text() == original


def test_switch_and_credential_only_revision(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    revision = store.load({}).config.revision
    assert store.save(config(), key(value="replacement"), {})
    assert store.load({}).config.revision == revision
    assert store.save(config("deepseek"), key("deepseek"), {})
    assert store.load({}).config.provider.id == "deepseek"
    assert store.save(config("anthropic"), None, {"ANTHROPIC_API_KEY": "from-env"})
    assert (tmp_path / "auth.json").exists()
    assert store.load({}).authentication.source == "stored"


def test_oauth_roundtrip_and_noop(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    assert store.save(config(auth="oauth"), oauth(), {})
    assert store.load({}).authentication.credential == oauth()
    assert not store.save(config(auth="oauth"), oauth(), {})
    assert (
        json.loads((tmp_path / "auth.json").read_bytes())["refresh_token"]
        == "test-refresh"
    )


def test_invalid_candidate_does_not_change_files(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    with pytest.raises(RuntimeConfigurationError):
        store.save(config("deepseek"), key(), {})
    assert store.load({}).config == config()


@pytest.mark.parametrize("existing", [False, True])
def test_config_write_failure_rolls_back_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    store = RuntimeConfigStore(tmp_path)
    if existing:
        store.save(config(), key(), {})
    original = store._replace
    calls = 0

    def fail_once(name: str, data: bytes | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("filesystem unavailable")
        original(name, data)

    monkeypatch.setattr(store, "_replace", fail_once)
    with pytest.raises(RuntimeConfigurationError, match="previous files restored"):
        store.save(config("deepseek"), key("deepseek"), {})
    if existing:
        assert store.load({}).authentication.credential == key()
    else:
        assert not (tmp_path / "config.json").exists()
        assert not (tmp_path / "auth.json").exists()


def test_interrupted_identity_fails_closed_and_can_be_replaced(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    credential_file = tmp_path / "auth.json"
    credential_file.write_text(
        '{"provider":"deepseek","auth":"api_key","api_key":"new"}'
    )
    # Loads without a credential, so the backend boots and setup can repair it.
    state = store.load({})
    assert state.authentication is None
    assert "identity differ" in (state.credential_error or "")
    assert "new" not in (state.credential_error or "")
    assert store.save(config("deepseek"), key("deepseek", "new"), {})
    assert store.load({}).config.provider.id == "deepseek"


def test_relative_path_fails_and_missing_config_is_unconfigured(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="absolute"):
        RuntimeConfigStore(Path("relative"))
    store = RuntimeConfigStore(tmp_path / "runtime")
    state = store.load({})
    assert state.config == RuntimeConfig() and not state.config.configured
    assert state.authentication is None and state.credential_error is None
    assert store.read_config() == RuntimeConfig()
    assert not (store.home / "config.json").exists()


def test_configured_provider_without_credential_loads_with_reason(
    tmp_path: Path,
) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    (tmp_path / "auth.json").unlink()
    state = store.load({})
    assert state.config.provider is not None
    assert state.authentication is None
    assert state.credential_error == "Provider API key missing; run setup."


def test_update_config_creates_a_missing_document(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.update_config(["backend", "log_level"], "DEBUG")
    assert json.loads((store.home / "config.json").read_text()) == {
        "version": 1,
        "backend": {"log_level": "DEBUG"},
    }
    assert not store.read_config().configured


@pytest.mark.parametrize("present", ["provider", "models"])
def test_provider_and_models_are_set_together(present: str) -> None:
    document = {
        "version": 1,
        "provider": {"id": "openai", "auth": "api_key"},
        "models": {"text": "gpt-4o-mini"},
    }
    del document["models" if present == "provider" else "provider"]
    with pytest.raises(RuntimeConfigurationError, match="config"):
        parse_config(json.dumps(document).encode())


def test_unconfigured_document_resolves_no_credential() -> None:
    assert resolve_credential(RuntimeConfig(), key(), {}) is None
    with pytest.raises(RuntimeConfigurationError, match="Select a Provider"):
        RuntimeConfigStore(Path("/nonexistent-hybro-test")).save(
            RuntimeConfig(), None, {}
        )


def test_symlink_credential_is_not_read(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime")
    store.save(config(), key(), {})
    (store.home / "auth.json").unlink()
    outside = tmp_path / "outside"
    outside.write_text("secret outside the configured home")
    (store.home / "auth.json").symlink_to(outside)
    with pytest.raises(RuntimeConfigurationError, match="Cannot read"):
        store.load({})
    assert outside.read_text() == "secret outside the configured home"


@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_unsafe_existing_directory_rejected(tmp_path: Path, mode: int) -> None:
    home = tmp_path / "unsafe"
    home.mkdir()
    home.chmod(mode)
    store = RuntimeConfigStore(home)
    with pytest.raises(RuntimeConfigurationError, match="0700"):
        store.save(config(), key(), {})
    assert not (home / "auth.json").exists()
    with pytest.raises(RuntimeConfigurationError, match="0700"):
        store.load({})


@pytest.mark.parametrize("name", ["auth.json", ".runtime.lock"])
@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_unsafe_existing_file_rejected_before_load_or_noop(
    tmp_path: Path, name: str, mode: int
) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    (tmp_path / name).chmod(mode)
    with pytest.raises(RuntimeConfigurationError, match="0600"):
        store.load({})
    with pytest.raises(RuntimeConfigurationError, match="0600"):
        store.save(config(), key(), {})


def _reject_fifo(home: str) -> None:
    with pytest.raises(RuntimeConfigurationError, match="regular"):
        RuntimeConfigStore(Path(home)).load({})


@pytest.mark.parametrize("name", ["config.json", "auth.json", ".runtime.lock"])
def test_fifo_rejected_without_blocking(tmp_path: Path, name: str) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    (tmp_path / name).unlink()
    os.mkfifo(tmp_path / name, mode=0o600)
    process = multiprocessing.get_context("spawn").Process(
        target=_reject_fifo, args=(str(tmp_path),)
    )
    try:
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


@pytest.mark.parametrize("config_state", ["missing", "mismatched", "invalid"])
def test_setup_reads_credential_independently_of_config(
    tmp_path: Path, config_state: str
) -> None:
    store = RuntimeConfigStore(tmp_path)
    assert store.read_stored_credential() is None
    store.save(config(), key(), {})
    path = tmp_path / "config.json"
    if config_state == "missing":
        path.unlink()
    elif config_state == "mismatched":
        path.write_text("provider: {id: deepseek, auth: api_key}\nmodels: {text: test}")
    else:
        path.write_text("invalid: config")
    assert store.read_stored_credential() == key()
    assert "test-secret" not in repr(store.read_stored_credential())


@pytest.mark.parametrize("auth_state", ["invalid", "permissions", "symlink", "fifo"])
def test_setup_read_credential_rejects_unsafe_auth(
    tmp_path: Path, auth_state: str
) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    path = tmp_path / "auth.json"
    if auth_state == "invalid":
        path.write_text("not json")
    elif auth_state == "permissions":
        path.chmod(0o644)
    else:
        path.unlink()
        if auth_state == "symlink":
            path.symlink_to(tmp_path / "config.json")
        else:
            os.mkfifo(path, mode=0o600)
    with pytest.raises(RuntimeConfigurationError):
        store.read_stored_credential()


@pytest.mark.parametrize("change", ["added", "modified", "deleted"])
def test_setup_save_checks_credential_snapshot_before_writing(
    tmp_path: Path, change: str
) -> None:
    store = RuntimeConfigStore(tmp_path)
    env = {"OPENAI_API_KEY": "fixture-env"}
    if change != "added":
        store.save(config(), key(), {})
    snapshot = store.read_stored_credential()
    if change == "deleted":
        store.save(config(), None, env)
    else:
        store.save(config(), key(value="fixture-concurrent"), {})
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(RuntimeConfigurationError, match="changed during setup; rerun"):
        store.save(config("deepseek"), key("deepseek"), {}, expected_stored=snapshot)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_setup_snapshot_checked_before_semantic_noop(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path)
    store.save(config(), key(), {})
    with pytest.raises(RuntimeConfigurationError, match="changed during setup"):
        store.save(config(), key(), {}, expected_stored=None)
    assert not store.save(config(), key(), {}, expected_stored=key())


def test_setup_snapshot_none_means_absent_and_default_remains_unconditional(
    tmp_path: Path,
) -> None:
    store = RuntimeConfigStore(tmp_path)
    assert store.save(config(), key(), {}, expected_stored=None)
    assert store.save(config(), key(value="replacement"), {})


def _process_save(home: str, provider: ProviderId) -> None:
    store = RuntimeConfigStore(Path(home))
    for _ in range(10):
        store.save(config(provider), key(provider), {})
        # Another writer can win; load must always see a matching complete pair.
        state = store.load({})
        assert state.config.provider.id == state.authentication.credential.provider


def test_processes_share_one_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_process_save, args=(str(tmp_path), provider))
        for provider in ("openai", "deepseek")
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
