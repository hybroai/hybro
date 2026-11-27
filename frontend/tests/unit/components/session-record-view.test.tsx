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
    client: { client_id: 'no_token', name: 'No token', has_token: false },
    title: 'Generate a travel plan',
    status: 'open',
    conversation_id: 'conv-1',
    closed_at: null,
  } as Record<string, unknown>,
  steps: [] as Record<string, unknown>[],
  pending: [] as Record<string, unknown>[],
  getSessionRecord: vi.fn(),
  getSessionSteps: vi.fn(),
  closeSession: vi.fn(async (id: string) => ({ session_id: id, status: 'closed' })),
  fetchPendingHitlRequests: vi.fn(),
  respondToHitlBatch: vi.fn(async () => ({ status: 'accepted', request_id: 'q1' })),
  cancelHitl: vi.fn(async () => ({ status: 'canceled', interaction_id: 'int-1', interaction_version: 4 })),
}))

vi.mock('@/lib/api/session', () => ({
  getSessionRecord: api.getSessionRecord,
  getSessionSteps: api.getSessionSteps,
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

// A test that changes the record gets the default back before the next one,
// even when its assertions fail.
const DEFAULT_RECORD = { ...api.record }

function question(overrides: Record<string, unknown> = {}) {
  return {
    question_id: 'int-1',
    asked_by: 'agent',
    prompt_type: 'text',
    asked_at: '2026-10-14T09:00:06Z',
    items: [{ label: 'Destination', text: 'Which destination?' }],
    outcome: 'pending',
    resolved_at: null,
    answers: [],
    answered_by: null,
    answered_by_client_id: null,
    ...overrides,
  }
}

function step(overrides: Record<string, unknown> = {}) {
  return {
    step_id: 'cr-1',
    client_id: 'no_token',
    prompt: 'Generate a travel plan',
    agent_id: 'agent-1',
    agent_name: 'Travel Planner Agent',
    skill: 'Trip planning',
    chosen_by: 'named',
    state: 'waiting_on_owner',
    reason: null,
    result: null,
    sent_at: '2026-10-14T09:00:00Z',
    started_at: '2026-10-14T09:00:01Z',
    finished_at: null,
    duration_ms: null,
    questions: [question()],
    ...overrides,
  }
}

function settledAt(overrides: Record<string, unknown>) {
  return step({ finished_at: '2026-10-14T09:02:00Z', ...overrides })
}

beforeEach(() => {
  api.record = { ...DEFAULT_RECORD }
  api.steps = [step()]
  api.pending = [{
    request_id: 'q1', message_id: 'm', source: 'agent', agent_name: 'Travel Planner Agent',
    prompt: 'Which destination?', prompt_type: 'text', status: 'pending', created_at: '2026-10-14T09:00:06Z',
    interaction_id: 'int-1', interaction_version: 3, client_request_id: 'cr-1',
  }]
  api.getSessionRecord.mockImplementation(async () => api.record)
  api.getSessionSteps.mockImplementation(async (id: string) => ({ session_id: id, steps: api.steps }))
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

  it('shows each registered agent\'s own avatar and a generic one for the external client', async () => {
    render(<SessionRecordView sessionId="sess-1" />)
    const record = await screen.findByRole('list', { name: 'Session record' })
    const [clientEntry, agentEntry] = within(record).getAllByRole('listitem').filter(item => item.firstElementChild?.hasAttribute('data-avatar'))

    expect(clientEntry.querySelector('[data-avatar]')).toHaveAttribute('data-avatar', 'external-agent')
    expect(clientEntry.querySelector('img')).toBeNull()
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
    for (const label of ['Status', 'Client', 'Created', 'Updated', 'Requests']) {
      expect(within(panel).getByText(label)).toBeInTheDocument()
    }
  })

  it('names a client without a label by its name', async () => {
    const saved = api.record
    api.record = { ...saved, client_label: null, client: { client_id: 'client_ci', name: 'CI bot', has_token: true } }
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

  it('tells the story of an answered request with the owner\'s answer, the reply and the skill', async () => {
    api.pending = []
    api.record = { ...api.record, state: 'completed' }
    api.steps = [settledAt({
      state: 'completed',
      result: 'Day 1: Fushimi Inari',
      duration_ms: 61000,
      questions: [question({ outcome: 'answered', resolved_at: '2026-10-14T09:01:00Z', answers: [{ label: 'Destination', text: 'Kyoto' }], answered_by: 'owner' })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(await within(record).findByText('Day 1: Fushimi Inari')).toBeInTheDocument()
    expect(within(record).getByText('asked you')).toBeInTheDocument()
    expect(within(record).getByText('Which destination?')).toBeInTheDocument()
    expect(within(record).getByText('answered')).toBeInTheDocument()
    expect(within(record).getByText('Kyoto')).toBeInTheDocument()
    expect(within(record).getAllByText('Destination')).toHaveLength(2)
    expect(within(record).getByText('replied after 1m 1s')).toBeInTheDocument()
    expect(within(record).getByText('Skill: Trip planning')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /needs your input/ })).toBeNull()
    api.record = { ...api.record, state: 'waiting_on_owner' }
  })

  it('folds a long reply behind Show full reply', async () => {
    api.pending = []
    api.steps = [settledAt({ state: 'completed', result: 'Line\n'.repeat(20), questions: [] })]
    render(<SessionRecordView sessionId="sess-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Show full reply' }))
    expect(screen.getByRole('button', { name: 'Show less' })).toBeInTheDocument()
  })

  it('shows a call the check rules denied with the rule sentence', async () => {
    api.pending = []
    api.steps = [settledAt({
      state: 'denied',
      reason: "The owner's check rule rule_abc denied this agent call.",
      questions: [],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('denied this agent call')).toBeInTheDocument()
    expect(within(record).getByText("The owner's check rule rule_abc denied this agent call.")).toBeInTheDocument()
  })

  it('shows a refused check rule question as the answer Reject and a rejected request', async () => {
    api.pending = []
    api.steps = [settledAt({
      state: 'rejected',
      questions: [question({
        asked_by: 'system',
        prompt_type: 'approval',
        items: [{ label: null, text: 'Allow Travel Planner Agent to book?' }],
        outcome: 'answered',
        resolved_at: '2026-10-14T09:01:00Z',
        answers: [{ label: null, text: 'Reject' }],
        answered_by: 'owner',
      })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('Allow Travel Planner Agent to book?')).toBeInTheDocument()
    expect(within(record).getByText('Reject')).toBeInTheDocument()
    expect(within(record).getByText('rejected the request')).toBeInTheDocument()
    expect(within(record).getAllByText('Your check rules')).toHaveLength(1)
  })

  it.each([
    ['no_token', 'no_token', 'No token'],
    ['other-client', 'no_token', 'A client'],
    ['no_token', null, 'A client'],
  ])('names the client %s (Step client %s) that answered for the owner', async (clientId, stepClient, name) => {
    api.pending = []
    api.steps = [settledAt({
      state: 'completed',
      result: 'Booked',
      client_id: stepClient,
      questions: [question({
        answered_by: 'client',
        outcome: 'answered',
        resolved_at: '2026-10-14T09:01:00Z',
        answers: [{ label: 'Destination', text: 'Kyoto' }],
        answered_by_client_id: clientId,
      })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    const answered = within(record).getByText('answered for you').closest('li') as HTMLElement
    expect(within(answered).getByText(name)).toBeInTheDocument()
    expect(within(answered).getByText('Kyoto')).toBeInTheDocument()
  })

  it('says when the record does not say who answered', async () => {
    api.pending = []
    api.steps = [settledAt({
      state: 'completed',
      result: 'Booked',
      questions: [question({ outcome: 'answered', resolved_at: '2026-10-14T09:01:00Z', answers: [{ label: null, text: 'Kyoto' }] })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    const answered = within(record).getByText('answered').closest('li') as HTMLElement
    expect(within(answered).getByText('Someone')).toBeInTheDocument()
    expect(within(answered).getByText('Who answered was not recorded.')).toBeInTheDocument()
  })

  it('credits a client\'s refusal of a check rule question to the client', async () => {
    api.pending = []
    api.steps = [settledAt({
      state: 'rejected',
      questions: [question({
        asked_by: 'system',
        prompt_type: 'approval',
        items: [{ label: null, text: 'Allow Travel Planner Agent to book?' }],
        outcome: 'answered',
        resolved_at: '2026-10-14T09:01:00Z',
        answers: [{ label: null, text: 'Reject' }],
        answered_by: 'client',
        answered_by_client_id: 'no_token',
      })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    const rejected = within(record).getByText('rejected the request').closest('li') as HTMLElement
    expect(within(rejected).getByText('No token')).toBeInTheDocument()
    expect(within(rejected).queryByText('You')).toBeNull()
    expect(within(record).getAllByText('rejected the request')).toHaveLength(1)
  })

  it('shows a Step the closed session stopped', async () => {
    api.pending = []
    api.record = { ...api.record, status: 'closed', state: 'closed' }
    api.steps = [step({ state: 'closed', questions: [] })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('stopped; the session was closed')).toBeInTheDocument()
    expect(within(record).queryByText(/is working on it/)).toBeNull()
  })

  it.each([
    ['provider_error', 'The model provider returned an error.'],
    ['agent_timeout', 'The request failed.'],
    ['The agent stopped responding.', 'The agent stopped responding.'],
  ])('shows the failure %s as %s', async (reason, text) => {
    api.pending = []
    api.steps = [settledAt({ state: 'failed', reason, questions: [] })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText(text)).toBeInTheDocument()
    if (reason !== text) expect(within(record).queryByText(reason)).toBeNull()
  })

  it.each(['canceled', 'canceled:room_closed'])('shows a Step that failed as %s as canceled', async (reason) => {
    api.pending = []
    api.steps = [settledAt({ state: 'failed', reason, duration_ms: 2000, questions: [] })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('stopped after 2s; the request was canceled')).toBeInTheDocument()
  })

  it('shows the spinner for a Step still working after an answered question', async () => {
    api.pending = []
    api.steps = [step({
      state: 'working',
      questions: [question({ outcome: 'answered', resolved_at: '2026-10-14T09:01:00Z', answers: [{ label: null, text: 'Kyoto' }], answered_by: 'owner' })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('Kyoto')).toBeInTheDocument()
    expect(within(record).getByText('is working on it')).toBeInTheDocument()
  })

  it('shows a pending check rule question as asked by your check rules', async () => {
    api.steps = [step({ questions: [question({ asked_by: 'system', prompt_type: 'approval' })] })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    const asked = within(record).getByText('needs your answer below').closest('li') as HTMLElement
    expect(within(asked).getByText('Your check rules')).toBeInTheDocument()
  })

  it('shows the owner\'s reject of a question once', async () => {
    api.pending = []
    api.steps = [settledAt({
      state: 'rejected',
      questions: [question({ outcome: 'canceled', resolved_at: '2026-10-14T09:01:00Z' })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getAllByText('rejected the request')).toHaveLength(1)
  })

  it.each([
    ['expired', 'The question expired before anyone answered.'],
    ['unanswered', 'The request ended before anyone answered.'],
    ['canceled', 'The question was canceled.'],
    ['error', 'The question could not be completed.'],
  ])('shows a %s question and why the request failed', async (outcome, text) => {
    api.pending = []
    api.steps = [settledAt({
      state: 'failed',
      reason: 'Human input request expired',
      duration_ms: 3000,
      questions: [question({ outcome })],
    })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText(text)).toBeInTheDocument()
    expect(within(record).getByText("couldn't finish after 3s")).toBeInTheDocument()
    expect(within(record).getByText('Human input request expired')).toBeInTheDocument()
  })

  it('shows a request that never started', async () => {
    api.pending = []
    api.steps = [step({ state: 'failed', reason: 'run_not_started', started_at: null, questions: [] })]
    render(<SessionRecordView sessionId="sess-1" />)

    const record = await screen.findByRole('list', { name: 'Session record' })
    expect(within(record).getByText('The request never started.')).toBeInTheDocument()
  })

  it('reports a missing session', async () => {
    api.getSessionRecord.mockRejectedValueOnce(new Error('404'))
    render(<SessionRecordView sessionId="missing" />)
    expect(await screen.findByText('This session was not found.')).toBeInTheDocument()
  })
})
