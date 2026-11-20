// Owner-side session API: list, read the record, close.
import { getApiUrl } from '../utils'
import { apiGet, apiPost } from '../api-client'
import type { RoomEventRecord } from '../session-record'

export type SessionState =
  | 'completed'
  | 'working'
  | 'waiting_on_owner'
  | 'rejected'
  | 'failed'
  | 'closed'

export interface SessionAgent {
  agent_id: string
  name: string | null
}

/** The source a session came from; `no_token` for a request without a token. */
export interface SessionSource {
  source_id: string
  name: string
  has_token: boolean
}

export interface SessionSummary {
  session_id: string
  /** The caller's label; null when a source token opened the session without one. */
  client_label: string | null
  source: SessionSource
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
  source: SessionSource
  title?: string | null
  status: 'open' | 'closed'
  conversation_id: string | null
  closed_at: string | null
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

/** Close the session as its owner; the client's own close route needs its token. */
export function closeSession(
  sessionId: string,
  getToken?: () => Promise<string | null>,
): Promise<SessionClosed> {
  return apiPost<SessionClosed>(sessionUrl(sessionId, '/record/close'), undefined, getToken)
}

export interface SessionEventsResponse {
  room_seq: number
  events: RoomEventRecord[]
}

// The route returns at most this many events per request.
const EVENTS_PAGE = 1000

// The owner reads the session's conversation container through the room event
// replay, the same durable record the client's state is resolved from. Pages
// until the log's latest sequence so long sessions keep their later replies.
export async function fetchSessionEvents(
  conversationId: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<SessionEventsResponse> {
  const events: RoomEventRecord[] = []
  let after = 0
  let roomSeq = 0
  for (;;) {
    const url = getApiUrl(`sse/room/${encodeURIComponent(conversationId)}/events?after=${after}&limit=${EVENTS_PAGE}`)
    const page = await apiGet<SessionEventsResponse>(url, getToken, signal)
    events.push(...page.events)
    roomSeq = page.room_seq
    const last = page.events.at(-1)?.room_seq ?? after
    if (page.events.length < EVENTS_PAGE || last <= after || last >= roomSeq) break
    after = last
  }
  return { room_seq: roomSeq, events }
}
