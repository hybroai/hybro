// The Inbox ticket list's search, column filters, and sort, kept in the URL
// so Back, refresh, and shared links return to the same view.
import type { SessionState, SessionSummary } from '@/lib/api/session'
import { sessionClientLabel, sessionRequester } from '@/lib/session-format'

export type SessionSortField = 'name' | 'status' | 'source' | 'created' | 'updated'
export type SessionSortDir = 'asc' | 'desc'
export type SessionTimeRange = 'any' | 'today' | '7d' | '30d'

export interface SessionListState {
  q: string
  name: string
  status: SessionState[]
  /** Source ids. */
  source: string[]
  created: SessionTimeRange
  updated: SessionTimeRange
  sort: SessionSortField
  dir: SessionSortDir
}

export const DEFAULT_SESSION_LIST_STATE: SessionListState = {
  q: '',
  name: '',
  status: [],
  source: [],
  created: 'any',
  updated: 'any',
  sort: 'updated',
  dir: 'desc',
}

// Status order for sorting and the filter menu: what needs the owner first.
export const SESSION_STATES: SessionState[] = [
  'waiting_on_owner',
  'working',
  'completed',
  'rejected',
  'failed',
  'closed',
]

export const TIME_RANGES: { value: SessionTimeRange; label: string }[] = [
  { value: 'any', label: 'Any time' },
  { value: 'today', label: 'Today' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
]

// The list's query string, kept per tab so a ticket's back link returns to it.
export const SESSION_LIST_QUERY_STORAGE_KEY = 'hybro:sessions-list-query'

const SORT_FIELDS: SessionSortField[] = ['name', 'status', 'source', 'created', 'updated']
const UNTITLED = 'Untitled session'

export function sessionName(session: Pick<SessionSummary, 'title'>): string {
  return session.title?.trim() || UNTITLED
}

function timeRange(value: string | null): SessionTimeRange {
  return TIME_RANGES.some(range => range.value === value) ? value as SessionTimeRange : 'any'
}

export function parseSessionListState(params: URLSearchParams): SessionListState {
  const status = (params.get('status') ?? '')
    .split(',')
    .filter((value): value is SessionState => SESSION_STATES.includes(value as SessionState))
  const sort = params.get('sort')
  return {
    q: params.get('q') ?? '',
    name: params.get('name') ?? '',
    status,
    // One source id per parameter.
    source: params.getAll('source').filter(Boolean),
    created: timeRange(params.get('created')),
    updated: timeRange(params.get('updated')),
    sort: SORT_FIELDS.includes(sort as SessionSortField) ? sort as SessionSortField : 'updated',
    dir: params.get('dir') === 'asc' ? 'asc' : 'desc',
  }
}

export function serializeSessionListState(state: SessionListState): string {
  const params = new URLSearchParams()
  if (state.q) params.set('q', state.q)
  if (state.name) params.set('name', state.name)
  if (state.status.length) params.set('status', state.status.join(','))
  for (const source of state.source) params.append('source', source)
  if (state.created !== 'any') params.set('created', state.created)
  if (state.updated !== 'any') params.set('updated', state.updated)
  if (state.sort !== 'updated') params.set('sort', state.sort)
  if (state.dir !== 'desc') params.set('dir', state.dir)
  return params.toString()
}

export function hasActiveFilters(state: SessionListState): boolean {
  return Boolean(
    state.q || state.name || state.status.length || state.source.length
    || state.created !== 'any' || state.updated !== 'any',
  )
}

function rangeStart(range: SessionTimeRange, now: number): number | null {
  if (range === 'any') return null
  if (range === 'today') {
    const start = new Date(now)
    start.setHours(0, 0, 0, 0)
    return start.getTime()
  }
  return now - (range === '7d' ? 7 : 30) * 24 * 60 * 60 * 1000
}

function within(value: string, range: SessionTimeRange, now: number): boolean {
  const start = rangeStart(range, now)
  return start === null || Date.parse(value) >= start
}

function matchesSearch(session: SessionSummary, needle: string): boolean {
  if (!needle) return true
  return [
    sessionName(session),
    sessionRequester(session),
    sessionClientLabel(session) ?? '',
    session.session_id,
    ...session.agents.flatMap(agent => [agent.name ?? '', agent.agent_id]),
  ].join('\n').toLowerCase().includes(needle)
}

function compare(a: SessionSummary, b: SessionSummary, field: SessionSortField): number {
  switch (field) {
    case 'name':
      return sessionName(a).localeCompare(sessionName(b), 'en')
    case 'source':
      return sessionRequester(a).localeCompare(sessionRequester(b), 'en')
    case 'status':
      return SESSION_STATES.indexOf(a.state) - SESSION_STATES.indexOf(b.state)
    case 'created':
      return Date.parse(a.opened_at) - Date.parse(b.opened_at)
    case 'updated':
      return Date.parse(a.last_activity_at) - Date.parse(b.last_activity_at)
  }
}

export function applySessionListState(
  sessions: readonly SessionSummary[],
  state: SessionListState,
  now: number = Date.now(),
): SessionSummary[] {
  const needle = state.q.trim().toLowerCase()
  const name = state.name.trim().toLowerCase()
  const sign = state.dir === 'asc' ? 1 : -1
  return sessions
    .filter(session => (
      matchesSearch(session, needle)
      && (!name || sessionName(session).toLowerCase().includes(name))
      && (!state.status.length || state.status.includes(session.state))
      && (!state.source.length || state.source.includes(session.source.source_id))
      && within(session.opened_at, state.created, now)
      && within(session.last_activity_at, state.updated, now)
    ))
    // Ties fall back to the most recently updated first.
    .sort((a, b) => sign * compare(a, b, state.sort) || compare(b, a, 'updated'))
}
