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
- `default_agents/`: bundled A2A agents and their manifest.
- `scripts/`: the `hybro` CLI (POSIX shell), its tests, and the image, migration, and functional-test helpers.
- `install.sh`: installs the released `hybro` script.
- `Dockerfile`: the single product image (UI, API, MCP, embedded MongoDB, and bundled agents behind Caddy on one port).

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
- `default_agents/agents.yaml` is the source of truth for bundled agents. It drives boot registration and the image's agent processes. Keep it, the agent uv workspace, and the root `Dockerfile` in sync; `default_agents/tests/test_manifest_in_sync.py` checks this.

- When changing a public backend `Protocol`, repository port, facade port, or compatibility interface, update exact method inventories and run the contract test documented in `backend/AGENTS.md`.
- For API contract changes, update route/schema tests, frontend consumers, and the tracked `backend/openapi.json` snapshot together.

## Commands

Run commands from the directory that owns the relevant configuration.

- Documentation-only changes: run `git diff --check`.
- Frontend changes: follow `frontend/AGENTS.md` and run the affected lint, test, and build checks.
- Backend changes: follow `backend/AGENTS.md` and run both Ruff gates plus focused Pytest coverage.
- Default-agent changes: run `default_agents/tests/test_manifest_in_sync.py` and the relevant agent tests.
- Cross-service, image, container, configuration, or end-to-end changes: from the repository root, build and smoke-test the image with `./scripts/hybro start --build` (dev mode: image `hybro:dev`, container `hybro-dev`, port 3100), then exercise the affected flow when the environment is available. CLI changes also run `scripts/test_hybro_cli.py` and `shellcheck -s sh scripts/hybro install.sh`.
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
- The configuration contract is JSON-based: `~/.hybro/config.json` is the single user configuration source for backend, frontend, and bundled agents. `HYBRO_HOME` is the only configuration-directory override; require an absolute path.
- Keep secrets in the sibling `auth.json`, not in public frontend configuration. Reuse the existing secure credential store and OAuth refresh ownership; do not create a second credential system.
- `.env`, `.env.example`, and `frontend/.env.local` are not configuration sources. Do not add dotenv loading, template-copy steps, or implicit legacy fallback. The owner will remove existing environment files manually; do not delete or overwrite them on the owner's behalf.
- Keep defaults, types, and validation in a thin configuration loader. Do not preserve a separate `settings.py` environment-loading path. Business code consumes the validated configuration object, not ad hoc file or environment reads.
- The Settings page and manually edited JSON must use the same validation rules. Validate JSON syntax, supported fields, types, ranges, and required combinations at startup; report safe field-level errors.
- The model provider is optional. Without one, Hybro starts, the provider-free agents work, and the UI directs the user to Settings, Model provider; never select a legacy Provider implicitly.
- Load a startup snapshot; other changes take effect after restart. The one exception is the provider settings saved from Settings, which reload live in the backend and the bundled agents. Do not add other hot reload, project-level overrides, configuration APIs, or a separate configuration-service process without a request.
- In the image, the entrypoint (`backend/runtime_entrypoint.py`) owns the projections: it reads `config.json` and gives the frontend, MCP, and each agent only its scoped settings and internal credential, never the whole auth store.
- Environment variables may remain framework metadata or allowlisted transport; they are not a user configuration source. The allowlisted script-to-container transport is `HYBRO_PUBLIC_PORT`, `HYBRO_LISTEN`, and `HYBRO_INSECURE_NO_AUTH`. `HYBRO_NO_UPDATE_CHECK` is a host-side switch for the script and never reaches the container.
- Keep README, `docs/configuration.md`, and the architecture documentation explicit about any still-unmigrated runtime paths.
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
