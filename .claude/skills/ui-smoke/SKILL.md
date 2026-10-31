---
name: ui-smoke
description: Walk the Hybro UI like a first-time user after a change - overview, agents, chat and rooms, HITL, networks, sessions, settings, responsive and console checks - using the built-in browser, then turn every finding into a Playwright spec. Use after frontend or cross-service changes, or when asked to verify the experience.
argument-hint: "[base URL, default http://localhost:3100] [focus: area or flow]"
disable-model-invocation: true
---

# Hybro UI smoke walk

Arguments: `$ARGUMENTS`. First token that looks like a URL is the base URL; the rest is the focus area. Default base URL is the dev instance `http://localhost:3100` (`./scripts/hybro start --build`, container `hybro-dev`). Never target the release instance on port 3000 unless the URL is given explicitly, and never use real provider keys or paid models unless told to.

Use the built-in browser (`mcp__Claude_Browser__*`). Prefer `read_page` and `get_page_text` for checks; screenshot only when layout or styling is the question. Record every observation as you go; do not fix anything until the walk is finished.

## 0. Preconditions
1. `curl -s <base>/api/v1/health` (or the root) answers. If not, say so and stop; do not start services unless asked.
2. Clear console messages, open `<base>/`.

## 1. Flows (skip ones outside the focus area, say which)
- **Overview `/`**: loads without errors; provider banner present only when no provider is configured; banner is dismissible here.
- **Agents `/agents`**: sample agents listed (Echo always; LLM-backed ones show "needs model" when unconfigured); health state visible; agent detail opens.
- **Chat `/chat`**: quick-start templates render; submitting creates a room and navigates without a sign-in redirect. If no provider is configured: permanent banner and disabled composer.
- **Room**: send a message, a turn appears; multi-agent reply grouped in one turn; activity strip collapses and expands; agent details open with history column still visible; no internal dispatch prompt ever shown in the stream; cancel in-flight works.
- **HITL**: when an agent asks for input, the reply control appears, the answer is accepted, and the resolved interaction stays visible after reload.
- **Files**: attach a small file and send; artifact shows a download control.
- **Networks `/networks`**: graph renders registered and discovered agents; search, scope and selection survive reload.
- **Sessions `/sessions`**: list renders; owner can approve a client session.
- **Manage `/manage` and MCP connect page**: readable and copyable at 1280px and 375px; legacy management routes redirect.
- **Model provider settings**: banner opens settings; saving a key clears the banner (use a dummy key only if the UI validates format, not a live call).
- **Errors**: unknown route gives a 404 page; `/rooms/<id>/anything` returns 404, not a settings route.
- **Responsive**: repeat Overview, Chat and a Room at 375x812 and 1280x800; no horizontal scroll, controls reachable.
- **Console**: zero errors across the walk; list any warnings that repeat.

## 2. Findings
Produce a table: flow, expected, observed, severity (P1 broken flow, P2 wrong but usable, P3 polish), evidence (page text, console line or screenshot).

## 3. Ratchet into Playwright
For every P1 and P2 finding, before any fix:
1. Add or extend a spec in `frontend/tests/e2e/` following the existing style (`auth.spec.ts`, `room-timeline.spec.ts`, `model-provider-settings.spec.ts`), so the spec fails on the current behaviour.
2. Run it: `cd frontend && npx playwright test <spec> --reporter=line`. Confirm it fails for the right reason.
Then stop and report. Fixing belongs to the `/dev` loop, with these specs as the failing tests.

## Rules
- Never delete rooms, agents, volumes or config on the instance; create test rooms with a `smoke-` prefix and list them in the report so the owner can remove them.
- Do not claim a flow passed unless you observed it.
