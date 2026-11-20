import { describe, expect, it } from 'vitest'
import { SESSIONS_POLL_MS, sessionsQueryKey, sessionsQueryOptions, waitingOnOwnerCount } from '@/hooks/useSessions'
import type { SessionSummary } from '@/lib/api/session'

const summary = (id: string, state: SessionSummary['state']): SessionSummary => ({
  session_id: id,
  client_label: 'Client',
  source: { source_id: 'no_token', name: 'No token', has_token: false },
  agents: [],
  state,
  paused_agent_id: null,
  conversation_id: `conv-${id}`,
  opened_at: '2026-10-14T00:00:00Z',
  last_activity_at: '2026-10-14T00:00:00Z',
  closed_at: null,
})

describe('useSessions', () => {
  it('keys the query by user and polls unconditionally', () => {
    const options = sessionsQueryOptions('user-1')
    expect(sessionsQueryKey('user-1')).toEqual(['sessions', 'user-1'])
    expect(options.queryKey).toEqual(['sessions', 'user-1'])
    expect(options.staleTime).toBe(10_000)
    // Sessions are created outside the app, so polling never stops.
    expect(SESSIONS_POLL_MS).toBe(15_000)
    expect(options.refetchInterval).toBe(15_000)
  })

  it('counts sessions waiting on the owner', () => {
    expect(waitingOnOwnerCount([
      summary('a', 'waiting_on_owner'),
      summary('b', 'working'),
      summary('c', 'waiting_on_owner'),
      summary('d', 'closed'),
    ])).toBe(2)
    expect(waitingOnOwnerCount(undefined)).toBe(0)
  })
})
