# Design: Headless Agent Network & Interop Kernel

> Status: Draft  
> Date: 2026-10-05  
> Audience: Hybro maintainers  
> Design partner: Blossom (self-hosted Hybro instance; their product UI embeds Hybro)  
> Related: [System-Architecture.md](./System-Architecture.md) § Agent-network API, HITL, A2A adapter

---

## 1. Purpose

Make Hybro a **comprehensive, domain-agnostic agent interoperability layer** that vertical products can embed **without requiring the Hybro chat UI**.

Blossom is the first design partner: they deploy Hybro on their own servers, register **Agent Card URLs** for domain agents (Iris, Arsène), keep Karl/Extract as tools, and surface human decisions in **BlossomDoc**. Domain agents remain **Hybro-agnostic** (standard A2A only). Hybro must not learn real-estate schemas.

**Product wedge (what we sell / what we are):**  
durable agent sessions + HITL + service identity + A2A routing — for SaaS teams that embed multi-agent workflows.

**Not the wedge:** “we speak A2A” alone, or a consumer chat portal as the primary product.

---

## 2. Goals and non-goals

### Goals

1. **Headless-first APIs** with the same durability guarantees as Rooms (tasks that survive minutes to days).
2. **Embeddable HITL** so partner UIs list pending interactions and post answers.
3. **Service principals for embedders** (partner backends/UIs)—not for remote agents. Agents authenticate **inbound** A2A traffic per their Agent Card; they do not call Hybro APIs.
4. **One execution core** — Network façade and Room UI share Execution / A2A adapter; no second orchestrator.
5. **Domain neutrality** — opaque metadata indexes only; no vertical types in Hybro schemas.
6. **Self-host slim profile** — deploy registry + A2A + sessions/HITL/events without requiring chat, pricing, or demo agents.
7. **Clear agent vs tool boundary** — A2A directory for agents; tools (MCP/HTTP) are either out of scope or a separate optional registry.
8. **Agent Hybro-agnosticism** — remote agents are standard A2A servers (+ their own tools). They must not import Hybro SDKs, call Hybro REST, or require `hybro_session_id` in their domain logic.

### Non-goals (this design)

- Replacing partner data planes (e.g. Blossom Supabase / `agent-gateway`).
- Hosting partner LLM inference (agents may bring their own models).
- Multi-vendor public marketplace (later).
- Making every microservice an A2A agent.
- Forcing end users into Hybro chat.
- Requiring domain agents to know Hybro exists.

---

## 3. Current state (gap summary)

| Surface | Today | Gap |
|---|---|---|
| `GET /agents/discovery` | Caller-visible cards | OK for v1; needs service auth + org later |
| `POST /agents/messages` | Blocking ≤600s send | No Hybro task ledger, HITL, poll, cancel, SSE, idempotency |
| Rooms + SSE + HITL | Durable, UI-oriented | Not exposed as a stable headless Network contract |
| `MCP/server.py` | Local mock-auth bridge to the two Network endpoints | Not production; mirrors sync-only API |
| Auth | Clerk user or mock developer | No first-class service accounts |
| Correlation | Client must retain remote `contextId`/`taskId` | No query by business metadata |

Docs already state Network send does **not** bind IDs to callers, store history, or orchestrate HITL. That is the hole partners hit.

---

## 4. Architecture

### 4.0 Who knows Hybro

```text
Knows Hybro                          Must NOT need to know Hybro
─────────────────                    ─────────────────────────────
Partner product (BlossomDoc UI/BFF)  Iris, Arsène, … (A2A servers)
Ops registering Card URLs            Karl, Extract, … (tools)
Hybro itself                         Any compliant third-party A2A agent
```

**Contract for agents:** publish an Agent Card, speak A2A (send/stream, GetTask, CancelTask, push if advertised), use MCP/HTTP for their own tools.  
**Contract for embedders:** call Hybro headless APIs; Hybro is the A2A **client** (and directory) toward those agents.

Push callbacks: when Hybro starts a Task, it registers a standard A2A `pushNotificationConfig` pointing at **Hybro’s webhook ingress**. The agent only sees a callback URL + credentials from the protocol—not a “Hybro API.”

### 4.1 Principle: one core, two façades

```text
Partner app / workers          Hybro chat UI (optional console)
        │                              │
        ▼                              ▼
 Headless Network API            Room / portal routes
        │                              │
        └──────────┬───────────────────┘
                   ▼
         Execution + A2A adapter
                   │
                   ▼
         Registered A2A agents
```

**Implementation preference for v1:** implement Network **Session** as a headless Room (`session_id` may equal `room_id` internally) so HITL, SSE, cancel, and webhook ingress are reused. Stable external URLs and schemas must not require chat.

### 4.2 Resource model (aligned with A2A)

A2A already defines **Message**, **Task**, **contextId**, **artifacts**, and Agent Cards.
Hybro’s headless API must not invent a competing identity system that partners confuse
with the wire protocol.

| Resource | What it is | Relation to A2A |
|---|---|---|
| **Session** (`session_id`) | Hybro **orchestration + authz** container (embedder’s “case”) | **Not** an A2A `contextId`. One session may hold many remote contexts (one per agent server). |
| **Call / Task record** | Hybro ledger row for a dispatch to one agent | Stores `remote_task_id`, `remote_context_id`, agent_id, state mirror |
| **Remote Task** | A2A `Task` owned by the **serving agent** | Server generates `task.id`; client continues with `message.taskId` + matching `contextId` |
| **Remote context** | A2A `contextId` | Groups related Tasks/Messages **on that agent**; opaque; often server-issued |
| **Interaction** | Hybro HITL projection | Usually materializes from remote `input-required` / `auth-required` (+ optional typed extension) |
| **Delivery** | SSE / partner webhooks / A2A push | Long waits should prefer protocol push when the Agent Card advertises it |

Minimum persisted fields (illustrative):

```text
Session: session_id, owner_principal, org_id?, agent_ids[], metadata{}, created_at, state

# Per remote agent under a Session:
AgentBinding: session_id, agent_id, remote_context_id?

# Per A2A Task Hybro is client for:
TaskRecord: hybro_task_record_id, session_id, agent_id,
            remote_task_id, remote_context_id,
            state, idempotency_key, client_request_id, metadata{}, updated_at
```

**Never** tell partners “Hybro `task_id` === A2A `taskId`” unless the record is explicitly
a 1:1 mirror and the serving agent created that id. Prefer returning both:

```json
{
  "session_id": "…",
  "hybro_task_record_id": "…",
  "a2a": { "agent_id": "iris", "taskId": "…", "contextId": "…" }
}
```

Task states mirror A2A (internal lowercase-hyphenated):  
`submitted | working | input-required | auth-required | completed | failed | canceled | rejected | expired | unknown`

Remote agent remains protocol authority; Hybro owns **authorization, correlation index, client-side delivery, and multi-agent orchestration across agents**.

#### A2A rules Hybro must respect

1. **Message vs Task** — Trivial replies may be Message-only; long work / HITL / multi-day waits **must** be Tasks. Hybrid agents negotiate with Messages then commit to a Task.
2. **Terminal immutability** — After `completed|canceled|rejected|failed`, **no further messages** on that `taskId`. Refinements = **new Task** in the same `contextId`, optionally with `referenceTaskIds`.
3. **Resume interrupted tasks** — Only `input-required` and `auth-required` are resumed by sending a new Message with the **same** `taskId` (+ `contextId`).
4. **IDs are server-owned** — Serving agent generates `taskId`; `contextId` is typically server-generated and opaque. Clients must not invent ids unless the agent documents acceptance.
5. **One task belongs to one agent server** — Iris and Arsène cannot share a single A2A `taskId`. Delegation creates **Arsène’s** Task; Iris keeps (or completes) **her** Task. Hybro Session ties them for the product case.
6. **Parallel tasks** — Multiple Tasks may run under one `contextId` on one agent; Hybro Session may track many TaskRecords.
7. **Artifacts** — Results travel as A2A Artifacts/Parts; clients track version lineage (`referenceTaskIds`, artifact names). Hybro should pass through, not drop.
8. **Card capabilities** — Streaming, push notifications, auth schemes, `supportedInterfaces` (JSON-RPC / HTTP+JSON) come from the Agent Card. Headless clients must honor Card auth and prefer push for multi-day waits.
9. **Protocol methods to expose (not only custom REST)** — Map clearly to A2A: send/stream message, **GetTask**, **CancelTask**, **ListTasks** (filter by `contextId`/`status`), push-notification config when supported.
10. **HITL extension** — Core A2A only requires continuing a Message into `input-required`. Hybro’s typed `hybro.ai/a2a/interaction` is an **extension** for rich questionnaires; untyped `input-required` must still work (forward text challenge to embedder).
11. **`auth-required` ≠ HITL text** — Credential/auth challenges need a distinct path from human business arbitration.
12. **Human is not an A2A peer** — The estate agent answers via the embedder UI; Hybro (as A2A **client**) sends the continuation Message to the agent. Do not market “human speaks A2A.”
### 4.3 Agent vs tool

| Kind | On Hybro directory? | Example (Blossom) |
|---|---|---|
| **Agent** | Yes (Agent Card, A2A) | Iris, Arsène, future Compliance |
| **Tool** | No (unless optional tool registry) | Karl, Extract, coherence EFs |

Interop rule for docs and partners: **A2A for peers that pause or wait; MCP/HTTP tools for one-shot capabilities.** Hybro’s kernel optimizes for the former.

### 4.4 Optional tool registry (phase decision)

- **Phase A:** Document tools as out-of-band; agents mount MCP/HTTP themselves.  
- **Phase B (optional):** Register tool descriptors (name, schema, base URL, auth scope) for discovery only — Hybro does not execute domain tools.

Do not block headless A2A on Phase B.

---

## 5. API design

Names are illustrative; final paths must update `openapi.json` and System-Architecture.

### 5.1 Keep and extend

```http
GET  /api/v1/agents/discovery
POST /api/v1/agents/messages          # sync wait-mode sugar over the same core
```

`POST /agents/messages`:

- `wait=true` (default for backward compatibility): block ≤ deadline; return Message/Task.  
- `wait=false`: accept work, return `202` + `{ session_id?, task_id, status }`.  
- On sync timeout after accept: `504` **including** `task_id` when known so clients poll instead of blind retry.

### 5.2 Headless durable cluster

```http
# Sessions (Hybro orchestration — not A2A contextId)
POST   /api/v1/network/sessions
GET    /api/v1/network/sessions/{session_id}
GET    /api/v1/network/sessions?metadata.<key>=...

# Dispatches that become A2A client calls
POST   /api/v1/network/sessions/{session_id}/messages
       # body: agent_id, message (A2A Message), wait?, push_notification_config?
       # → mirrors message/send or message/stream; may return Message or Task

GET    /api/v1/network/tasks/{hybro_task_record_id}     # includes a2a.taskId/contextId
GET    /api/v1/network/sessions/{session_id}/tasks      # ListTasks-shaped filter
POST   /api/v1/network/tasks/{hybro_task_record_id}/messages   # continue ONLY if state is input-/auth-required
POST   /api/v1/network/tasks/{hybro_task_record_id}/cancel     # → tasks/cancel
POST   /api/v1/network/sessions/{session_id}/tasks      # explicit new Task after terminal (refinement)
       # body may include reference_task_ids[]

# HITL (embeddable; Hybro-as-client then continues A2A Message to the agent)
GET    /api/v1/network/sessions/{session_id}/hitl/pending
POST   /api/v1/network/sessions/{session_id}/hitl/respond-batch
POST   /api/v1/network/hitl/interactions/{interaction_id}/cancel

# Events / push
GET    /api/v1/network/sessions/{session_id}/events   # SSE to embedder
POST   /api/v1/network/sessions/{session_id}/webhooks # embedder callbacks
# Plus: register A2A Task pushNotificationConfig toward Hybro ingress when Card supports push
```

**Continue vs refine (enforce in API):**

| Remote state | Allowed client action |
|---|---|
| `input-required` / `auth-required` | `POST …/tasks/{id}/messages` (same A2A taskId) |
| `working` / `submitted` | Wait / cancel / get; do not “complete” from client |
| Terminal | **Reject** continue; use new message/task with `referenceTaskIds` + same remote `contextId` when possible |

### 5.3 Example: start async task

```http
POST /api/v1/network/sessions/{session_id}/tasks
Idempotency-Key: partner-key-1
Authorization: Bearer <user-or-service-token>

{
  "agent_id": "iris",
  "message": {
    "role": "user",
    "parts": [{ "kind": "text", "text": "Review coherence for this file." }],
    "metadata": {}
  },
  "metadata": {
    "dossier_id": "uuid",
    "blossom_user_id": "uuid"
  }
}

→ 202 {
  "task_id": "…",
  "session_id": "…",
  "status": "submitted",
  "client_request_id": "…"
}
```

### 5.4 Example: continue after external wait

```http
POST /api/v1/network/tasks/{task_id}/messages
Idempotency-Key: mail-reply-1

{
  "message": {
    "parts": [{ "kind": "text", "text": "Third party supplied the missing PDF; analysis saved." }],
    "metadata": { "document_type": "pv_ag", "file_id": "…" }
  }
}
```

### 5.5 Event types (domain-agnostic)

```text
task.status_changed
task.artifact_added
hitl.opened
hitl.resolved
task.terminal          # completed | failed | canceled | rejected | expired
```

Webhooks: at-least-once, signed, include `event_id` for dedupe. SSE for interactive partner UIs.

### 5.6 Correlation

Sessions/tasks accept opaque `metadata`. Hybro indexes configured keys for equality queries only — it does not interpret values.

Partners define key names in their own contracts (e.g. `dossier_id`, `external_ref`).

### 5.7 Idempotency and delivery certainty

- Require `Idempotency-Key` (or promote `client_request_id` to a true idempotency key) on start, continue, and HITL respond.  
- Replays return the original result.  
- Expose `delivery: "confirmed" | "uncertain"` when transport fails after a possible send (align with existing adapter reject-vs-transport split).  
- Document: never blind-resend on `uncertain` without the same idempotency key.

### 5.8 Auth

| Principal | Use |
|---|---|
| **User** (Clerk or partner-mapped) | Interactive partner UI talking to **Hybro** |
| **Service** (m2m) | Partner **backends** (BFF, workers that are not A2A agents) talking to **Hybro** |
| **Webhook signing** | Hybro → partner embedder callbacks (HITL/events) |
| **A2A Card security** | Hybro → agent (client authenticates to Iris/Arsène per Card schemes) |

Rules:

- Domain **agents do not hold Hybro service tokens** and do not call `/api/v1/network/*`.  
- Task/session IDs are not capabilities for embedders; every Hybro read/write checks principal ownership.  
- Self-host single-tenant mode: one org, embedder service auth + metadata user binding is enough for design partners.

---

## 6. Related interop features (beyond raw Network CRUD)

| Feature | Why |
|---|---|
| **Slim self-host profile** | Compose/config flag: kernel without demo agents, pricing, or required chat |
| **Bring-your-own-LLM** | Partner agents use their own providers; Hybro LLM gateway optional for bundled agents only |
| **Trust tiers** (later) | first-party / partner / untrusted — Card sanitization and egress policy |
| **Org / workspace** (later) | Multi-tenant cloud motion; not blocking Blossom self-host |
| **Policy hooks** (thin) | e.g. require metadata keys before certain side effects; only session owner answers HITL |
| **Trace propagation** | Stable `trace_id` / `task_id` in events for partner observability |
| **MCP adapter v2** | Authenticated; calls durable Network APIs, not sync-only send |
| **Console role for chat UI** | Inspector / Networks / timelines for operators; not end-user path |

---

## 7. Phased delivery

### Phase 0 — Contract freeze
- OpenAPI draft for Session/Task/HITL/Events  
- Auth story for service principals (self-host)  
- Explicit agent-vs-tool guidance in README  

### Phase 1 — Durable accept + get
- Persist Session + TaskRecord (with **remote** `taskId`/`contextId`) on Network sends  
- `wait=false` → `202`  
- `GET` task record + **GetTask** refresh from agent when needed  
- Timeout responses include hybro record id **and** A2A ids when known  

### Phase 2 — Continue / cancel / list / push
- Continue **only** for `input-required` / `auth-required`  
- Cancel → A2A cancel; ListTasks filters  
- Session SSE + embedder webhooks  
- A2A **push notification** ingress when Card advertises push (preferred for multi-day)  
- Idempotency keys; reject continue-on-terminal with clear error (force refinement path)  

### Phase 3 — Headless HITL
- Project room HITL onto `session_id`  
- Typed extension optional; **untyped** `input-required` still surfaces to embedder  
- Distinct handling for `auth-required`  
- Respond → Hybro sends A2A continuation Message as **client** (human is not on the wire)  
- Design-partner validation with Blossom (conflict arbitration)  

### Phase 4 — Service auth + correlation queries
- m2m tokens  
- `GET /sessions?metadata.…`  
- MCP adapter updated  

### Phase 5 — Slim self-host + hardening
- Slim profile packaging  
- Card/metadata sanitization at boundary  
- Quota/admission cross-worker (as needed)  

### Later
- Orgs, trust tiers, optional tool registry, managed cloud  

---

## 8. Success metrics (design partner)

Blossom (or equivalent) can:

1. Deploy Hybro in their VPC.  
2. Run a multi-day A2A task (delegate to a channel agent, resume via **A2A push**) **without** opening Hybro chat.  
3. Render and answer HITL entirely in their product UI.  
4. Correlate Hybro Session with their business primary key via metadata.  
5. Keep domain tools off the A2A directory.  
6. Run Iris/Arsène **without any Hybro client code or tokens** inside those agents.

Internal metrics: API task completions, HITL round-trips, self-host upgrade success — not Hybro chat DAU.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Two runtimes (Network vs Room) diverge | Session-as-Room internally; one Execution owner |
| Partners encode domain types into Hybro | Refuse vertical schemas; metadata only |
| Sync API remains the only documented path | README / OpenAPI lead with async; deprecate mental model of 600s-only |
| Service auth delayed → partners hack user JWTs | Phase 4 early for design partner; document interim securely |
| Orchestrator product goals conflict with embed kernel | Keep Supervisor/synthesize optional; Network path must not require them |
| **Conflating Session with A2A `contextId`** | Document dual identity; store per-agent `remote_context_id` |
| **Sharing one `taskId` across Iris and Arsène** | Forbidden; Session links TaskRecords; delegation = new Task on callee |
| **Continue after terminal** | API 409 + refinement helper using `referenceTaskIds` |
| **Ignoring Agent Card push/auth/interfaces** | Resolve Card before every effect; prefer push for long waits |
| **Typed HITL-only** | Always support untyped `input-required` text challenges |
| **Requiring agents to call Hybro REST** | Forbidden; agents are A2A-only; embedder owns Hybro client |
| **Leaking `hybro_session_id` into agent domain logic** | Pass domain ids in A2A message metadata if needed; Session mapping stays in Hybro/embedder |

---

## 10. Documentation updates when implementing

- `backend/docs/System-Architecture.md` — Network durable path, auth principals, Session model  
- `backend/README.md` — replace “no poll/cancel/HITL” with new contract  
- Root `README.md` — embed/self-host positioning  
- `MCP/` README — durable tools  
- `openapi.json` snapshot + route tests  

---

## 11. Summary

Hybro becomes a comprehensive interop layer by turning the Agent Network into a **headless durable session/task/HITL/event API** on top of the existing Execution/A2A core, adding **embedder identity** and **opaque correlation**, and treating the chat UI as an **optional console**. Domain products self-host or embed; they own tools, data, and end-user UX. **Remote agents stay portable A2A servers**—they never depend on Hybro. A2A remembers; partners’ tools carry the work.

---

## 12. A2A protocol checklist (review)

Verified against A2A concepts (Agent Card, Message/Task, contextId, task lifecycle, MCP complementarity):

| Topic | Design status |
|---|---|
| A2A vs MCP split | OK — agents on mesh; tools off-mesh |
| Task lifecycle + `input-required` | OK if continue semantics enforced |
| Terminal immutability | **Was gap** — now explicit reject + refinement |
| Multi-agent ID ownership | **Was gap** — Session ≠ single shared A2A task |
| GetTask / CancelTask / ListTasks | **Was gap** — added to API surface |
| Push notifications | **Was gap** — required for multi-day, not only partner webhooks |
| Artifacts / `referenceTaskIds` | **Was gap** — called out |
| `auth-required` | **Was gap** — distinct from business HITL |
| Human as non-peer | Clarified — Hybro is A2A client on resume |
| Card `supportedInterfaces` / auth | Must remain in implementation (Hybro already has Card path) |
| Agents Hybro-agnostic | **Required** — only embedder talks Hybro REST; agents speak A2A + tools |
