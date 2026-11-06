"""read_token: the per-start diagnostics token handed over by the entrypoint."""

import json

import pytest

from diagnostics import read_token


def test_reads_the_token(tmp_path):
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps({"base_url": "http://x", "token": "a" * 64}))

    assert read_token(str(path)) == "a" * 64


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        "{not json",
        json.dumps({"token": 7}),
        json.dumps(["token"]),
        "{}",
        json.dumps({"token": ""}),
        json.dumps({"token": "short"}),
        json.dumps({"token": "G" * 64}),
        json.dumps({"token": "A" * 64}),
        json.dumps({"token": "a" * 64 + "\n"}),
        pytest.param("[" * 100_000, id="deeply-nested"),
    ],
)
def test_missing_or_malformed_file_reads_as_empty(tmp_path, content):
    path = tmp_path / "diagnostics.json"
    if content is not None:
        path.write_text(content)

    assert read_token(str(path)) == ""


def test_empty_path_reads_as_empty():
    assert read_token("") == ""
