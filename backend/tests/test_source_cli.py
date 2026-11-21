"""The `hybro source` helper: the request, the token output and exit codes."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import source_cli as cli

MACHINE_TOKEN = "cd" * 32
BASE_URL = "http://127.0.0.1:8000/api/v1/machine"
# An obviously fake source token, as the backend would return it.
SOURCE_TOKEN = "hybro_src_" + "x" * 43
CREATED = {
    "source_id": "src_1",
    "name": "CI bot",
    "has_token": True,
    "token_last4": "xxxx",
    "created_at": "2026-11-20T00:00:00Z",
    "last_used_at": None,
    "revoked_at": None,
    "token": SOURCE_TOKEN,
}


class FakeBackend:
    def __init__(self, status: int = 201, body: object = CREATED) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status
        self.body = body

    def __call__(self, url: str, token: str, data: bytes) -> tuple[int, bytes]:
        self.calls.append((url, token, json.loads(data)))
        if isinstance(self.body, Exception):
            raise self.body
        return self.status, json.dumps(self.body).encode()


@pytest.fixture(autouse=True)
def on_the_host(monkeypatch):
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)


@pytest.fixture
def handoff(tmp_path: Path) -> Path:
    # As a backend writes it: a 0600 file in a 0700 directory of this user.
    tmp_path.chmod(0o700)
    path = tmp_path / "machine.json"
    path.write_text(json.dumps({"base_url": BASE_URL, "token": MACHINE_TOKEN}))
    path.chmod(0o600)
    return path


def run(argv, handoff, backend) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=stdout, stderr=stderr, handoff=handoff, post=backend)
    return code, stdout.getvalue(), stderr.getvalue()


def test_create_posts_the_name_and_prints_the_token_once(handoff):
    backend = FakeBackend()

    code, out, err = run(["create", "--name", "CI bot"], handoff, backend)

    assert code == 0
    assert backend.calls == [(f"{BASE_URL}/sources", MACHINE_TOKEN, {"name": "CI bot"})]
    assert out.count(SOURCE_TOKEN) == 1
    assert "CI bot" in out
    assert "shown once" in out
    assert MACHINE_TOKEN not in out + err
    assert err == ""


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["create"],
        ["create", "--name"],
        ["delete", "--name", "x"],
        ["create", "--x", "y"],
    ],
)
def test_usage_errors_exit_2_without_a_request(handoff, argv):
    backend = FakeBackend()
    code, out, err = run(argv, handoff, backend)
    assert code == 2
    assert backend.calls == []
    assert "usage" in err


def test_an_invalid_name_is_refused(handoff):
    backend = FakeBackend(422, {"detail": {"code": "invalid_request", "message": "x"}})
    code, out, err = run(["create", "--name", " "], handoff, backend)
    assert code == 1
    assert "name" in err
    assert out == ""


def test_a_refused_machine_token_exits_2(handoff):
    backend = FakeBackend(
        401, {"detail": {"code": "machine_token_required", "message": "x"}}
    )
    code, _, err = run(["create", "--name", "CI bot"], handoff, backend)
    assert code == 2
    assert "restart Hybro" in err


@pytest.mark.parametrize(
    ("status", "body"), [(500, {}), (201, {"source_id": "src_1"}), (201, [])]
)
def test_an_unknown_outcome_exits_2_and_prints_no_token(handoff, status, body):
    code, out, err = run(
        ["create", "--name", "CI bot"], handoff, FakeBackend(status, body)
    )
    assert code == 2
    assert out == ""
    assert "hybro source" in err


def test_no_handoff_file_exits_2(tmp_path):
    code, _, err = run(
        ["create", "--name", "CI bot"], tmp_path / "missing.json", FakeBackend()
    )
    assert code == 2
    assert "handoff" in err


def test_an_unreachable_backend_exits_2(handoff):
    code, _, err = run(
        ["create", "--name", "CI bot"], handoff, FakeBackend(body=OSError("down"))
    )
    assert code == 2
    assert "cannot reach the backend" in err
