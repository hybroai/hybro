#!/usr/bin/env bash
# Offline defaults only. Live A2A/Provider checks require a separately scoped run.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-${XDG_DATA_HOME:-$HOME/.local/share}/hybro/venv}"
export PYTHONPATH="$PWD/default_agents${PYTHONPATH:+:$PYTHONPATH}"
exec uv run --project backend --frozen --no-env-file python -m pytest --noconftest \
    default_agents/tests/test_manifest_in_sync.py \
    default_agents/tests/test_load_repo_env.py "$@"
