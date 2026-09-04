# Hybro Frontend Architecture

> Last scanned: 2026-06-04
>
> Source of truth: current repository files under `src/`, `tests/`, and root config files. Historical design notes in `docs/` are not treated as current architecture.

## 1. Overview

Hybro Frontend is a Next.js App Router application for the Hybro multi-agent platform. A single unified portal provides chat, agent inventory and registration, room timelines, HITL replies, file attachments, pricing, and public information pages.

The app talks to the backend through REST APIs and room-scoped Server-Sent Events (SSE). The room UI uses normalized message state, transient streaming buffers, selector-driven view models, and a conversation renderer built around turns rather than raw message rows.

## 2. Current Scan Summary

| Area | Current count / source |
|---|---|
| Source files | 263 files under `src/` |
| Test/support files | 103 files under `tests/` |
| App Router files | 28 files under `src/app/` |
| Component files | 109 files under `src/components/` |
| shadcn/ui primitives | 27 files under `src/components/ui/` |
| Conversation components | 17 files under `src/components/conversation/` |
| Hooks | 41 files under `src/hooks/` |
| Library modules | 65 files under `src/lib/` |
| Stores | 19 files under `src/stores/` |

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
| Testing | Vitest, Testing Library, MSW, Playwright |

## 4. Root Config And Tooling

| File | Purpose |
|---|---|
| `package.json` | npm scripts and dependency manifest |
| `package-lock.json` | locked dependency graph |
| `.nvmrc` | recommended Node version (`20.19`) |
| `next.config.ts` | Next.js configuration |
| `tsconfig.json` | TypeScript compiler configuration |
| `eslint.config.mjs` | ESLint 9 configuration |
| `vitest.config.ts` | unit/integration test configuration |
| `playwright.config.ts` | e2e test configuration |
| `components.json` | shadcn/ui generator aliases, Tailwind CSS entry, icon library |
| `postcss.config.mjs` | Tailwind/PostCSS pipeline |

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

`npm run lint` currently invokes `next lint`; with the current Next.js CLI this may fail before linting by treating `lint` as a project directory. Build and tests are the practical validation gates until the lint script is updated.

## 5. App Routing

Routes are defined under `src/app/`.

```text
src/app/
|-- layout.tsx
|-- globals.css
|-- favicon.ico
|-- robots.ts
|-- sitemap.ts
|-- privacy/page.tsx
|-- (auth)/
|   |-- layout.tsx
|   |-- sign-in/[[...sign-in]]/page.tsx
|   `-- sign-up/[[...sign-up]]/page.tsx
`-- (portal)/
    |-- layout.tsx
    |-- page.tsx
    |-- about/page.tsx
    |-- core/page.tsx
    |-- agents/page.tsx
    |-- agents/[id]/page.tsx
    |-- agents/new/page.tsx
    |-- chat/page.tsx
    |-- pricing/page.tsx
    |-- room/[id]/page.tsx
    `-- manage/
        |-- page.tsx
        |-- agents/page.tsx
        |-- agents/[id]/page.tsx
        `-- agents/new/page.tsx
```

### Unified routing

The application exposes one unprefixed route tree. Agent inventory, details,
and registration live at `/agents`, `/agents/[id]`, and `/agents/new`; chat
routes live at `/chat` and `/room/[id]`. There is no host-based route rewrite.
Legacy `/manage` and `/manage/agents*` paths redirect to their canonical
`/agents*` equivalents.

The `/agents` inventory merges visible registered agents with locally available
agents. Its **Discover Local Agents** action calls the authenticated
`POST /api/v1/local-agents/discovery` endpoint, waits for the backend discovery
cycle, and then invalidates both agent inventory queries. Directly discovered
`source=local` agents are displayed while active.

### Provider hierarchy

`src/app/layout.tsx` wraps the app with:

1. `ThemeProvider`
2. `QueryProvider`
3. `Toaster`
4. `CookieBanner`

The portal layout adds `BannerHost`, `SidebarProvider`,
`SettingsDialogProvider`, `PortalSidebar`, and `PortalHeader`.

## 6. Component Organization

```text
src/components/
|-- ui/                       # shadcn/ui primitives
|-- conversation/             # turn/timeline rendering system
|-- composer/                 # chat composer shell and HITL response bar
|-- portal/                   # unified sidebar/header/footer, Core page, Manage navigation
|-- open-source/              # Core page terminal animation
|-- providers/                # React Query provider
|-- settings/                 # settings dialog sections and helpers
|-- room-page-shell.tsx       # room workspace shell
|-- room-chat-input.tsx       # composer input, mentions, uploads
|-- group-selector.tsx
|-- group-management-modal.tsx
|-- consumer-agent-card.tsx
|-- artifact-list.tsx
|-- artifact-renderer.tsx
|-- attachment-preview.tsx
|-- markdown-content.tsx
|-- part-renderer.tsx
|-- mode-selector.tsx
|-- nav-agent.tsx
|-- nav-user.tsx
|-- nav-docs-button.tsx
|-- nav-discord-button.tsx
|-- require-auth.tsx
|-- use-case-card.tsx
|-- cookie-banner.tsx
|-- theme-provider.tsx
|-- theme-toggle.tsx
|-- logo.tsx
|-- icons.tsx
`-- video-embed.tsx
```

### Conversation renderer

The current room UI is centered on `src/components/conversation/`:

- `ConversationMessageList.tsx`: top-level message/timeline list.
- `TurnRenderer.tsx`, `TurnBody.tsx`: turn-level rendering.
- `UserMessageBlock.tsx`, `UserAttachmentCard.tsx`, `UserAnswerCard.tsx`: user-side turn content.
- `AgentCard.tsx`, `AgentContentBlock.tsx`, `AgentResultContent.tsx`, `AgentIndex.tsx`: agent response presentation. Multi-agent turns with LLM synthesis (`llm_synthesis`) show the combined answer in the primary surface and compact per-agent index rows below. Supervisor DONE without synthesis (`deterministic_done`) shows the digest intro in the primary surface and full per-agent bodies in the expanded `AgentIndex`. Substantive `summary-*` content classifies as `llm_synthesis`; the short coordinator digest stub (`"N agents responded. Expand below…"`) classifies as `deterministic`.
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
|-- useGroupManagement.ts      # saved groups and room group selection
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

- Reads `roomId` from the route.
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

Turn-terminal state now arrives as durable-confirmed frames over the
snapshot-driven room stream; the former `infer-turn-terminal-status.ts`
inference module has been removed.

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
canonical replacement, `RoomReducer` clears the incumbent processing/send guard
only when its stored User message ID and client request ID exactly match a
terminal canonical root. This repairs a missed `run_settled` at or below the
snapshot watermark without unlocking unrelated active legacy work in a mixed
room.

Snapshots without both capability fields remain pure legacy snapshots and
continue to hydrate the incumbent message, streaming, and diagnostic trace
stores. Mixed snapshots still hydrate legacy logs/trace/HITL for historical
legacy roots; suppression is exact-root only, and canonical `awaiting_input`
restores every actionable request-scoped HITL entity plus the dispatcher-owned
`hitlRequestIndex` through the production `RoomReducer` snapshot path, without
recency inference. Multiple requests may retain the same canonical Agent Card
message identity but never share their client-side projection identity. During
rolling-deploy recovery, an older legacy-shaped HITL
request may recreate only its composer message projection when its
`client_request_id` and related User message exactly match one canonical Turn;
it never infers or mutates canonical Turn lifecycle state. This keeps rooms
written before the producer upgrade answerable while new events use the strict
canonical contract.
Canonical renderers never consume legacy prose or diagnostic trace nodes. This
is a per-User-request mutual-exclusion boundary:
exact `run_started`/snapshot root binding selects the canonical renderer;
otherwise the legacy renderer remains in use.

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
Its trigger has no visible “Turn Trace” label or divider: the left-aligned status
is `Running`, `Waiting for input`, or green `Finished`, followed immediately by
the whole-Turn duration. Failed and canceled terminal Runs also display
`Finished`; child action rows retain their truthful failure/cancellation state.
The current Assistant safely renders through `MarkdownContent` in the final slot;
`message_end(final)` remains provisional until the exact durable response commits
it. Canonical terminal duration is server-authoritative; live canonical and legacy
durations tick from the Turn start, while terminal legacy duration uses its last
durable observation. Active traces initialize open, historical terminal snapshots
initialize collapsed, and focus-safe auto-collapse is consumed once. Canonical and
legacy Trace surfaces use the answer body's 1rem/1.75 typography and left edge,
with no internal card borders. Tool/Agent-call rows use the Bot marker icon;
completed rows and the terminal `Finished` status use the shared success green.
Trace and Agent-call lists grow within the conversation's single scroll owner;
they do not create clipped nested scroll regions when a Turn has many calls.
Only active content is live-announced, and motion has reduced-motion fallbacks.
HYBRO AI summary/presenter entities still own final-answer data but no
longer render an Agent Card; only actual delegated Agent executions appear as
Agent Cards.

Trace and Agent Cards are separate UI projections of the same canonical
`TurnProjection.activity` Tool row selected by `(run_id, tool_call_id)`;
neither MessageStore task entities nor TraceStore nodes own execution state.
Repeated calls remain separate and activity summaries count calls, not unique
Agent names. Both surfaces expose the same `data-call-id` and normalized
`data-status`, including cancellation.

Canonical `model_decision` events fold into `TurnProjection.activity` entries of
`kind:"decision"` (validated in `contract.ts`, folded in `fold.ts`, rendered by
`CanonicalTurnTrace`). Decisions make the model-first HITL loop visible in the
Trace: `interaction_received`, `answered_from_context`, `no_progress`, and
`degraded_to_user`. Only backend-computed summaries and sanitized Agent labels
are projected; raw model reasoning is never surfaced.
 Cards prefer the durable sanitized root
Agent name, and generic,
blank, opaque, or internal update labels cannot downgrade it during live,
snapshot, or database hydration. For old records only, the exact opaque-call
Tool activity label is the final safe fallback. Live and snapshot guards require
`run_id + opaque_public_call_id` and
the exact derived `orchestrator:{run_id}:{opaque_public_call_id}` message
identity. Any `task_*`, partial-agent, raw artifact, or compatibility processing
frame is excluded from canonical lifecycle state only when it matches an exact
canonical User/client root; canonical capability elsewhere in the same room does
not suppress unrelated legacy activity. Malformed duplicate status owners
trigger snapshot recovery. Before `run_started`, the latest optimistic
User root renders a preparation-only live Trace shell from local send state so
HTTP/preflight latency never leaves a blank conversation body. It owns no cards,
final content, or server lifecycle and is atomically replaced when the exact
canonical root arrives. Other message-derived turns remain User-only. Memoized
Turn boundaries and card-ID-scoped selectors keep
active deltas from rerendering historical Turns. Canonical cards without a
real Agent profile ID render a non-link label (never a fabricated
`/agents/orchestrator…` route), and clickable cards contain no nested interactive
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
An absent `parts` field alone enables the
legacy `output` fallback during a rolling deploy. Descriptors carry the durable room-file ID,
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
The HTTP response and child-task updates cannot clear that state. Durable terminal
`run_settled` (canonical) or `processing_status` (legacy) owns cleanup. After
refresh, the existing room `active_runs` response restores `canceling` and
hydrates both message and client-request correlation from the triggering user
message; the canonical room-event snapshot schema is unchanged.

### `src/stores/streaming-store/`

The streaming store contains transient live artifact/text buffers. It is intentionally separate from `message-store`: streaming artifacts are displayed live, then cleared after DB reconcile or task checkpoint persistence.

Live buffer text is derived via `extractStreamTextFromArtifacts`, which concatenates all text-only artifacts in emission order (matching backend final assembly). Persisted entity text still uses `extractTextFromArtifacts` (last text-only artifact) for thinking + answer agents — this asymmetry is intentional today and disappears under the AG-UI roadmap (`REASONING_*` events split thinking from answer at the wire layer).

**Streaming invariants** (enforced after the convergence plan in [`docs/STREAMING_UI_ISSUES_AND_FIXES.md`](STREAMING_UI_ISSUES_AND_FIXES.md)):

- **I1** — One live ingest pipeline. All live streaming text flows through `streaming-store.append(message_id, …)`. `agent_response_partial` is a compat shim that first creates a correlation-preserving message shell, then maps `content_delta` to a synthetic artifact and calls the same append; a live buffer is never left without a renderable turn entity.
- **I2** — Live buffer key is always `message_id`. `client_request_id` is correlation/cleanup metadata, never a buffer key or display merge dimension.
- **I3** — Live text equals persisted text. `extractStreamTextFromArtifacts` over the live artifact list equals backend `extract_parts_from_artifacts` over the persisted artifact list at terminal.
- **I4** — Detail pane content for terminal entities comes from `message-store`, never from the live buffer (strict terminal guard in `selectAgentResponseDetail`).
- **I5** — Per-agent terminal SSE clears that message's buffer only. Legacy Turn-level clear remains owned by terminal `processing_status`; canonical Turns do not use that frame for lifecycle or content.
- **I6** — `streaming-store/append` does not import `mergeArtifacts` from `message-store/upsert`. Live merge is `mergeStreamArtifacts` (disjoint-segment push, prefix-relation replace).
- **I7** — Streaming UI (badge, cursor, Streamdown caret) is driven only by an incomplete live buffer while the agent view-model status is `working`. Terminal agent status always wins over a stale buffer. Late `artifact_update` frames after terminal `task_update` are ignored and any leftover buffer for that `message_id` is cleared.

**Conversation markdown normalization** (`src/lib/markdown/`, applied in `MarkdownContent` when `className` includes `conversation-markdown-body`):

- **Pre-parse** (`preprocessConversationMarkdown` in `normalize-conversation.ts`, `normalize-agent-list-markers.ts`, and `split-inline-ordered.ts`) runs before Streamdown: rewrites common agent list-marker mistakes (`1. • …` and bare `•` lines → `-` bullets; skips fenced code); inline ordered split for run-on `1. foo 2. bar` lines on list-item lines only (skips prose like `See step 1. For details`); supervisor-shaped lines (`3. **#3 — …`) only split before the next `N. **#N` marker so prose like `adoption in 4. The era` stays one item; inline splits are deferred while `isStreaming` is true; ATX heading lines and fenced code are skipped; bare `###` markers on their own line are folded into the next content line. Section-label promotion and list renumbering are **not** done in pre-parse.
- **Render-time remark plugins** (`conversation-remark-plugins.ts`, passed to Streamdown `remarkPlugins`) operate on the mdast tree Streamdown actually renders — no remark-stringify/reparse gap. The bundle includes `remark-gfm` because Streamdown replaces (not merges) default plugins when `remarkPlugins` is set. For conversation markdown, `parseMarkdownIntoBlocksFn={(md) => [md]}` parses the full message in one pass (including during streaming) so section/list surgery is not split across Streamdown blocks. Plugin order: `remark-gfm` → `remarkSplitSectionLists` → `remarkNestAdjacentBulletLists` → `remarkCoalesceOrderedLists` → `remarkAssignOrderedListStarts`.
- **Rehype pipeline** (`markdown-content.tsx`): custom `rehypePlugins` must include Streamdown's `defaultRehypePlugins` (`rehype-raw`, `rehype-sanitize`, `rehype-harden`) before `rehype-highlight`, because passing `rehypePlugins` replaces rather than merges defaults.
- Agent `message_text` is stored and returned by the backend as produced; markdown repair is client-side only. Hybro-controlled LLM synthesis prompts (`backend/common/prompts/markdown_response_format.py`) encourage `###` section headers for cleaner source text; the frontend AST pipeline is the universal compatibility layer for third-party agents.
- Completed agent tasks treat backend-projected `message_text` as the human-readable response and render non-text Task artifacts alongside it. Raw `TaskStatus.message` and Task history are never promoted by the client; when public `message_text` is absent, a completed text-only artifact remains the compatibility fallback.
- Agent entities retain the backend-published `extend_info.public_dispatch_text` separately from the short `public_task_label`. The main Agent Card keeps the compact label; the expanded response detail prefers the full dispatch text in its existing collapsible task region, followed by the agent `message_text` and artifacts.
- The renderer maps top-level `<ol>` elements to `style.counterReset = 'conv-section-ol <start - 1>'` from the mdast `start` prop. CSS counters in `conversation-tokens.css` provide visible `N.` markers for ordinary lists; items that already start with `#N` (supervisor-style `**#1 — …` rows) get `conv-hash-numbered-item` and suppress the extra counter.

Display helpers in `src/lib/streaming/display.ts` split live **text** (buffer) from **non-text artifacts** (files/data) during stream so the detail pane and activity strip can show file attachments while text is still growing. `AgentResponseDetailPane` uses `useDetailPaneScroll` with ChatGPT-aligned behavior: first open scrolls to top; reopening the same message restores saved scroll from `room-ui-store.detailScrollByMessageId`; optional tail-follow when pinned near bottom during stream; no scroll reset on stream complete; detail body uses `overflow-anchor: none`.

**Main feed scroll (`ConversationMessageList`):** The logical bottom of the feed is the `[data-content-end]` sentinel after the last turn — not the full `scrollHeight`, which includes the fixed `[data-scroll-spacer]` below it. `content-end-scroll.ts` centralizes `scrollToContentEnd`, `isNearContentEnd`, and snapshot `atBottom` detection. On first open (no saved position), the list scrolls to content-end. When revisiting a room, the last scroll position is restored from `room-ui-store.conversationScrollByRoom` (including an `atBottom` flag so rooms left pinned to the latest message still land at content-end). Scroll snapshots persist across `resetRoom` and are cleared on `resetAll`. Every user message bubble uses native CSS sticky (`.conversation-user-sticky { position: sticky; top: 0 }`) for both live and completed turns. On send (`localSendSeq`), `useScrollUserMessageOnSend` scrolls the sticky wrapper into the top of the scrollport once so sticky engages; CSS sticky then holds the question visible while HYBRO/agent content grows below. While the room is processing (`turnLive`), `tailFollowRef` (detail-pane-style sticky follow flag) stays true once the user reaches content-end and only clears on explicit user scroll (wheel/touchmove with movement, or `scrollTop` decrease while not programmatic), not when content growth temporarily moves the viewport away from the threshold. The 150ms programmatic-scroll suppress window only skips scroll-position re-enable inference and snapshot writes — it does not block user cancel. `usePrimaryStreamScroll` and layout-driven follow scroll whenever `tailFollowRef` is set. `.conversation-scroll-area` and `.conversation-frame` use `overflow-anchor: none`. Users who scroll away see the scroll-to-bottom button, which re-enables tail follow.

Known issues and the convergence plan are in [`docs/STREAMING_UI_ISSUES_AND_FIXES.md`](STREAMING_UI_ISSUES_AND_FIXES.md).

## 10. SSE And Room Sync

SSE handling is split into small handlers under `src/hooks/room/sse-handlers/`.

```text
src/hooks/room/sse-handlers/
|-- dispatch.ts
|-- correlation.ts
|-- pending-turn-buffer.ts
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

`src/lib/types/sse.ts` defines the final room SSE frame envelope as `{ type, room_id, timestamp, data }`. The handled room frame types are:

- Connection/system: `connected`, `heartbeat`, `error`, `run_event`, `cancellation`.
- Turn and task updates: `processing_status`, `task_submitted`, `task_update`, `artifact_update`.
- Agent output: `agent_response_partial`, `agent_response`.
- HITL and orchestration: `hitl_request`, `hitl_response`.

Legacy `user_message`, `turn_event`, `hitl_input_requested`, and `hitl_status_update` frames are not part of the handled room SSE contract. Unknown frame types are ignored after a debug log.

`createSSEDispatcher` first offers ordered deltas to the canonical fold. Known
canonical `run_event` payloads are closed runtime-validated unions; unknown
subtypes remain rolling-deploy tolerant and continue only through the legacy
handler. Canonical `processing_status` compatibility frames are ignored only
after their allowlisted status, exact nonempty User/client roots,
`details: null`, and absence of Agent/free-text fields validate; malformed
adapters request snapshot recovery.
Matching final responses and Agent-card task IDs enter the Turn projection by
exact durable identity before their normalized message entities are updated.

Canonical `hitl_request` and `hitl_response` require exact `run_id`,
`client_request_id`, related User-message roots, the opaque public Tool message
ID, and durable interaction/request/question identities. The ordered
`run_waiting_input` and `run_resumed` controls make the interaction and Turn
state change explicit; they reconstruct Turn-owned HITL history without recency
or REST inference. The producer persists responses and `run_resumed` before it
can dispatch a continuation that might immediately ask a follow-up question.
The legacy handler and pending REST overlay remain rolling-deploy recovery only.
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

**Multi-agent turn completion fallback:** Per-agent terminal SSE alone does not complete a multi-agent turn. The backend emits a `turn_completion_kind` field (`"synthesis"` or `"deterministic"`) as part of the COMPLETED `processing_status` SSE `details` and persists it on the user message `extend_info` before emitting the event. The frontend stores this as `turnCompletionKind` on the user `MessageEntity`.

`deriveFinalAnswer` promotes to `deterministic_done` when `turnCompletionKind === 'deterministic'`, a deterministic `summary-*` digest entity is present, mixed terminal agents resolve without synthesis, or `turnCompletionKind === 'synthesis'` was persisted but no synthesis evidence ever appeared (backend queue path can set synthesis kind even when no LLM step runs). When synthesis is actively in flight — processing logs, synthesis ephemerals, or a working empty LLM summary — the turn stays `pending`/`synthesizing` until content arrives. When `turnCompletionKind` is absent, supervisor turns stay pending until backend truth stamps or synthesis signals arrive; non-supervisor and mixed-failure paths use entity evidence and `isDeterministicCompletionExpected`.

`turnCompletionKind` is delivered via three redundant paths: (1) SSE `processing_status` COMPLETED `details`, (2) DB `extend_info.turn_completion_kind` on the user message (read during hydration/reconcile), (3) `inquiryActiveRuns` response (queried during truth-check when `trigger_message_id` is passed and no active run matches). This ensures correctness across SSE drops, page refreshes, and reconnects.

`hasActiveSynthesisGap` treats positive synthesis evidence (`turnPhase: 'synthesizing'` on processing logs, log lines containing "synthesiz" or "compiling summary", synthesis ephemerals, or a working non-deterministic summary agent) as synthesis in progress and drives `phase: synthesizing`. Stale synthesis logs are ignored once all real agents are terminal and at least one failed without an in-flight LLM summary entity — partial-failure turns promote to `deterministic_done` instead of staying on **Working**. While the room run is still open (`!turnTerminalStatus`) and live-run evidence exists (transient processing logs, an active run trigger id, or room processing still active on the latest turn), multi-agent turns with all real agents terminal stay `status: active` and show **Synthesizing** until terminal processing_status or final answer content arrives. Hydrated historical turns without live-run evidence remain `completed`. Supervisor turns without `turnCompletionKind` stay pending in `deriveFinalAnswer` until synthesis resolves or backend truth stamps — preventing a flash of expanded `deterministic_done` bodies before synthesizing starts. Delegation logs alone on hydrated turns are **not** synthesis evidence.

**Entity-first invariant:** When a non-deterministic `summary-*` entity has substantive LLM content, `deriveFinalAnswer` returns `llm_synthesis` even if `turnCompletionKind` was incorrectly stamped or inferred as `deterministic`. `turnHasSubstantiveLlmSynthesis` blocks backend-truth stamping and debounced recovery from overwriting synthesis turns.

Backend-truth stamping (`turn-terminal-stamp.ts`) uses `isBackendRunConfirmedNonSynthesisCompletion` when `inquiryActiveRuns` reports no active run: broader than `isDeterministicCompletionExpected` so supervisor no-synthesis turns can stamp even without a pre-set kind; it infers `turnCompletionKind: 'deterministic'` on stamp when inquiry did not return a kind. `isDeterministicCompletionExpected` still gates live `deriveFinalAnswer` promotion to avoid premature expand. Backend-truth passes `turnCompletionKind` atomically alongside `turnTerminalStatus` and queries `inquiryActiveRuns` with `trigger_message_id` when the SSE path was missed.

**Debounced recovery (`shouldScheduleTurnTerminalRecovery`):** schedules the 1.5s backend-truth check on terminal `FAILED`/`REJECTED`/`CANCELED`, or `COMPLETED` when all real agents are terminal. Summary-agent `agent_response` frames (`summary-*` / coordinator summary agent id) never schedule recovery. When `processing_status` COMPLETED arrives with `turn_completion_kind: 'synthesis'` after a prior deterministic stamp, the handler monotonically upgrades `turnCompletionKind` on the user entity.

Backend queue/resume/supervisor completion paths set `turn_completion_kind` from `_emit_unified_summary` return value (`synthesis` when LLM/supervisor synthesis is used — including when a duplicate `summary-*` row is skipped for fewer than two trajectory responses — and `deterministic` when the digest path runs). The kind is persisted on the user message after durable terminal `processing_status` COMPLETED wins (so a cancel CAS winner does not leave a stale kind). Terminal `processing_status` details include `turn_phase: 'terminal'`; synthesis stage emits `turn_phase: 'synthesizing'`. The frontend stores `turnPhase` on `ProcessingStatusLogEntry` when appending logs from SSE `details`.

**Hydrate repair for stuck `system:hybro`:** Older runs could leave `system:hybro` with answer text while task state stayed `submitted`. When `turnTerminalStatus === 'completed'` and the summary agent has non-empty content, `buildAgentResult` treats it as completed so refresh does not spin on Synthesizing. Live turns (no terminal stamp yet) keep contentful `submitted`/`working` as working so mid-stream synthesis still shows Synthesizing. `deriveFinalAnswer` returns `llm_synthesis` / Synthesized when the orchestrator has answer text and either its status or `turnTerminalStatus` is completed.

**Live streaming (target):** `artifact_update` is the primary path into `streaming-store.append(message_id, …)`. `agent_response_partial` (rare in production; delivery-layer alias) should shim into the same message-keyed append — not a separate turn-level buffer. **Checkpoints:** terminal `task_update` and final `agent_response` write to `message-store`, read the message-scoped buffer for fallback text, then clear that message's stream buffer (turn-level clear only on turn complete).

SSE artifact conversion is defensive at the client boundary. `task_update`
`parts` and `artifact_update` payloads drop legacy inline `file.bytes`; file
parts are renderable when they carry a durable `file_id` or a URI. Canonical
artifact events and synthetic terminal `${messageId}-parts` projections are
reconciled by stable part identity (`file_id`, then SHA-256, URI, or canonical
data), so live detail matches post-refresh hydration without collapsing distinct
same-name files. `file_unavailable` data renders as a safe unavailable-output
notice and is not counted as a file. `PartRenderer` does not create `data:` URLs
from inline bytes, so stale or malicious legacy SSE cannot surface private file
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
- While a turn is processing, polls backend run truth every five seconds until a
  terminal state is observed. A transient poll/reconcile failure does not stop
  later checks, so a missed terminal SSE cannot leave the turn spinning until refresh.

## 11. Timeline And View Models

Timeline construction lives under `src/lib/room-timeline/`.

```text
src/lib/room-timeline/
|-- build-turns.ts
|-- derive-final-answer.ts
|-- event-log.ts
|-- map-result-display.ts
|-- message-groups.ts
|-- turn-agent-terminal.ts
|-- turn-live-shell.ts
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
- Wraps HTTP failures in `ApiError`.
- Logs client errors as warnings and server/unexpected errors as errors.

API modules live in `src/lib/api/`. Consumers import the specific module they
need rather than a shared barrel, keeping client bundles scoped to the active
feature:

```text
src/lib/api/
|-- agent.ts
|-- agent-group.ts
|-- room.ts
|-- sse.ts
|-- inspection.ts
|-- files.ts
`-- hitl.ts
```

Type definitions live in `src/lib/types/`:

```text
agent.ts, agent-group.ts, attachments.ts, chat-mode.ts, error.ts, index.ts,
quote.ts, request.ts, response.ts, sse.ts
```

Other library modules:

- `auth.ts`: local self-hosted identity and authentication-header adapter.
- `routes.ts`: canonical public and management route vocabulary.
- `utils.ts`: `cn`, `getApiUrl`, and formatting helpers.
- `consumer-nav.ts`, `nav-items.ts`: top-level navigation configuration.
- `system-agents.ts`: system/supervisor agent classification.
- `agent-avatar.ts`, `agent-icon-utils.ts`, `file-icon-utils.ts`: display helpers.
- `api/files.ts` and `hooks/useRoomFile.ts`: authenticated room-file upload,
  download, and preview blob lifecycle. The authenticated same-origin download
  path normalizes `NEXT_PUBLIC_API_PREFIX` to the same leading/trailing-slash
  form used by the Next rewrite.
- `selection-plain-text.ts`: quote/selection text extraction.
- `streaming/display.ts`: streaming display helpers.

### Send Message Routing

`src/lib/api/room.ts` sends every room message with a required
`client_request_id`, request-scoped `mode: 'direct' | 'supervisor'`, and one
canonical `agent_scope` discriminated union:

- Mention scope: `{ source: 'mention', agent_ids: [...] }` with a non-empty ID tuple.
- Room default: `{ source: 'room_default' }`.
- All visible active Agents: `{ source: 'all_agents' }`.
- Saved group: `{ source: 'saved_group', group_id }`; the Backend expands and
  authorizes membership, so the Frontend never sends group member IDs.

The Frontend does not emit legacy `mentioned_agent_ids`, `message_target_mode`,
`target_group_id`, `target_group`, or candidate-scope fields. Fast maps to
`direct`, Ultimate maps to `supervisor`, and room settings only provide the UI
default.

## 13. Unified Portal

The `(portal)` route group provides one shared shell without adding a URL
segment.

- `/`: redirects to `/core`. Hybro Core does not require sign-in.
- `/core`: Hybro Core product page. The hero composer is the same `RoomChatInput`
  as `/chat`, with group and mode menus visible but non-selectable and mention
  and attach buttons visible but non-clickable. While idle it typewrites the
  featured use cases from `src/lib/use-case-templates.ts` (Travel Planner,
  Story & Image Creator). The header logo links to `/core`. The logo wall lists Hermes, OpenClaw, Pi, Ollama,
  n8n, CrewAI, LangChain, and LangGraph. Send-on-demo creates a room named after the current
  use case, seeds those Agents, and prefills the prompt without auto-sending or a sign-in redirect.
- `/chat` and `/room/[id]`: chat creation and real-time room workspace.
- `/agents`: unified local inventory of registered Remote agents and currently
  discoverable Local agents.
- `/agents/[id]`: unified AgentCard detail with Share, Chat, and Remote-only
  Unregister actions.
- `/agents/new`: Remote agent registration.
- `/about`, `/pricing`: public pages.

Remote agents use the persisted backend `agent_status` without frontend health
probing. Directly discovered Local agents are shown only while
`source === "local"` and status is active. Agent-detail chat actions
write a one-shot `pendingChatHandoff` to `room-ui-store` (optional draft text plus
`seedAgents`) and navigate to `/chat`. That handoff seeds room membership with
the selected Agent and uses `room_default` scope on send; it is not an `@mention`.
The composer consumes any draft text and focuses the input without URL query
parameters or creating a saved Team. The group selector shows the seeded Agent
name and sends with `room_default`. After the room is created, membership stays
on `room_agent_set` and later turns keep `room_default` unless the user switches
teams. Selection uses the `room_team` id for room membership and `all_agents`
only for true network broadcast; the menu lists the room membership row and All
Agents as separate options.

Featured use-case cards on `/chat` stay on the page. A card resolves its declared
Agents against the live catalog, finds the authenticated user's saved preset Team
by a stable use-case marker, and creates that Team through `/agentGroups` only
when it is absent. Creation includes an owner-scoped `preset_key`, so the Backend
also guarantees idempotency across concurrent tabs. Existing preset membership
is reconciled to the template's current Agent IDs before selection. The card then
selects the saved Team in the group selector and prefills the composer; room creation and
navigation do not occur until the user sends the message. Failed creates perform
one catalog refresh as a compatibility fallback.

The shared shell is implemented by `src/components/portal/` and exposes only New
Chat and Agents as primary navigation before chat history. Chat history uses the
lightweight authenticated `GET /roomCenter/history` resource through TanStack Query.
Pinned rooms render above Recent rooms; desktop drag handles persist pinned order
through the reorder mutation while Recent is derived from descending
`last_activity_at`. The section header can collapse or expand the history list.
Rename, pin/unpin, reorder, and delete mutations update the query cache
optimistically and roll back on failure. Active room states (`queued`,
`processing`, and `awaiting_input`) are returned in the list payload, so rooms
without active work remain unbadged and the sidebar does not issue per-room
requests. The query refreshes on focus and
polls every ten seconds only while an active state is present. Room creation
invalidates the authenticated user-scoped query under the shared
`ROOM_HISTORY_QUERY_KEY` prefix; the former global `rooms:refresh` browser event
is no longer used. Legacy `/manage/agents*` routes
are redirect-only compatibility paths. `src/lib/routes.ts` is the canonical
route vocabulary for application links.

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
|   |-- global-setup.ts
|   |-- auth.spec.ts
|   |-- authenticated-flows.spec.ts
|   |-- chat.spec.ts
|   |-- error-handling.spec.ts
|   |-- room.spec.ts
|   |-- room-timeline.spec.ts
|   `-- fixtures/auth.ts
|-- fixtures/index.ts
`-- utils/test-utils.tsx
```

Unit coverage is broad across components, hooks, API clients, room timeline logic, selectors, and stores. Playwright coverage is organized around auth, chat, room, room timeline, authenticated flows, and error handling.

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
- Add room realtime behavior through `src/hooks/room/sse-handlers/` and preserve correlation buffering rules.
- Add turn/timeline display logic under `src/lib/room-timeline/` or `src/lib/selectors/` instead of inside rendering components.
- Do not document files from deleted or historical docs as current source structure.

## Request-scoped execution mode and Agent scope

Every message send carries two immutable fields alongside `client_request_id`:

```ts
type ExecutionMode = 'direct' | 'supervisor'
type AgentScopeInput =
  | { source: 'mention'; agent_ids: [string, ...string[]] }
  | { source: 'room_default' }
  | { source: 'all_agents' }
  | { source: 'saved_group'; group_id: string }
```

Fast maps to `direct`; Ultimate maps to `supervisor`. Changing the selector remains
local until the user sends a message. `SendMessage` emits `mode` and `agent_scope`;
before handing a valid, authorized send attempt to Execution, the backend atomically
persists a changed mode as `room.extend_info.use_supervisor` without replacing other
room metadata. The mode write completes before any Execution acknowledgement, so a
hard refresh initializes the selector from the most recent send attempt, even when
that attempt is an idempotent replay or is later rejected by Execution. A missing
flag still defaults to Ultimate. Saved-team member IDs are expanded and authorized
by the backend. Existing `client_request_id` optimistic-message replacement and
early SSE buffering remain unchanged.

Debate is not a `ChatMode`, Room setting, request flag, or handled SSE event. The
ModeSelector retains one disabled `Debate (Coming Soon)` row as display-only UI;
it has no selection handler and can never create a Debate request. Historical room
`debateMode` metadata is ignored when selecting the Fast/Ultimate UI default.

## HITL questionnaire composer

An authoritative open HITL interaction replaces the normal room composer; the UI
never stacks a second form over a disabled chat input. `selectPendingHitls` groups
questions by durable `interaction_id`, and `HitlResponseBar` keeps drafts keyed by
stable `request_id`, presents one question at a time, and submits directly from the
last answer step (no separate review screen). When the open answer surface mounts —
including after "Applying…" is replaced by a follow-up interaction — the bar
autofocuses the text/date control (or the prompt heading for choice prompts). While
answers are applying, the bar
auto-refreshes pending HITL so a follow-up open prompt replaces the recovery UI
without a manual "Check status" click; that button remains only for
`delivery_uncertain`. When both an applying recovery and a new open prompt exist,
the composer prefers the open prompt. The client submits the complete answer
inventory to `POST /rooms/{room_id}/hitl/respond-batch`, preserving
`client_request_id` for run correlation. When several questions share one A2A
Agent `message_id`, each question receives a deterministic interaction-and-request-scoped
MessageStore identity while retaining the wire message identity separately. This is
also mandatory for singleton interactions: sequential one-question rounds from one
Agent call never overwrite the prior round's entity. A rolling-deploy raw-message
projection for the same request is resolved when the scoped entity arrives, avoiding
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
pipeline; even singleton interactions use the batch endpoint. File-upload
instructions arrive in the ordinary terminal HYBRO summary message, so they do
not replace the composer. Historical `file` and `unknown` prompt records remain
wire-compatible but share one unsupported-state renderer.

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
inside the shared `Card`, `Input`, and `Button` surfaces. While the Turn awaits input,
the canonical Final slot stays empty and the Conversation Body retains only the
truthful `Asking you · Waiting for input` Trace event. Compatibility legacy HITL
projections for the same active canonical interaction are suppressed from the Body so
question content is never duplicated.

Canonical cards use their opaque message identity only to derive the authenticated
`run_id + public_call_id` detail request. The detail pane fetches private output from the
room-authorized backend endpoint and does not reinterpret the opaque card/message ID as
an Agent profile ID. Public message state remains output-free.
