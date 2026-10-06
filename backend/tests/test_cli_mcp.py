"""MCP status probes never read credentials or execute agent tools."""

import tomllib
from pathlib import Path

import httpx
import pytest

import cli_mcp


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"service": "hybro-mcp", "status": "ready"}, "ready"),
        ({"service": "hybro-mcp", "status": "unsupported_auth"}, "unsupported_auth"),
        ({"service": "other", "status": "ready"}, "unavailable"),
        ({"service": "hybro-mcp", "status": ["ready"]}, "unavailable"),
        ([], "unavailable"),
    ],
)
def test_status_validates_identity_and_state(monkeypatch, body, expected):
    factory = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    def client(**kwargs):
        assert kwargs == {"timeout": 4.0, "trust_env": False, "follow_redirects": False}
        return factory(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli_mcp.httpx, "Client", client)
    assert cli_mcp.read_status() == expected
    assert str(calls[0].url) == "http://127.0.0.1:8001/health"
    assert calls[0].method == "GET"
    assert "authorization" not in calls[0].headers


def test_mcp_probe_is_in_wheel_module_inventory():
    metadata = Path(__file__).resolve().parents[1] / "pyproject.toml"
    document = tomllib.loads(metadata.read_text(encoding="utf-8"))
    assert "cli_mcp" in document["tool"]["setuptools"]["py-modules"]


def test_connection_failure_is_safe(monkeypatch):
    def failed(**kwargs):
        raise httpx.ConnectError("private diagnostics")

    monkeypatch.setattr(cli_mcp.httpx, "Client", failed)
    assert cli_mcp.read_status() == "unavailable"
