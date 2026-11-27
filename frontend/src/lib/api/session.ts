// Owner-side session API: list, read the record and its Steps, close.
import { getApiUrl } from '../utils'
import { apiGet, apiPost } from '../api-client'

export type SessionState =
  | 'completed'
  | 'working'
  | 'waiting_on_owner'
  | 'rejected'
  | 'denied'
  | 'failed'
  | 'closed'

export interface SessionAgent {
  agent_id: string
  name: string | null
}

/** The client a session came from; `no_token` for a request without a token. */
export interface SessionClient {
  client_id: string
  name: string
  has_token: boolean
}

export interface SessionSummary {
  session_id: string
  /** The caller's label; null when a client token opened the session without one. */
  client_label: string | null
  client: SessionClient
  /** The first message, set when the session opened; null for older sessions. */
  title?: string | null
  agents: SessionAgent[]
  state: SessionState
  paused_agent_id: string | null
  conversation_id: string | null
  opened_at: string
  last_activity_at: string
  closed_at: string | null
}

export interface SessionListResponse {
  sessions: SessionSummary[]
}

export interface SessionRecord {
  session_id: string
  state: SessionState
  paused_agent_id: string | null
  reason: string | null
  agents: SessionAgent[]
  opened_at: string
  last_activity_at: string
  client_label: string | null
  client: SessionClient
  title?: string | null
  status: 'open' | 'closed'
  conversation_id: string | null
  closed_at: string | null
}

/** How a Question ended. `unanswered`: the Step ended before anyone answered. */
export type QuestionOutcome = 'pending' | 'answered' | 'canceled' | 'expired' | 'error' | 'unanswered'

export interface SessionStepItem {
  /** The question's short header, when it has one. */
  label: string | null
  text: string
}

export interface SessionStepQuestion {
  question_id: string
  /** `agent`, or `system` for a check rule's question. */
  asked_by: string | null
  prompt_type: string | null
  asked_at: string | null
  items: SessionStepItem[]
  outcome: QuestionOutcome
  resolved_at: string | null
  answers: SessionStepItem[]
  // Who answered an answered Question; null when it was not answered or the
  // record does not say.
  answered_by: 'owner' | 'client' | null
  // The client that answered, when answered_by is 'client'.
  answered_by_client_id: string | null
}

/** One prompt from a client for one agent, with its result. */
export interface SessionStep {
  step_id: string
  client_id: string | null
  prompt: string
  agent_id: string | null
  agent_name: string | null
  skill: string | null
  chosen_by: 'named'
  state: SessionState
  reason: string | null
  result: string | null
  sent_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  questions: SessionStepQuestion[]
}

export interface SessionSteps {
  session_id: string
  steps: SessionStep[]
}

export interface SessionClosed {
  session_id: string
  status: 'open' | 'closed'
}

const sessionUrl = (sessionId: string, suffix = '') =>
  getApiUrl(`sessions/${encodeURIComponent(sessionId)}${suffix}`)

export function listSessions(
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<SessionListResponse> {
  return apiGet<SessionListResponse>(getApiUrl('sessions'), getToken, signal)
}

export function getSessionRecord(
  sessionId: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<SessionRecord> {
  return apiGet<SessionRecord>(sessionUrl(sessionId, '/record'), getToken, signal)
}

export function getSessionSteps(
  sessionId: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<SessionSteps> {
  return apiGet<SessionSteps>(sessionUrl(sessionId, '/steps'), getToken, signal)
}

/** Close the session as its owner; the client's own close route needs its token. */
export function closeSession(
  sessionId: string,
  getToken?: () => Promise<string | null>,
): Promise<SessionClosed> {
  return apiPost<SessionClosed>(sessionUrl(sessionId, '/record/close'), undefined, getToken)
}
