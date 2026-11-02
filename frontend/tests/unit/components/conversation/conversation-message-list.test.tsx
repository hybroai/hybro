import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen } from '../../../utils/test-utils'
import { ConversationMessageList } from '@/components/conversation/ConversationMessageList'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { useTurnStore } from '@/stores/turn-store'
import type { MessageEntity } from '@/stores/message-store/types'

type Upsert = Parameters<ReturnType<typeof useMessageStore.getState>['upsertMessage']>[0]

function user(id: string, content: string, timestamp: string, extra: Partial<Upsert> = {}): Upsert {
  return {
    id, conversationId: 'room-1', messageType: 'user', content, senderName: 'You',
    timestamp, clientRequestId: `client-${id}`, ...extra,
  }
}

function agent(id: string, userId: string, content: string, extra: Partial<Upsert> = {}): Upsert {
  return {
    id, conversationId: 'room-1', messageType: 'agent', content, senderName: 'Weather Agent',
    timestamp: '2030-01-01T00:00:01.000Z', relatedMessageId: userId,
    taskStatus: 'completed' as MessageEntity['taskStatus'], ...extra,
  }
}

function canonicalTurn(userMessageId: string) {
  return {
    hybro_turn_id: `run-${userMessageId}`, run_id: `run-${userMessageId}`, user_message_id: userMessageId,
    client_request_id: `client-${userMessageId}`, state: 'completed' as const,
    started_at: '2030-01-01T00:00:10.000Z', settled_at: '2030-01-01T00:00:12.000Z',
    duration_ms: 2000, terminal_code: null, terminal_summary: null,
    internal_turns: [{
      internal_turn_id: 'turn-1', attempt: 1, message_ids: ['final-1'],
      tool_call_ids: [], status: 'completed' as const,
    }],
    activity: [], current_assistant: null,
    final_answer: {
      message_id: 'final-1', internal_turn_id: 'turn-1', text: 'Canonical answer',
      status: 'completed' as const, order: 1,
    },
    final_committed: true, hitl_interactions: [], active_interaction_id: null,
    agent_call_message_ids: [],
  }
}

/** Tag, class and data/aria-role attributes of a subtree, without text or ids. */
function markupShape(node: Element | null): unknown {
  if (!node) return null
  const attrs = [...node.attributes]
    .filter((attr) => attr.name === 'class' || attr.name === 'role' || attr.name.startsWith('data-'))
    .filter((attr) => !attr.name.startsWith('data-state') || attr.value !== '')
    .map((attr) => `${attr.name}=${attr.value}`)
    .sort()
  return { tag: node.tagName, attrs, children: [...node.children].map(markupShape) }
}

describe('ConversationMessageList', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useTurnStore.getState().clear()
    useRoomUiStore.getState().resetAll()
  })

  afterEach(cleanup)

  it('renders history without a canonical root as a read-only transcript', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Weather?', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-old', 'user-old', '**Sunny** today'), 'db')

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    const turn = container.querySelector('[data-turn-kind="transcript"]')
    expect(turn).toBeInTheDocument()
    expect(screen.getByText('Weather?')).toBeInTheDocument()
    expect(screen.getByText('Weather Agent')).toBeInTheDocument()
    expect(screen.getByText('Sunny').closest('.conversation-markdown-body')).toBeInTheDocument()
    expect(screen.queryByText(/\*\*Sunny\*\*/)).not.toBeInTheDocument()
    expect(screen.queryByRole('status', { name: /Weather Agent/ })).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace, .canonical-turn-trace')).not.toBeInTheDocument()
  })

  it('makes transcript replies quotable', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Weather?', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-old', 'user-old', 'Sunny', { agentId: 'weather' }), 'db')

    render(<ConversationMessageList conversationId="room-1" />)

    const quote = screen.getByText('Sunny').closest('[data-quote-message-id]')
    expect(quote).toHaveAttribute('data-quote-message-id', 'agent-old')
    expect(quote).toHaveAttribute('data-quote-agent-name', 'Weather Agent')
    expect(quote).toHaveAttribute('data-quote-agent-id', 'weather')
    expect(quote).toHaveAttribute('data-quote-source-kind', 'agent')
  })

  it('hides text artifacts that repeat the reply body', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Weather?', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-old', 'user-old', 'Sunny today', {
      artifacts: [{ artifactId: 'artifact-1', name: 'Forecast', parts: [{ kind: 'text', text: 'Sunny today' }] }],
    }), 'db')

    render(<ConversationMessageList conversationId="room-1" />)

    expect(screen.getAllByText('Sunny today')).toHaveLength(1)
    expect(screen.queryByText('Forecast')).not.toBeInTheDocument()
  })

  it('leaves ephemeral, HITL, digest and empty agent messages out of the transcript', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Weather?', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-ok', 'user-old', 'Sunny'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-empty', 'user-old', '   '), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-hitl', 'user-old', 'Which city?', {
      hitlRequestId: 'request-1', hitlPrompt: 'Which city?', taskStatus: 'input-required' as MessageEntity['taskStatus'],
    }), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-digest', 'user-old', '2 agents responded. Expand below to read each answer.', {
      senderName: 'HYBRO AI', summaryOrigin: 'deterministic',
    }), 'db')

    render(<ConversationMessageList conversationId="room-1" />)

    expect(screen.getByText('Sunny')).toBeInTheDocument()
    expect(screen.queryByText('Which city?')).not.toBeInTheDocument()
    expect(screen.queryByText(/agents responded/)).not.toBeInTheDocument()
    expect(screen.getAllByText('Weather Agent')).toHaveLength(1)
  })

  it('keeps orphaned HITL prompts out of the conversation body', () => {
    useMessageStore.getState().upsertMessage({
      id: 'hitl-message-1', conversationId: 'room-1', messageType: 'agent', content: '',
      senderName: 'Cyber Broker Agent', timestamp: '2030-01-01T00:00:01.000Z',
      taskStatus: 'input-required', hitlRequestId: 'cloud-providers',
      hitlInteractionId: 'interaction-1', hitlPrompt: 'Which cloud providers do you use?',
      hitlPromptType: 'text', hitlGroupIndex: 0, hitlGroupTotal: 1, hitlResolved: false,
    }, 'sse')

    render(<ConversationMessageList conversationId="room-1" />)

    expect(screen.queryByText('Unattributed responses')).not.toBeInTheDocument()
    expect(screen.queryByText('Which cloud providers do you use?')).not.toBeInTheDocument()
  })

  it('skips user shells that have no content yet', () => {
    useMessageStore.getState().upsertMessage(user('user-shell', '', '2030-01-01T00:00:00.000Z'), 'sse')

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    expect(container.querySelector('[data-turn-kind]')).not.toBeInTheDocument()
  })

  it('shows the Running header without work logs while a sent message waits for its canonical Turn', () => {
    useMessageStore.getState().upsertMessage(user('user-pending', 'Weather?', '2030-01-01T00:00:00.000Z'), 'optimistic')
    useRoomUiStore.getState().setProcessing('room-1', true)

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    expect(container.querySelector('[data-turn-kind="pending-user"]')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^work logs, running,/i })).toHaveAttribute('aria-expanded', 'true')
    expect(container.querySelector('[data-canonical-turn-trace][data-status="running"]')).toBeInTheDocument()
    expect(screen.getByText('Preparing a response')).toBeVisible()
    expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
  })

  it('swaps the pending header for the canonical Turn without repeating the message', () => {
    useMessageStore.getState().upsertMessage(user('user-pending', 'Weather?', '2030-01-01T00:00:00.000Z'), 'optimistic')
    useRoomUiStore.getState().setProcessing('room-1', true)
    const { container } = render(<ConversationMessageList conversationId="room-1" />)
    const pendingShape = markupShape(container.querySelector('[data-turn-slot="trace"]'))

    act(() => {
      useTurnStore.getState().replaceSnapshot('room-1', [{
        ...canonicalTurn('user-pending'), state: 'active' as const, settled_at: null, duration_ms: null,
        internal_turns: [], final_answer: null, final_committed: false,
      }])
    })

    expect(container.querySelector('[data-turn-kind="pending-user"]')).not.toBeInTheDocument()
    expect(container.querySelector('[data-turn-kind="canonical"]')).toBeInTheDocument()
    expect(screen.getAllByText('Weather?')).toHaveLength(1)
    expect(screen.getByRole('button', { name: /^work logs, running,/i })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Preparing a response')).toBeVisible()
    expect(markupShape(container.querySelector('[data-turn-slot="trace"]'))).toEqual(pendingShape)
  })

  it('shows a processing last message as a transcript once a reply has arrived', () => {
    useMessageStore.getState().upsertMessage(user('user-pending', 'Weather?', '2030-01-01T00:00:00.000Z'), 'optimistic')
    useMessageStore.getState().upsertMessage(agent('agent-pending', 'user-pending', 'Checking the forecast'), 'sse')
    useRoomUiStore.getState().setProcessing('room-1', true)

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    expect(container.querySelector('[data-turn-kind="pending-user"]')).not.toBeInTheDocument()
    expect(screen.getByText('Checking the forecast')).toBeInTheDocument()
  })

  it('keeps only the last message pending while the room is processing', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Old question', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(user('user-new', 'New question', '2030-01-01T00:00:10.000Z'), 'optimistic')
    useRoomUiStore.getState().setProcessing('room-1', true)

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    const kinds = [...container.querySelectorAll('[data-turn-kind]')].map((node) => node.getAttribute('data-turn-kind'))
    expect(kinds).toEqual(['transcript', 'pending-user'])
  })

  it('does not repeat agent messages of a canonical Turn as a transcript', () => {
    useMessageStore.getState().upsertMessage(user('user-new', 'New question', '2030-01-01T00:00:10.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-call', 'user-new', 'Agent call output'), 'db')
    useTurnStore.getState().replaceSnapshot('room-1', [canonicalTurn('user-new')])

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    expect(container.querySelector('[data-turn-kind="transcript"]')).not.toBeInTheDocument()
    expect(screen.queryByText('Agent call output')).not.toBeInTheDocument()
  })

  it('orders transcript and canonical Turns by their user messages', () => {
    useMessageStore.getState().upsertMessage(user('user-old', 'Old question', '2030-01-01T00:00:00.000Z'), 'db')
    useMessageStore.getState().upsertMessage(agent('agent-old', 'user-old', 'Old answer'), 'db')
    useMessageStore.getState().upsertMessage(user('user-new', 'New question', '2030-01-01T00:00:10.000Z'), 'db')
    useTurnStore.getState().replaceSnapshot('room-1', [canonicalTurn('user-new')])

    const { container } = render(<ConversationMessageList conversationId="room-1" />)

    const kinds = [...container.querySelectorAll('[data-turn-kind]')].map((node) => node.getAttribute('data-turn-kind'))
    expect(kinds).toEqual(['transcript', 'canonical'])
    expect(screen.getByText('Canonical answer')).toBeInTheDocument()
  })
})
