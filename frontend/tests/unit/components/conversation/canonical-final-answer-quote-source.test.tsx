import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '../../../utils/test-utils'
import { CanonicalTurnRenderer } from '@/components/conversation/CanonicalTurnRenderer'
import type { AssistantProjection, TurnProjection } from '@/lib/turn-lifecycle/types'
import { useTurnPresentationStore } from '@/stores/turn-presentation-store'

function assistant(status: AssistantProjection['status']): AssistantProjection {
  return {
    messageId: 'assistant-1', internalTurnId: 'turn-1', text: 'Paris is rainy today.',
    status, contentIndex: 0, nextDeltaIndex: 0, endOffset: 21, order: 1,
  }
}

function turn(overrides: Partial<TurnProjection>): TurnProjection {
  return {
    id: 'run-1',
    runId: 'run-1',
    conversationId: 'room-1',
    userMessageId: 'user-1',
    clientRequestId: 'client-1',
    state: 'active',
    startedAt: '2030-01-01T00:00:00.000Z',
    internalTurns: [{
      internalTurnId: 'turn-1', attempt: 1, messageIds: ['assistant-1'], toolCallIds: [], status: 'active',
    }],
    activity: [],
    finalCommitted: false,
    hitlInteractions: [],
    agentCallMessageIds: [],
    ...overrides,
  }
}

function finalAnswerSurface(container: HTMLElement) {
  return container.querySelector('[data-canonical-final-answer]')
}

describe('canonical final answer quote source', () => {
  afterEach(() => {
    cleanup()
    useTurnPresentationStore.setState(useTurnPresentationStore.getInitialState(), true)
  })

  it('marks the completed final answer as a synthesis quote source', () => {
    const value = turn({ state: 'completed', finalCommitted: true, finalAnswer: assistant('completed') })
    useTurnPresentationStore.getState().ensure(value, true)
    const { container } = render(<CanonicalTurnRenderer turn={value} />)

    const surface = finalAnswerSurface(container)
    expect(surface).toHaveAttribute('data-quote-message-id', 'assistant-1')
    expect(surface).toHaveAttribute('data-quote-source-kind', 'synthesis')
    expect(surface).toHaveAttribute('data-quote-agent-name', 'HYBRO AI')
  })

  it('does not mark a completed answer as quotable before it is stored', () => {
    const value = turn({ state: 'completed', finalCommitted: false, finalAnswer: assistant('completed') })
    useTurnPresentationStore.getState().ensure(value, true)
    const { container } = render(<CanonicalTurnRenderer turn={value} />)

    expect(finalAnswerSurface(container)).not.toHaveAttribute('data-quote-message-id')
  })

  it('does not mark a streaming answer as quotable', () => {
    const value = turn({ currentAssistant: assistant('streaming') })
    useTurnPresentationStore.getState().ensure(value, true)
    const { container } = render(<CanonicalTurnRenderer turn={value} />)

    const surface = finalAnswerSurface(container)
    expect(surface).not.toBeNull()
    expect(surface).not.toHaveAttribute('data-quote-message-id')
  })
})
