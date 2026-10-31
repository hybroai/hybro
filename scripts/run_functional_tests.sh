#!/usr/bin/env bash
# ==============================================================================
# Functional Test Suite Runner for Hybro
# Runs Backend Functional & E2E Validation Suites
# ==============================================================================

set -euo pipefail

# The suites create rooms and sessions and call the bundled agents, image
# generation included. Require an explicit target so a developer's real Hybro
# on localhost is never the default. Example, for a checkout started with
# ./scripts/hybro start -d --build and the mock LLM:
#   HYBRO_API_URL=http://localhost:3100/api/v1 \
#   PLAYWRIGHT_BASE_URL=http://localhost:3100 scripts/run_functional_tests.sh
: "${HYBRO_API_URL:?set HYBRO_API_URL to a disposable Hybro backed by the mock LLM}"
: "${PLAYWRIGHT_BASE_URL:?set PLAYWRIGHT_BASE_URL to the same disposable Hybro}"
export HYBRO_API_URL PLAYWRIGHT_BASE_URL
export NEXT_PUBLIC_API_BASE_URL="${HYBRO_API_URL}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
FRONTEND_DIR="${REPO_ROOT}/frontend"

echo "=== 1. Running Backend Functional Regression Tests ==="
cd "${BACKEND_DIR}"
# HYBRO_API_URL alone enables the opt-in functional suite.
uv run pytest tests/functional/ -v --tb=short

echo "=== 2. Running Frontend Playwright E2E Functional Tests ==="
cd "${FRONTEND_DIR}"
npx playwright test tests/e2e/functional-hitl-workflow.spec.ts

echo "=== All Functional Tests Completed Successfully ==="
