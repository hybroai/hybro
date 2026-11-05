"""The `hybro doctor` helper: rendering, problems, exit codes and the bundle."""

from __future__ import annotations

import copy
import gzip
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler

import pytest

import diagnostics_cli as cli
from diagnostics.models import DiagnosticsSnapshot

TOKEN = "ab" * 32
BASE_URL = "http://127.0.0.1:8000/api/v1/diagnostics"

HEALTHY: dict = {
    "generated_at": "2026-11-05T12:00:00Z",
    "service": {
        "version": "0.4.4",
        "instance_id": "ab12cd34",
        "environment": "production",
        "started_at": "2026-11-05T08:48:00Z",
        "uptime_seconds": 11520.0,
        "auth_mode": "mock",
        "log_level": "INFO",
        "log_format": "json",
    },
    "health": {
        "mongo_connected": True,
        "redis_expected": False,
        "delivery_pubsub_connected": True,
        "delivery_kv_connected": True,
        "eventing_connected": True,
        "redis_runtime_connected": False,
        "agent_search_index_ready": True,
    },
    "provider": {"state": "configured", "last_error": None},
    "agents": {
        "total": 6,
        "active": 5,
        "inactive": 1,
        "by_source": {"bundled": 6},
        "items": [],
        "truncated": False,
    },
    "jobs": [
        {
            "job": "orchestrator_recovery",
            "running": True,
            "interval_seconds": 30.0,
            "last_outcome": "ok",
            "last_started_at": "2026-11-05T11:59:47Z",
            "last_completed_at": "2026-11-05T11:59:48Z",
            "last_duration_ms": 12.0,
            "last_skipped_at": None,
            "runs": 3,
            "failures": 0,
            "skips": 0,
            "consecutive_failures": 0,
            "last_error_type": None,
        },
        {
            "job": "agent_health_check",
            "running": True,
            "interval_seconds": 60.0,
            "last_outcome": "ok",
            "last_started_at": "2026-11-05T11:58:55Z",
            "last_completed_at": "2026-11-05T11:58:55Z",
            "last_duration_ms": 5.0,
            "last_skipped_at": None,
            "runs": 2,
            "failures": 0,
            "skips": 0,
            "consecutive_failures": 0,
            "last_error_type": None,
        },
    ],
    "leader": {"enabled": False, "instance_id": "ab12cd34"},
    "eventing": {
        "connected": True,
        "transport": "memory",
        "dead_letter_count": 0,
        "dead_letter_capacity": 100,
        "dead_letters_by_stage": {},
        "last_dead_letter_at": None,
    },
    "sse": {"rooms": 1, "connections": 2},
    "recovery": {
        "last_cycle_completed_at": "2026-11-05T11:59:48Z",
        "last_cycle_failed_phases": [],
        "non_terminal_runs": 0,
        "agent_calls_outstanding": 0,
        "projection_outbox_pending": 0,
        "projection_outbox_blocked": 0,
        "observation_conflicts_open": 0,
    },
    "canary": {
        "job_enabled": False,
        "metrics": {
            "runs_by_status": {},
            "runs_by_status_window": {},
            "projection_outbox_pending": 0,
            "projection_outbox_blocked": 0,
            "projection_outbox_blocked_oldest_at": None,
            "agent_calls_outstanding": 0,
            "observation_conflicts_open": 0,
            "recovery_cycle_last_run_at": None,
            "collected_at": "2026-11-05T11:59:50Z",
        },
        "breaches": [],
    },
    "store": {
        "ready": True,
        "queued": 0,
        "dropped": 0,
        "written": 10,
        "write_failures": 0,
        "last_error_type": None,
        "last_write_at": "2026-11-05T11:59:59Z",
        "ring_size": 10,
        "ring_capacity": 2000,
    },
    "errors": [],
}

HEALTHY_TEXT = """\
Hybro 0.4.4, instance ab12cd34, up 3h 12m
Provider     configured
MongoDB      connected
Redis        not configured
Agents       5 active, 1 inactive
Jobs         2 running, 0 failing
  orchestrator_recovery    ok       12s ago   every 30s
  agent_health_check       ok       1m 5s ago every 1m
Leader       not used (single process)
Event bus    memory, 0 dead letters
SSE          2 connections in 1 room
Recovery     last cycle 12s ago, 0 open runs, 0 outstanding agent calls, 0 blocked projections
Canary       job off, 0 breaches
Diagnostics  store ready, 0 dropped, 0 write failures
Problems: none"""


def snapshot(**changes: object) -> dict:
    data = copy.deepcopy(HEALTHY)
    data.update(changes)
    return data


def validated(data: dict) -> dict:
    """The fixture as the API serialises it, so a model change breaks the tests."""
    return json.loads(DiagnosticsSnapshot.model_validate(data).model_dump_json())


def body(data: dict) -> bytes:
    return json.dumps(validated(data)).encode()


def render(data: dict) -> str:
    return cli.render(validated(data))


def problems(data: dict) -> list[str]:
    return cli.problems(validated(data))


def lines_of(data: dict) -> dict[str, str]:
    """Each section line of the rendered text by its label."""
    return {line[:13].strip(): line[13:] for line in render(data).splitlines()}


# ---- render and problems ----


def test_render_a_healthy_snapshot():
    assert render(HEALTHY) == HEALTHY_TEXT
    assert problems(HEALTHY) == []


def test_problems_lists_each_kind():
    data = snapshot(
        provider={"state": "unconfigured", "last_error": None},
        agents=None,
        errors=[{"section": "agents", "error_type": "TimeoutError"}],
    )
    data["health"].update(
        mongo_connected=False, redis_expected=True, delivery_kv_connected=False
    )
    data["health"]["redis_runtime_connected"] = True
    data["jobs"][1].update(last_outcome="failed", last_error_type="TimeoutError")
    data["eventing"]["dead_letter_count"] = 2
    data["recovery"].update(
        last_cycle_failed_phases=["projections"], projection_outbox_blocked=3
    )
    data["canary"]["breaches"] = [
        {"metric": "projection_outbox_blocked", "threshold": 0.0, "observed": 3.0}
    ]
    data["store"].update(ready=False, write_failures=4, last_error_type="OSError")

    assert problems(data) == [
        "provider: not configured",
        "mongodb: not connected",
        "redis: not connected (delivery_kv)",
        "agents: unavailable (TimeoutError)",
        "job agent_health_check: last run failed (TimeoutError)",
        "event bus: 2 dead letters",
        "recovery: failed phases: projections",
        "recovery: 3 blocked projections",
        "canary: projection_outbox_blocked at 3 (threshold 0)",
        "diagnostics store: not ready",
        "diagnostics store: 4 write failures (OSError)",
    ]
    text = render(data)
    assert text.endswith(
        "Problems:\n" + "\n".join(f"  {line}" for line in problems(data))
    )
    lines = lines_of(data)
    assert lines["Provider"] == "not configured"
    assert lines["MongoDB"] == "not connected"
    assert lines["Redis"] == "not connected (delivery_kv)"
    assert lines["Jobs"] == "2 running, 1 failing"
    assert "agent_health_check       failed" in text


def test_null_sections_with_errors_render_unavailable():
    data = snapshot(
        agents=None,
        canary=None,
        errors=[
            {"section": "agents", "error_type": "TimeoutError"},
            {"section": "canary", "error_type": "TimeoutError"},
        ],
    )
    data["health"]["mongo_connected"] = False
    data["recovery"].update(
        non_terminal_runs=None,
        agent_calls_outstanding=None,
        projection_outbox_pending=None,
        projection_outbox_blocked=None,
        observation_conflicts_open=None,
    )

    lines = lines_of(data)
    assert lines["Agents"] == "unavailable (TimeoutError)"
    assert lines["Canary"] == "unavailable (TimeoutError)"
    assert lines["MongoDB"] == "not connected"
    assert lines["Recovery"] == (
        "last cycle 12s ago, unknown open runs, unknown outstanding agent calls, "
        "unknown blocked projections"
    )
    found = problems(data)
    assert "agents: unavailable (TimeoutError)" in found
    assert "canary: unavailable (TimeoutError)" in found


def test_recovery_before_the_first_cycle():
    data = snapshot()
    data["recovery"]["last_cycle_completed_at"] = None
    assert lines_of(data)["Recovery"].startswith("no cycle yet, 0 open runs")
    assert problems(data) == []


def test_null_section_without_error_is_not_a_problem():
    data = snapshot(canary=None)
    assert lines_of(data)["Canary"] == "not available"
    assert problems(data) == []


def test_failed_recovery_phases_show_on_the_line_and_fail_the_check(tmp_path):
    data = snapshot()
    data["recovery"]["last_cycle_failed_phases"] = ["agent_calls", "projections"]
    assert lines_of(data)["Recovery"].endswith(
        ", failed phases: agent_calls, projections"
    )
    assert problems(data) == ["recovery: failed phases: agent_calls, projections"]
    run = Run(tmp_path, {"/snapshot": body(data)})
    assert run("doctor") == 1


# ---- doctor: exit codes and transport ----


class Run:
    """``main`` with an injected handoff file and HTTP call."""

    def __init__(self, tmp_path: Path, routes: dict, *, handoff: bool = True):
        self.handoff = tmp_path / "diagnostics.json"
        if handoff:
            self.handoff.write_text(json.dumps({"base_url": BASE_URL, "token": TOKEN}))
        self.routes = routes
        self.requests: list[tuple[str, dict[str, list[str]]]] = []
        self.stdout = io.BytesIO()
        self.stderr = io.StringIO()

    def fetch(self, url: str, token: str) -> bytes:
        assert token == TOKEN
        parts = urlsplit(url)
        assert url.startswith(BASE_URL)
        path = parts.path[len(urlsplit(BASE_URL).path) :]
        query = parse_qs(parts.query)
        self.requests.append((path, query))
        key = path + ("?source=memory" if query.get("source") == ["memory"] else "")
        route = self.routes[key]
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(query)
        return route

    def __call__(self, *argv: str, stdin: bytes = b"") -> int:
        return cli.main(
            list(argv),
            stdin=io.BytesIO(stdin),
            stdout=self.stdout,
            stderr=self.stderr,
            handoff=self.handoff,
            fetch=self.fetch,
        )


def test_doctor_exit_codes(tmp_path):
    healthy = Run(tmp_path, {"/snapshot": body(HEALTHY)})
    assert healthy("doctor") == 0
    assert healthy.stdout.getvalue().decode() == HEALTHY_TEXT + "\n"

    failing = snapshot(provider={"state": "unconfigured", "last_error": None})
    problem = Run(tmp_path, {"/snapshot": body(failing)})
    assert problem("doctor") == 1
    assert "  provider: not configured" in problem.stdout.getvalue().decode()


def test_doctor_json_prints_the_snapshot_unchanged(tmp_path):
    raw = body(snapshot(canary=None))
    run = Run(tmp_path, {"/snapshot": raw})
    assert run("doctor", "--json") == 0
    assert run.stdout.getvalue() == raw + b"\n"
    failing = snapshot(store=None, errors=[{"section": "store", "error_type": "X"}])
    run = Run(tmp_path, {"/snapshot": body(failing)})
    assert run("doctor", "--json") == 1


def test_doctor_without_the_handoff_file(tmp_path):
    run = Run(tmp_path, {}, handoff=False)
    assert run("doctor") == 2
    message = run.stderr.getvalue()
    assert str(run.handoff) in message
    assert "hybro logs --service backend --level error" in message
    assert run.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        URLError(ConnectionRefusedError(111, "refused")),
        HTTPError(BASE_URL + "/snapshot", 503, "unavailable", None, None),  # type: ignore[arg-type]
        TimeoutError(),
    ],
)
def test_doctor_when_the_api_cannot_be_reached(tmp_path, failure):
    run = Run(tmp_path, {"/snapshot": failure})
    assert run("doctor") == 2
    message = run.stderr.getvalue()
    assert "cannot reach the diagnostics API" in message
    assert "hybro logs --service backend --level error" in message
    assert TOKEN not in message


def test_doctor_rejects_a_body_that_is_not_a_snapshot(tmp_path):
    run = Run(tmp_path, {"/snapshot": b"<html>"})
    assert run("doctor") == 2


def test_the_opener_ignores_proxy_variables(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("no_proxy", "")

    def proxies(opener) -> list[dict]:
        return [h.proxies for h in opener.handlers if isinstance(h, ProxyHandler)]

    # The default opener would route the loopback call through the proxy.
    assert any(proxies(urllib.request.build_opener()))
    assert not any(proxies(cli.build_opener()))


def test_usage_errors_exit_2(tmp_path):
    run = Run(tmp_path, {})
    assert run() == 2
    assert run("frobnicate") == 2
    assert run("doctor", "--bogus") == 2
    assert run("bundle", "--json") == 2
    assert run.requests == []


# ---- bundle ----


def record(index: int) -> dict:
    return {"id": str(index), "event": "x", "fields": {}}


def pages(total: int):
    """A route that serves ``total`` records newest first, paged by ``before``."""

    def route(query: dict[str, list[str]]) -> bytes:
        limit = int(query["limit"][0])
        start = int(query["before"][0]) if "before" in query else total
        items = [record(i) for i in range(start - 1, max(start - 1 - limit, -1), -1)]
        full = len(items) == limit
        next_before = items[-1]["id"] if full and int(items[-1]["id"]) > 0 else None
        return json.dumps({"items": items, "next_before": next_before}).encode()

    return route


def endless(query: dict[str, list[str]]) -> bytes:
    limit = int(query["limit"][0])
    start = int(query["before"][0]) if "before" in query else 10**6
    items = [record(i) for i in range(start - 1, start - 1 - limit, -1)]
    return json.dumps({"items": items, "next_before": items[-1]["id"]}).encode()


def all_routes() -> dict:
    return {
        "/snapshot": body(HEALTHY),
        "/events": endless,
        "/logs": pages(510),
        "/logs?source=memory": pages(3),
    }


def archive(run: Run) -> dict[str, bytes]:
    data = run.stdout.getvalue()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
        }


def jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines()]


def test_bundle_holds_every_file_and_pages_each_endpoint(tmp_path):
    run = Run(tmp_path, all_routes())
    assert run("bundle", stdin=b"backend | ready\n") == 0
    files = archive(run)
    assert list(files) == [
        "manifest.json",
        "snapshot.json",
        "system-events.jsonl",
        "logs.jsonl",
        "memory-logs.jsonl",
        "container.log",
    ]
    assert json.loads(files["snapshot.json"]) == validated(HEALTHY)
    events = jsonl(files["system-events.jsonl"])
    assert len(events) == 2000 and events[0]["id"] == str(10**6 - 1)
    assert len(jsonl(files["logs.jsonl"])) == 510
    assert len(jsonl(files["memory-logs.jsonl"])) == 3
    assert files["container.log"] == b"backend | ready\n"

    event_requests = [query for path, query in run.requests if path == "/events"]
    assert len(event_requests) == 4
    assert "before" not in event_requests[0]
    assert event_requests[1]["before"] == [str(10**6 - 500)]
    log_requests = [
        query
        for path, query in run.requests
        if path == "/logs" and "source" not in query
    ]
    assert len(log_requests) == 2

    manifest = json.loads(files["manifest.json"])
    assert manifest["format_version"] == 1
    assert manifest["hybro_version"] == "0.4.4"
    assert manifest["version_source"] == "snapshot"
    assert manifest["created_at"]
    assert manifest["files"] == list(files)
    assert manifest["missing"] == {}
    assert manifest["container_log"] == {"backend": {"kept": 1, "dropped": 0}}
    assert "review" in manifest["redaction"]


def test_one_failed_endpoint_leaves_the_others(tmp_path):
    routes = all_routes()
    routes["/logs"] = HTTPError(BASE_URL + "/logs", 503, "x", None, None)  # type: ignore[arg-type]
    run = Run(tmp_path, routes)
    assert run("bundle") == 0
    files = archive(run)
    assert "logs.jsonl" not in files
    assert {"snapshot.json", "system-events.jsonl", "memory-logs.jsonl"} <= set(files)
    manifest = json.loads(files["manifest.json"])
    assert manifest["missing"] == {"logs.jsonl": "HTTP 503"}


def test_every_fetch_failing_still_writes_the_logs(tmp_path):
    failure = URLError(ConnectionRefusedError(111, "refused"))
    routes = dict.fromkeys(all_routes(), failure)
    run = Run(tmp_path, routes)
    assert run("bundle", stdin=b"caddy | {}\n") == 0
    files = archive(run)
    assert list(files) == ["manifest.json", "container.log"]
    missing = json.loads(files["manifest.json"])["missing"]
    assert set(missing) == {
        "snapshot.json",
        "system-events.jsonl",
        "logs.jsonl",
        "memory-logs.jsonl",
    }


def test_bundle_without_the_handoff_file_keeps_the_logs(tmp_path):
    run = Run(tmp_path, all_routes(), handoff=False)
    assert run("bundle", stdin=b"backend | x\n") == 0
    files = archive(run)
    assert list(files) == ["manifest.json", "container.log"]
    assert run.requests == []


def test_offline_bundle_makes_no_request(tmp_path):
    run = Run(tmp_path, all_routes(), handoff=False)
    assert run("bundle", "--offline", stdin=b"backend | x\n") == 0
    files = archive(run)
    assert list(files) == ["manifest.json", "container.log"]
    manifest = json.loads(files["manifest.json"])
    assert set(manifest["missing"].values()) == {"offline"}
    assert manifest["hybro_version"]
    assert manifest["version_source"] == "helper image"
    assert run.requests == []


def test_bundle_redacts_the_container_log(tmp_path):
    backend_json = b'backend | {"event":"x","level":"info"}'
    caddy_json = b'caddy | {"level":"info"}'
    lines = [
        backend_json,
        b"backend | Traceback ... mongodb://user:hunter2@db/x",
        caddy_json,
        b"agent-echo | the user asked for SECRET-PROMPT",
        b'mongod | {"attr":{"command":"SECRET-DOC"}}',
        b"frontend | GET /",
        b"hybro: Invalid configuration: api_key=sk-PLANTED",
        b'{"msg":"x","attr":{"uri":"mongodb://root:tailpw@db"}}',
    ]
    run = Run(tmp_path, all_routes())
    assert run("bundle", stdin=b"\n".join(lines) + b"\n") == 0

    raw = gzip.decompress(run.stdout.getvalue())
    for secret in (
        b"hunter2",
        b"SECRET-PROMPT",
        b"SECRET-DOC",
        b"sk-PLANTED",
        b"tailpw",
    ):
        assert secret not in raw
    assert TOKEN.encode() not in raw
    files = archive(run)
    kept = files["container.log"].splitlines()
    assert kept[0] == backend_json
    assert kept[2] == caddy_json
    assert len(kept) == 5
    manifest = json.loads(files["manifest.json"])
    assert manifest["container_log"] == {
        "backend": {"kept": 2, "dropped": 0},
        "caddy": {"kept": 1, "dropped": 0},
        "agent-echo": {"kept": 0, "dropped": 1},
        "mongod": {"kept": 0, "dropped": 1},
        "frontend": {"kept": 0, "dropped": 1},
        "unprefixed": {"kept": 2, "dropped": 0},
    }


def test_paging_stops_at_an_empty_page(tmp_path):
    routes = all_routes()
    routes["/events"] = json.dumps({"items": [], "next_before": "5"}).encode()
    run = Run(tmp_path, routes)
    assert run("bundle") == 0
    assert archive(run)["system-events.jsonl"] == b""
    assert [path for path, _ in run.requests].count("/events") == 1


@pytest.mark.parametrize("command", ["doctor", "bundle"])
def test_an_unexpected_error_exits_2_without_its_message(
    tmp_path, monkeypatch, command
):
    def boom(**_: object) -> int:
        raise ValueError("SECRET-DETAIL")

    monkeypatch.setattr(cli, command, boom)
    run = Run(tmp_path, all_routes())
    assert run(command) == 2
    message = run.stderr.getvalue()
    assert "ValueError" in message
    assert "SECRET-DETAIL" not in message


def test_a_failing_backend_import_exits_2(tmp_path, monkeypatch):
    # A module set to None in sys.modules raises ImportError on import.
    monkeypatch.setitem(sys.modules, "common.observability", None)
    monkeypatch.setitem(sys.modules, "common.observability.logging", None)
    run = Run(tmp_path, all_routes())
    assert run("bundle", stdin=b"backend | x\n") == 2
    assert "ModuleNotFoundError" in run.stderr.getvalue()
    # doctor needs no backend module, so it still answers.
    assert Run(tmp_path, all_routes())("doctor") == 0
