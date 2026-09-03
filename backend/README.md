# Hybro Backend

This directory is the canonical backend for the current Hybro repository and is
the backend used by the application, Docker Compose configuration, and CI.

## Run from the monorepo

### Docker Compose

From the repository root:

```sh
docker compose up -d --build
```

The API is available at <http://localhost:8000> and its health endpoint is
<http://localhost:8000/health>.

### Run the backend directly

Python 3.12+ and MongoDB 4.2+ are required. Terminal projection scheduling uses
MongoDB aggregation update pipelines; Docker Compose currently provides MongoDB
7.0.

```sh
cd backend
# From the monorepo root: cp .env.example .env  (if not already created)
uv sync --extra dev
uv run uvicorn main:app --reload
```

Use `AUTH_MODE=mock` for local development without Clerk credentials. Redis is
optional for a single-process local server; cross-process delivery and locking
require it.

### Production upgrade note

Releases that introduce terminal task writer fencing must not be deployed with a
rolling mixed-version writer fleet. Coordinately drain or stop all old backend
writers, deploy the new version to every replica, and only then resume traffic.
An older writer does not apply the MongoDB terminal winner fences and can undo the
new version's guarantees.

## Project layout

- `main.py`: FastAPI application and lifespan entry point.
- `container.py`: runtime composition root.
- `api_gateway/`: the only HTTP route package.
- `agent/`, `room/`, `execution/`, `context_memory/`, `delivery/`: domain modules.
- `a2a_adapter/`: A2A SDK boundary.
- `llm_gateway/`: LLM provider boundary.
- `local_agents/`: Docker-host A2A Agent discovery and lifecycle coordination.
- `dal/`: MongoDB and Redis adapters.
- `room_files/`: room-owned file metadata and local content storage.
- `tests/`: unit, boundary, and workflow tests.

The former `api/` compatibility package has been removed. New routes belong in
`api_gateway/routes/` and must use injected owner protocols rather than concrete
repositories.

See [`docs/System-Architecture.md`](docs/System-Architecture.md) for the current
runtime architecture.

## Validation

Run from `backend/`:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pytest -m core
uv run pytest
```

Real Redis integration tests are explicit:

```sh
HYBRO_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration
```

See [`tests/README.md`](tests/README.md) for test lanes and cleanup conventions.

## A2A inline file limits

`A2A_INLINE_FILE_MAX_RAW_BYTES` limits one uploaded file before base64 encoding.
`A2A_INLINE_MESSAGE_MAX_ENCODED_BYTES` limits aggregate encoded file bytes in an
outbound A2A message. Uploaded files sent to agents use inline A2A bytes; local
filesystem paths and authenticated room-file URLs remain internal to Hybro.
