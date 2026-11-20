import { describe, expect, it } from 'vitest'
import type { SessionSummary } from '@/lib/api/session'
import {
  DEFAULT_SESSION_LIST_STATE,
  applySessionListState,
  hasActiveFilters,
  parseSessionListState,
  serializeSessionListState,
  sessionName,
} from '@/lib/session-list-state'

const NOW = Date.parse('2026-10-15T12:00:00Z')

const session = (id: string, overrides: Partial<SessionSummary> = {}): SessionSummary => ({
  session_id: id,
  client_label: `Client ${id}`,
  source: { source_id: 'no_token', name: 'No token', has_token: false },
  title: `Request ${id}`,
  agents: [{ agent_id: 'travel', name: 'Travel Planner Agent' }],
  state: 'completed',
  paused_agent_id: null,
  conversation_id: `conv-${id}`,
  opened_at: '2026-10-15T09:00:00Z',
  last_activity_at: '2026-10-15T09:05:00Z',
  closed_at: null,
  ...overrides,
})

const ciSource = { source_id: 'src_ci', name: 'CI bot', has_token: true }

const sessions = [
  session('a', { title: 'Generate a travel plan', client_label: 'Northside buyer agent', state: 'waiting_on_owner', opened_at: '2026-10-14T21:16:00Z', last_activity_at: '2026-10-14T21:16:00Z' }),
  session('b', { title: 'Weather in Kyoto', client_label: 'Blossom Lettings client', opened_at: '2026-10-15T09:46:00Z', last_activity_at: '2026-10-15T09:47:00Z' }),
  session('c', { title: null, client_label: 'Blossom Lettings client', state: 'closed', opened_at: '2026-09-01T10:00:00Z', last_activity_at: '2026-09-02T10:00:00Z' }),
]
const ids = (list: SessionSummary[]) => list.map(item => item.session_id)

describe('session list URL state', () => {
  it('round-trips every filter and the sort', () => {
    const state = {
      q: 'kyoto',
      name: 'plan',
      status: ['waiting_on_owner', 'completed'] as const,
      requester: ['Blossom Lettings client', 'A, B'],
      created: '7d' as const,
      updated: 'today' as const,
      sort: 'created' as const,
      dir: 'asc' as const,
    }
    const parsed = parseSessionListState(new URLSearchParams(serializeSessionListState({ ...state, status: [...state.status] })))
    expect(parsed).toEqual({ ...state, status: [...state.status] })
  })

  it('omits defaults and ignores unknown values', () => {
    expect(serializeSessionListState(DEFAULT_SESSION_LIST_STATE)).toBe('')
    expect(parseSessionListState(new URLSearchParams('status=bogus,closed&created=forever&sort=x&dir=up')))
      .toEqual({ ...DEFAULT_SESSION_LIST_STATE, status: ['closed'] })
  })

  it('reports whether any filter is active', () => {
    expect(hasActiveFilters(DEFAULT_SESSION_LIST_STATE)).toBe(false)
    expect(hasActiveFilters({ ...DEFAULT_SESSION_LIST_STATE, requester: ['x'] })).toBe(true)
    expect(hasActiveFilters({ ...DEFAULT_SESSION_LIST_STATE, sort: 'name' })).toBe(false)
  })
})

describe('applySessionListState', () => {
  const apply = (patch: Partial<typeof DEFAULT_SESSION_LIST_STATE>) =>
    ids(applySessionListState(sessions, { ...DEFAULT_SESSION_LIST_STATE, ...patch }, NOW))

  it('sorts by latest update by default', () => {
    expect(apply({})).toEqual(['b', 'a', 'c'])
  })

  it('searches names, requesters, agents, and session ids', () => {
    expect(apply({ q: 'kyoto' })).toEqual(['b'])
    expect(apply({ q: 'northside' })).toEqual(['a'])
    expect(apply({ q: 'travel planner' })).toEqual(['b', 'a', 'c'])
    expect(apply({ q: 'C' })).toContain('c')
  })

  it('filters each column', () => {
    expect(apply({ name: 'travel' })).toEqual(['a'])
    expect(apply({ status: ['waiting_on_owner', 'closed'] })).toEqual(['a', 'c'])
    expect(apply({ requester: ['Blossom Lettings client'] })).toEqual(['b', 'c'])
    expect(apply({ created: 'today' })).toEqual(['b'])
    expect(apply({ updated: '7d' })).toEqual(['b', 'a'])
    expect(apply({ created: '30d' })).toEqual(['b', 'a'])
  })

  it('sorts by any column in either direction', () => {
    expect(apply({ sort: 'created', dir: 'asc' })).toEqual(['c', 'a', 'b'])
    expect(apply({ sort: 'name', dir: 'asc' })).toEqual(['a', 'c', 'b'])
    expect(apply({ sort: 'requester', dir: 'asc' })).toEqual(['b', 'c', 'a'])
    expect(apply({ sort: 'status', dir: 'asc' })).toEqual(['a', 'b', 'c'])
  })
})

describe('sessionName', () => {
  it('uses the title, or a fallback for sessions opened before titles existed', () => {
    expect(sessionName(sessions[0])).toBe('Generate a travel plan')
    expect(sessionName(sessions[2])).toBe('Untitled session')
  })

  it('uses the source name for a session without a label', () => {
    const list = [...sessions, session('d', { client_label: null, source: ciSource })]
    const ids = (overrides: Partial<typeof DEFAULT_SESSION_LIST_STATE>) =>
      applySessionListState(list, { ...DEFAULT_SESSION_LIST_STATE, ...overrides }, NOW).map(s => s.session_id)
    expect(ids({ q: 'ci bot' })).toEqual(['d'])
    expect(ids({ requester: ['CI bot'] })).toEqual(['d'])
    expect(ids({ sort: 'requester', dir: 'asc' })).toEqual(['b', 'c', 'd', 'a'])
  })
})
