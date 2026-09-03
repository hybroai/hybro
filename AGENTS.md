# Development Rules

## Conversational Style

- Keep answers short and concise.
- No emojis in commits, issues, PR comments, or code.
- Use direct technical prose without cheerful filler.
- Prefer clear, simple language. Define unavoidable jargon.
- Explain non-trivial work as: problem, concrete example or short trace, then solution. Distinguish required work from optional complexity.
- When the user asks a question, answer it before making edits or running implementation commands.
- When responding to feedback or analysis, explicitly say whether you agree or disagree before describing changes.

## Repository Scope

These rules apply to the entire `hybro` repository. More specific rules in `frontend/AGENTS.md` and `backend/AGENTS.md` apply within those directories.

The product flow is:

```text
frontend/ -> backend/ -> A2A agents, Hub relay, and external services
```

- `frontend/`: Next.js 16, React 19, and TypeScript UI.
- `backend/`: Python 3.12 FastAPI platform backend.
- `default_agents/`: bundled A2A agents, registration, and Compose generation.
- `scripts/`: lifecycle and functional-test helpers.
- `docker-compose.yml`: local product stack.

Do not inspect or treat paths outside this repository as product source unless the task explicitly requires it.

## Code Quality

- Read files in full before wide-ranging changes, before editing files not yet fully inspected, and when asked to investigate or audit. Do not base broad changes on search snippets alone.
- Reuse existing services, repositories, adapters, components, hooks, stores, and UI primitives before adding abstractions.
- Keep changes focused on requested behavior. Do not refactor unrelated code opportunistically.
- Prefer the simplest design that satisfies the current requirements. Keep changes minimal and focused; do not add speculative abstractions, fallback paths, or compatibility layers.
- Add defensive handling at untrusted or failure-prone boundaries, such as user input, network calls, persistence, concurrency, and third-party APIs. Do not add redundant guards for states already excluded by validated internal contracts.
- Always ask before removing functionality or code that appears intentional.
- Do not add backward-compatibility layers unless required by an existing public contract or explicitly requested.
- Do not guess third-party APIs. Inspect installed package types, local framework documentation, or official upstream documentation.
- Avoid TypeScript `any`; use it only when the boundary cannot be typed more precisely and document why.
- Use typed, async-aware Python for I/O paths. Keep HTTP route adapters thin and business behavior in the existing domain/service layers.
- Add or update focused tests for behavior changes. Mock network, LLM, database, wallet, and webhook calls unless explicitly testing integration behavior.
- Treat explicitly marked generated regions as outputs. Update their source and regenerate them instead of editing them directly.
- For review-only requests, do not edit files.

## Architecture Contracts

- Keep frontend API types and behavior aligned with backend request/response contracts.
- For room, SSE, streaming, HITL, processing-status, message-sending, or agent-response changes, preserve `client_request_id` correlation across frontend and backend.
- A2A response ingestion and finalization are owned by backend Execution. Do not introduce a second response owner in Room or transport adapters.
- `default_agents/agents.yaml` is the source of truth for bundled agents. It drives registration and the generated default-agent region in `docker-compose.yml`.
- Do not edit the generated Compose region directly. After changing the agent manifest, run:

```bash
uv run --with pyyaml python default_agents/render_compose.py
uv run --with pyyaml python default_agents/render_compose.py --check
```

- When changing a public backend `Protocol`, repository port, facade port, or compatibility interface, update exact method inventories and run the contract test documented in `backend/AGENTS.md`.
- For API contract changes, update route/schema tests, frontend consumers, and the tracked `backend/openapi.json` snapshot together.

## Commands

Run commands from the directory that owns the relevant configuration.

- Documentation-only changes: run `git diff --check`.
- Frontend changes: follow `frontend/AGENTS.md` and run the affected lint, test, and build checks.
- Backend changes: follow `backend/AGENTS.md` and run both Ruff gates plus focused Pytest coverage.
- Default-agent changes: run the renderer check and relevant tests.
- Cross-service, Compose, container, environment, or end-to-end changes: from the repository root, run `docker compose up -d --build` and exercise the affected flow when the environment is available.
- If a test file is created or modified, run that test and iterate until it passes.
- Do not use real provider APIs, keys, paid tokens, or external services unless the user explicitly requests live integration testing.
- Do not claim a check passed unless it completed successfully. Report skipped checks and why.
- Never commit unless the user asks.

## Dependency and Configuration Security

- Treat dependency manifests and lockfiles as reviewed code. Keep the corresponding lockfile in sync with dependency changes.
- Before adding or upgrading a dependency, inspect its release notes, compatibility impact, and lifecycle scripts when relevant.
- Use `npm ci` for clean frontend installs and CI parity. Use `npm install` when intentionally updating frontend dependencies or the lockfile.
- Use `uv sync --frozen --extra dev` for backend CI parity. Use `uv sync --extra dev` when intentionally updating backend dependencies or `uv.lock`.
- Never commit secrets or print them in logs, fixtures, screenshots, or command output.
- The repository-root `.env`, created from `.env.example`, is the single source of truth for local runtime configuration.
- `frontend/.env.local` is generated from the root `.env` by `sh backend/scripts/ensure_frontend_env.sh .env frontend/.env.local`; do not hand-edit it.
- Do not broaden environment-variable, secret, workflow-permission, or container exposure without an explicit security reason.

## Git

Multiple users or agents may modify this worktree concurrently. Git operations must not disturb changes outside the current task.

Committing:

- Commit only files changed for the current task.
- Stage explicit paths; never use `git add -A` or `git add .`.
- Before committing, inspect `git status` and the staged diff.
- Use concise imperative Conventional Commit subjects such as `feat:`, `fix:`, `test:`, `docs:`, `chore:`, or a scoped variant.

Never run commands that can destroy unrelated work or bypass checks:

- `git reset --hard`
- `git checkout .`
- `git clean -fd`
- `git stash`
- `git add -A` or `git add .`
- `git commit --no-verify`
- force push

If a rebase or merge conflicts in a file you did not modify, stop and ask the user rather than resolving it speculatively.

## Issues and Pull Requests

See `CONTRIBUTING.md` and the templates under `.github/`.

- When reviewing a PR, do not switch the worktree to the PR branch unless explicitly requested. Prefer `gh pr view`, `gh pr diff`, `gh api`, and `git show`.
- Do not post, reply to, resolve, react to, or edit GitHub review comments unless the user separately asks for that exact action.
- Pull requests should summarize behavior changes, tests run, documentation or migration impact, and unresolved risks.
- Include screenshots or recordings for visible UI changes and sample requests/responses for API or agent-contract changes when useful.
- When a commit should close an issue, use `fixes #<number>` or `closes #<number>`. Repeat the keyword for multiple issues.

## Documentation

- Assess documentation impact after every code change.
- Update `frontend/docs/System-Architecture.md` or `backend/docs/System-Architecture.md` when routes, APIs, schemas, data flow, streaming, persistence, module boundaries, authentication, deployment, or major workflows change.
- Document current behavior, not the implementation journey.
- If no documentation update is needed, say so in the handoff.
- Do not create or commit tool-specific planning artifacts unless explicitly requested. Never remove pre-existing files merely to clean the worktree.

## Changelog and Releases

Hybro uses Release Please with root lockstep versioning.

- Do not manually edit released sections of `CHANGELOG.md`.
- Do not manually bump `VERSION`, `.release-please-manifest.json`, `backend/pyproject.toml`, `backend/uv.lock`, `frontend/package.json`, or `frontend/package-lock.json` unless performing an explicitly requested release task.
- Release Please owns synchronized version updates through `release-please-config.json` and `.github/workflows/release-please.yml`.
- Before shipping a release-related change, review all generated version and lockfile diffs together.

## User Override

If a user instruction conflicts with this document, explain the conflict and ask for explicit confirmation before overriding it.
