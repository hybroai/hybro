import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '../../../utils/test-utils'
import { CanonicalTurnRenderer } from '@/components/conversation/CanonicalTurnRenderer'
import type { TurnActivityItem, TurnProjection } from '@/lib/turn-lifecycle/types'
import { useMessageStore } from '@/stores/message-store'
import { useTurnPresentationStore } from '@/stores/turn-presentation-store'
import { fetchCanonicalAgentCallDetail } from '@/lib/api/agent-call-detail'
import { ApiError } from '@/lib/api-client'

vi.mock('@/lib/api/agent-call-detail', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/lib/api/agent-call-detail')>()
  return {
    ...original,
    fetchCanonicalAgentCallDetail: vi.fn(),
  }
})

vi.mock('@/hooks/useRoomFile', () => ({
  useRoomFile: () => ({
    objectUrl: 'blob:generated-image',
    error: null,
    download: vi.fn(),
  }),
}))

function turn(overrides: Partial<TurnProjection> = {}): TurnProjection {
  return {
    id: 'run-1',
    runId: 'run-1',
    conversationId: 'room-1',
    userMessageId: 'user-1',
    clientRequestId: 'client-1',
    state: 'active',
    startedAt: '2030-01-01T00:00:00.000Z',
    internalTurns: [{
      internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: [], status: 'active',
    }],
    activity: [],
    finalCommitted: false,
    hitlInteractions: [],
    agentCallMessageIds: [],
    ...overrides,
  }
}

function toolItem(
  toolCallId: string,
  overrides: Partial<Extract<TurnActivityItem, { kind: 'tool' }>> = {},
): Extract<TurnActivityItem, { kind: 'tool' }> {
  return {
    kind: 'tool',
    id: toolCallId,
    internalTurnId: 'turn-1',
    toolCallId,
    label: 'Weather Agent',
    input: {},
    partialResult: '',
    result: 'Sunny',
    isError: false,
    durationMs: 100,
    updateIndex: 0,
    status: 'completed',
    executionKind: 'tool',
    requestSummary: '',
    detailAvailable: false,
    order: 2,
    ...overrides,
  }
}

beforeEach(() => {
  useMessageStore.getState().clearRoom()
  useMessageStore.getState().setRoom('room-1')
  useMessageStore.getState().upsertMessage({
    id: 'user-1', conversationId: 'room-1', messageType: 'user', content: 'Weather?',
    senderName: 'You', timestamp: '2030-01-01T00:00:00.000Z', clientRequestId: 'client-1',
  }, 'db')
  useTurnPresentationStore.getState().clear()
  vi.mocked(fetchCanonicalAgentCallDetail).mockReset()
  vi.mocked(fetchCanonicalAgentCallDetail).mockResolvedValue(null)
})

afterEach(cleanup)

describe('canonical Turn renderer', () => {
  it('uses explicit User → Trace → Final → Agent Cards DOM order and safe Markdown final rendering', () => {
    // Stale legacy card entities must never leak into the canonical render.
    useMessageStore.getState().upsertMessage({
      id: 'orchestrator:run-1:inv_weather_0001', conversationId: 'room-1', messageType: 'agent',
      content: 'PRIVATE_SECRET', senderName: 'Weather Agent', agentId: 'private-agent-id',
      timestamp: '2030-01-01T00:00:01.000Z', clientRequestId: 'client-1',
      relatedMessageId: 'user-1', taskStatus: 'completed',
    }, 'sse')
    const value = turn({
      currentAssistant: {
        messageId: 'assistant-1', internalTurnId: 'turn-1', text: '**Sunny** in Shanghai',
        status: 'streaming', contentIndex: 0, nextDeltaIndex: 1, endOffset: 21, order: 6,
      },
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['inv_weather_0001'], status: 'active',
      }],
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent', targetName: 'Weather Agent', requestSummary: 'Weather?',
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    const { container } = render(<CanonicalTurnRenderer turn={value} isLastTurn />)

    expect(screen.getByText('Weather?')).toBeInTheDocument()
    expect(screen.getByText('Sunny', { selector: '[data-streamdown="strong"]' })).toBeInTheDocument()
    expect(screen.queryByText('PRIVATE_SECRET')).not.toBeInTheDocument()
    expect(container).not.toHaveTextContent('private-agent-id')
    const slots = [...container.querySelectorAll('[data-turn-slot]')]
    expect(slots.map((node) => node.getAttribute('data-turn-slot'))).toEqual(['user', 'trace', 'final', 'agents'])
    expect(slots[1]?.nextElementSibling).toHaveAttribute('data-slot', 'separator')
    expect(slots[1]?.nextElementSibling).toHaveClass('conversation-trace-separator')
  })

  it('renders canonical room-file artifacts under the final answer from authenticated call details', async () => {
    vi.mocked(fetchCanonicalAgentCallDetail).mockResolvedValue({
      run_id: 'run-1',
      public_call_id: 'inv_image_0001',
      status: 'completed',
      parts: [{ kind: 'text', text: '[Generated file]' }],
      artifacts: [{
        artifact_ref: '/api/v1/files/af011190aaba4f97b459e7656bba7f7e/content',
        file_id: 'af011190aaba4f97b459e7656bba7f7e',
        name: 'generated-image.png',
        mime_type: 'image/png',
        size_bytes: 2332106,
      }],
    })
    const value = turn({
      state: 'completed',
      finalCommitted: true,
      finalAnswer: {
        messageId: 'assistant-1', internalTurnId: 'turn-1', text: 'Here is the image.',
        status: 'completed', contentIndex: 0, nextDeltaIndex: 0, endOffset: 18, order: 5,
      },
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: ['assistant-1'],
        toolCallIds: ['inv_image_0001'], status: 'completed',
      }],
      activity: [toolItem('inv_image_0001', {
        label: 'Image Generator Agent', executionKind: 'agent',
        targetName: 'Image Generator Agent', detailAvailable: true,
      })],
    })
    useTurnPresentationStore.getState().ensure(value, true)
    render(<CanonicalTurnRenderer turn={value} />)

    expect(await screen.findByRole('img', { name: 'generated-image.png' })).toHaveAttribute(
      'src',
      'blob:generated-image',
    )
    expect(fetchCanonicalAgentCallDetail).toHaveBeenCalledWith(
      'room-1',
      'orchestrator:run-1:inv_image_0001',
      expect.any(Function),
      expect.any(AbortSignal),
    )
  })

  it('retries transient final-artifact detail failures before exposing an error', async () => {
    vi.mocked(fetchCanonicalAgentCallDetail)
      .mockRejectedValueOnce(new ApiError(503, 'Detail projection unavailable'))
      .mockResolvedValueOnce({
        run_id: 'run-1',
        public_call_id: 'inv_retry_image_0001',
        status: 'completed',
        parts: [],
        artifacts: [{
          artifact_ref: '/api/v1/files/af011190aaba4f97b459e7656bba7f7e/content',
          file_id: 'af011190aaba4f97b459e7656bba7f7e',
          name: 'retry-image.png',
          mime_type: 'image/png',
          size_bytes: 1024,
        }],
      })
    const value = turn({
      state: 'completed',
      finalCommitted: true,
      finalAnswer: {
        messageId: 'assistant-1', internalTurnId: 'turn-1', text: 'Image ready.',
        status: 'completed', contentIndex: 0, nextDeltaIndex: 0, endOffset: 12, order: 5,
      },
      activity: [toolItem('inv_retry_image_0001', {
        executionKind: 'agent', targetName: 'Image Generator Agent', detailAvailable: true,
      })],
    })
    useTurnPresentationStore.getState().ensure(value, true)
    render(<CanonicalTurnRenderer turn={value} />)

    expect(await screen.findByRole('img', { name: 'retry-image.png' })).toBeInTheDocument()
    expect(fetchCanonicalAgentCallDetail).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('Generated files could not be loaded.')).not.toBeInTheDocument()
  })

  it('shows Retry when an artifact-only final response cannot load its detail', async () => {
    vi.mocked(fetchCanonicalAgentCallDetail).mockRejectedValue(
      new ApiError(503, 'Detail projection unavailable'),
    )
    const value = turn({
      state: 'completed',
      finalCommitted: true,
      activity: [toolItem('inv_artifact_only_0001', {
        executionKind: 'agent', targetName: 'Image Generator Agent', detailAvailable: true,
      })],
    })
    useTurnPresentationStore.getState().ensure(value, true)
    render(<CanonicalTurnRenderer turn={value} />)

    expect(await screen.findByText('Generated files could not be loaded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('keeps canonical HITL content out of the Final body and records it in Trace', () => {
    const value = turn({
      state: 'awaiting_input', activeInteractionId: 'interaction-1',
      hitlInteractions: [{
        interactionId: 'interaction-1', state: 'awaiting_input', requestIds: ['request-1'],
        requestedAt: '2030-01-01T00:00:02.000Z',
        requests: [{
          requestId: 'request-1', messageId: 'hitl-message-1', questionIndex: 0,
          questionCount: 1, prompt: 'Which city?', promptType: 'text', choices: [],
          source: 'supervisor', status: 'requested',
        }],
      }],
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['ask-1'], status: 'active',
      }],
      activity: [toolItem('ask-1', {
        label: 'request_user_input',
        executionKind: 'tool',
        status: 'suspended',
        result: '',
        durationMs: undefined,
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    const { container } = render(<CanonicalTurnRenderer turn={value} />)

    // The question content lives only in the composer interaction UI; the
    // body/final slot stays empty while the Trace records the event.
    expect(screen.queryByText('Your input is needed')).not.toBeInTheDocument()
    expect(screen.queryByText('Which city?')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /waiting for input, 0\.0s/i })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Asking you')).toBeInTheDocument()
    expect(container.querySelector('[data-kind="tool"]')).toHaveTextContent('Waiting for input')
    const slots = [...container.querySelectorAll('[data-turn-slot]')]
    expect(slots.map((node) => node.getAttribute('data-turn-slot'))).toEqual(['user', 'trace', 'final'])
    expect(slots[1]?.nextElementSibling).toBe(slots[2])
    expect(container.querySelector('.conversation-trace-separator')).not.toBeInTheDocument()
    expect(slots.find((node) => node.getAttribute('data-turn-slot') === 'final')?.textContent).toBe('')
  })

  it('never substitutes a skill-qualified Trace label for a missing root Agent name', () => {
    const value = turn({
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['inv_weather_0001'], status: 'active',
      }],
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent',
        label: 'Weather Agent - Get Current Weather',
        targetName: undefined,
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    render(<CanonicalTurnRenderer turn={value} />)

    fireEvent.click(screen.getByRole('button', { name: /agent responses/i }))
    expect(screen.getByRole('status', { name: /unknown agent — completed/i })).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: /weather agent - get current weather/i })).not.toBeInTheDocument()
  })

  it('preserves a legitimate hyphenated Agent name from the execution target', () => {
    const value = turn({
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['inv_claims_0001'], status: 'active',
      }],
      activity: [toolItem('inv_claims_0001', {
        executionKind: 'agent', targetName: 'Acme - Claims', label: 'Acme - Claims',
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    render(<CanonicalTurnRenderer turn={value} />)

    fireEvent.click(screen.getByRole('button', { name: /agent responses/i }))
    expect(screen.getByRole('status', { name: /acme - claims — completed/i })).toBeInTheDocument()
  })

  it('opens canonical Agent detail with only the opaque durable message ID', () => {
    const onOpenAgentDetail = vi.fn()
    const value = turn({
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['inv_weather_0001'], status: 'active',
      }],
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent', targetName: 'Weather Agent',
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    render(
      <CanonicalTurnRenderer
        turn={value}
        onOpenAgentDetail={onOpenAgentDetail}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /agent responses/i }))
    const cardButton = screen.getByRole('button', { name: /weather agent/i })
    expect(screen.queryByRole('link', { name: 'Weather Agent' })).not.toBeInTheDocument()
    expect(cardButton.querySelector('a, button, [role="button"], [role="link"]')).toBeNull()
    fireEvent.click(cardButton)
    expect(onOpenAgentDetail).toHaveBeenCalledWith('orchestrator:run-1:inv_weather_0001')
    expect(onOpenAgentDetail).not.toHaveBeenCalledWith('private-agent-id')
  })

  it('opens active Trace, derives a concise preparation action, and uses active-only live roles with decorative icons', () => {
    const value = turn()
    useTurnPresentationStore.getState().ensure(value, false)
    const { container } = render(<CanonicalTurnRenderer turn={value} />)

    expect(screen.getByRole('button', { name: /running, 0\.0s/i })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('log')).toHaveAttribute('aria-live', 'polite')
    expect(container.querySelector('[data-kind="preparing"]')).toHaveTextContent('Preparing a responseRunning')
    expect(container.querySelector('[data-slot="marker-icon"]')).toHaveAttribute('aria-hidden', 'true')
  })

  it('keeps only the concise preparation action when an Assistant response starts', () => {
    const value = turn({
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: ['assistant-1'], toolCallIds: [], status: 'active',
      }],
      currentAssistant: {
        messageId: 'assistant-1', internalTurnId: 'turn-1', text: '', status: 'streaming',
        contentIndex: 0, nextDeltaIndex: 0, endOffset: 0, order: 2,
      },
    })
    useTurnPresentationStore.getState().ensure(value, false)
    render(<CanonicalTurnRenderer turn={value} />)
    expect(screen.getByText('Preparing a response')).toBeInTheDocument()
    expect(screen.queryByText('Thinking…')).not.toBeInTheDocument()
  })

  it('initializes a historical terminal snapshot with a concise Finished status and server duration', () => {
    const value = turn({
      state: 'completed',
      durationMs: 18_600,
      finalCommitted: true,
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: ['assistant-1'],
        toolCallIds: ['inv_weather_0001'], status: 'completed',
      }],
      activity: [toolItem('inv_weather_0001', {
        durationMs: 2900, order: 4,
      })],
      finalAnswer: {
        messageId: 'assistant-1', internalTurnId: 'turn-1', text: 'Sunny',
        status: 'completed', contentIndex: 0, nextDeltaIndex: 0, endOffset: 5, order: 8,
      },
    })
    useTurnPresentationStore.getState().ensure(value, true)
    render(<CanonicalTurnRenderer turn={value} />)

    const trigger = screen.getByRole('button', { name: /finished, 18\.6s/i })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('log')).not.toBeInTheDocument()
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByRole('log')).not.toBeInTheDocument()
    expect(screen.getByText('Used Weather Agent')).toBeInTheDocument()
  })

  it('keeps suspended Agent call identity and awaiting status synchronized', () => {
    const value = turn({
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent', targetName: 'Weather Agent', status: 'suspended',
        durationMs: undefined,
      })],
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [],
        toolCallIds: ['inv_weather_0001'], status: 'active',
      }],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    const { container } = render(<CanonicalTurnRenderer turn={value} isLastTurn />)

    const traceCall = container.querySelector('[data-kind="agent-call"]')
    const card = container.querySelector('.conversation-agent-card')
    expect(traceCall).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(traceCall).toHaveAttribute('data-status', 'awaiting_input')
    expect(card).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(card).toHaveAttribute('data-status', 'awaiting_input')
    expect(screen.getAllByText('Waiting for input').length).toBeGreaterThanOrEqual(2)
  })

  it('uses Finished for a canceled Run while Trace and Card share canceled call state', () => {
    const value = turn({
      state: 'canceled', durationMs: 1_200,
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent', targetName: 'Weather Agent', status: 'canceled',
      })],
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [],
        toolCallIds: ['inv_weather_0001'], status: 'aborted',
      }],
    })
    useTurnPresentationStore.getState().ensure(value, true)
    const { container } = render(<CanonicalTurnRenderer turn={value} isLastTurn />)

    const trigger = screen.getByRole('button', { name: /finished, 1\.2s/i })
    fireEvent.click(trigger)
    const traceCall = container.querySelector('[data-kind="agent-call"]')
    const card = container.querySelector('.conversation-agent-card')
    expect(traceCall).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(traceCall).toHaveAttribute('data-status', 'canceled')
    expect(card).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(card).toHaveAttribute('data-status', 'canceled')
    expect(screen.getAllByText('Canceled').length).toBeGreaterThanOrEqual(2)
  })

  it('shows agent executions as orchestrator log lines without input/output details', () => {
    const value = turn({
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: ['inv_weather_0001'], status: 'active',
      }],
      activity: [toolItem('inv_weather_0001', {
        executionKind: 'agent', targetName: 'Weather Agent',
        input: { city: 'Shanghai' }, requestSummary: 'Check the weather',
      })],
    })
    useTurnPresentationStore.getState().ensure(value, false)
    const { container } = render(<CanonicalTurnRenderer turn={value} isLastTurn />)

    expect(screen.queryByRole('button', { name: /show weather agent input and output/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/city: Shanghai/)).not.toBeInTheDocument()
    const call = container.querySelector('[data-kind="agent-call"]')
    const card = container.querySelector('.conversation-agent-card')
    expect(call).toHaveClass('conversation-trace-action-success')
    expect(call?.querySelector('[data-slot="marker-icon"] svg')).toHaveClass('lucide-bot')
    expect(call).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(call).toHaveAttribute('data-status', 'completed')
    expect(card).toHaveAttribute('data-call-id', 'inv_weather_0001')
    expect(card).toHaveAttribute('data-status', 'completed')
  })

  it('defers automatic settlement collapse while focus remains inside Trace', () => {
    const toolActivity: TurnProjection['activity'] = [toolItem('inv_weather_0001', {
      input: { city: 'Shanghai' }, durationMs: 200, order: 3,
    })]
    const active = turn({ activity: toolActivity })
    useTurnPresentationStore.getState().ensure(active, false)
    const { rerender } = render(<CanonicalTurnRenderer turn={active} />)
    const traceTrigger = screen.getByRole('button', { name: /running, 0\.0s/i })
    traceTrigger.focus()
    expect(traceTrigger).toHaveFocus()

    const settled = turn({
      state: 'failed', durationMs: 400, terminalSummary: 'Tool failed', activity: toolActivity,
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [],
        toolCallIds: ['inv_weather_0001'], status: 'error',
      }],
    })
    rerender(<CanonicalTurnRenderer turn={settled} />)
    const settledTrigger = screen.getByRole('button', { name: /finished, 0\.4s/i })
    expect(settledTrigger).toHaveAttribute('aria-expanded', 'true')

    const outside = document.createElement('button')
    document.body.append(outside)
    fireEvent.blur(settledTrigger, { relatedTarget: outside })
    expect(settledTrigger).toHaveAttribute('aria-expanded', 'false')
    outside.remove()
  })

  it('preserves manual reopen after the one-time settlement collapse', () => {
    const active = turn()
    useTurnPresentationStore.getState().ensure(active, false)
    const { rerender } = render(<CanonicalTurnRenderer turn={active} />)
    const activeTrigger = screen.getByRole('button', { name: /running, 0\.0s/i })
    fireEvent.click(activeTrigger)
    expect(activeTrigger).toHaveAttribute('aria-expanded', 'false')

    const settled = turn({
      state: 'failed', durationMs: 750, terminalCode: 'provider_error', terminalSummary: 'Provider unavailable',
      internalTurns: [{
        internalTurnId: 'turn-1', attempt: 1, messageIds: [], toolCallIds: [], status: 'error',
      }],
    })
    rerender(<CanonicalTurnRenderer turn={settled} />)
    const settledTrigger = screen.getByRole('button', { name: /finished, 0\.8s/i })
    expect(settledTrigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(settledTrigger)
    expect(settledTrigger).toHaveAttribute('aria-expanded', 'true')

    rerender(<CanonicalTurnRenderer turn={{ ...settled }} />)
    expect(screen.getByRole('button', { name: /finished, 0\.8s/i })).toHaveAttribute('aria-expanded', 'true')
  })
})
