"""The functional suite runs only when a run explicitly targets a live Hybro.

A plain ``pytest`` must never reach whatever answers on localhost:8000, which may
be a developer's real instance with a paid model provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.functional import conftest as functional


@pytest.mark.parametrize(
    ("environ", "enabled"),
    [
        ({}, False),
        ({"HYBRO_FORCE_FUNCTIONAL_TESTS": "0"}, False),
        ({"HYBRO_API_URL": ""}, False),
        ({"HYBRO_FORCE_FUNCTIONAL_TESTS": "1"}, True),
        ({"HYBRO_API_URL": "http://localhost:3100/api/v1"}, True),
    ],
)
def test_functional_tests_are_opt_in(environ, enabled):
    assert functional.functional_tests_enabled(environ) is enabled


class _Item:
    def __init__(self, *keywords: str) -> None:
        self.keywords = dict.fromkeys(keywords, True)
        self.markers: list = []

    def add_marker(self, marker) -> None:
        self.markers.append(marker)


def test_default_run_skips_functional_items_without_probing(monkeypatch):
    monkeypatch.delenv("HYBRO_FORCE_FUNCTIONAL_TESTS", raising=False)
    monkeypatch.delenv("HYBRO_API_URL", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("the gate must not probe a backend")

    monkeypatch.setattr(functional.httpx, "get", no_network)
    live, unit = _Item("functional"), _Item()

    functional.pytest_collection_modifyitems(SimpleNamespace(), [live, unit])

    assert [marker.kwargs["reason"] for marker in live.markers] == [
        functional.FUNCTIONAL_SKIP_REASON
    ]
    assert unit.markers == []


def test_forced_run_keeps_functional_items(monkeypatch):
    monkeypatch.setenv("HYBRO_FORCE_FUNCTIONAL_TESTS", "1")
    live = _Item("functional")

    functional.pytest_collection_modifyitems(SimpleNamespace(), [live])

    assert live.markers == []


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://localhost:3100/api/v1", "http://localhost:3100"),
        ("https://hybro.test/api/v1/", "https://hybro.test"),
    ],
)
def test_the_functional_client_sends_hybros_own_origin(url, origin):
    assert functional.own_origin(url) == origin


async def test_signing_in_without_the_owner_credentials_fails_clearly(monkeypatch):
    monkeypatch.delenv(functional.OWNER_EMAIL_ENV, raising=False)
    monkeypatch.delenv(functional.OWNER_PASSWORD_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception, match=functional.OWNER_PASSWORD_ENV):
        await functional.sign_in_as_owner(SimpleNamespace())
