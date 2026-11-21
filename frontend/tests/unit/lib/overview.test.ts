import { describe, expect, it } from 'vitest'
import { overviewMetrics, recentRooms, recentSessions, topAgentsByCalls } from '@/lib/overview'
import { placeholderAgent } from '@/lib/network-agent'
import type { AgentGroup } from '@/lib/types/agent-group'
import type { RoomHistoryItem } from '@/lib/api/room'
import type { SessionSummary } from '@/lib/api/session'

const room = (id: string, status: RoomHistoryItem['status']): RoomHistoryItem => ({ conversation_id: id, title: id, last_activity_at: '2026-10-10T00:00:00Z', is_pinned: false, pin_order: null, status })
const agents = [
  { ...placeholderAgent('a', '', 'active'), roomCalls: 3 },
  { ...placeholderAgent('b', '', 'inactive'), name: 'travel-planner', roomCalls: 9 },
  { ...placeholderAgent('c', '', 'active'), roomCalls: 0 },
]
const networks = [{ group_id: 'R', name: 'R', agents: ['a', 'b'], type: 'user', owner_id: 'u' }] as AgentGroup[]
const rooms = [room('r1', 'awaiting_input'), room('r2', 'processing'), room('r3', 'idle')]
const session = (id: string, state: SessionSummary['state']): SessionSummary => ({
  session_id: id, client_label: `Client ${id}`, client: { client_id: 'no_token', name: 'No token', has_token: false }, agents: [], state, paused_agent_id: null,
  conversation_id: null, opened_at: '2026-10-10T00:00:00Z', last_activity_at: '2026-10-10T00:00:00Z', closed_at: null,
})
const sessions = [session('s1', 'waiting_on_owner'), session('s2', 'working'), session('s3', 'closed')]

describe('overview', () => {
  it('computes metrics', () => {
    expect(overviewMetrics({ agents, networks, rooms, sessions })).toEqual({
      agents: { total: 3, online: 2, offline: 1 },
      subnets: { total: 1, unassigned: 1 },
      rooms: { total: 3, running: 1, needsInput: 1 },
      sessions: { open: 2, waiting: 1 },
    })
  })

  it('lists rooms needing input first, then the latest', () => {
    const list = [
      { ...room('old', 'idle'), last_activity_at: '2026-10-01T00:00:00Z' },
      { ...room('new', 'idle'), last_activity_at: '2026-10-12T00:00:00Z' },
      { ...room('ask', 'awaiting_input'), last_activity_at: '2026-10-02T00:00:00Z' },
    ]
    expect(recentRooms(list, 2).map(item => item.conversation_id)).toEqual(['ask', 'new'])
  })

  it('lists sessions waiting for the owner first, then the latest', () => {
    const list = [
      { ...session('old', 'completed'), last_activity_at: '2026-10-01T00:00:00Z' },
      { ...session('new', 'completed'), last_activity_at: '2026-10-12T00:00:00Z' },
      { ...session('wait', 'waiting_on_owner'), last_activity_at: '2026-10-02T00:00:00Z' },
    ]
    expect(recentSessions(list, 3).map(item => item.session_id)).toEqual(['wait', 'new', 'old'])
  })

  it('ranks agents by calls and drops zero-call agents', () => {
    expect(topAgentsByCalls(agents, 5).map(agent => agent.id)).toEqual(['b', 'a'])
  })
})
