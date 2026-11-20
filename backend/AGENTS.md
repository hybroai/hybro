# Backend Rules

These rules apply to `backend/` in addition to the repository-root `AGENTS.md`.

## Structure and Conventions

- This is the canonical Python 3.12 FastAPI backend.
- Keep HTTP routes in `api_gateway/` thin; put business behavior in existing domain, service, facade, repository, translator, or orchestration modules.
- Cross-module contracts live in `common/`; a DTO or Protocol used by one module lives in that module. Connection management and generic Mongo/Redis clients live in `dal/`, each module's persistence in its own `repository/` package, remote A2A protocol and transport adapters in `a2a_adapter/`, and tests in `tests/`.
- A2A response ingestion and finalization remain owned by `execution/`.
- Use explicit type hints and async I/O. Follow Ruff's configured 88-character line length and existing naming conventions.

## Configuration

- Follow the root JSON configuration contract: load `config.json` from `HYBRO_HOME` (default `~/.hybro`) through the thin shared loader and reuse its validated startup snapshot.
- Keep defaults and validation in code, user overrides in JSON, and credentials in the existing secure `auth.json` store. Do not introduce another `settings.py` or dotenv fallback.
- Reject malformed or unsupported configuration before service initialization, without echoing secrets or raw input values. Missing setup must direct the user to "open Settings, Model provider".
- Keep LLM Provider authentication and OAuth refresh inside the gateway's existing credential boundary; ordinary business modules must not load the full auth file.

## Commands

Run from `backend/`:

```bash
uv sync --extra dev
uv run pytest
uv run ruff format --check .
uv run ruff check .
```

Run focused tests for the changed path before broader suites. `tests/functional/` is opt-in (`HYBRO_FORCE_FUNCTIONAL_TESTS=1` or `HYBRO_API_URL`); never point it at a real instance or provider. If formatting fails, run `uv run ruff format .`, then repeat both Ruff gates.

When adding, removing, or renaming a public method on a `Protocol`, repository port, facade port, or compatibility interface, update the relevant exact method inventory in `tests/test_common_foundation.py` and run:

```bash
uv run pytest tests/test_common_foundation.py::test_protocol_methods_match_design_doc
```

For API contract changes, update affected route/schema tests and the tracked `openapi.json` snapshot. Review `docs/System-Architecture.md` for architecture-impacting changes and `tests/README.md` for test lanes and cleanup conventions.

## Code Review Rules

These add to the root `AGENTS.md` Code Review Rules and use its severities.

- A public method is added, removed, or renamed on a `Protocol`, repository port, facade port, or compatibility interface. Check that the exact inventory in `tests/test_common_foundation.py` changes with it. Must fix.
- An `HTTPException` detail or error response built from `str(exc)`, `exc.message`, or an f-string of an exception. Check that the client gets fixed text and the exception goes to a log event. Must fix.
- An `except` that returns `None`, an empty result, or a default; a `pass`; or a missing service on `app.state` treated as "nothing there". Check that a failed admission or ownership lookup refuses the request (often 503), that a missing required service fails startup or returns 503, and that every swallowed error emits a log event. Must fix.
- A send, cancel, or HITL answer path that writes a message or record and then starts a Run, or reads state and then writes it (registration takeover, Run claims, leases, session request lists). Check that a failure between the two steps leaves a terminal state the client sees, and that the write is conditional on the state it read. Must fix.
- A new outbound HTTP fetch of a URL that a user or an Agent Card supplied (cards, redirects, artifacts, health probes, inspection). Check that it uses the bounded A2A client and the per-call destination policy, caps the body size, has an overall deadline, and sits behind the signed-in user dependency. Must fix.
- A `find` or `aggregate` without a limit on a growing collection, a read that takes only the first page (for example the first 1000 events or 100 sessions), or a new silent default cap. Check that the caller either bounds the read on purpose or pages to the end. Should fix.
- A change to how A2A stream frames become reply text. Check that an answer delivered only in artifact updates survives a final status with no message, and that a status message repeating the artifact text is not added twice. Should fix.
- A change that reads or counts on the legacy path (the legacy runs mirror, `A2AService` send paths, legacy turn traces). Check that canonical orchestrator Runs get the same behavior. Should fix.
