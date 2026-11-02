import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '../../../utils/test-utils'
import { TurnRenderer } from '@/components/conversation/TurnRenderer'
import { useMessageStore } from '@/stores/message-store'
import type { TurnViewModel } from '@/lib/room-timeline/types'
import { CANCELED_TURN_INTRO, FAILED_TURN_INTRO } from '@/lib/room-timeline/derive-final-answer'

beforeEach(() => {
  useMessageStore.getState().clearRoom()
  useMessageStore.getState().setRoom('room-1')
})

afterEach(() => {
  cleanup()
})

function makeTurn(overrides: Partial<TurnViewModel> = {}): TurnViewModel {
  return {
    id: 'user-1',
    conversationId: 'room-1',
    userMessageId: 'user-1',
    userContent: 'how are you?',
    userAttachments: [],
    timestamp: '2026-06-04T01:00:00.000Z',
    status: 'active',
    events: [],
    summary: null,
    agentResults: [],
    activeAgentIds: [],
    isSupervisorTurn: false,
    displayMode: 'working',
    phase: 'collecting',
    processingStatusLogs: [
      {
        id: 'processing-log-1',
        message: 'Thinking...',
        timestamp: '2026-06-04T01:00:01.000Z',
      },
    ],
    finalAnswer: { kind: 'pending', label: 'Working' },
    ...overrides,
  }
}

describe('TurnRenderer', () => {

  it('renders a legacy Turn awaiting input with its work logs and no trace header', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1',
      conversationId: 'room-1',
      messageType: 'user',
      content: 'weather?',
      senderName: 'Test',
      timestamp: '2026-06-04T01:00:00.000Z',
      clientRequestId: 'request-1',
    }, 'db')

    const { container } = render(<TurnRenderer turn={makeTurn({
      status: 'awaiting_input',
      displayMode: 'awaiting_input',
      processingStatusLogs: [{
        id: 'processing-log-1',
        message: 'Asking Weather Agent',
        timestamp: '2026-06-04T01:00:01.000Z',
      }],
      finalAnswer: { kind: 'hitl', label: 'Needs input' },
    })} />)

    expect(screen.getByText('weather?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /work logs/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /waiting for input,/i })).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace')).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace-separator')).not.toBeInTheDocument()
  })

  it('keeps the Thinking placeholder out of a legacy Turn awaiting input', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'weather?',
      senderName: 'Test', timestamp: '2026-06-04T01:00:00.000Z', clientRequestId: 'request-1',
    }, 'db')

    const { container } = render(<TurnRenderer turn={makeTurn({
      status: 'awaiting_input',
      displayMode: 'awaiting_input',
      finalAnswer: { kind: 'hitl', label: 'Needs input' },
    })} />)

    expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /waiting for input,/i })).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace')).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-body-frame')).not.toBeInTheDocument()
  })

  it('renders no trace panel for a settled legacy Turn whose only log is the Thinking placeholder', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'weather?',
      senderName: 'Test', timestamp: '2026-06-04T01:00:00.000Z', clientRequestId: 'cr-1',
    }, 'db')

    const { container } = render(<TurnRenderer isLastTurn turn={makeTurn({
      status: 'completed', phase: 'completed',
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
      agentResults: [{
        agentId: 'weather-agent', agentName: 'Weather Agent', messageId: 'call-1',
        status: 'completed', content: 'Sunny', artifacts: [],
        isSummaryAgent: false, isEphemeral: false,
      }],
    })} />)

    expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /finished,/i })).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace-separator')).not.toBeInTheDocument()
  })

  it.each([
    ['failed', { kind: 'failed', label: 'Failed', failedIntro: FAILED_TURN_INTRO }, FAILED_TURN_INTRO],
    ['canceled', { kind: 'canceled', label: 'Canceled', canceledIntro: CANCELED_TURN_INTRO }, CANCELED_TURN_INTRO],
  ] as const)('keeps the %s message for a legacy Turn whose only log is the Thinking placeholder', (status, finalAnswer, intro) => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'weather?',
      senderName: 'Test', timestamp: '2026-06-04T01:00:00.000Z', clientRequestId: 'cr-1',
    }, 'db')

    const { container } = render(<TurnRenderer isLastTurn turn={makeTurn({
      status: status === 'failed' ? 'failed' : 'partial',
      phase: 'completed',
      turnTerminalStatus: status,
      finalAnswer,
    })} />)

    expect(container).toHaveTextContent(intro)
    expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /finished,/i })).not.toBeInTheDocument()
  })

  it('shows the preparing marker for a running legacy Turn whose only log is the Thinking placeholder', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'weather?',
      senderName: 'Test', timestamp: '2026-06-04T01:00:00.000Z', clientRequestId: 'cr-1',
    }, 'db')

    render(<TurnRenderer isLastTurn turn={makeTurn()} />)

    expect(screen.getByText('Preparing a response')).toBeVisible()
    expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
  })

  it('renders agent cards even when the detail drawer callback is absent', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1',
      conversationId: 'room-1',
      messageType: 'user',
      content: 'weather?',
      senderName: 'Test',
      timestamp: '2026-06-04T01:00:00.000Z',
    }, 'db')

    render(<TurnRenderer isLastTurn turn={makeTurn({
      status: 'completed',
      phase: 'completed',
      processingStatusLogs: [],
      finalAnswer: {
        kind: 'deterministic_done',
        label: 'Combined agent responses',
        deterministicIntro: '1 agent responded.',
      },
      agentResults: [{
        agentId: 'weather-agent',
        agentName: 'Weather Agent',
        messageId: 'call-1',
        status: 'completed',
        content: 'Sunny',
        artifacts: [],
        isSummaryAgent: false,
        isEphemeral: false,
      }],
    })} />)

    expect(screen.getByRole('status', { name: 'Weather Agent — Completed' })).toBeInTheDocument()
  })

  it('renders separate cards for repeated calls to the same agent', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1',
      conversationId: 'room-1',
      messageType: 'user',
      content: 'weather twice',
      senderName: 'Test',
      timestamp: '2026-06-04T01:00:00.000Z',
    }, 'db')
    const result = {
      agentId: 'weather-agent',
      agentName: 'Weather Agent',
      status: 'completed' as const,
      content: 'Sunny',
      artifacts: [],
      isSummaryAgent: false,
      isEphemeral: false,
    }

    render(<TurnRenderer isLastTurn turn={makeTurn({
      status: 'completed',
      phase: 'completed',
      processingStatusLogs: [],
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
      agentResults: [
        { ...result, messageId: 'call-1' },
        { ...result, messageId: 'call-2', content: 'Cloudy' },
      ],
    })} />)

    expect(screen.getAllByRole('status', { name: 'Weather Agent — Completed' })).toHaveLength(2)
  })

  it('renders a settled legacy Turn with its agent results and no trace panel', () => {
    useMessageStore.getState().upsertMessage({
      id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'weather?',
      senderName: 'Test', timestamp: '2026-06-04T01:00:00.000Z', clientRequestId: 'cr-1',
    }, 'db')

    const { container } = render(<TurnRenderer isLastTurn turn={makeTurn({
      status: 'completed', phase: 'completed', processingStatusLogs: [],
      finalAnswer: { kind: 'deterministic_done', label: 'Combined agent responses' },
      agentResults: [{
        agentId: 'weather-agent', agentName: 'Weather Agent', messageId: 'call-1',
        status: 'completed', content: 'Sunny', artifacts: [],
        isSummaryAgent: false, isEphemeral: false,
      }],
    })} />)

    expect(screen.getByText('weather?')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Weather Agent — Completed' })).toBeInTheDocument()
    expect(container.querySelector('.conversation-trace')).not.toBeInTheDocument()
    expect(container.querySelector('.conversation-trace-separator')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /finished,/i })).not.toBeInTheDocument()
  })
})
