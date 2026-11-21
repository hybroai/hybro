"""The `hybro owner` helper: requests, password input, messages and exit codes."""

from __future__ import annotations

import io
import json
import os
import urllib.request
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler

import pytest

import owner_cli as cli

TOKEN = "cd" * 32
BASE_URL = "http://127.0.0.1:8000/api/v1/machine"
PASSWORD = "correct horse battery"


class FakeBackend:
    """Records each POST and answers with the queued status and body."""

    def __init__(self, status: int = 201, body: object = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status
        self.body = body

    def __call__(self, url: str, token: str, data: bytes) -> tuple[int, bytes]:
        self.calls.append((url, token, json.loads(data)))
        if isinstance(self.body, Exception):
            raise self.body
        raw = b"" if self.body is None else json.dumps(self.body).encode()
        return self.status, raw


@pytest.fixture(autouse=True)
def on_the_host(monkeypatch):
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)


def write_handoff(path: Path, content: str) -> Path:
    # As the backend writes it: a 0600 file in a 0700 directory of this user.
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def handoff(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return write_handoff(
        tmp_path / "machine.json", json.dumps({"base_url": BASE_URL, "token": TOKEN})
    )


def run(
    argv: list[str],
    handoff: Path,
    backend: FakeBackend,
    stdin: bytes = (PASSWORD + "\n").encode(),
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(
        argv,
        stdin=io.BytesIO(stdin),
        stdout=stdout,
        stderr=stderr,
        handoff=handoff,
        post=backend,
    )
    return code, stdout.getvalue(), stderr.getvalue()


PROFILE = {"user_id": "user_local_developer", "name": "Ada", "email": "ada@example.com"}
CREATE = ["create", "--name", "Ada", "--email", "ada@example.com"]


def test_create_posts_the_owner_with_the_password_from_stdin(handoff):
    backend = FakeBackend(201, PROFILE)
    code, out, err = run(CREATE, handoff, backend)
    assert (code, err) == (0, "")
    assert backend.calls == [
        (
            f"{BASE_URL}/owner",
            TOKEN,
            {"name": "Ada", "email": "ada@example.com", "password": PASSWORD},
        )
    ]
    assert out == "Created the owner account for ada@example.com.\n"
    assert PASSWORD not in out


def test_create_passes_the_owner_id(handoff):
    backend = FakeBackend(201, PROFILE)
    code, _, _ = run([*CREATE, "--owner-id", "user_2"], handoff, backend)
    assert code == 0
    assert backend.calls[0][2]["owner_id"] == "user_2"


def test_reset_password_posts_the_new_password(handoff):
    backend = FakeBackend(204)
    code, out, err = run(["reset-password"], handoff, backend)
    assert (code, err) == (0, "")
    assert backend.calls == [
        (f"{BASE_URL}/owner/password", TOKEN, {"password": PASSWORD})
    ]
    assert out == "Set the new password and signed out every browser.\n"


@pytest.mark.parametrize(
    ("stdin", "sent"),
    [
        (b"  spaced pass  \n", "  spaced pass  "),
        (b"no-newline-pass", "no-newline-pass"),
        (b"first-line-pass\nsecond line\n", "first-line-pass"),
        ("pässwörd\n".encode(), "pässwörd"),
    ],
)
def test_the_password_is_the_first_line_unchanged(handoff, stdin, sent):
    backend = FakeBackend(204)
    code, _, _ = run(["reset-password"], handoff, backend, stdin=stdin)
    assert code == 0
    assert backend.calls[0][2]["password"] == sent


@pytest.mark.parametrize(
    "stdin",
    [b"", b"\n", b"short\n", ("x" * 1025 + "\n").encode(), b"x" * 5000],
    ids=["empty", "blank", "short", "too_long", "unbounded"],
)
def test_a_password_out_of_bounds_is_refused_before_any_request(handoff, stdin):
    backend = FakeBackend(204)
    code, out, err = run(["reset-password"], handoff, backend, stdin=stdin)
    assert code == 1
    assert backend.calls == []
    assert "8 to 1024 characters" in err and "nothing was changed" in err
    assert out == ""


def test_the_longest_password_is_accepted(handoff):
    backend = FakeBackend(204)
    password = "ä" * 1024
    code, _, _ = run(
        ["reset-password"], handoff, backend, stdin=(password + "\n").encode()
    )
    assert code == 0
    assert backend.calls[0][2]["password"] == password


def test_a_password_that_is_not_utf8_is_refused(handoff):
    backend = FakeBackend(204)
    code, _, err = run(
        ["reset-password"], handoff, backend, stdin=b"\xff\xfe-pass-word\n"
    )
    assert code == 1
    assert backend.calls == []
    assert "UTF-8" in err


def _refusal(status: int, code: str, message: str, **extra: object) -> FakeBackend:
    return FakeBackend(status, {"detail": {"code": code, "message": message, **extra}})


def test_create_when_the_account_exists_points_to_reset(handoff):
    backend = _refusal(409, "account_exists", "The owner account already exists.")
    code, out, err = run(CREATE, handoff, backend)
    assert code == 1 and out == ""
    assert err == (
        "hybro owner: The owner account already exists. "
        "To set a new password, run: hybro owner reset-password\n"
    )


def test_reset_without_an_account_points_to_create(handoff):
    backend = _refusal(409, "no_account", "No owner account exists yet.")
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 1
    assert err == (
        "hybro owner: No owner account exists yet. "
        "Create it with: hybro owner create --name <name> --email <email>\n"
    )


def test_create_that_needs_an_owner_id_lists_the_choices(handoff):
    backend = _refusal(
        409,
        "owner_id_required",
        "Choose which administrator the owner account takes over.",
        choices=["user_a", "user_b"],
    )
    code, _, err = run(CREATE, handoff, backend)
    assert code == 1
    assert err == (
        "hybro owner: Choose which administrator the owner account takes over. "
        "Run again with --owner-id and one of: user_a, user_b\n"
    )


def test_create_with_an_invalid_name_or_email_says_which_fields(handoff):
    backend = _refusal(422, "invalid_request", "Invalid request; check the fields.")
    code, _, err = run(CREATE, handoff, backend)
    assert code == 1
    assert "the name or the email is not valid" in err


def test_an_invalid_create_with_an_owner_id_names_it(handoff):
    backend = _refusal(422, "invalid_request", "Invalid request; check the fields.")
    code, _, err = run([*CREATE, "--owner-id", "   "], handoff, backend)
    assert code == 1
    assert "the name, the email or the owner id is not valid" in err


def test_other_refusals_print_the_backend_message(handoff):
    backend = _refusal(
        429, "too_many_attempts", "Too many attempts at once. Try again shortly."
    )
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 1
    assert err == "hybro owner: Too many attempts at once. Try again shortly.\n"


def test_a_refusal_without_a_message_names_the_status(handoff):
    backend = FakeBackend(400, None)
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 1
    assert err == "hybro owner: refused (HTTP 400); nothing was changed.\n"


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, None), (503, {"detail": {"code": "x", "message": "m"}}), (200, None)],
)
def test_a_server_error_or_unexpected_answer_exits_2_without_claiming_nothing_changed(
    handoff, status, body
):
    backend = FakeBackend(status, body)
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 2
    assert f"the backend answered HTTP {status}" in err
    assert "nothing was changed" not in err
    assert "hybro logs --service backend --level error" in err


def test_a_timeout_says_the_change_may_have_applied(handoff):
    backend = FakeBackend(body=TimeoutError("timed out"))
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 2
    assert "did not answer within 30 s" in err
    assert "may have applied" in err
    assert "cannot reach" not in err


def test_a_refused_machine_token_exits_2(handoff):
    backend = _refusal(
        401,
        "machine_token_required",
        "This route is for the hybro command on the machine.",
    )
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 2
    assert "refused the machine token" in err
    assert TOKEN not in err


def test_an_unreachable_backend_exits_2_with_the_log_hint(handoff):
    backend = FakeBackend(body=URLError("refused"))
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 2
    assert "cannot reach the backend (URLError)" in err
    assert "hybro logs --service backend --level error" in err


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"base_url": "x"}'])
def test_a_missing_or_incomplete_handoff_exits_2(tmp_path, content):
    tmp_path.chmod(0o700)
    path = tmp_path / "machine.json"
    if content is not None:
        write_handoff(path, content)
    backend = FakeBackend(204)
    code, _, err = run(["reset-password"], path, backend)
    assert code == 2
    assert backend.calls == []
    assert str(path) in err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["create"],
        ["create", "--name", "Ada"],
        ["create", "--name", "Ada", "--email"],
        [*CREATE, "--name", "Bob"],
        [*CREATE, "--password", "x"],
        ["reset-password", "--name", "Ada"],
    ],
)
def test_usage_errors_exit_2_without_a_request(handoff, argv):
    backend = FakeBackend(204)
    code, _, err = run(argv, handoff, backend)
    assert code == 2
    assert backend.calls == []
    assert err.startswith("usage: owner_cli.py")


def test_an_unexpected_error_prints_its_type_only(handoff):
    backend = FakeBackend(body=RuntimeError(PASSWORD))
    code, _, err = run(["reset-password"], handoff, backend)
    assert code == 2
    assert err == "hybro owner: unexpected RuntimeError\n"


def test_the_transport_sends_json_with_the_token_and_no_proxy(monkeypatch):
    seen: dict = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b""

    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    status, body = cli.http_post(
        f"{BASE_URL}/owner/password", TOKEN, b'{"password": "x"}'
    )
    assert (status, body) == (204, b"")
    request = seen["request"]
    assert request.get_method() == "POST"
    assert request.get_header("X-hybro-machine-token") == TOKEN
    assert request.get_header("Content-type") == "application/json"
    assert request.data == b'{"password": "x"}'
    (handler,) = seen["handlers"]
    assert isinstance(handler, ProxyHandler) and handler.proxies == {}
    assert seen["timeout"] == cli.TIMEOUT_SECONDS


def test_the_transport_returns_an_error_status_with_its_body(monkeypatch):
    from urllib.error import HTTPError

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    assert cli.http_post(f"{BASE_URL}/owner", TOKEN, b"{}") == (409, b"{}")


def test_the_helper_imports_only_the_standard_library():
    import ast
    import sys

    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


# -- setup-code ----------------------------------------------------------------

SETUP_CODE = {"code": "fake-setup-code", "expires_at": "2030-01-01T00:00:00Z"}


def test_setup_code_prints_only_the_code_and_reads_no_password(handoff):
    backend = FakeBackend(200, SETUP_CODE)
    code, out, err = run(["setup-code"], handoff, backend, stdin=b"")
    assert (code, out, err) == (0, "fake-setup-code\n", "")
    assert backend.calls == [(f"{BASE_URL}/setup-code", TOKEN, {})]


def test_setup_code_when_the_account_exists_exits_1_without_output(handoff):
    backend = _refusal(409, "account_exists", "The owner account already exists.")
    code, out, err = run(["setup-code"], handoff, backend, stdin=b"")
    assert (code, out) == (1, "")
    assert "already exists" in err


@pytest.mark.parametrize("body", [None, {"code": ""}, {"code": 7}, {"other": "x"}])
def test_setup_code_without_a_code_in_the_answer_exits_2(handoff, body):
    code, out, err = run(["setup-code"], handoff, FakeBackend(200, body), stdin=b"")
    assert (code, out) == (2, "")
    assert "hybro logs --service backend --level error" in err


def test_setup_code_takes_no_options(handoff):
    backend = FakeBackend(200, SETUP_CODE)
    code, _, err = run(["setup-code", "--name", "Ada"], handoff, backend, stdin=b"")
    assert code == 2
    assert backend.calls == []
    assert err.startswith("usage: owner_cli.py")


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "too_many_attempts"), (400, "other"), (409, "something_else")],
)
def test_setup_code_exits_1_only_when_the_account_exists(handoff, status, code):
    backend = _refusal(status, code, "Refused.")
    exit_code, out, _ = run(["setup-code"], handoff, backend, stdin=b"")
    assert (exit_code, out) == (2, "")


def test_handoff_option_names_another_handoff_file(tmp_path):
    other = tmp_path / "run" / "machine.json"
    other.parent.mkdir(mode=0o700)
    write_handoff(other, json.dumps({"base_url": BASE_URL, "token": TOKEN}))
    backend = FakeBackend(200, SETUP_CODE)
    stdout, stderr = io.StringIO(), io.StringIO()

    code = cli.main(
        ["--handoff", str(other), "setup-code"],
        stdin=io.BytesIO(b""),
        stdout=stdout,
        stderr=stderr,
        handoff=tmp_path / "missing.json",
        post=backend,
    )

    assert (code, stdout.getvalue()) == (0, "fake-setup-code\n")
    assert backend.calls[0][1] == TOKEN


@pytest.mark.parametrize("argv", [["--handoff"], ["--handoff", "", "setup-code"]])
def test_handoff_option_needs_a_path(handoff, argv):
    code, _, err = run(argv, handoff, FakeBackend(200, SETUP_CODE), stdin=b"")
    assert code == 2
    assert err.startswith("usage: owner_cli.py")


def test_the_default_handoff_on_the_host_is_under_hybro_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)
    assert cli.default_handoff(image=tmp_path / "absent.json") == (
        tmp_path / "home" / "run" / "machine.json"
    )


def test_a_relative_hybro_home_is_refused(tmp_path, monkeypatch):
    # The loader refuses a relative HYBRO_HOME, so the helper does too.
    monkeypatch.setenv("HYBRO_HOME", "relative/home")
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(
        ["setup-code"],
        stdin=io.BytesIO(b""),
        stdout=stdout,
        stderr=stderr,
        post=FakeBackend(200, SETUP_CODE),
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert "HYBRO_HOME must be an absolute path" in stderr.getvalue()


def test_on_the_host_the_checkout_file_wins_over_a_file_in_tmp(tmp_path, monkeypatch):
    # /tmp is shared: a file another user put at the image path is never used
    # outside the image.
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HYBRO_CONTAINER", raising=False)
    image = tmp_path / "image.json"
    image.write_text("{}")

    assert cli.default_handoff(image=image) == (
        tmp_path / "home" / "run" / "machine.json"
    )


def test_in_the_image_the_entrypoint_file_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYBRO_CONTAINER", "image")
    image = tmp_path / "absent.json"

    assert cli.default_handoff(image=image) == image


def test_a_connection_error_names_the_checkout_port(handoff):
    def unreachable(url, token, body):
        raise OSError("refused")

    code, _, err = run(["setup-code"], handoff, unreachable, stdin=b"")
    assert code == 2
    assert "port 8000" in err


# -- On the host the file must be this user's private file ------------------------


def _planted(tmp_path: Path, case: str) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    content = json.dumps({"base_url": BASE_URL, "token": TOKEN})
    path = directory / "machine.json"
    if case == "open_directory":
        write_handoff(path, content)
        directory.chmod(0o777)
    elif case == "open_file":
        write_handoff(path, content).chmod(0o644)
    elif case == "linked_file":
        write_handoff(tmp_path / "elsewhere.json", content)
        path.symlink_to(tmp_path / "elsewhere.json")
    elif case == "linked_directory":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        write_handoff(real / "machine.json", content)
        directory.rmdir()
        directory.symlink_to(real, target_is_directory=True)
    elif case == "remote_base_url":
        write_handoff(
            path, json.dumps({"base_url": "http://attacker.example/m", "token": TOKEN})
        )
    elif case == "https_base_url":
        write_handoff(
            path, json.dumps({"base_url": "https://127.0.0.1:8000/m", "token": TOKEN})
        )
    return path


@pytest.mark.parametrize(
    "case",
    [
        "open_directory",
        "open_file",
        "linked_file",
        "linked_directory",
        "remote_base_url",
        "https_base_url",
    ],
)
def test_the_password_is_never_sent_with_a_file_others_could_plant(tmp_path, case):
    path = _planted(tmp_path, case)
    backend = FakeBackend(204)

    code, _, err = run(["reset-password"], path, backend)

    assert code == 2
    assert backend.calls == []
    assert PASSWORD not in err and TOKEN not in err


def test_in_the_image_the_entrypoint_file_needs_only_a_loopback_url(
    tmp_path, monkeypatch
):
    # The container has one user; the entrypoint's file is read as it is.
    monkeypatch.setenv("HYBRO_CONTAINER", "image")
    path = tmp_path / "machine.json"
    path.write_text(json.dumps({"base_url": BASE_URL, "token": TOKEN}))
    path.chmod(0o644)
    backend = FakeBackend(200, SETUP_CODE)

    code, out, _ = run(["setup-code"], path, backend, stdin=b"")

    assert (code, out) == (0, "fake-setup-code\n")


def test_a_linked_file_is_named_as_refused_not_missing(tmp_path):
    path = _planted(tmp_path, "linked_file")

    code, _, err = run(["reset-password"], path, FakeBackend(204))

    assert code == 2
    assert "not a private file of this user" in err


def test_a_fifo_is_refused_without_waiting(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    path = directory / "machine.json"
    os.mkfifo(path, 0o600)
    backend = FakeBackend(204)

    code, _, err = run(["reset-password"], path, backend)

    assert code == 2
    assert backend.calls == []
    assert "not a private file of this user" in err
