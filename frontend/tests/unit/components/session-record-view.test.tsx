import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '../../utils/test-utils'

const api = vi.hoisted(() => ({
  record: {
    session_id: 'sess-1',
    state: 'waiting_on_owner',
    paused_agent_id: 'agent-1',
    reason: null,
    agents: [{ agent_id: 'agent-1', name: 'Travel Planner Agent' }],
    opened_at: '2026-10-14T09:00:00Z',
    last_activity_at: '2026-10-14T09:05:00Z',
    client_label: 'Buyer agent',
    source: { source_id: 'no_token', name: 'No token', has_token: false },
    title: 'Generate a travel plan',
    status: 'open',
    conversation_id: 'conv-1',
    closed_at: null,
  } as Record<string, unknown>,
  events: [] as Record<string, unknown>[],
  pending: [] as Record<string, unknown>[],
  getSessionRecord: vi.fn(),
  fetchSessionEvents: vi.fn(),
  closeSession: vi.fn(async (id: string) => ({ session_id: id, status: 'closed' })),
  fetchPendingHitlRequests: vi.fn(),
  respondToHitlBatch: vi.fn(async () => ({ status: 'accepted', request_id: 'q1' })),
  cancelHitl: vi.fn(async () => ({ status: 'canceled', interaction_id: 'int-1', interaction_version: 4 })),
}))

vi.mock('@/lib/api/session', () => ({
  getSessionRecord: api.getSessionRecord,
  fetchSessionEvents: api.fetchSessionEvents,
  closeSession: api.closeSession,
}))
vi.mock('@/lib/api/hitl', () => ({
  fetchPendingHitlRequests: api.fetchPendingHitlRequests,
  respondToHitlBatch: api.respondToHitlBatch,
  cancelHitl: api.cancelHitl,
}))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ getToken: async () => null }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: React.ComponentProps<'a'>) => <a href={String(href)} {...props}>{children}</a>,
}))

import { SessionRecordView } from '@/components/sessions/session-record-view'
import { SESSION_LIST_QUERY_STORAGE_KEY } from '@/lib/session-list-state'

function runEvent(seq: number, type: string, payload: Record<string, unknown>) {
  return { room_seq: seq, kind: 'run_event', ts: '2026-10-14T09:00:05Z', payload_public: { type, run_id: 'run-1', correlation_id: 'cr-1', payload } }
}

beforeEach(() => {
  api.events = [
    runEvent(1, 'run_started', { user_message_id: 'msg-1', started_at: '2026-10-14T09:00:01Z' }),
    runEvent(2, 'tool_execution_start', { tool_name: 'Travel Planner Agent - Trip planning', execution_kind: 'agent', input: { task: 'Generate a travel plan' } }),
    { room_seq: 3, kind: 'hitl_request', ts: '2026-10-14T09:00:06Z', payload_public: { request_id: 'q1', interaction_id: 'int-1', message_id: 'orchestrator:run-1:inv_1', prompt: 'Which destination?', header: 'Destination' } },
    runEvent(4, 'run_waiting_input', { interaction_id: 'int-1' }),
  ]
  api.pending = [{
    request_id: 'q1', message_id: 'm', source: 'agent', agent_name: 'Travel Planner Agent',
    prompt: 'Which destination?', prompt_type: 'text', status: 'pending', created_at: '2026-10-14T09:00:06Z',
    interaction_id: 'int-1', interaction_version: 3, client_request_id: 'cr-1',
  }]
  api.getSessionRecord.mockImplementation(async () => api.record)
  api.fetchSessionEvents.mockImplementation(async () => ({ room_seq: api.events.length, events: api.events }))
  api.fetchPendingHitlRequests.mockImplementation(async () => ({ requests: api.pending }))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionRecordView', () => {
  it('shows the ticket title, the record in plain words, and the question card at the end', async () => {
    render(<SessionRecordView sessionId="sess-1" />)

    expect(await screen.findByRole('heading', { level: 1, name: 'Generate a travel plan' })).toBeInTheDocument()
    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('No token')).toBeInTheDocument()
    expect(within(record).getByText('asked Travel Planner Agent')).toBeInTheDocument()
    expect(within(record).getByText('needs your answer below')).toBeInTheDocument()
    // The question itself is only in the card, not repeated in the record.
    expect(within(record).queryByText('Which destination?')).toBeNull()
    const card = await screen.findByRole('region', { name: 'Travel Planner Agent needs your input' })
    expect(within(card).getByText('Which destination?')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /message/i })).toBeNull()
    expect(document.body.textContent).not.toMatch(/[\u4e00-\u9fff]/)
  })

  it('shows each registered agent\'s own avatar and a generic one for the external requester', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    const record = await screen.findByRole('list', { name: 'Session record' })
    const [requesterEntry, agentEntry] = within(record).getAllByRole('listitem').filter(item => item.firstElementChild?.hasAttribute('data-avatar'))

    expect(requesterEntry.querySelector('[data-avatar]')).toHaveAttribute('data-avatar', 'external-agent')
    expect(requesterEntry.querySelector('img')).toBeNull()
    expect(agentEntry.querySelector('[data-avatar]')).toHaveAttribute('data-avatar', 'agent-1')
    expect(agentEntry.querySelector('img')?.getAttribute('src')).toMatch(/^data:image\/svg\+xml/)
  })

  it('shows session details in the side panel', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    const panel = await screen.findByRole('complementary', { name: 'Details' })
    expect(within(panel).getByText('Needs you')).toBeInTheDocument()
    expect(within(panel).getByText('No token')).toBeInTheDocument()
    expect(within(panel).getByText('Client label')).toBeInTheDocument()
    expect(within(panel).getByTitle('Name the client gave; not verified')).toHaveTextContent('Buyer agent')
    expect(within(panel).getByText('Travel Planner Agent')).toBeInTheDocument()
    expect(within(panel).getByText('sess-1')).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Copy session ID' })).toBeInTheDocument()
    for (const label of ['Status', 'Requester', 'Created', 'Updated', 'Requests']) {
      expect(within(panel).getByText(label)).toBeInTheDocument()
    }
  })

  it('names a requester without a label by its source', async () => {
    const saved = api.record
    api.record = { ...saved, client_label: null, source: { source_id: 'src_ci', name: 'CI bot', has_token: true } }
    render(<SessionRecordView sessionId="sess-1" />)
    const panel = await screen.findByRole('complementary', { name: 'Details' })
    expect(within(panel).getByText('CI bot')).toBeInTheDocument()
    expect(within(panel).queryByText('Client label')).toBeNull()
    api.record = saved
  })

  it('returns to the list view the owner came from', async () => {
    window.sessionStorage.setItem(SESSION_LIST_QUERY_STORAGE_KEY, 'status=waiting_on_owner')
    render(<SessionRecordView sessionId="sess-1" />)
    await waitFor(() => expect(screen.getByRole('link', { name: 'Inbox' })).toHaveAttribute('href', '/sessions?status=waiting_on_owner'))
    window.sessionStorage.removeItem(SESSION_LIST_QUERY_STORAGE_KEY)
  })

  it('answers a pending question', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    fireEvent.change(await screen.findByPlaceholderText('Type your answer…'), { target: { value: 'Kyoto' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(api.respondToHitlBatch).toHaveBeenCalledWith(
      'conv-1', 'int-1', [{ requestId: 'q1', answer: 'Kyoto' }], 'cr-1', expect.any(Function),
    ))
  })

  it('rejects the request with its interaction version', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Reject request' }))

    await waitFor(() => expect(api.cancelHitl).toHaveBeenCalledWith(
      'conv-1', 'int-1', 3, expect.any(String), expect.any(Function),
    ))
  })

  it('closes the session from the panel after confirmation', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    const panel = await screen.findByRole('complementary', { name: 'Details' })
    fireEvent.click(within(panel).getByRole('button', { name: 'Close session' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(api.closeSession).toHaveBeenCalledWith('sess-1', expect.any(Function)))
  })

  it('tells the story of an answered request with the owner\'s answer and the reply', async () => {
    api.pending = []
    api.record = { ...api.record, state: 'completed' }
    api.events = [
      ...api.events,
      { room_seq: 5, kind: 'hitl_response', ts: '2026-10-14T09:01:00Z', payload_public: { request_id: 'q1', interaction_id: 'int-1', run_id: 'run-1', status: 'responded', answer_text: 'Kyoto' } },
      { room_seq: 6, kind: 'agent_response', ts: '2026-10-14T09:02:00Z', payload_public: { message_id: 'final-1', content: 'Day 1: Fushimi Inari' } },
      runEvent(7, 'run_settled', { status: 'completed', final_message_id: 'final-1', duration_ms: 61000 }),
    ]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(await within(record).findByText('Day 1: Fushimi Inari')).toBeInTheDocument()
    expect(within(record).getByText('asked you')).toBeInTheDocument()
    expect(within(record).getByText('Which destination?')).toBeInTheDocument()
    expect(within(record).getByText('answered')).toBeInTheDocument()
    expect(within(record).getByText('Kyoto')).toBeInTheDocument()
    expect(within(record).getAllByText('Destination')).toHaveLength(2)
    expect(within(record).getByText('replied after 1m 1s')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /needs your input/ })).toBeNull()
    api.record = { ...api.record, state: 'waiting_on_owner' }
  })

  it('folds a long reply behind Show full reply', async () => {
    api.pending = []
    api.events = [
      ...api.events,
      { room_seq: 6, kind: 'agent_response', ts: '2026-10-14T09:02:00Z', payload_public: { message_id: 'final-1', content: 'Line\n'.repeat(20) } },
      runEvent(7, 'run_settled', { status: 'completed', final_message_id: 'final-1' }),
    ]
    render(<SessionRecordView sessionId="sess-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Show full reply' }))
    expect(screen.getByRole('button', { name: 'Show less' })).toBeInTheDocument()
  })

  it('reports a missing session', async () => {
    api.getSessionRecord.mockRejectedValueOnce(new Error('404'))
    render(<SessionRecordView sessionId="missing" />)
    expect(await screen.findByText('This session was not found.')).toBeInTheDocument()
  })
})
