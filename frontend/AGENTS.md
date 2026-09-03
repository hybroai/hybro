# Frontend Rules

These rules apply to `frontend/` in addition to the repository-root `AGENTS.md`.

## Structure and Conventions

- This is a Next.js 16, React 19, strict TypeScript application.
- Routes live in `src/app`; feature components live in `src/components`; shared primitives live in `src/components/ui`.
- Hooks live in `src/hooks`, shared logic and API clients in `src/lib`, and Zustand stores in `src/stores`.
- Use the `@/*` alias for `src/`, 2-space indentation, single quotes, no semicolons, PascalCase components, and `use*` hook names.
- Prefer feature-local code unless it is genuinely shared.
- Preserve `client_request_id` correlation in room, SSE, streaming, HITL, processing-status, sending, and agent-response flows.

## Commands

Use Node `20.19` from `.nvmrc`. Run from `frontend/`:

```bash
npm ci
npm run lint
npm run test
npm run build
npm run test:e2e
```

Run focused Vitest or Playwright coverage first. For visible UI changes, verify the affected route in a browser across relevant viewports and check loading, empty, error, overflow, and interaction states.

Review `docs/System-Architecture.md` when frontend routes, API integrations, data flow, streaming, state management, authentication, module boundaries, or major UI workflows change.

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
