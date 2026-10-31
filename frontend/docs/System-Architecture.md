# Hybro Frontend Architecture

> Last scanned: 2026-10-23
>
> Source of truth: current repository files under `src/`, `tests/`, and root config files.

## 1. Overview

Hybro Frontend is a Next.js App Router application for the Hybro multi-agent platform. A single unified portal provides an Overview, the Network workspace (agents, subnets, and their relationships), agent registration and detail pages, Rooms (chat, room timelines, HITL replies, file attachments), Connect pages for MCP and HTTP API access, pricing, and public information pages.

The app talks to the backend through REST APIs and room-scoped Server-Sent Events (SSE). The room UI uses normalized message state, transient streaming buffers, selector-driven view models, and a conversation renderer built around turns rather than raw message rows.

## 2. Current Scan Summary

| Area | Current count / source |
|---|---|
| Source files | 324 files under `src/` |
| Test/support files | 166 files under `tests/` |
| App Router files | 36 files under `src/app/` |
| Component files | 113 files under `src/components/` |
| shadcn/ui primitives | 27 files under `src/components/ui/` |
| Conversation components | 22 files under `src/components/conversation/` |
| Hooks | 48 files under `src/hooks/` |
| Library modules | 104 files under `src/lib/` |
| Stores | 23 files under `src/stores/` |

## 3. Tech Stack

| Category | Technology |
|---|---|
| Framework | Next.js 16 App Router with Turbopack |
| Runtime UI | React 19 |
| Language | TypeScript |
| Styling | Tailwind CSS v4, CSS variables, project CSS tokens |
| Component system | shadcn/ui with Radix primitives, `components.json`, New York style |
| Icons | Lucide React |
| Forms | React Hook Form + Zod |
| Auth | Local self-hosted identity adapter (`src/lib/auth.tsx`) |
| Server state | TanStack React Query |
| Client state | Zustand |
| Real-time transport | SSE over `fetch()` streaming |
| Markdown/rendering | Streamdown + rehype-highlight |
| Agent protocol | `@a2a-js/sdk` |
| Network visualization | `d3-force` with one two-dimensional SVG graph simulation |
| Testing | Vitest, Testing Library, MSW, Playwright |

## 4. Root Config And Tooling

| File | Purpose |
|---|---|
| `package.json` | npm scripts and dependency manifest |
| `package-lock.json` | locked dependency graph |
| `.nvmrc` | recommended Node version (`24`) |
| `next.config.ts` | Next.js configuration |
| `tsconfig.json` | TypeScript compiler configuration |
| `eslint.config.mjs` | ESLint 9 configuration |
| `vitest.config.ts` | unit/integration test configuration |
| `playwright.config.ts` | e2e test configuration |
| `components.json` | shadcn/ui generator aliases, Tailwind CSS entry, icon library |
| `postcss.config.mjs` | Tailwind/PostCSS pipeline |

The PostCSS configuration uses the canonical plugin object
`plugins: { "@tailwindcss/postcss": {} }`, shared by Next.js and Vitest's CSS
module processing.

### User configuration

Frontend settings originate in the `frontend` section of the same
`~/.hybro/config.json` used by backend (`HYBRO_HOME` overrides the directory).
The image entrypoint (`backend/runtime_entrypoint.py`) validates the document
and passes an allowlisted public JSON projection to the frontend server as
`HYBRO_FRONTEND_CONFIG`. For `npm run dev`, CONTRIBUTING passes
`packaging/frontend/public-config.json`, generated from the backend schema for a
backend on port 8000.

The projection is **deployment configuration, not build input**. One published
image serves every install:

1. `src/app/hybro-runtime-config/route.ts` is a `force-dynamic`, `no-store` route
   that serves the container's projection as a small script. The container reads
   its own `HYBRO_FRONTEND_CONFIG` per request.
2. `src/app/layout.tsx` loads that route with a blocking `<script>` in `<head>`.
   It must be a real tag in the served HTML: a React-rendered script is created
   during hydration and never executes, and a deferred one would race the
   application modules that read the projection while they initialize.
3. `src/lib/config.ts` resolves it once per page load — the delivered global in
   the browser, the environment where no document exists (tests, server code) —
   and `parsePublicConfig` validates it either way, so nothing unvalidated
   reaches business code.

`next.config.ts` builds with `output: 'standalone'`, so the single image
carries only the traced server and static files. When `HYBRO_FRONTEND_CONFIG`
is present at build time (`npm run dev`) it compiles server-side rewrites: its
`api_prefix` to the backend on 8000, and `/mcp` and `/mcp/*` to the native MCP
server on `127.0.0.1:8001`, so the Connect page's `{origin}/mcp` answers in
development too. The image builds without a projection and compiles no
rewrites, because Caddy routes the API and MCP on the same port. `src/app/layout.tsx` sets `dynamic = "force-dynamic"`, so
no page is prerendered with one deployment's settings and server code always
reads the container's projection. `next.config.ts` deliberately does not
republish the projection through the `env` key: that inlines values into every
bundle, including the serving route, which pins one image to one deployment.

In the single image the entrypoint (`backend/runtime_entrypoint.py`) supplies
the projection: `api_base_url` empty, `server_url` the browser's origin
(`http://localhost:<port>` on loopback, `frontend.server_url` on a public
listener), and `mcp_disabled` true on a public listener. The standalone server listens on `127.0.0.1:3000` behind Caddy.

An empty `api_base_url` means same origin: `getApiUrl` then returns a relative
`{api_prefix}/...` path, which the browser sends to the address it loaded the
page from (Caddy in the image; the Next rewrite in `npm run dev`). API modules therefore never pass these URLs to `new URL()`; query
strings are appended with `URLSearchParams`. In this mode the Connect page shows
the page origin as the API base URL; otherwise it shows the projection's
`api_base_url`. The MCP address is always `{origin}/mcp`. The optional
`mcp_disabled` flag is derived, not user-settable; when true the Connect page
says MCP is disabled on public listeners.

Neither dotenv files nor `auth.json` are browser configuration inputs. Server
credentials are runtime-only and never build arguments. A browser gets a new
projection on its next page load; an already-open tab keeps the one it loaded.

Available package scripts:

```bash
npm run dev
npm run build
npm run start
npm run lint
npm run test
npm run test:watch
npm run test:coverage
npm run test:ui
npm run test:e2e
npm run test:e2e:ui
npm run test:e2e:headed
npm run test:all
```

`npm run lint` invokes ESLint using `eslint.config.mjs`.

## 5. App Routing

Routes are defined under `src/app/`.

```text
src/app/
|-- layout.tsx
|-- globals.css
|-- favicon.ico
|-- robots.ts
|-- sitemap.ts
|-- hybro-mcp/route.ts         # public read-only MCP status
|-- privacy/page.tsx
|-- (auth)/
|   |-- layout.tsx
|   |-- sign-in/[[...sign-in]]/page.tsx
|   `-- sign-up/[[...sign-up]]/page.tsx
`-- (portal)/
    |-- layout.tsx
    |-- page.tsx                 # Overview
    |-- about/page.tsx
    |-- network/page.tsx         # Network workspace (topology and structure views)
    |-- network/agents/[id]/page.tsx
    |-- network/agents/new/page.tsx
    |-- connect/mcp/page.tsx
    |-- connect/api/page.tsx
    |-- chat/page.tsx
    |-- pricing/page.tsx
    |-- room/[id]/page.tsx
    |-- sessions/page.tsx        # Sessions workspace: searchable list, no selection
    |-- sessions/[id]/page.tsx   # same workspace with the session record selected
    |-- agents/page.tsx          # redirect -> /network
    |-- agents/[id]/page.tsx     # redirect -> /network/agents/[id]
    |-- agents/new/page.tsx      # redirect -> /network/agents/new
    |-- networks/page.tsx        # redirect -> /network
    |-- access/mcp/page.tsx      # redirect -> /connect/mcp
    |-- access/api/page.tsx      # redirect -> /connect/api
    `-- manage/                  # redirect-only paths
        |-- page.tsx
        |-- agents/page.tsx
        |-- agents/[id]/page.tsx
        `-- agents/new/page.tsx
```

### Unified routing

The application exposes one unprefixed route tree with four destinations:

- `/`: Overview.
- `/network`: the Network workspace. View state lives in the URL: `view`
  (`topology` default, `structure`), `q` (search text), `scope` (`all` default,
  `unassigned`, `subnet:<id>`), `layout` (`list` default, `subnet`), and
  `select` (`agent:<id>` or `subnet:<id>`). Defaults are omitted, the params are
  independent, and unknown or stale values fall back to defaults without an
  error page. Search updates replace the history entry; other params push one.
- `/network/agents/[id]` and `/network/agents/new`: agent detail and remote
  agent registration.
- `/chat` and `/room/[id]`: Chats (chat home and room workspace).
- `/connect/mcp` and `/connect/api`: Connect pages (MCP and API tabs).

There is no host-based route rewrite. `/agents*`, `/networks`,
`/access/*`, and `/manage*` paths are page-level `redirect()` routes to their
canonical equivalents. `src/lib/routes.ts` is the canonical route vocabulary
(`overview`, `network`, `networkWith(query)`, `networkAgent(id)`,
`registerAgent`, `connectMcp`, `connectApi`, `chat`, `room(id)`).

The Network workspace merges visible registered agents with locally available
agents through `useNetworks`. Its **Add agent** menu offers **Add Remote Agent**
and **Scan Local Agent**; discovery calls the authenticated
`POST /api/v1/local-agents/discovery` endpoint, waits for the backend discovery
cycle, and then refreshes the agent inventory. Directly discovered
`source=local` agents are displayed while active.

### Provider hierarchy

`src/app/layout.tsx` wraps the app with:

1. `ThemeProvider`
2. `QueryProvider`
3. `Toaster`
4. `CookieBanner`

The portal layout composes `SidebarProvider` and `SettingsDialogProvider`
directly.

Notifications: transient feedback, including platform and backend errors, uses
Sonner `toast` from the root `Toaster`. Errors that must stay visible inside a
page or dialog use the `Alert` primitive. Agent task failures and rejections are
not toasted; they render in the conversation as part of the agent's turn.

Every portal route shares `PortalSidebar` and `SidebarInset`; the inset contains `PortalHeader` and the route content.
There is no separate workspace shell or route-classified navigation renderer.

Signed-out marketing pages
use their public header; signed-in pages share the side menu. Desktop pages have
no additional header, while mobile pages use the existing header and sidebar
trigger. Individual pages own their sizing, scrolling, and business content.

## 6. Component Organization

```text
src/components/
|-- ui/                       # shadcn/ui primitives
|-- conversation/             # turn/timeline rendering system
|-- composer/                 # chat composer shell and HITL response bar
|-- portal/                   # shared sidebar, header, footer, ChatHistory
|-- overview/                 # Overview page and static mini topology
|-- sessions/                 # Sessions list (SessionsTablePage) and ticket (SessionRecordView)
|-- networks/                 # Network workspace: toolbar, SVG graph, structure view, details panel, add-to-subnet menu, Agent picker
|-- subnets/                  # one subnet dialog (create/edit), delete confirmation, room/chat dialog host
|-- connect/                  # Connect pages, per-assistant setups, copyable access materials
|-- providers/                # React Query provider
|-- settings/                 # settings dialog sections (account, model provider) and helpers
|-- setup/                    # model provider banners
|-- room-page-shell.tsx       # room workspace shell
|-- room-chat-input.tsx       # composer input, mentions, uploads
|-- file-attachment-button.tsx
|-- group-selector.tsx
|-- status-indicator.tsx      # shared status dot and label (src/lib/status.ts mapping)
|-- page-header.tsx           # shared page title and one-line description
|-- agent-source-badge.tsx
|-- artifact-list.tsx
|-- artifact-renderer.tsx
|-- attachment-preview.tsx
|-- image-lightbox.tsx
|-- markdown-content.tsx
|-- part-renderer.tsx
|-- mode-selector.tsx
|-- nav-agent.tsx
|-- nav-user.tsx
|-- nav-docs-button.tsx
|-- nav-discord-button.tsx
|-- require-auth.tsx
|-- use-case-card.tsx
|-- fade-in-section.tsx
|-- cookie-banner.tsx
|-- theme-provider.tsx
|-- theme-toggle.tsx
|-- logo.tsx
`-- icons.tsx
```

### Conversation renderer

The current room UI is centered on `src/components/conversation/`:

- `ConversationMessageList.tsx`: top-level message/timeline list.
- `TurnRenderer.tsx`, `TurnBody.tsx`: turn-level rendering. `TurnRenderer` selects `CanonicalTurnRenderer` for a turn with an exact canonical root and the message-store renderer otherwise.
- `CanonicalTurnRenderer.tsx`, `CanonicalTurnTrace.tsx`, `turn-trace-time.ts`: the canonical Turn (User, Trace, Final Answer, Agent Cards).
- `TurnTracePanel.tsx`: Work Logs (or a Running/Preparing header when there are none) for pending and non-canonical Turns.
- `ProcessingStatusLog.tsx`: progress entries for turns without a canonical root.
- `UserMessageBlock.tsx`, `UserAttachmentCard.tsx`, `UserAnswerCard.tsx`: user-side turn content.
- `AgentCard.tsx`, `AgentContentBlock.tsx`, `AgentResultContent.tsx`, `AgentIndex.tsx`: agent response presentation. In the message-store renderer, multi-agent turns with LLM synthesis (`llm_synthesis`) show the combined answer in the primary surface and compact per-agent index rows below. Supervisor DONE without synthesis (`deterministic_done`) shows the digest intro in the primary surface and full per-agent bodies in the expanded `AgentIndex`. Substantive `summary-*` content classifies as `llm_synthesis`; the short coordinator digest stub (`"N agents responded. Expand below…"`) classifies as `deterministic`.
- `FinalAnswerSurface.tsx`, `SynthesisContent.tsx`: final/synthesis answer surfaces.
- `AgentResponseDetailPane.tsx`: right-side detail pane for a selected agent response.
- `ScrollToBottomButton.tsx`, `scroll-state.ts`: scroll affordances and state.
- `conversation-tokens.css`, `shimmer.css`: conversation-specific CSS tokens and loading effects. Reading typography uses 16px / 1.75 line-height, system UI sans, 400 weight (light and dark), neutral letter-spacing, 1em paragraph gaps, 2.75rem turn spacing, an 800px content column, and 14px table cell text.

`src/components/room-page-shell.tsx` owns the room workspace. It renders the conversation list, the composer dock, desktop resizable detail panes, and mobile detail sheets. It also wires selected message state from `room-ui-store`, streaming buffers from `useStreamBuffer`, and detail view models from `selectAgentResponseDetail`.

## 7. Hooks And Room Orchestration

Top-level hooks live in `src/hooks/`; room-specific orchestration lives in `src/hooks/room/`.

```text
src/hooks/
|-- useRoomWebhook.ts          # public re-export/entry hook
|-- useRoomSSE.ts              # low-level SSE connection hook
|-- useTurnViewModels.ts       # turn view model builder
|-- useChatRoomCreation.ts     # room creation and navigation
|-- useGroupManagement.ts      # saved subnets and room scope selection
|-- useNetworks.ts             # owner-scoped subnet queries, agent inventory, and confirmed CRUD writes
|-- useNetworkUrlState.ts      # Network workspace URL state bound to the router
|-- useGraphDisplayPrefs.ts    # graph label switches persisted in localStorage
|-- useRoomHistory.ts          # shared room history query (sidebar, Chats badge, Overview)
|-- useRoomFile.ts             # authenticated room-file blob and preview lifecycle
|-- useSessions.ts             # owner session list, polled every 15 s (sidebar badge, Overview, /sessions)
|-- useSessionListUrlState.ts  # Sessions list view state bound to the URL
|-- useHitlInteractionController.ts # HITL questionnaire state reducer (lib/hitl/interaction-controller)
|-- useDetailPaneScroll.ts     # Agent detail pane scroll restore and tail follow
|-- useMcpStatus.ts            # shared /hybro-mcp gateway status query
|-- useScrollUserMessageOnSend.ts # one-time scroll into sticky zone on send
|-- usePrimaryStreamScroll.ts
|-- useStreamBuffer.ts
|-- useTextSelectionQuote.ts
`-- use-mobile.ts
```

```text
src/hooks/room/
|-- useRoomWebhook.ts          # orchestrates room data, SSE, sends, actions
|-- useAgentCatalog.ts
|-- useRoomData.ts
|-- useRoomHydration.ts
|-- useProcessingRestore.ts
|-- useRoomReset.ts
|-- useRoomSSEConnection.ts
|-- useSendMessage.ts
|-- useRoomActions.ts
|-- processing-lifecycle.ts
|-- processing-status-log.ts
|-- hitl-submission-fence.ts   # per-room, per-interaction submit fencing
|-- types.ts
`-- sse-handlers/
```

`useRoomWebhook` composes the room feature:

1. Reads per-room UI flags from `room-ui-store`.
2. Loads agents through `useAgentCatalog`.
3. Loads room settings and room agents through `useRoomData`.
4. Creates a per-room `ProcessingLifecycle`.
5. Resets room-local state when room changes.
6. Hydrates and reconciles DB messages through `useRoomHydration`.
7. Restores active processing placeholders with `useProcessingRestore`.
8. Creates an SSE dispatcher with `createSSEDispatcher`.
9. Connects SSE with `useRoomSSEConnection`.
10. Sends messages through `useSendMessage`.
11. Exposes room actions from `useRoomActions`.

## 8. Room Page Interaction Flow

`src/app/(portal)/room/[id]/page.tsx` is the room page. It is a client component that:

- Reads the `[id]` route param as `conversationId`.
- Reads user/auth state from the local identity adapter.
- Calls `useRoomWebhook`.
- Manages local chat mode, quote state, and prefilled input handoff.
- Uses `useGroupManagement` for saved groups and room-team defaults.
- Consumes pending room handoff data from `room-ui-store`.
- Persists chat-mode changes lazily before sending.
- Pre-writes room agent membership for empty rooms when a saved group is selected.
- Passes a `TimelineAdapter` into `RoomPageShell`.

High-level flow:

```text
Room route
  -> RoomChatPage
    -> useRoomWebhook
      -> room setting query
      -> initial DB hydration
      -> SSE connect
      -> normalized message store writes
    -> useGroupManagement
    -> RoomPageShell
      -> ConversationMessageList
      -> ComposerShell
      -> AgentResponseDetailPane / mobile Sheet
```

## 9. State Management

### `src/stores/message-store/`

The message store is the normalized source of truth for persistent room messages.

Main responsibilities:

- Store entities by message id and ordered ids per current room.
- Merge DB, HTTP, and SSE writes through `applyUpsert`.
- Replace optimistic IDs with server IDs.
- Convert backend messages into `IncomingMessage`.
- Filter hydration data.
- Detect stale tasks.
- Resolve display type for renderer consumers.

Key files:

- `index.ts`
- `types.ts`
- `upsert.ts`
- `convert-api-message.ts`
- `hydration-filter.ts`
- `stale-detection.ts`
- `resolve-display-type.ts`

Other stores: `turn-store.ts` (the server-owned canonical Turn projection),
`turn-presentation-store.ts` (per-Turn Trace disclosure and scroll-follow
state), `streaming-store/` (below), and `room-ui-store.ts`.

### Snapshot-driven room sync (`src/lib/room-sync/room-reducer.ts`)

The room stream is snapshot-driven: state = latest full snapshot + ordered
deltas after it. `RoomReducer` remains the delivery entry point: it buffers
pre-snapshot frames, applies only deltas above the watermark, drains the bounded
`room_seq` reorder window, and requests `?snapshot=1` when continuity or the
canonical protocol is violated. Heartbeats track the highest observed server
watermark and recover even a single missing tail event. A snapshot older than
an already-applied watermark is rejected before store mutation; accepted
snapshots prune covered reorder entries and empty timers. The fetch transport
awaits each `void | Promise<void>` message handler so frames fold in network
order.

A snapshot containing the paired capability fields
`turn_lifecycle_schema: 1` and `turns` atomically replaces the server-owned
projection in `turn-store`. Every snapshot Turn and every known canonical
`run_event` payload is runtime validated against the closed backend DTO before
it can mutate state. Buffered live deltas then replay through the same pure,
idempotent fold used by normal live delivery. Invalid known events leave the
projection unchanged and request one fresh snapshot. Presentation state is
stored separately in `turn-presentation-store`, so snapshot replacement does
not reset manual disclosure or pinned-bottom ownership. After a successful
canonical replacement, `RoomReducer` performs authoritative terminal cleanup
only when its stored User message ID and client request ID exactly match a
terminal canonical root, even if HITL already cleared the send guard. Like live
`run_settled`, it marks processing resolved, stops processing, disarms the cancel
timeout, clears `cancelling`, and removes/dismisses the placeholder. This repairs
a missed `run_settled` at or below the snapshot watermark. Wrong-root,
nonterminal, invalid, and stale snapshots cannot unlock unrelated work.

The backend always sends both capability fields. A snapshot that advertises
only one of them is rejected; one with neither is folded only into the message
and streaming stores. The snapshot's `messages` and `hitl` sections still
hydrate those stores for User requests that have no canonical
root (room history written before canonical Runs); suppression is exact-root
only. Canonical `awaiting_input` restores every actionable request-scoped HITL
entity plus the dispatcher-owned `hitlRequestIndex` through the `RoomReducer`
snapshot path, without recency inference. Multiple requests may share a canonical
Agent Card message identity but never their client-side projection identity. A
HITL request row without `run_id` may recreate only its composer message
projection when its `client_request_id` and related User message exactly match
one canonical Turn; it never infers or mutates Turn lifecycle state. Canonical
renderers never consume message-store prose. This is a
per-User-request boundary: exact `run_started`/snapshot root binding selects the
canonical renderer; otherwise the message-store renderer is used.

The backend room stream sends `Cache-Control: no-cache, no-transform` and
`X-Accel-Buffering: no`. These headers must survive the frontend API rewrite:
compression middleware can otherwise buffer an open SSE response, withholding
`connected`, the initial snapshot, and live deltas. Ordinary message history
contains user messages and final answers, not the complete canonical execution
projection; a buffered snapshot can therefore hide Agent Cards and Work Logs
even when that history request succeeds. Stream-specific transformation is
disabled without disabling compression for normal frontend assets or JSON APIs.

### Canonical Turn projection and Trace

`src/lib/turn-lifecycle/` defines the strict wire contract, snapshot mapping,
and pure Turn fold. `run_started` is the only live root binding and must exactly match
`run_id`, `correlation_id`, and the durable User message. The projection owns
internal turns, offset-checked Assistant assembly, and exact equality between
an already assembled delta stream and `message_end.text`; a mismatch requests
protocol recovery instead of truncating/replacing text. It also owns commentary/final
classification, opaque Tool rows, retries, exact-root HITL interactions, the
provisional final, its three-field `agent_response` commitment, child closure,
and `run_settled`. Activity order is `room_seq`; no timestamp, prose, content,
Agent-name, card-status, or recency inference participates.

`CanonicalTurnRenderer` has explicit User → Trace → Final Answer → Agent Cards
DOM order. The Trace uses the owned shadcn `Marker` source plus shadcn
`Collapsible` and presents only concise tool, Agent-call, retry, ask-user, and
preparation actions; it never repeats Assistant prose or exposes input/output.
Its **Work Logs** trigger includes the left-aligned status `Running`,
`Waiting for input`, or green `Finished`, followed by the whole-Turn duration.
Failed and canceled terminal Runs also display `Finished`; child action rows
retain their truthful failure/cancellation state.
A failed `tool_execution_end` (and its snapshot Tool row) may carry the Agent's
own `error_code` and `error_message` (null is treated as absent); the fold keeps
them as `errorCode`/`errorMessage`, and the Agent Card shows the message, or the
code, under the failed status. When the user mentioned an Agent (`<@id|Name>`
chips) and every call to that Agent in the Turn failed, the Final Answer is
preceded by a notice naming the Agent and reason and stating that the reply is
from Hybro, not that Agent (`failedAddressedAgents`).
The current Assistant safely renders through `MarkdownContent` in the final slot;
`message_end(final)` remains provisional until the exact durable response commits
it. Canonical terminal duration is server-authoritative; live durations tick from the Turn start, while a terminal turn without a
canonical root uses its last durable observation. Active traces initialize open, historical terminal snapshots
initialize collapsed, and focus-safe auto-collapse is consumed once. Both Trace surfaces use the answer body's 1rem/1.75 typography and left edge,
with no internal card borders. Tool/Agent-call rows use the Bot marker icon;
completed rows and the terminal `Finished` status use the shared success green.
Trace and Agent-call lists grow within the conversation's single scroll owner;
they do not create clipped nested scroll regions when a Turn has many calls.
Only active content is live-announced, and motion has reduced-motion fallbacks.
HYBRO AI summary/presenter entities own final-answer data but render no Agent
Card; only delegated Agent executions appear as Agent Cards.

Turns without a canonical root show progress entries through `ProcessingStatusLog`, including
logs restored from stream snapshots. The compact log remains readable without
expanding it. These Turns have no per-turn Tool trace: retired non-canonical
`run_event` kinds are not folded, so old history renders its messages and logs
only.

Trace and Agent Cards are separate UI projections of the same canonical
`TurnProjection.activity` Tool row selected by `(run_id, tool_call_id)`;
MessageStore task entities never own execution state.
Repeated calls remain separate and activity summaries count calls, not unique
Agent names. Both surfaces expose the same `data-call-id` and normalized
`data-status`, including cancellation.

Canonical `model_decision` events fold into `TurnProjection.activity` entries of
`kind:"decision"` (validated in `contract.ts`, folded in `fold.ts`, rendered by
`CanonicalTurnTrace`). Current decisions make the model-first HITL loop visible
through `interaction_received`, `answered_from_context`, `no_progress`, and
explicit `forwarded_to_user` events; `degraded_to_user` is accepted by the
contract but no backend producer emits it. Only backend-computed summaries
and sanitized Agent labels are projected; raw model reasoning is never surfaced.
 Cards prefer the durable sanitized root
Agent name, and generic,
blank, opaque, or internal update labels cannot downgrade it during live,
snapshot, or database hydration. For old records only, the exact opaque-call
Tool activity label is the final safe fallback. Live and snapshot guards require
`run_id + opaque_public_call_id` and
the exact derived `orchestrator:{run_id}:{opaque_public_call_id}` message
identity. Any `task_*`, partial-agent, raw artifact, or `processing_status` frame is excluded from canonical lifecycle state only when it matches an exact
canonical User/client root; canonical capability elsewhere in the same room does
not suppress unrelated message-store activity. Malformed duplicate status owners
trigger snapshot recovery. Before `run_started`, the latest optimistic User root
renders a preparation or Work Logs surface while no Agent responses are available.
Received message-store Agent responses remain visible even while that User
message is optimistic. An exact canonical root atomically takes ownership of the
matching turn, so message-store cards do not duplicate canonical cards. Memoized Turn boundaries
and card-ID-scoped selectors keep
active deltas from rerendering historical Turns. Canonical cards without a
real Agent profile ID render a non-link label (never a fabricated
`/network/agents/orchestrator…` route), and clickable cards contain no nested interactive
control. Private output is fetched only after a terminal card. A transient 404
uses bounded backoff before showing an explicit reopen-to-retry message. The
same authenticated detail boundary supplies room-owned artifact descriptors for
completed canonical calls: the Final Answer aggregates and deduplicates those
artifacts, while the selected Agent detail renders only that call's artifacts.
Both surfaces use one TanStack Query identity per Room/run/opaque call, including
bounded transient retry and a visible final-body retry state, so opening a card
reuses the already fetched detail. Agent detail treats the authenticated A2A
`parts` sequence as authoritative: declared `TextPart` values render as Markdown
without JSON inference, while declared `DataPart` values render as collapsed JSON
disclosures. The detail pane groups all Text Parts first, Data Parts second, and
authorized file parts last, preserving source order within each type. This keeps
type boundaries explicit instead of showing a flattened JSON-plus-text string.
`parts` is required in the detail response. Descriptors carry the durable room-file ID,
MIME type, size, and display name and are mapped into the standard authenticated
`ArtifactList`/`useRoomFile` preview path, including responses with no text.
Room-file Blob reads use a user/file-keyed promise cache so simultaneous Final
Answer and detail previews share one authenticated download while retaining
independent object-URL lifetimes. The cache is bounded by entry count and 64 MiB,
expires entries after five minutes, and never retains a single Blob over that byte
budget. Model-authored `sandbox:/api/v1/files/…`
Markdown destinations are never trusted as artifact identity; conversation
Markdown preserves their label without a broken link, and the actual artifact is
rendered from its authorized descriptor instead.

### Agent Dispatch Privacy

Frontend message state treats `taskContent` as public display metadata only.
Internal dispatch prompts are not accepted from API/SSE payloads and must not be
rendered in timeline stage details or Agent response detail panes. Streaming
correlation continues to rely on `client_request_id`; privacy filtering must
not drop that correlation field.

### `src/stores/room-ui-store.ts`

The room UI store contains ephemeral per-room UI state. Canonical per-Turn
Trace disclosure and scroll-follow state lives separately in
`turn-presentation-store` and is never snapshot-owned.


- sending / processing / cancelling flags
- SSE enabled / connected / error state
- initial hydration marker
- pending room handoff data
- selected agent-response detail state

The SSE reconnect surface also exposes `reconnectWithSnapshot` (gap-recovery
reconnect with `?snapshot=1`).

Cancellation remains a pending UI operation until the root terminal lifecycle is
folded. Both `pending_reconciliation` and `canceled` Stop responses keep the
processing message ID, `client_request_id`, placeholder, and `cancelling` flag
intact and display a disabled `Stopping...` spinner; the timeout is warning-only.
HITL Cancel follows the same ownership rule locally: it sets `cancelling` before
its version-fenced request, optimistically closes only the interaction after
success, then performs best-effort DB reconciliation and a forced canonical
snapshot request. Recovery failure does not turn an accepted cancellation into a
request failure, and no second Stop request is sent. A rejected HITL cancellation
clears `cancelling` after any conflict reconciliation. The HTTP response and
child-task updates cannot otherwise clear that state. Durable terminal
`run_settled` or an accepted exact-root terminal snapshot (canonical turns), or terminal `processing_status` (turns without a canonical
root), owns cleanup. After
refresh, the existing room `active_runs` response restores `canceling` and
hydrates both message and client-request correlation from the triggering user
message.

### `src/stores/streaming-store/`

The streaming store contains transient live artifact/text buffers. It is intentionally separate from `message-store`: streaming artifacts are displayed live, then cleared after DB reconcile or task checkpoint persistence.

Live buffer text is derived via `extractStreamTextFromArtifacts`, which concatenates all text-only artifacts in emission order (matching backend final assembly). Persisted entity text uses `extractTextFromArtifacts` (last text-only artifact) for thinking + answer agents; this asymmetry is intentional.

**Streaming invariants:**

- **I1** — One live ingest pipeline. All live streaming text flows through `streaming-store.append(message_id, …)`. `agent_response_partial` first creates a correlation-preserving message shell, then maps `content_delta` to a synthetic artifact and calls the same append; a live buffer is never left without a renderable turn entity.
- **I2** — Live buffer key is always `message_id`. `client_request_id` is correlation/cleanup metadata, never a buffer key or display merge dimension.
- **I3** — Live text equals persisted text. `extractStreamTextFromArtifacts` over the live artifact list equals backend `extract_parts_from_artifacts` over the persisted artifact list at terminal.
- **I4** — Detail pane content for terminal entities comes from `message-store`, never from the live buffer (strict terminal guard in `selectAgentResponseDetail`).
- **I5** — Per-agent terminal SSE clears that message's buffer only. For turns without a canonical root, turn-level clear is owned by terminal `processing_status`; canonical Turns do not use that frame for lifecycle or content.
- **I6** — `streaming-store/append` does not import `mergeArtifacts` from `message-store/upsert`. Live merge is `mergeStreamArtifacts` (disjoint-segment push, prefix-relation replace).
- **I7** — Streaming UI (badge, cursor, Streamdown caret) is driven only by an incomplete live buffer while the agent view-model status is `working`. Terminal agent status always wins over a stale buffer. Late `artifact_update` frames after terminal `task_update` are ignored and any leftover buffer for that `message_id` is cleared.

**Conversation markdown normalization** (`src/lib/markdown/`, applied in `MarkdownContent` when `className` includes `conversation-markdown-body`):

- **Pre-parse** (`preprocessConversationMarkdown` in `normalize-conversation.ts`, `normalize-agent-list-markers.ts`, and `split-inline-ordered.ts`) runs before Streamdown: rewrites common agent list-marker mistakes (`1. • …` and bare `•` lines → `-` bullets; skips fenced code); inline ordered split for run-on `1. foo 2. bar` lines on list-item lines only (skips prose like `See step 1. For details`); supervisor-shaped lines (`3. **#3 — …`) only split before the next `N. **#N` marker so prose like `adoption in 4. The era` stays one item; inline splits are deferred while `isStreaming` is true; ATX heading lines and fenced code are skipped; bare `###` markers on their own line are folded into the next content line. Section-label promotion and list renumbering are **not** done in pre-parse.
- **Render-time remark plugins** (`conversation-remark-plugins.ts`, passed to Streamdown `remarkPlugins`) operate on the mdast tree Streamdown actually renders — no remark-stringify/reparse gap. The bundle includes `remark-gfm` because Streamdown replaces (not merges) default plugins when `remarkPlugins` is set. For conversation markdown, `parseMarkdownIntoBlocksFn={(md) => [md]}` parses the full message in one pass (including during streaming) so section/list surgery is not split across Streamdown blocks. Plugin order: `remark-gfm` → `remarkSplitSectionLists` → `remarkNestAdjacentBulletLists` → `remarkCoalesceOrderedLists` → `remarkAssignOrderedListStarts`.
- **Rehype pipeline** (`markdown-content.tsx`): custom `rehypePlugins` must include Streamdown's `defaultRehypePlugins` (`rehype-raw`, `rehype-sanitize`, `rehype-harden`) before `rehype-highlight`, because passing `rehypePlugins` replaces rather than merges defaults.
- Agent `message_text` is stored and returned by the backend as produced; markdown repair is client-side only. The frontend AST pipeline normalizes third-party agent markdown.
- Completed agent tasks treat backend-projected `message_text` as the human-readable response and render non-text Task artifacts alongside it. Raw `TaskStatus.message` and Task history are never promoted by the client; when public `message_text` is absent, a completed text-only artifact is the fallback.
- Agent entities keep `extend_info.public_dispatch_text`, when a stored message carries it, separately from the short `public_task_label`. The main Agent Card keeps the compact label; the expanded response detail prefers the full dispatch text in its existing collapsible task region, followed by the agent `message_text` and artifacts.
- The renderer maps top-level `<ol>` elements to `style.counterReset = 'conv-section-ol <start - 1>'` from the mdast `start` prop. CSS counters in `conversation-tokens.css` provide visible `N.` markers for ordinary lists; items that already start with `#N` (supervisor-style `**#1 — …` rows) get `conv-hash-numbered-item` and suppress the extra counter.

Display helpers in `src/lib/streaming/display.ts` split live **text** (buffer) from **non-text artifacts** (files/data) during stream so the detail pane and activity strip can show file attachments while text is still growing. `AgentResponseDetailPane` uses `useDetailPaneScroll` with ChatGPT-aligned behavior: first open scrolls to top; reopening the same message restores saved scroll from `room-ui-store.detailScrollByMessageId`; optional tail-follow when pinned near bottom during stream; no scroll reset on stream complete; detail body uses `overflow-anchor: none`.

**Main feed scroll (`ConversationMessageList`):** The logical bottom of the feed is the `[data-content-end]` sentinel after the last turn — not the full `scrollHeight`, which includes the fixed `[data-scroll-spacer]` below it. `src/lib/conversation/content-end-scroll.ts` centralizes `scrollToContentEnd`, `isNearContentEnd`, and snapshot `atBottom` detection. On first open (no saved position), the list scrolls to content-end. When revisiting a room, the last scroll position is restored from `room-ui-store.conversationScrollByRoom` (including an `atBottom` flag so rooms left pinned to the latest message still land at content-end). Scroll snapshots persist across `resetRoom` and are cleared on `resetAll`. Every user message bubble uses native CSS sticky (`.conversation-user-sticky { position: sticky; top: 0 }`) for both live and completed turns. On send (`localSendSeq`), `useScrollUserMessageOnSend` scrolls the sticky wrapper into the top of the scrollport once so sticky engages; CSS sticky then holds the question visible while HYBRO/agent content grows below. While the room is processing (`turnLive`), `tailFollowRef` (detail-pane-style sticky follow flag) stays true once the user reaches content-end and only clears on explicit user scroll (wheel/touchmove with movement, or `scrollTop` decrease while not programmatic), not when content growth temporarily moves the viewport away from the threshold. The 150ms programmatic-scroll suppress window only skips scroll-position re-enable inference and snapshot writes — it does not block user cancel. `usePrimaryStreamScroll` and layout-driven follow scroll whenever `tailFollowRef` is set. `.conversation-scroll-area` and `.conversation-frame` use `overflow-anchor: none`. Users who scroll away see the scroll-to-bottom button, which re-enables tail follow.

## 10. SSE And Room Sync

SSE handling is split into small handlers under `src/hooks/room/sse-handlers/`.

```text
src/hooks/room/sse-handlers/
|-- dispatch.ts
|-- client-request.ts           # client_request_id extraction and user-message lookup
|-- apply-commands.ts
|-- artifacts.ts
|-- types.ts
`-- handlers/
    |-- agent-response.ts
    |-- processing-status.ts
    |-- task-submitted.ts
    |-- task-update.ts
    |-- artifact-update.ts
    |-- hitl.ts
    `-- misc.ts
```

`src/lib/types/sse.ts` defines the final room SSE frame envelope as `{ type, conversation_id, timestamp, data }`. The handled room frame types are:

- Connection/system: `connected`, `heartbeat`, `error`, `run_event`, `cancellation`.
- Turn and task updates: `processing_status`, `task_submitted`, `task_update`, `artifact_update`.
- Agent output: `agent_response_partial`, `agent_response`.
- HITL and orchestration: `hitl_request`, `hitl_response`.

`user_message`, `turn_event`, `hitl_input_requested`, and `hitl_status_update` frames are not part of the handled room SSE contract. Unknown frame types are ignored after a debug log.

`createSSEDispatcher` first offers ordered deltas to the canonical fold. Known
canonical `run_event` payloads are closed runtime-validated unions; unknown subtypes are tolerated
and only clear the matching pending send acknowledgement. Canonical `processing_status` frames are ignored only
after their allowlisted status, exact nonempty User/client roots,
`details: null`, and absence of Agent/free-text fields validate; malformed
adapters request snapshot recovery.
Matching final responses and Agent-card task IDs enter the Turn projection by
exact durable identity before their normalized message entities are updated.

Canonical `hitl_request` and `hitl_response` require exact `run_id`,
`client_request_id`, related User-message roots, the opaque public Tool message
ID, and durable interaction/request/question identities. They also accept
the public presentation fields the translator always sends (`header`,
`required`, `allow_other`, `choice_options`) and the sanitized `answer_text` on
a response; any other key is a protocol violation. The ordered
`run_waiting_input` and `run_resumed` controls make the interaction and Turn
state change explicit; they reconstruct Turn-owned HITL history without recency
or REST inference. The producer persists responses and `run_resumed` before it
can dispatch a continuation that might immediately ask a follow-up question.
The message-store handler and pending REST overlay serve HITL rows without a canonical root.
Snapshot HITL applies canonical claims only after exact-root validation even when
a message entity already exists, and canceled/expired/error members hydrate as
resolved so they cannot replace the normal composer.
Live canonical requests also populate the incumbent HITL message projection so
the existing dedicated response composer can render the validated request.
The projection preserves HITL `source` as first-class message state. Agent
requests therefore render the external agent name (for example,
`Cyber Broker Agent · Needs Input`), while supervisor requests render HYBRO AI.
Raw agent task states such as `input-required`, `auth-required`, and
`policy-required` are not actionable UI state by themselves: until the message
has a durable `hitlRequestId`, the timeline and agent header continue to show
Working while backend recovery runs silently. Durable HITL content appears only
in the composer questionnaire; the conversation body does not repeat its prompt
or render an orphaned “Unattributed responses” Turn. The Turn Trace may still
show the Waiting for input lifecycle state, and its running state stays active
through the HITL wait. After answers apply, the HYBRO AI Working
avatar spinner returns while the turn stays `active` (including synthesizing and
any early `deterministic_done` surface) and stops when the turn leaves active /
reaches `phase: completed`. Spinner state follows turn lifecycle, not finalAnswer
kind alone — treating `deterministic_done` as “settled” would stop the spinner
too early and skip the synthesizing wait. Only the durable HITL projection may
show Needs Input or an interaction component.
Hydration marks input-required messages that are absent from the pending set as
resolved so canceled or expired request metadata cannot recreate stale HITL UI.

`processing_status` requires `message_id`, non-empty `client_request_id`, a known status, and `details` as either an object or `null`. Active statuses such as `queued`, `processing`, and `awaiting_input` keep the user turn active; terminal statuses mark the correlated user turn and clear the send guard only when they target the user message rather than a per-agent task. HITL resume can introduce a new backend `client_request_id`; in that case, a terminal frame with an agent-task `message_id` is accepted only when `related_message_id` points at the resolved user turn and the new request id differs from the user message's original request id.

Failed and canceled user turns are absorbing lifecycle states: a delayed active
`processing_status` frame cannot restart their composer processing/Stop state.
When a live tab misses terminal SSE and the room snapshot reports no active run,
`useProcessingRestore` rechecks `inquiryActiveRuns` for the exact trigger,
reconciles messages from the database, and stops processing only after the
reloaded user message carries a terminal status. Before mutating the lifecycle
after those asynchronous checks, it verifies that the same user message still
owns processing and that no new send or pending run-event acknowledgement has
started. This preserves the send-race guard while allowing backend-confirmed
failures with zero agent tasks to recover without a page refresh.

**Message-store turn completion.** Turns without a canonical root (room history
written before canonical Runs) are completed from message-store evidence. The
backend sends no completion kind; per-agent terminal SSE alone does not complete
a multi-agent turn. Backend-truth stamping (`turn-terminal-stamp.ts`) checks
`inquiryActiveRuns` and, when no Run is active and
`isBackendRunConfirmedNonSynthesisCompletion` holds, records
`turnTerminalStatus` together with `turnCompletionKind: 'deterministic'`.
`shouldScheduleTurnTerminalRecovery` schedules that check 1.5 s after a terminal
`FAILED`/`REJECTED`/`CANCELED`, or `COMPLETED` once all real agents are terminal;
summary-agent `agent_response` frames never schedule it. `deriveFinalAnswer`
returns `llm_synthesis` whenever a non-deterministic `summary-*` entity has
substantive content (`turnHasSubstantiveLlmSynthesis` also blocks stamping over
it), promotes to `deterministic_done` on a deterministic stamp, a deterministic
`summary-*` digest, or mixed terminal agents without synthesis
(`isDeterministicCompletionExpected` gates live promotion), and otherwise keeps
the turn `pending`/`synthesizing`. `hasActiveSynthesisGap` treats positive
synthesis evidence (`turnPhase: 'synthesizing'` logs, "synthesiz" or "compiling
summary" log lines, synthesis ephemerals, or a working non-deterministic summary
agent) as synthesis in progress; stale synthesis logs are ignored once all real
agents are terminal and one failed. Hydrated turns without live-run evidence stay
`completed`. When `turnTerminalStatus === 'completed'` and the summary agent has
content, `buildAgentResult` treats a `submitted` `system:hybro` entity as
completed.

**Live streaming checkpoints:** `artifact_update` is the primary path into
`streaming-store.append(message_id, …)`, and `agent_response_partial` shims into
the same message-keyed append. Terminal `task_update` and final `agent_response`
write to `message-store`, read the message-scoped buffer for fallback text, then
clear that message's buffer.

SSE artifact conversion is defensive at the client boundary. `task_update`
`parts` and `artifact_update` payloads drop inline `file.bytes`; file
parts are renderable when they carry a durable `file_id` or a URI. Canonical
artifact events and synthetic terminal `${messageId}-parts` projections are
reconciled by stable part identity (`file_id`, then SHA-256, URI, or canonical
data), so live detail matches post-refresh hydration without collapsing distinct
same-name files. `ArtifactList` numbers repeated display names within one list
("report (2).docx", `withDistinctFileNames`); file ids and download URLs are
unchanged. `file_unavailable` data renders as a safe unavailable-output
notice and is not counted as a file. `PartRenderer` does not create `data:` URLs
from inline bytes, so stale or malicious SSE cannot surface private file
content in message state or rendered media.

Room DB synchronization lives under `src/lib/room-sync/`:

- `hydrate-room.ts`: initial hydration, reconcile, and HITL overlay orchestration.
- `apply-db-messages.ts`: applies fetched messages to the normalized store.
- `hitl-overlay.ts`: overlays pending HITL requests.
- `types.ts`: hydration result and option types.

`useRoomSSEConnection` handles reconnect behavior:

- Mirrors SSE connection state into `room-ui-store`.
- Rehydrates pending HITL requests on reconnect.
- Reconciles with DB after reconnect gaps.
- On reconnect while a turn is processing, checks whether the backend still has
  an active Run; if not, it reconciles with the DB and recovers the turn's
  terminal state from backend truth. There is no periodic poll: missed events are
  recovered by the reducer's gap detection and snapshot re-request.

## 11. Timeline And View Models

Timeline construction lives under `src/lib/room-timeline/`.

```text
src/lib/room-timeline/
|-- build-turns.ts
|-- derive-final-answer.ts
|-- event-log.ts
|-- map-result-display.ts
|-- message-groups.ts
|-- multi-agent-turn-complete.ts
|-- stamp-live-turn-terminal.ts
|-- turn-agent-terminal.ts
|-- turn-live-shell.ts
|-- turn-terminal-stamp.ts
`-- types.ts
```

`buildTurns` groups normalized messages into turn view models. User messages define turn boundaries. Agent messages route by `relatedMessageId`, `clientRequestId`, and fallback order. System messages before the first user message are placed in a synthetic system turn.

Selectors under `src/lib/selectors/` adapt store state into UI-specific slices:

- `select-hitl.ts`
- `select-composer-state.ts`
- `select-agent-response-detail.ts`
- `map-agent-display.ts`
- `route-agent.ts`
- `conversation-types.ts`

## 12. API Layer

`src/lib/api-client.ts` is the shared fetch wrapper. It:

- Injects local identity auth headers through `getClientAuthHeaders`.
- Supports abort signals and a default timeout.
- Wraps HTTP failures in `ApiError`, with the parsed JSON error body in
  `details`.
- Returns `undefined` for a 204 response instead of parsing a body.
- Logs client errors as warnings and server/unexpected errors as errors.

API modules live in `src/lib/api/`. Consumers import the specific module they
need rather than a shared barrel, keeping client bundles scoped to the active
feature:

```text
src/lib/api/
|-- agent.ts
|-- agent-group.ts
|-- agent-call-detail.ts       # canonical Agent call detail request and parsing
|-- canonical-agent-call-detail-query.ts # shared TanStack Query identity for call detail
|-- room.ts
|-- session.ts                 # owner session list, record, close, event replay
|-- sse.ts
|-- inspection.ts
|-- files.ts
|-- setup.ts                   # browser setup API: status, models, API key, OAuth, image size
`-- hitl.ts
```

Type definitions live in `src/lib/types/`:

```text
agent.ts, agent-group.ts, attachments.ts, chat-mode.ts, error.ts, index.ts,
quote.ts, request.ts, response.ts, sse.ts
```

Other library modules:

- `auth.tsx`: local self-hosted identity and authentication-header adapter.
- `routes.ts`: canonical public and management route vocabulary.
- `utils.ts`: `cn`, `getApiUrl`, and formatting helpers.
- `consumer-nav.ts`, `nav-items.ts`: top-level navigation configuration.
- `agent-inventory.ts`: shared registered/discovered inventory merge and visibility
  policy for the Agents page and Network graph.
- `system-agents.ts`: system/supervisor agent classification.
- `agent-avatar.ts`, `agent-icon-utils.ts`, `file-icon-utils.ts`: display helpers.
- `api/files.ts` and `hooks/useRoomFile.ts`: authenticated room-file upload,
  download, and preview blob lifecycle. The authenticated same-origin download
  path uses the validated JSON `api_prefix` shared with the Next rewrite.
- `selection-plain-text.ts`: quote/selection text extraction.
- `streaming/display.ts`: streaming display helpers.

### MCP connection status

`GET /hybro-mcp` (`src/app/hybro-mcp/route.ts`) is a public, read-only Next.js
route, not a backend API endpoint or tool proxy. It probes only MCP `/health`
with a four-second deadline, no redirects, and no caching. The destination is
fixed to `http://127.0.0.1:8001`, because MCP shares the container's (or, in
native development, the host's) loopback. Request parameters cannot
select an upstream destination.

`src/lib/mcp.ts` validates the service identity and allowlisted status. The route
returns only `service: "hybro-mcp"` and
`status: "ready" | "unavailable" | "unsupported_auth"`; invalid responses and
probe failures become unavailable. It never forwards tool traffic, browser
credentials, upstream errors, or Agent Cards, and cannot control services or
bypass backend authentication. MCP's own health check performs a bounded,
read-only backend discovery request, never Agent execution. Clerk rejection is
reported as unsupported authentication.

### Send Message Routing

`src/lib/api/room.ts` sends every room message with a required
`client_request_id`, request-scoped `mode: 'direct' | 'supervisor'`, and one
canonical `agent_scope` discriminated union:

- Mention scope: `{ source: 'mention', agent_ids: [...] }` with a non-empty ID tuple.
- Room default: `{ source: 'room_default' }`.
- All visible active Agents: `{ source: 'all_agents' }`.
- Saved group: `{ source: 'saved_group', group_id }`; the Backend expands and
  authorizes membership, so the Frontend never sends group member IDs.

The Frontend does not emit `mentioned_agent_ids`, `message_target_mode`,
`target_group_id`, `target_group`, or candidate-scope fields. Fast maps to
`direct`, Ultimate maps to `supervisor`, and room settings only provide the UI
default.

## 13. Unified Portal

The `(portal)` route group provides one shared shell without adding a URL
segment.

- `/`: Overview, stacked line by line: a visible title with **Add agent** and
  **New chat**; four clickable tiles in the order Sessions (open, and how many
  need you; opens `/sessions` filtered to Needs you when any do), Chats (how
  many need input), Agents (offline count when any), and MCP gateway (its
  status, plus "Check the connection" only when it is not ready). Each tile's
  number takes its side-menu icon color: Sessions and Chats green
  (`text-icon-create`), Agents blue (`text-icon-network`), and MCP gateway
  purple (`text-hybro-bro`, also the Connect icon); Recent
  sessions and Recent chats (five each, anything waiting on the owner first,
  then the latest) in one shared row design of title, a grey line of who and
  when, and status; the static mini topology linking to `/network`; and
  Most-called agents with their avatar and call count, last. A model provider
  banner appears under the title when setup needs attention (see Model
  provider setup). Derivation lives
  in `src/lib/overview.ts`. Call counts are the agent's `call_count`, which
  counts every orchestrator agent call from Chat and Sessions once it finishes.
- The header logo links to the Overview at `/`. The public product page is not
  part of this app; `/core` has no route and returns 404.
- `/chat` and `/room/[id]`: chat creation and real-time room workspace.
- `/sessions` and `/sessions/[id]`: client sessions opened through MCP or
  `POST /sessions/messages`, shown as a work-ticket system on two pages. The
  list (`SessionsTablePage`) is a table of Name (the session title, or
  "Untitled session" for sessions opened before titles existed), Status,
  Requester, Created, and Updated. A top search covers names, requesters,
  agents, and session IDs; each column header sorts and has a filter menu
  (name contains, status and requester checkboxes, time presets for Created
  and Updated). The view lives in the URL (`src/lib/session-list-state.ts`,
  `useSessionListUrlState`), which builds each change on the last requested
  URL so quick successive changes are not lost; the query is also kept in
  `sessionStorage` so a ticket's back link returns to the same view. On narrow
  screens the requester and update time move under the name. The ticket page
  (`SessionRecordView`) shows the title, a chronological record, and a
  Details panel (status, requester, agents, created, updated, closed, request
  count, copyable session ID, Close session with confirmation). The record is
  built by `foldSessionEvents` (`src/lib/session-record.ts`) from the room
  event replay (`GET /sse/room/{id}/events`, paged 1000 events at a time up to
  the log's latest sequence) and reads as plain sentences with
  an avatar per actor (a registered agent's own `getAgentAvatarUri` avatar,
  matched by name to the session's agents; a generic agent avatar for the
  external requester or an unmatched agent; "You" for the owner): the requester asked an agent, the agent asked you (each
  question under its header), you answered (your answer text) or rejected,
  the agent replied after its duration (long replies fold behind Show full
  reply), or couldn't finish. A pending question appears at the end of the
  record in the same `HitlResponseBar` card the chat composer uses (pending
  rows from `GET /rooms/{id}/hitl/pending` mapped with the shared
  `hitlLifecycleState`; Send calls `respondToHitlBatch`, Reject cancels through
  `cancelHitl`), and the page opens scrolled to it. The record, events, and
  pending questions poll every 4 s while the session is working or waiting
  and every 20 s otherwise. Times are formatted in English
  (`src/lib/session-format.ts`)
  whatever the browser locale. The sidebar **Sessions** entry carries its own
  badge with the number of sessions in `waiting_on_owner`; `useSessions` polls
  unconditionally every 15 s because sessions are created outside the app.
  Chat history and the Overview Chats tile exclude session containers because
  the backend history filters them out.
- `/network`: the Network workspace (see below).
- `/network/agents/[id]`: AgentCard detail with Chat, Documentation, Share, and
  Remote-only Unregister actions, plus a Subnets section with **Add to subnet**.
- `/network/agents/new`: Remote agent registration. A 409 conflict toast shows the backend `detail.message` (`registrationConflictMessage`).
- `/connect/mcp`, `/connect/api`: Connect pages with a scope picker.
- `/about`, `/pricing`: public pages.

Agent API responses use the backend `AgentRouteResponse` shape (flat
`AgentView` fields such as `name`, `icon_url`, `status`, `source`, and
`inactive_reason`; see `src/lib/types/agent.ts`). Remote agents use the
persisted backend `status` without frontend health probing. `source=bundled`
marks a sample Agent the backend registers at boot; it is labeled **Sample**,
and message badges treat it as local. While a bundled Agent waits for setup,
`inactive_reason` is `provider_not_configured` or `image_model_not_configured`:
`agentStatusView` shows it as **Needs model provider** or **Needs image model**,
and the agent profile and network details show the sentence from
`inactiveReasonText`. Directly discovered Local agents are shown only while
`source === "local"` and status is active. Agent-detail chat actions
write a one-shot `pendingChatHandoff` to `room-ui-store` (optional draft text plus
`seedAgents`) and navigate to `/chat`. That handoff seeds room membership with
the selected Agent and uses `room_default` scope on send; it is not an `@mention`.
The composer consumes any draft text and focuses the input without URL query
parameters or creating a saved subnet. The group selector shows the seeded Agent
name and sends with `room_default`. After the room is created, membership stays
on `room_agent_set` and later turns keep `room_default` unless the user switches
subnets. Selection uses the `room_team` id for room membership (labeled **Room
agents**) and `all_agents` only for true network broadcast; the menu lists the
room membership row and All agents as separate options.

Featured use-case cards on `/chat` stay on the page. A card resolves its declared
Agents against the live catalog, finds the authenticated user's saved preset subnet
by a stable use-case marker, and creates that subnet through `/agentGroups` only
when it is absent. Creation includes an owner-scoped `preset_key`, so the Backend
also guarantees idempotency across concurrent tabs. Existing preset membership
is reconciled to the template's current Agent IDs before selection. The card then
selects the saved subnet in the group selector and prefills the composer; room creation and
navigation do not occur until the user sends the message. A failed create re-reads the saved groups once and selects a subnet a concurrent tab created.

The shared side menu is `PortalSidebar`, a 14.5rem Sidebar with a 4rem
icon-only collapsed state. `NavAgent` renders the five flat `CONSUMER_NAV`
destinations; the active item's icon uses the primary color:

- **Overview** opens `/` (exact match only).
- **Network** opens `/network` and stays active on `/network/*`.
- **Sessions** opens `/sessions` and stays active on `/sessions/*`. It shows a
  count badge for sessions in `waiting_on_owner`.
- **Chats** opens `/chat` and remains active on Room routes. It shows a count
  badge (a dot when collapsed) for rooms with `awaiting_input` status, read from
  the shared `useRoomHistory` query; the accessible name becomes
  "Chats, N need input".
- **Connect** opens `/connect/mcp` and stays active on `/connect/*`.

The same primary menu is used on `/about`, the other existing portal pages, and
the feature pages. Chat and Room routes add a separate History column
immediately to its right on desktop. That column has no collapse control and
stays visible when the primary sidebar collapses. It starts with a secondary
**New chat** action button that opens `/chat`; it is a page action rather than a
navigation item, so it never mirrors the primary menu's active styling.
Network and Connect do not add a second column. The Logo, primary collapse control,
Documentation, Discord/community, and full user panel retain their original
shared components.

`NavAgentItem` supports grouped links (`Collapsible` and `SidebarMenuSub`), but
`CONSUMER_NAV` uses only direct links. Link
selection closes the mobile Sheet. The existing mobile header and Sidebar-owned
Sheet are shared across routes rather than replaced by a workspace-specific drawer.

Opening an Agent response detail collapses only the primary sidebar; the History
column remains visible. Closing the detail restores the primary sidebar state.
On mobile, History is shown inside the existing navigation Sheet to avoid a
two-column drawer, with the same list folding. Agent details use a bottom Sheet. Page content, Room workflows, and
`client_request_id` correlation are independent of the navigation structure.

Chat history uses the lightweight authenticated `GET /rooms`
resource through the shared `useRoomHistory` TanStack Query hook (also used by
the Chats badge and the Overview). Each row shows the shared status indicator:
`queued` is **Queued**, `processing` is **Running** (spinner only without
reduced motion), `awaiting_input` is **Needs input** with the warning color,
and `idle` shows nothing.
Pinned rooms render above Recent rooms; desktop drag handles persist pinned order
through the reorder mutation while Recent is derived from descending
`last_activity_at`. The History header folds and unfolds the conversation list
only; the column and its heading stay in place, so folding never hides the
secondary navigation. Rename, pin/unpin, reorder, and delete mutations update
the query cache
optimistically and roll back on failure. Active room states (`queued`,
`processing`, and `awaiting_input`) are returned in the list payload, so rooms
without active work remain unbadged and the sidebar does not issue per-room
requests. The query refreshes on focus and
polls every ten seconds only while an active state is present. Room creation
cancels any in-flight history read and immediately upserts the new unpinned room
with an activity value newer than the current cache. Auto-send navigation keeps
that optimistic order until the first message succeeds; prefill-only creation
invalidates the authenticated user-scoped query immediately. Sending a message
similarly cancels an in-flight history read before optimistically advancing that
room's activity and status, then invalidates the query after a successful
acknowledgement. These paths share the user-scoped `ROOM_HISTORY_QUERY_KEY`
prefix. `/agents*`, `/networks`, `/access/*`, and `/manage*` routes are
redirect-only paths. `src/lib/routes.ts` is
the canonical route vocabulary for application links.

### Network workspace

`src/app/(portal)/network/page.tsx` renders `NetworkWorkspace`
(`src/components/networks/network-workspace.tsx`) under a `Suspense` boundary
because the workspace reads `useSearchParams`. The workspace composes a
`PageHeader`, `NetworkToolbar` (Topology / Structure `SegmentedControl`, search
input, **Add agent** menu with **Add Remote Agent** and **Scan Local
Agent**, and the single primary **Create subnet** button), and either the
topology stage (`NetworkCanvas` plus canvas controls and `GraphDisplayMenu`) or
`StructureView`. The shared `NetworkDetailsPanel` renders in both views while
`select` is set. `useNetworks` is the only data source.

URL state (`src/lib/network-url-state.ts`, bound by `useNetworkUrlState`) is
parsed, serialized with defaults omitted, and resolved against the loaded
subnet and agent IDs so a deleted subnet scope falls back to **All agents** and a
missing selection closes the panel. Search matching (`src/lib/network-search.ts`)
is a trimmed, case-insensitive substring match over name, description,
provider, skills, tags, and subnet names; regex characters are literal.

`NetworkAgent` (`src/components/networks/types.ts`) is projected from the
backend `Agent` by `projectAgent` in `src/lib/network-agent.ts` and carries
name, description, skills, tags, status, inactive reason, icon, provider,
version, source, and room call counters. Saved member IDs missing from the catalog become
`placeholderAgent` entries marked unavailable or unverified rather than being
pruned.

Topology: React owns SVG identity; `network-canvas-controller.ts` owns one
two-dimensional `d3-force` simulation (link, many-body charge, collide, `forceX`
and `forceY`) and its renderer. There is exactly one node per Agent ID and one
per subnet ID, both drawn at the same radius; type is shown by color from the
`--graph-*` tokens, so the graph follows light and dark themes. Offline and
unverified agents are hollow with a dashed outline. Each unique membership is
one edge. Label rules live in `src/lib/graph-labels.ts`: subnet labels are
visible by default, agent labels appear on hover or focus of the agent, of a
subnet it belongs to, or when selected or related to the selection. The
**Display options** menu toggles "Always show subnet labels" and "Always show
agent labels", persisted under `hybro.network.graph-display` in `localStorage`
(`useGraphDisplayPrefs`). Emphasis dims everything outside the hovered node's
neighborhood; a search highlights matching nodes and their edges without hiding
others; hover takes precedence over search over selection. Motion is physics
only; reduced motion computes the layout before paint. Keyboard: Tab through
nodes, Enter for details, arrows pan, plus and minus zoom, Escape clears, zero
fits all. Controls: zoom out, zoom readout, zoom in, **Fit all**, **Reset node
layout**, Display options.

Structure view (`structure-view.tsx`, `src/lib/structure-groups.ts`): a scope
list (**All agents**, **Unassigned**, then each subnet with a match count and an
info button that selects the subnet) and an agent table with Agent, Status,
Subnets, Source, and **Room calls** columns (tooltip: direct MCP and API sends
are not counted). Default sort is room calls descending; name and status sort
too. **List** shows one row per agent in scope; **By subnet** groups rows under
each subnet and an Unassigned group, so an agent in several subnets appears
under each with a group-qualified key, every instance tinted when selected, and
only the first instance in the Tab order. Rows are buttons that set `select`
without changing `scope`. Below 900px the scope list becomes a select, rows
stack, and the details panel becomes a bottom sheet.

Details panel: a subnet shows name, description, copyable ID (monospace),
members with status indicators, and **Manage members**, **Connect** (opens
`/connect/mcp`), and **Delete subnet** (danger,
confirmed in `SubnetDeleteDialog`). An agent shows avatar, name, version and
provider, description, status, source, room calls, skills, subnet chips, **Open
details**, and the **Add to subnet** menu (`add-to-subnet-menu.tsx`): each
subnet is a checkbox item that adds or removes the agent with a membership-only
`setMembers` write computed from the latest cached members, disabled while a
write is pending, with a toast on success and a Retry item on failure; the last
item **Create subnet…** opens the shared dialog with the agent preselected.

Subnet dialog: `src/components/subnets/subnet-dialog.tsx` is the one create and
edit dialog. It is a two-column form on desktop: **Details** (name, optional
description) and **Members**, where `AgentPicker` offers a search box, a chip
row of the selected agents with remove buttons, and a bordered list of
clickable rows (bear avatar or icon, name, status indicator, one-line
description, checkbox). Unavailable members stay listed and keep their
membership. The footer holds Delete subnet (edit only), Cancel, and the primary
action. The workspace
uses it with `useNetworks` writes; `/chat` and `/room/[id]` use it through
`SubnetDialogHost`, which calls the `agent-group` API directly, invalidates the
`['networks']` query prefix, and reports saves and deletions back to
`useGroupManagement`. Metadata-only edits omit `agents` to preserve concurrent
membership changes. The saved `AgentGroup` is called a **Subnet** everywhere in
the UI; the room membership snapshot (`room_team`) is **Room agents**.

Loading, failed reads, and successful empty lists are distinct states; a failed
catalog read with saved data shows an inline alert with Retry. Writes apply the
server's canonical result and cancel in-flight reads. Dragging a node fixes only
that node's `fx/fy` until release. Node positions are transient and not persisted.

### Connect workspace

`/connect/mcp` and `/connect/api` render one `ConnectPage`
(`src/components/connect/connect-page.tsx`) titled "Connect" with a top-level
MCP / API tab that navigates between the two routes. The page is deliberately
minimal and written for a non-technical reader.

The MCP tab shows the MCP address (`mcpUrl(pageOrigin)`: `{origin}/mcp`), or
only a notice when
`mcp_disabled` is true, with one primary
**Copy address** button, then "Add Hybro to your assistant" with tabs for
Claude Code, Cursor, Codex, VS Code, and Other. Each assistant tab holds only
one snippet with a hint of where it goes and a copy button (a `claude mcp add`
command for Claude Code, `mcpServers` or `servers` JSON, or Codex's
`config.toml` lines), built by `client-setups.ts`. A **Features** section lists
the two capabilities in plain language: discover agents and send tasks. There
is no scope control, no status probe, and no step-by-step prose.

The API tab shows the public runtime `api_base_url` (the page origin when it is
empty) with a copy button, the two
request examples built by `access-material.ts` (discovery via
`GET {api_prefix}/agents/discovery` and sending a message via
`POST {api_prefix}/agents/messages`, each with a copy button), and the same
Features section phrased for HTTP. The page reads no query parameters, and its
examples carry no `group_id`.

Neither tab executes discovery, registers clients, sends messages, or invokes
a model. Copy failures expose manual-copy instructions and select the text;
copied text never includes a real session token. The Overview Gateway tile is
the only place that probes `/hybro-mcp`, through `useMcpStatus`.

### Model provider setup

`useSetupStatus` (`src/hooks/useSetupStatus.ts`) reads `GET {api_prefix}/setup/status`
through TanStack Query (key `['setup', 'status']`), polling every 30 s and on
window focus, because the backend also picks up saves from another instance
and manual file edits. `setupNotice` (`src/lib/setup-notice.ts`) turns the
status into one notice keyed by an error code, with a tone, title, Overview
body, Room Chat body, and action label:

- `unconfigured`: amber "Connect a model provider", action **Set up provider**,
  blocking.
- An expired ChatGPT sign-in (`last_error: oauth_refresh_failed`, or
  `credential_invalid` with OAuth; `isSignInExpired`): red "ChatGPT sign-in
  expired", action **Sign in again**, blocking, because model calls fail.
- `credential_invalid` with an API key (blocking), and `config_invalid`,
  `config_unreadable`, or another `last_error` of a working provider (not
  blocking): amber "Model provider needs attention".

A status that cannot be read, such as a 403 for a non-admin in clerk mode,
shows no notice.

- Overview shows `OverviewSetupBanner` under the title: a gear icon, title,
  body, the action link, and a dismiss button. Dismissal is stored per
  browser in `localStorage` (`hybro.setup-notice.dismissed`) as the dismissed
  code, so a new code shows the banner again; a healthy status clears it.
- Room Chat (`/chat` and the room composer in `ComposerShell`) shows the same
  banner with the Room Chat body and no dismiss button. A blocking notice
  disables the composer (`RoomChatInput` `disabled`, placeholder "Connect a
  model provider to start a chat") and, on `/chat`, the use-case cards; HITL
  answers are not gated.
- A use-case card on `/chat` is disabled with the reason while one of its
  Agents is inactive with an `inactive_reason` (`templateUnavailableReason`),
  such as the Image Generator without an image model. The page reads the full
  agent catalog (`useAgentCatalog`, inactive Agents included), and a settings
  save invalidates that catalog, because the save re-gates the bundled Agents.

Every banner links to `?settings=model-provider` on the current path
(`src/lib/settings-url.ts`). `SettingsDialogProvider` watches that parameter
inside a `Suspense` boundary and opens the dialog on that section; closing the
dialog removes the parameter. The dialog keeps one size on every section
(880px wide, `min(760px, 85vh)` tall) and scrolls the section body inside it.
It has a section menu with icons (Model
provider, Profile, Password, Sessions, About; Profile also holds the danger
zone) and shows one section at a time, keeping all mounted so a sign-in in
progress survives switching. Model provider is the default, and its menu item
carries a dot in the status color. About shows the version from
`package.json`.

`ModelProviderSection` (`src/components/settings/model-provider-section.tsx`)
shows a status card and three numbered cards. `providerPhase` maps the status
and the local login to the card: **Not set up** (amber), **Waiting for
sign-in** (cyan, a login is pending here), **Connected** (green), or **ChatGPT
sign-in expired** (red, with **Sign in again**).

1. Provider: radio cards for OpenAI, Anthropic, and DeepSeek.
2. Connect: for OpenAI, a ChatGPT account / API key switch (ChatGPT account by
   default).
   - ChatGPT account: **Sign in with ChatGPT** opens a tab inside the click,
     then `POST /setup/oauth/start` (a 409 `login_pending` resumes the pending
     login) points it at the authorization URL; a blocked tab leaves a link.
     The waiting panel polls `GET /setup/oauth/{id}` every 2 s, has **Cancel**
     (`DELETE`), and a disclosure with the paste fallback for the
     `localhost:1455` address (`POST /setup/oauth/complete`), open by default
     when the listener is not bound. Once authenticated, the section posts the
     recommended models with the login ID at once, so the login ends
     connected. Connected shows **Sign in again**.
   - API key: a password field and **Check and save** (`POST /setup/api-key`
     with the Models card's values). The key is sent only in the request body
     and cleared after a save. Connected shows "API key saved" and **Replace
     key**; the backend exposes no part of the key.
3. Models: disabled until the shown choice is connected. Text model on the
   left (the first is marked recommended); image model and image size on the
   right, disabled when the choice offers no image model. **Save models**
   posts the models without a credential (`oauth/models` without a login ID,
   or `api-key` with `api_key: null`), then `PUT /setup/image-size` when the
   size changed, which applies after a restart.

A save writes the returned status into the query cache, so banners and composer
gating update at once. Failures show the backend's safe `detail` under the card
that failed.

### Visual system

Colors are HSL triplets in `src/app/globals.css` registered through
`@theme inline`. Both themes use a neutral gray scale with one teal accent
(`--color-primary`), used only for the primary button, the active navigation
icon, selection, subnet nodes, and focus rings. Status tokens
(`--color-status-online`, `-offline`, `-unverified`, `-warning`, `-error`) and
graph tokens (`--graph-subnet`, `-agent`, `-agent-offline`, `-edge`,
`-highlight`, `-label`) exist for light and dark. `src/lib/status.ts` maps
agent, gateway, and room states to a `StatusView` (label, tone, dot style)
rendered by `StatusIndicator`; offline is never the error color, and only
**Needs input** colors its label. `Button` variants are `default` (primary),
`secondary`, `ghost`, `danger` (ghost with destructive text), `dangerSolid`
(confirmation dialogs), `outline` (same look as secondary), `link`, and
`brandTint`; sizes include `icon` (36px) and `iconSm` (32px). `--radius` is 8px
for controls; cards use 12px. `SegmentedControl` is a `radiogroup` with roving
focus, and `PageHeader` renders the page title and one-line description.
Marketing pages keep their gradient CTAs.

## 14. Testing Layout

```text
tests/
|-- setup/
|   |-- vitest.setup.ts
|   |-- msw-server.ts
|   |-- msw-handlers.ts
|   `-- mock-fetch-sse.ts
|-- unit/
|   |-- components/
|   |-- hooks/
|   |-- lib/
|   `-- stores/
|-- e2e/
|   |-- auth.spec.ts
|   |-- authenticated-flows.spec.ts
|   |-- chat.spec.ts
|   |-- error-handling.spec.ts
|   |-- functional-hitl-workflow.spec.ts
|   |-- mcp-connection.spec.ts
|   |-- networks.spec.ts
|   |-- portal-routing.spec.ts
|   |-- room.spec.ts
|   |-- room-internal-dispatch-privacy.spec.ts
|   |-- room-timeline.spec.ts
|   `-- fixtures/
|-- fixtures/index.ts
`-- utils/
    |-- test-utils.tsx
    `-- fetch-without-signal.ts
```

Component tests run in jsdom, whose `AbortSignal` Node's `fetch` rejects. A
component test that lets MSW answer real API-client requests calls
`stubFetchWithoutSignal()` in `beforeEach` and `vi.unstubAllGlobals()` in
`afterEach`.

Unit coverage is broad across components, hooks, API clients, room timeline logic, selectors, and stores, including the Network URL state, search, graph label rules, structure grouping, status mapping, Overview derivation, and access materials. Playwright coverage is organized around auth, chat, room, room timeline, authenticated flows, error handling, portal routing and redirects, the Network workspace, and the Connect pages.

## 15. Current Directory Inventory

```text
src/
|-- app/                 # Next.js App Router routes and layouts
|-- components/          # UI, portal shells, room workspace, conversation renderer
|-- hooks/               # public hooks and room orchestration
|-- lib/                 # API clients, type definitions, selectors, room sync, timeline logic
`-- stores/              # Zustand message, streaming, and room UI stores
```

Important generated/local-only files:

- `tsconfig.tsbuildinfo` is TypeScript incremental build cache and is ignored by `*.tsbuildinfo`.
- `.next/`, coverage output, and test artifacts are not architecture sources.

## 16. Contributor Notes

- Keep route-level code under `src/app/`; shared UI belongs under `src/components/`.
- Prefer the existing shadcn/ui primitives in `src/components/ui/`.
- Use `src/lib/api-client.ts` for backend requests instead of raw fetch wrappers.
- Keep permanent room message data in `message-store`; keep transient stream display data in `streaming-store`.
- Add room realtime behavior through `src/hooks/room/sse-handlers/` and preserve `client_request_id` correlation; ordering belongs to the `RoomReducer`.
- Add turn/timeline display logic under `src/lib/room-timeline/` or `src/lib/selectors/` instead of inside rendering components.
- Document only files that exist under `src/` as current source structure.

## Request-scoped execution mode and Agent scope

Every message send carries two immutable fields alongside `client_request_id`:

```ts
type ExecutionMode = 'direct' | 'supervisor'  // chat sends
type AgentScopeInput =
  | { source: 'mention'; agent_ids: [string, ...string[]] }
  | { source: 'room_default' }
  | { source: 'all_agents' }
  | { source: 'saved_group'; group_id: string }
```

Fast maps to `direct`; Ultimate maps to `supervisor`. The canonical
`run_started` payload also accepts `passthrough`, the model-free mode the
backend uses for session sends; the chat composer never sends it. Changing the selector remains
local until the user sends a message. `SendMessage` emits `mode` and `agent_scope`;
before handing a valid, authorized send attempt to Execution, the backend atomically
persists a changed mode as `room.extend_info.use_supervisor` without replacing other
room metadata. The mode write completes before any Execution acknowledgement, so a
hard refresh initializes the selector from the most recent send attempt, even when
that attempt is an idempotent replay or is later rejected by Execution. A missing
flag defaults to Ultimate. Saved-team member IDs are expanded and authorized
by the backend. `client_request_id` drives optimistic-message replacement.

Quotes are sent only as the structured `message.quote` (`RoomQuoteWire`: text,
source message ID, source kind, sender display name, source Agent ID);
`SendMessage` sends `extend_info: null` and no plain quoted text. The quote
button (`useTextSelectionQuote`) reads the source from the nearest
`data-quote-message-id` region: agent results and the Agent detail pane
(`agent`, including canonical Agent Card IDs `orchestrator:{run_id}:inv_…`),
synthesis (`synthesis`), and terminal turn copy (`user_turn`, only when the turn
has a persisted user message; placeholder and optimistic `cr:` turns are not
quotable). The backend validates the source, persists the quote, and returns
`quote_id`; stored messages carry `extend_info.quoted_text`,
`quoted_sender_name`, and `quote_id`, which `convert-api-message.ts` maps for
`UserMessageBlock` and the detail pane. The optimistic user message shows the
quote locally until the server message replaces it.

Debate is not a `ChatMode`, Room setting, request flag, or handled SSE event. The
ModeSelector retains one disabled `Debate (Coming Soon)` row as display-only UI;
it has no selection handler and can never create a Debate request.

## HITL questionnaire composer

An authoritative open HITL interaction replaces the normal room composer; the UI
never stacks a second form over a disabled chat input. `selectPendingHitls` groups
questions by durable `interaction_id`, and `HitlResponseBar` keeps drafts keyed by
stable `request_id` (unsent drafts survive a remount of the bar, such as a
reconnect or snapshot refresh, per `interaction_id` until submitted), presents
one question at a time, and submits directly from the
last answer step (no separate review screen). When the open answer surface mounts —
including after "Applying…" is replaced by a follow-up interaction — the bar
autofocuses the text/date control (or the prompt heading for choice prompts). While
answers are applying, the bar
auto-refreshes pending HITL so a follow-up open prompt replaces the recovery UI
without a manual "Check status" click; that button remains only for
`delivery_uncertain`. When both an applying recovery and a new open prompt exist,
the composer prefers the open prompt. The client submits the complete answer
inventory to `POST /rooms/{conversation_id}/hitl/respond-batch`, preserving
`client_request_id` for run correlation. A successful answer acknowledgement
confirms the submitted answers, but resumes processing only if the current exact
User/client root and interaction still permit it. Terminal canonical state, a
subsequent waiting interaction, pending cancellation, or a newer processing root
cannot be overwritten by a late HTTP acknowledgement; an acknowledgement for a
room already left cannot mutate the newly selected room. Roots without a canonical Turn resume through the message-store path, subject to
current-root and terminal-state checks.
When several questions share one A2A
Agent `message_id`, each question receives a deterministic interaction-and-request-scoped
MessageStore identity while retaining the wire message identity separately. This is
also mandatory for singleton interactions: sequential one-question rounds from one
Agent call never overwrite the prior round's entity. A raw-message projection for the same request is resolved when the scoped entity arrives, avoiding
a duplicate composer item without mutating the canonical Agent Card.
SSE, snapshot, and `/hitl/pending` overlays use the same composite projection and
request index, so sibling questions—and later interactions that reuse a stable
question ID—cannot overwrite one another; request projections
are composer-only and never create duplicate Agent Cards. Exact interaction/request
state merges are monotonic: a REST recovery row with an equal, missing, or older
version cannot clear a saved answer or regress responded/applying/applied state to
open, while a genuinely new interaction identity can open normally. Concurrent
submits are fenced per room and interaction. A 409/410 triggers DB reconciliation,
a successful authoritative `/pending` overlay, and a best-effort forced canonical
snapshot reconnect, but is never inferred as success from local state; identical
durable retries already return success from the backend, so typed conflicts remain
visible. A failed pending read is an explicit refresh error. Delivery uncertainty,
routing failure, timeout, and applying states remain explicit. The frontend has no single-request response
pipeline; even singleton interactions use the batch endpoint. `file` and
`unknown` prompt types are accepted on the wire and share one unsupported-state
renderer.

Pending HITL hydration is authoritative only after a successful `/pending` read.
`HydrateRoomResult.hitlFetchFailed` distinguishes a degraded read from a real empty
set so existing input requests are not marked resolved during an outage. Initial
hydration may mark hydrated open HITL absent from pending as resolved via
`markResolvedHitlFromHydrationBatch`. Live applying refresh (`hitl_overlay` /
composer auto-refresh) only clears local *applying* projections that are no
longer pending — open `input-required` prompts are left alone so a brief empty
pending window cannot dismiss a still-open UI. Resolved answers remain
non-actionable timeline summaries sourced from durable message projection. The
composer keeps answered siblings as context only inside their active questionnaire;
its queue badge counts distinct other actionable/open interactions and excludes
answered, applying, delivery-uncertain, and routing-failed rows.

### Canonical HITL and private Agent details

Canonical snapshot HITL requests normalize snapshot-only timestamp metadata before the
strict live-wire validator is applied. Before any Turn or message store replacement,
the complete top-level request set is checked against the exact Turn interaction
inventory, User/client root, request fields, and any existing projection root; a
contradiction rejects the whole snapshot and requests recovery without advancing the
watermark. This lets snapshot-first hydration recreate the pending message entity and
composer response controls atomically. The Composer exclusively owns
question text and answer controls through the shadcn `Questionnaire` primitive composed
inside one flat bordered card (`HitlResponseBar`, shared with Sessions). Several
questions in one interaction show as header tabs that mark answered questions; choices
show their labels, descriptions, and number shortcuts; `allow_other` adds an Other row
with its own text field; optional questions offer Skip (or Skip and send on the last
one) and are left out of the submitted answers. A single approval or yes/no
question is a decision card instead: the prompt with two buttons (Approve and a red
Reject, or Confirm and Decline) that send the answer in one click. Presentation fields from the
`hitl_request` frame, snapshot requests, and pending rows are parsed once by
`parseHitlPresentation` (`src/lib/hitl/presentation.ts`) into the message entity's
`hitlPresentation`. On other cards the red Reject asks for confirmation before
canceling the interaction. While the Turn awaits input,
the canonical Final slot stays empty and the Conversation Body retains only the
truthful `Asking you · Waiting for input` Trace event. Message-store HITL projections for the same active canonical interaction are suppressed from the Body so
question content is never duplicated.

Canonical cards use their opaque message identity only to derive the authenticated
`run_id + public_call_id` detail request. The detail pane fetches private output from the
room-authorized backend endpoint and does not reinterpret the opaque card/message ID as
an Agent profile ID. Public message state remains output-free. The pane's status and
request text come from the canonical Turn activity whenever it has that call, even if a
MessageStore entity shares the card id (snapshot HITL recovery anchors an open question
on a status-less shell entity there); the entity is used only when no activity exists.
