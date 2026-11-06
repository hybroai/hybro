"""The container side of ``hybro doctor``; run by ``scripts/hybro``::

    docker exec <container> /app/backend/.venv/bin/python \\
        /app/backend/diagnostics_cli.py doctor [--json]
    docker logs --tail 2000 <container> 2>&1 | docker exec -i <container> \\
        /app/backend/.venv/bin/python /app/backend/diagnostics_cli.py bundle
    ... | docker run --rm -i --entrypoint /app/backend/.venv/bin/python <image> \\
        /app/backend/diagnostics_cli.py bundle --offline

``doctor`` reads the backend's diagnostics snapshot over loopback with the
per-start token of the handoff file and prints one line per section, then the
problems. Exit status: 0 no problems, 1 problems, 2 the check could not run.

``bundle`` reads container log lines on stdin and writes a gzipped tar to
stdout: the snapshot, system events, stored and in-memory logs, and the
container log with only the backend, MCP, Caddy and unprefixed lines kept.
Only ``bundle`` imports backend code (``common.observability``), inside
``main``'s error handling. It reads neither ``config.json`` nor ``auth.json``, and the token is written
to no file of the bundle.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import urlencode

HANDOFF = Path("/tmp/hybro/diagnostics.json")
TOKEN_HEADER = "X-Hybro-Diagnostics-Token"
TIMEOUT_SECONDS = 10.0
LOG_HINT = "Check the backend's errors with: hybro logs --service backend --level error"

BUNDLE_FORMAT = 1
BUNDLE_RECORDS = 2000
PAGE_LIMIT = 500
LINE_LIMIT = 64 * 1024
KEPT_PROGRAMS = frozenset({"backend", "mcp", "caddy"})
DROPPED_PROGRAMS = frozenset({"frontend", "mongod"})
UNPREFIXED = "unprefixed"
REDACTION = (
    "snapshot.json holds counts, states, ids, timestamps and enumerated names; "
    "agent names are sanitised. The .jsonl records were redacted when they were "
    "logged. container.log keeps the backend, mcp, caddy and unprefixed lines and "
    "drops the frontend, mongod and agent lines; a kept line that is not JSON is "
    "redacted by pattern only, so review the archive before sharing it."
)
# Archive name, endpoint path and query of each fetched file, in archive order.
RECORD_FILES = (
    ("system-events.jsonl", "/events", {}),
    ("logs.jsonl", "/logs", {}),
    ("memory-logs.jsonl", "/logs", {"source": "memory"}),
)

Fetch = Callable[[str, str], bytes]
Snapshot = Mapping[str, Any]


class Unavailable(Exception):
    """The diagnostics API cannot be used; the message is safe to print."""


# ── Transport ──


def build_opener() -> urllib.request.OpenerDirector:
    """An opener without proxies: the loopback call and its token stay local."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers={TOKEN_HEADER: token})
    with build_opener().open(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def read_handoff(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable(
            f"the diagnostics handoff file {path} is missing or unreadable, "
            "so the backend has not started"
        ) from exc
    base_url = data.get("base_url") if isinstance(data, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if not (isinstance(base_url, str) and isinstance(token, str) and token):
        raise Unavailable(f"the diagnostics handoff file {path} is incomplete")
    return base_url, token


def failure_reason(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


class Api:
    def __init__(self, base_url: str, token: str, fetch: Fetch) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._fetch = fetch

    def get(self, path: str, query: Mapping[str, object] | None = None) -> bytes:
        url = self._base_url + path
        if query:
            url += "?" + urlencode(query)
        return self._fetch(url, self._token)

    def get_json(self, path: str, query: Mapping[str, object] | None = None) -> Any:
        return json.loads(self.get(path, query))

    def records(self, path: str, query: Mapping[str, object]) -> list[Any]:
        """Up to ``BUNDLE_RECORDS`` records, newest first, paged by ``before``."""
        records: list[Any] = []
        before: str | None = None
        while len(records) < BUNDLE_RECORDS:
            params = {**query, "limit": min(PAGE_LIMIT, BUNDLE_RECORDS - len(records))}
            if before is not None:
                params["before"] = before
            page = self.get_json(path, params)
            items = page["items"]
            records.extend(items)
            before = page["next_before"]
            # An empty page ends the walk even with a cursor, so it cannot spin.
            if before is None or not items:
                break
        return records


# ── Rendering ──


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def duration(seconds: float) -> str:
    """``45s``, ``1m 5s``, ``3h 12m`` or ``2d 4h``: two units at most."""
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        major, minor, units = total // 60, total % 60, ("m", "s")
    elif total < 86400:
        major, minor, units = total // 3600, total % 3600 // 60, ("h", "m")
    else:
        major, minor, units = total // 86400, total % 86400 // 3600, ("d", "h")
    return f"{major}{units[0]}" + (f" {minor}{units[1]}" if minor else "")


def plural(count: object, singular: str, plural_form: str | None = None) -> str:
    if count is None:
        return f"unknown {plural_form or singular + 's'}"
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count} {word}"


def _age(snapshot: Snapshot, value: object) -> str | None:
    now = _parse_time(snapshot.get("generated_at"))
    then = _parse_time(value)
    if now is None or then is None:
        return None
    return duration((now - then).total_seconds()) + " ago"


# The field holding when each outcome was last recorded; "ok" uses last_completed_at.
_OUTCOME_STAMP = {
    "failed": "last_failed_at",
    "skipped_not_leader": "last_skipped_at",
}


def _number(value: float) -> str:
    return f"{value:g}"


class _Report:
    """Section lines and problems, built in one pass over the snapshot."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.lines: list[str] = []
        self.problems: list[str] = []
        errors = snapshot.get("errors") or []
        self.errors = {
            str(error.get("section")): str(error.get("error_type"))
            for error in errors
            if isinstance(error, Mapping)
        }
        self.reported: set[str] = set()

    def line(self, label: str, text: str) -> None:
        self.lines.append(f"{label:<13}{text}")

    def section(self, name: str, labels: Iterable[str]) -> Any:
        """The section, or None after printing why it is absent."""
        value = self.snapshot.get(name)
        if value is not None:
            return value
        error_type = self.errors.get(name)
        text = "not available" if error_type is None else f"unavailable ({error_type})"
        for label in labels:
            self.line(label, text)
        if error_type is not None and name not in self.reported:
            self.reported.add(name)
            self.problems.append(f"{name}: unavailable ({error_type})")
        return None

    def build(self) -> None:
        service = self.snapshot.get("service") or {}
        self.lines.append(
            f"Hybro {service.get('version')}, instance {service.get('instance_id')}, "
            f"up {duration(float(service.get('uptime_seconds') or 0))}"
        )
        self.provider()
        self.health()
        self.agents()
        self.jobs()
        self.leader()
        self.eventing()
        self.sse()
        self.recovery()
        self.canary()
        self.store()
        # An error of a section that still has a value is a problem as well.
        for name, error_type in sorted(self.errors.items()):
            if name not in self.reported:
                self.reported.add(name)
                self.problems.append(f"{name}: unavailable ({error_type})")

    def provider(self) -> None:
        provider = self.section("provider", ["Provider"])
        if provider is None:
            return
        state = str(provider.get("state"))
        text = "not configured" if state == "unconfigured" else state.replace("_", " ")
        self.line("Provider", text)
        if state != "configured":
            self.problems.append(f"provider: {text}")

    def health(self) -> None:
        health = self.section("health", ["MongoDB", "Redis"])
        if health is None:
            return
        if health.get("mongo_connected"):
            self.line("MongoDB", "connected")
        else:
            self.line("MongoDB", "not connected")
            self.problems.append("mongodb: not connected")
        if not health.get("redis_expected"):
            self.line("Redis", "not configured")
            return
        down = [
            flag
            for flag in (
                "delivery_pubsub",
                "delivery_kv",
                "eventing",
                "redis_runtime",
            )
            if not health.get(f"{flag}_connected")
        ]
        if down:
            text = f"not connected ({', '.join(down)})"
            self.line("Redis", text)
            self.problems.append(f"redis: {text}")
        else:
            self.line("Redis", "connected")

    def agents(self) -> None:
        agents = self.section("agents", ["Agents"])
        if agents is not None:
            self.line(
                "Agents",
                f"{agents.get('active')} active, {agents.get('inactive')} inactive",
            )

    def jobs(self) -> None:
        jobs = self.section("jobs", ["Jobs"])
        if jobs is None:
            return
        running = sum(1 for job in jobs if job.get("running"))
        failing = [job for job in jobs if job.get("last_outcome") == "failed"]
        self.line("Jobs", f"{running} running, {len(failing)} failing")
        for job in jobs:
            outcome = job.get("last_outcome")
            stamp = job.get(_OUTCOME_STAMP.get(outcome, "last_completed_at"))
            age = _age(self.snapshot, stamp) or "never"
            interval = job.get("interval_seconds")
            every = "" if interval is None else f"every {duration(float(interval))}"
            self.lines.append(
                f"  {job.get('job')!s:<24} {outcome or 'none':<8} {age:<9} {every}".rstrip()
            )
        for job in failing:
            error_type = job.get("last_error_type")
            cause = f" ({error_type})" if error_type else ""
            self.problems.append(f"job {job.get('job')}: last run failed{cause}")

    def leader(self) -> None:
        leader = self.section("leader", ["Leader"])
        if leader is None:
            return
        if leader.get("enabled"):
            self.line(
                "Leader", f"elected among workers, instance {leader.get('instance_id')}"
            )
        else:
            self.line("Leader", "not used (single process)")

    def eventing(self) -> None:
        eventing = self.section("eventing", ["Event bus"])
        if eventing is None:
            return
        count = eventing.get("dead_letter_count") or 0
        connected = "" if eventing.get("connected") else ", not connected"
        self.line(
            "Event bus",
            f"{eventing.get('transport')}{connected}, {plural(count, 'dead letter')}",
        )
        if count > 0:
            self.problems.append(f"event bus: {plural(count, 'dead letter')}")

    def sse(self) -> None:
        sse = self.section("sse", ["SSE"])
        if sse is not None:
            self.line(
                "SSE",
                f"{plural(sse.get('connections'), 'connection')} in "
                f"{plural(sse.get('rooms'), 'room')}",
            )

    def recovery(self) -> None:
        recovery = self.section("recovery", ["Recovery"])
        if recovery is None:
            return
        age = _age(self.snapshot, recovery.get("last_cycle_completed_at"))
        parts = [
            f"last cycle {age}" if age else "no cycle yet",
            plural(recovery.get("non_terminal_runs"), "open run"),
            plural(recovery.get("agent_calls_outstanding"), "outstanding agent call"),
            plural(recovery.get("projection_outbox_blocked"), "blocked projection"),
        ]
        phases = list(recovery.get("last_cycle_failed_phases") or [])
        if phases:
            parts.append(f"failed phases: {', '.join(phases)}")
            self.problems.append(f"recovery: failed phases: {', '.join(phases)}")
        self.line("Recovery", ", ".join(parts))
        blocked = recovery.get("projection_outbox_blocked")
        if blocked:
            self.problems.append(f"recovery: {plural(blocked, 'blocked projection')}")

    def canary(self) -> None:
        canary = self.section("canary", ["Canary"])
        if canary is None:
            return
        breaches = canary.get("breaches") or []
        job = "on" if canary.get("job_enabled") else "off"
        self.line(
            "Canary",
            f"job {job}, {plural(len(breaches), 'breach', 'breaches')}",
        )
        for breach in breaches:
            self.problems.append(
                f"canary: {breach.get('metric')} at "
                f"{_number(float(breach.get('observed', 0)))} "
                f"(threshold {_number(float(breach.get('threshold', 0)))})"
            )

    def store(self) -> None:
        store = self.section("store", ["Diagnostics"])
        if store is None:
            return
        failures = store.get("write_failures") or 0
        ready = "ready" if store.get("ready") else "not ready"
        self.line(
            "Diagnostics",
            f"store {ready}, {store.get('dropped')} dropped, "
            f"{plural(failures, 'write failure')}",
        )
        if not store.get("ready"):
            self.problems.append("diagnostics store: not ready")
        if failures > 0:
            error_type = store.get("last_error_type")
            cause = f" ({error_type})" if error_type else ""
            self.problems.append(
                f"diagnostics store: {plural(failures, 'write failure')}{cause}"
            )


def _report(snapshot: Snapshot) -> _Report:
    report = _Report(snapshot)
    report.build()
    return report


def problems(snapshot: Snapshot) -> list[str]:
    return _report(snapshot).problems


def render(snapshot: Snapshot) -> str:
    report = _report(snapshot)
    lines = list(report.lines)
    if report.problems:
        lines.append("Problems:")
        lines.extend(f"  {problem}" for problem in report.problems)
    else:
        lines.append("Problems: none")
    return "\n".join(lines)


# ── Commands ──


def doctor(
    *,
    json_output: bool,
    handoff: Path,
    fetch: Fetch,
    stdout: IO[bytes],
    stderr: IO[str],
) -> int:
    try:
        base_url, token = read_handoff(handoff)
        try:
            body = Api(base_url, token, fetch).get("/snapshot")
        except Exception as exc:
            raise Unavailable(
                f"cannot reach the diagnostics API ({failure_reason(exc)})"
            ) from exc
        try:
            snapshot = json.loads(body)
        except ValueError as exc:
            raise Unavailable("the diagnostics API returned no JSON") from exc
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("service"), dict
        ):
            raise Unavailable("the diagnostics API returned no snapshot")
        report = _report(snapshot)
    except Unavailable as exc:
        stderr.write(f"hybro doctor: {exc}. {LOG_HINT}\n")
        return 2
    text = body.decode("utf-8") if json_output else render(snapshot)
    stdout.write(text.encode("utf-8") + b"\n")
    stdout.flush()
    return 1 if report.problems else 0


def filter_container_log(
    lines: Iterable[str],
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """Kept lines and per-program counts of kept and dropped lines."""
    # Imported here, inside main's error handling: a broken backend import
    # exits 2 instead of failing with a traceback, and doctor never needs it.
    from common.observability import sanitize_text

    kept: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for line in lines:
        program, separator, body = line.partition(" | ")
        if not separator or not (
            program in KEPT_PROGRAMS
            or program in DROPPED_PROGRAMS
            or program.startswith("agent-")
        ):
            program = UNPREFIXED
        count = counts.setdefault(program, {"kept": 0, "dropped": 0})
        if program in DROPPED_PROGRAMS or program.startswith("agent-"):
            count["dropped"] += 1
            continue
        count["kept"] += 1
        if program == UNPREFIXED:
            kept.append(sanitize_text(line, limit=LINE_LIMIT))
        elif _is_json_object(body):
            kept.append(line)
        else:
            kept.append(f"{program} | {sanitize_text(body, limit=LINE_LIMIT)}")
    return kept, counts


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, RecursionError):
        return False


def _jsonl(records: Iterable[Any]) -> bytes:
    return b"".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        for record in records
    )


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mtime = int(mtime)
    member.mode = 0o644
    tar.addfile(member, io.BytesIO(data))


def bundle(
    *,
    offline: bool,
    handoff: Path,
    fetch: Fetch,
    stdin: IO[bytes],
    stdout: IO[bytes],
) -> int:
    from common.observability.logging import release_version

    files: dict[str, bytes] = {}
    missing: dict[str, str] = {}
    names = ["snapshot.json", *(name for name, _, _ in RECORD_FILES)]
    version: str | None = None
    if offline:
        missing = dict.fromkeys(names, "offline")
    else:
        try:
            api: Api | None = Api(*read_handoff(handoff), fetch)
        except Unavailable:
            api = None
            missing = dict.fromkeys(names, "handoff file missing")
        if api is not None:
            try:
                snapshot = api.get_json("/snapshot")
                files["snapshot.json"] = (
                    json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                version = (snapshot.get("service") or {}).get("version")
            except Exception as exc:
                missing["snapshot.json"] = failure_reason(exc)
            for name, path, query in RECORD_FILES:
                try:
                    files[name] = _jsonl(api.records(path, query))
                except Exception as exc:
                    missing[name] = failure_reason(exc)

    text = stdin.read().decode("utf-8", errors="replace")
    # Split on newlines only: str.splitlines would also split inside a JSON line.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    kept, counts = filter_container_log(lines)
    files["container.log"] = "".join(f"{line}\n" for line in kept).encode("utf-8")

    order = ["manifest.json", *names, "container.log"]
    present = [name for name in order if name in files or name == "manifest.json"]
    now = time.time()
    manifest = {
        "format_version": BUNDLE_FORMAT,
        "hybro_version": version or release_version(Path(__file__).resolve()),
        # Without a snapshot the version is the image this helper runs in,
        # which for an offline bundle can differ from the container's.
        "version_source": "snapshot" if version else "helper image",
        "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "files": present,
        "missing": missing,
        "records_order": "newest first",
        "container_log": counts,
        "redaction": REDACTION,
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    with tarfile.open(fileobj=stdout, mode="w|gz") as tar:
        for name in present:
            _add(tar, name, files[name], now)
    stdout.flush()
    return 0


USAGE = "usage: diagnostics_cli.py doctor [--json] | bundle [--offline]\n"


def main(
    argv: list[str],
    *,
    stdin: IO[bytes],
    stdout: IO[bytes],
    stderr: IO[str],
    handoff: Path = HANDOFF,
    fetch: Fetch = http_get,
) -> int:
    try:
        return _dispatch(
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            handoff=handoff,
            fetch=fetch,
        )
    except Exception as exc:
        # The type only: an exception message or traceback can carry data.
        stderr.write(f"hybro doctor: unexpected {type(exc).__name__}\n")
        return 2


def _dispatch(
    argv: list[str],
    *,
    stdin: IO[bytes],
    stdout: IO[bytes],
    stderr: IO[str],
    handoff: Path,
    fetch: Fetch,
) -> int:
    command, options = (argv[0], argv[1:]) if argv else ("", [])
    if command == "doctor" and options in ([], ["--json"]):
        return doctor(
            json_output=bool(options),
            handoff=handoff,
            fetch=fetch,
            stdout=stdout,
            stderr=stderr,
        )
    if command == "bundle" and options in ([], ["--offline"]):
        return bundle(
            offline=bool(options),
            handoff=handoff,
            fetch=fetch,
            stdin=stdin,
            stdout=stdout,
        )
    stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1:],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr,
        )
    )
