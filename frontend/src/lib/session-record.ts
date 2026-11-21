// Folds a session conversation's durable room events into a reviewable record:
// one exchange per client request (one canonical run), with the agent call,
// the approvals asked of the owner, the owner's decisions, and the outcome.

export interface RoomEventRecord {
  room_seq: number
  kind: string
  ts?: string | null
  payload_public: Record<string, unknown>
}

export type ExchangeStatus = 'working' | 'waiting' | 'completed' | 'rejected' | 'denied' | 'failed' | 'canceled'

export type ExchangeStepKind = 'sent' | 'asked' | 'answered' | 'rejected' | 'denied' | 'replied' | 'failed' | 'canceled'

export interface ExchangeItem {
  /** The question's short header, when the agent gave one. */
  label?: string
  text: string
}

export interface ExchangeStep {
  kind: ExchangeStepKind
  at: string | null
  text?: string
  /** Questions asked, or the owner's answers, one per question. */
  items?: ExchangeItem[]
  /** The owner's check rule that denied the agent call. */
  ruleId?: string
}

export interface SessionExchange {
  runId: string
  userMessageId: string | null
  startedAt: string | null
  agentName: string | null
  skill: string | null
  task: string | null
  status: ExchangeStatus
  steps: ExchangeStep[]
  reply: string | null
  failure: string | null
  durationMs: number | null
}

// The passthrough kernel's internal tool that forwards questions to the owner.
const SURFACE_TOOL = 'surface_agent_questions'
const ORCHESTRATOR_PREFIX = 'orchestrator:'

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function splitAgentLabel(label: string): { agentName: string; skill: string | null } {
  const index = label.indexOf(' - ')
  if (index <= 0) return { agentName: label, skill: null }
  return { agentName: label.slice(0, index), skill: label.slice(index + 3) || null }
}

function runIdFromMessageId(messageId: unknown): string | null {
  const value = text(messageId)
  if (!value?.startsWith(ORCHESTRATOR_PREFIX)) return null
  return value.slice(ORCHESTRATOR_PREFIX.length).split(':')[0] || null
}

interface Draft extends SessionExchange {
  askedSteps: Map<string, ExchangeStep>
  decidedSteps: Map<string, ExchangeStep>
  headers: Map<string, string>
  rejected: boolean
}

export function foldSessionEvents(events: readonly RoomEventRecord[]): SessionExchange[] {
  const drafts = new Map<string, Draft>()
  const order: string[] = []
  const replies = new Map<string, string>()

  const draftFor = (runId: string): Draft => {
    let draft = drafts.get(runId)
    if (!draft) {
      draft = {
        runId, userMessageId: null, startedAt: null, agentName: null, skill: null, task: null,
        status: 'working', steps: [], reply: null, failure: null, durationMs: null,
        askedSteps: new Map(), decidedSteps: new Map(), headers: new Map(), rejected: false,
      }
      drafts.set(runId, draft)
      order.push(runId)
    }
    return draft
  }

  for (const event of [...events].sort((a, b) => a.room_seq - b.room_seq)) {
    const data = event.payload_public
    const at = event.ts ?? null

    if (event.kind === 'agent_response') {
      const messageId = text(data.message_id)
      const content = text(data.content)
      if (messageId && content) replies.set(messageId, content)
      continue
    }

    if (event.kind === 'hitl_request') {
      const runId = runIdFromMessageId(data.message_id)
      if (!runId) continue
      const draft = draftFor(runId)
      const interaction = text(data.interaction_id) ?? text(data.request_id) ?? ''
      const label = text(data.header)
      const requestId = text(data.request_id)
      if (label && requestId) draft.headers.set(requestId, label)
      let step = draft.askedSteps.get(interaction)
      if (!step) {
        step = { kind: 'asked', at, items: [] }
        draft.askedSteps.set(interaction, step)
        draft.steps.push(step)
      }
      const prompt = text(data.prompt)
      if (prompt) step.items!.push(label ? { label, text: prompt } : { text: prompt })
      continue
    }

    if (event.kind === 'hitl_response') {
      const runId = text(data.run_id)
      if (!runId) continue
      const draft = draftFor(runId)
      const interaction = text(data.interaction_id) ?? text(data.request_id) ?? ''
      let step = draft.decidedSteps.get(interaction)
      if (!step) {
        if (data.status === 'canceled') {
          draft.rejected = true
          step = { kind: 'rejected', at }
        } else if (data.status === 'responded') {
          step = { kind: 'answered', at, items: [] }
        } else {
          continue
        }
        draft.decidedSteps.set(interaction, step)
        draft.steps.push(step)
      }
      const answer = text(data.answer_text)
      if (step.kind === 'answered' && answer) {
        const label = draft.headers.get(text(data.request_id) ?? '')
        step.items!.push(label ? { label, text: answer } : { text: answer })
      }
      continue
    }

    if (event.kind !== 'run_event') continue
    const runId = text(data.run_id)
    const type = text(data.type)
    if (!runId || !type) continue
    const payload = record(data.payload)
    const draft = draftFor(runId)

    if (type === 'run_started') {
      draft.userMessageId = text(payload.user_message_id)
      draft.startedAt = text(payload.started_at) ?? at
    } else if (type === 'tool_execution_start') {
      const tool = text(payload.tool_name)
      if (!tool || tool === SURFACE_TOOL || draft.agentName) continue
      const { agentName, skill } = splitAgentLabel(tool)
      draft.agentName = agentName
      draft.skill = skill
      draft.task = text(record(payload.input).task)
      draft.steps.push({ kind: 'sent', at })
    } else if (type === 'run_settled') {
      draft.durationMs = typeof payload.duration_ms === 'number' ? payload.duration_ms : null
      const settledAt = text(payload.settled_at) ?? at
      if (payload.status === 'completed') {
        draft.status = 'completed'
        draft.reply = replies.get(text(payload.final_message_id) ?? '') ?? null
        draft.steps.push({ kind: 'replied', at: settledAt })
      } else if (payload.status === 'canceled' && draft.rejected) {
        draft.status = 'rejected'
      } else if (payload.status === 'canceled' && payload.cancellation_code === 'owner_refused') {
        // The owner refused the agent call at a check rule's question.
        draft.status = 'rejected'
        draft.steps.push({ kind: 'rejected', at: settledAt })
      } else if (payload.status === 'canceled' && payload.cancellation_code === 'policy') {
        // One of the owner's check rules denied the agent call.
        draft.status = 'denied'
        const ruleId = text(payload.rule_id)
        draft.steps.push(ruleId ? { kind: 'denied', at: settledAt, ruleId } : { kind: 'denied', at: settledAt })
      } else if (payload.status === 'canceled') {
        draft.status = 'canceled'
        draft.steps.push({ kind: 'canceled', at: settledAt })
      } else {
        draft.status = 'failed'
        draft.failure = text(payload.error_summary) ?? text(payload.failure_code)
        draft.steps.push({ kind: 'failed', at: settledAt, text: draft.failure ?? undefined })
      }
    }
  }

  return order.map(runId => {
    const { askedSteps, decidedSteps, headers, rejected, ...exchange } = drafts.get(runId)!
    void headers
    void rejected
    if (exchange.status === 'working') {
      const waiting = [...askedSteps.keys()].some(id => !decidedSteps.has(id))
      exchange.status = waiting ? 'waiting' : 'working'
    }
    return exchange
  })
}
