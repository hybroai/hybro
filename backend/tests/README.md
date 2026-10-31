# Backend test suite

The suite is intentionally kept flat for now so existing capability-oriented file
names remain searchable. Do not reorganize files solely for directory aesthetics;
move them only when an owning module or shared test utility has a clear boundary.

## Test lanes

```bash
# Fast critical product-flow regression baseline
uv run pytest -m core

# Normal local suite; external-service tests are skipped unless configured
uv run pytest

# Real Redis integration contract
HYBRO_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration

# Functional suite against a disposable Hybro (opt-in; see below)
HYBRO_API_URL=http://localhost:3100/api/v1 uv run pytest tests/functional
```

CI runs the core baseline first, then the complete suite with a disposable Redis
service. A configured but unavailable Redis is a failure; an unconfigured Redis
integration lane is skipped locally.

## Markers

- `core`: critical user journeys and the narrow execution contracts protecting
  Direct A2A, Supervisor, Hub, HITL, cancellation, idempotency, and terminal SSE.
- `integration`: requires an explicitly configured external service.
- `functional`: calls a live Hybro and its agents. These tests are skipped
  unless `HYBRO_FORCE_FUNCTIONAL_TESTS=1` or `HYBRO_API_URL` is set; nothing
  probes `localhost:8000`, because that may be a developer's real instance with
  a paid model provider. Point them only at a disposable stack backed by the
  mock LLM, as the `functional-ci` lane does.
- `asyncio`: explicit async marker retained where older tests already use it;
  pytest-asyncio also runs in auto mode.

The `core` lane complements rather than replaces the complete suite. Add a test to
it only when failure means a principal local product journey is broken.

## Organization conventions

- `test_<capability>.py`: behavior and contract tests for one capability.
- `test_*_gate.py`, `test_*_boundaries.py`, and confinement tests: architecture
  rules. Preserve scanner self-tests; consolidate duplicate repository scans
  before deleting a gate.
- `fakes/`: reusable typed fakes shared by multiple test modules.
- `fixtures/`: static JSON or other data fixtures.
- `conftest.py`: broadly shared fixtures only. Prefer a local fixture or a narrow
  protocol fake when a fixture is used by one capability.

## Cleanup policy

Safe cleanup includes exact duplicate tests, stale generated files, unused
fixtures/imports, and phase names whose migration is complete. Do not delete a
legacy/phase gate merely because of its name: first identify the current
architecture invariant it enforces, move that invariant to its owning test, and
then remove the obsolete wrapper or manifest.
