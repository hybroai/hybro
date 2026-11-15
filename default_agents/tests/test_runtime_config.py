"""Agent configuration is a scoped CLI projection, not dotenv discovery."""

import json

import pytest

from runtime_config import get_config


@pytest.fixture(autouse=True)
def clear_config_cache():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_dotenv_is_not_a_configuration_source(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=fixture-old\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HYBRO_AGENT_CONFIG", raising=False)
    with pytest.raises(RuntimeError, match="start through hybro"):
        get_config()


def test_scoped_projection_is_validated_and_token_not_in_repr(monkeypatch):
    monkeypatch.setenv("HYBRO_AGENT_CONFIG", json.dumps({"base_url": "http://backend:8000/api/v1/internal/llm", "token": "fixture-secret-" * 4, "image_size": "1024x1024"}))
    config = get_config()
    assert config.text_model == "gpt-4o-mini"
    assert "fixture-secret" not in repr(config)
    assert get_config() is config


def test_full_or_invalid_config_is_rejected_without_echo(monkeypatch):
    monkeypatch.setenv("HYBRO_AGENT_CONFIG", '{"api_key":"private-value"}')
    with pytest.raises(RuntimeError) as caught:
        get_config()
    assert "private-value" not in str(caught.value)
