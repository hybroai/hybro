import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, cleanup } from '@testing-library/react'
import {
  ComposerShell,
  type ComposerShellAdapter,
} from '@/components/composer/ComposerShell'
import { useMessageStore } from '@/stores/message-store'
import { useTurnStore } from '@/stores/turn-store'
import { createSSEDispatcher } from '@/hooks/room/sse-handlers/dispatch'
import type { AnySSEFrame } from '@/lib/types/sse'
import { createProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { useRoomUiStore } from '@/stores/room-ui-store'
import {
  hitlQuestionEntityId,
  hitlRequestKey,
} from '@/lib/hitl/hitl-message-projection'

import { setupNotice as realSetupNotice, type SetupNotice } from '@/lib/setup-notice'

let setupNotice: SetupNotice | null = null
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupNotice: () => setupNotice }))
vi.mock('next/navigation', () => ({ usePathname: () => '/room/test-room' }))

vi.mock('@/components/room-chat-input', () => ({
  RoomChatInput: (props: any) => (
    <div
      data-testid="room-chat-input"
      data-disabled={props.disableSend}
      data-editor-disabled={props.disabled ? 'true' : 'false'}
      data-placeholder={props.placeholder}
      data-processing={props.processing}
      data-has-top-slot={props.topSlot ? 'true' : 'false'}
      data-target-mode={props.selectedGroupDispatch?.message_target_mode}
    >
      {props.topSlot}
    </div>
  ),
}))

function installCompletedCanonicalTurn(): void {
  const result = useTurnStore.getState().replaceSnapshot('test-room', [{
    hybro_turn_id: 'canonical-complete', run_id: 'canonical-complete',
    user_message_id: 'canonical-user', client_request_id: 'canonical-client',
    state: 'completed', started_at: '2030-01-01T00:00:00.000Z',
    settled_at: '2030-01-01T00:00:01.000Z', duration_ms: 1000,
    terminal_code: null, terminal_summary: null,
    internal_turns: [{
      internal_turn_id: 'canonical-turn', attempt: 1,
      message_ids: ['canonical-final'], tool_call_ids: [], status: 'completed',
    }],
    activity: [], current_assistant: null,
    final_answer: {
      message_id: 'canonical-final', internal_turn_id: 'canonical-turn',
      text: 'Done', status: 'completed', order: 4,
    },
    final_committed: true, hitl_interactions: [], active_interaction_id: null,
    agent_call_message_ids: [],
  }])
  if (!result.ok) throw new Error(result.violation)
}

const mockAdapter: ComposerShellAdapter = {
  conversationId: 'test-room',
  onSendMessage: vi.fn(),
  onCancelProcessing: vi.fn(),
  onRespondToHitlBatch: vi.fn(),
  onCancelHitl: vi.fn(),
  onRefreshHitl: vi.fn(),
  onChatModeChange: vi.fn(),
  isSending: false,
  isProcessing: false,
  isCancelling: false,
  agents: [],
  roomAgentIds: [],
  groupManagement: {
    groups: [],
    loadingGroups: false,
    selectedGroup: 'all_agents',
    resolvedTargetMode: { message_target_mode: 'room_default' },
    handleGroupChange: vi.fn(),
    handleCreateGroup: vi.fn(),
    handleEditGroup: vi.fn(),
    handleDeleteGroup: vi.fn(),
  },
  quoteState: { quote: null, clearQuote: vi.fn() },
  chatMode: 'ultimate',
}

describe('ComposerShell', () => {
  beforeEach(() => {
    cleanup()
    const store = useMessageStore.getState()
    store.clearRoom()
    store.setRoom('test-room')
    useTurnStore.getState().clear()
    useRoomUiStore.getState().setProcessing('test-room', false)
  })

  it('restores canonical HITL through connected → snapshot → composer wiring', async () => {
    const ts = '2030-01-01T00:00:00.000Z'
    const hitlRequestIndex = { current: new Map<string, string>() }
    const requestSnapshot = vi.fn()
    const dispatcher = createSSEDispatcher({
      conversationId: 'test-room',
      lifecycle: createProcessingLifecycle(() => {}),
      getAgentName: vi.fn().mockResolvedValue('Agent'),
      getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(),
      hitlRequestIndex,
      setCancelling: vi.fn(),
      requestSnapshotRef: { current: requestSnapshot },
    })
    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-1', room_seq: 2 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 2, messages: [], tasks: [], streaming: {},
        turn_lifecycle_schema: 1,
        turns: [{
          hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
          client_request_id: 'client-1', state: 'awaiting_input',
          started_at: ts, settled_at: null, duration_ms: null,
          terminal_code: null, terminal_summary: null, internal_turns: [], activity: [],
          current_assistant: null, final_answer: null, final_committed: false,
          hitl_interactions: [{
            interaction_id: 'interaction-1', state: 'awaiting_input',
            request_ids: ['request-1'], requested_at: ts, resumed_at: null,
            requests: [{
              request_id: 'request-1', message_id: 'hitl-message-1',
              question_index: 0, question_count: 1, prompt: 'Continue?',
              prompt_type: 'confirmation', choices: [], source: 'supervisor',
              agent_label: null, status: 'requested', answer_ref: null,
            }],
          }],
          active_interaction_id: 'interaction-1', agent_call_message_ids: [],
        }],
        hitl: {
          requests: [{
            room_seq: 2, run_id: 'run-1', request_id: 'request-1',
            message_id: 'hitl-message-1', interaction_id: 'interaction-1',
            related_user_message_id: 'user-1', client_request_id: 'client-1',
            question_index: 0, question_count: 1, prompt: 'Continue?',
            prompt_type: 'confirmation', source: 'supervisor', ts,
          }],
          resolved: [],
        },
      },
    } as AnySSEFrame)

    await dispatcher({
      type: 'processing_status', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 3, room_event_id: 'compat-1', trace_id: 'trace-1', status: 'awaiting_input',
        message_id: 'user-1', related_message_id: 'user-1',
        client_request_id: 'client-1', details: null,
      },
    } as AnySSEFrame)
    expect(requestSnapshot).not.toHaveBeenCalled()
    await dispatcher({
      type: 'processing_status', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 4, room_event_id: 'compat-2', status: 'awaiting_input',
        message_id: 'user-1', related_message_id: 'user-1',
        client_request_id: 'client-1', details: null, agent_id: 'private',
      },
    } as AnySSEFrame)
    // Compatibility processing adapters never participate in canonical
    // lifecycle authority, regardless of extra legacy fields.
    expect(requestSnapshot).not.toHaveBeenCalled()

    render(<ComposerShell adapter={mockAdapter} />)
    expect(screen.getByText('Continue?')).toBeDefined()
    expect(screen.getByTestId('hitl-response-frame')).toBeDefined()
    expect(hitlRequestIndex.current.get(
      hitlRequestKey('interaction-1', 'request-1'),
    )).toBe(hitlQuestionEntityId(
      'hitl-message-1', 'interaction-1', 'request-1', 1,
    ))
  })

  it('releases the exact canonical processing guard after terminal snapshot recovery', async () => {
    const ts = '2030-01-01T00:00:00.000Z'
    const lifecycle = createProcessingLifecycle((processing) => {
      useRoomUiStore.getState().setProcessing('test-room', processing)
    })
    lifecycle.startProcessing('user-1', 'client-1')
    lifecycle.setProcessing(false)
    const dispatcher = createSSEDispatcher({
      conversationId: 'test-room',
      lifecycle,
      getAgentName: vi.fn().mockResolvedValue('Agent'),
      getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(),
      hitlRequestIndex: { current: new Map<string, string>() },
      setCancelling: vi.fn(),
      requestSnapshotRef: { current: vi.fn() },
    })
    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-1', room_seq: 0 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 0, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1, turns: [],
      },
    } as AnySSEFrame)
    await dispatcher({
      type: 'run_event', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 1, event_id: 'event-started', run_id: 'run-1', seq: 1,
        type: 'run_started', correlation_id: 'client-1',
        payload: {
          hybro_turn_id: 'run-1', user_message_id: 'user-1',
          started_at: ts, mode: 'ultimate',
        },
      },
    } as AnySSEFrame)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    lifecycle.markSseDisconnection()

    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-2', room_seq: 2 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 2, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1,
        turns: [{
          hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'user-1',
          client_request_id: 'client-1', state: 'completed', started_at: ts,
          settled_at: '2030-01-01T00:00:01.000Z', duration_ms: 1000,
          terminal_code: null, terminal_summary: null,
          internal_turns: [{
            internal_turn_id: 'turn-1', attempt: 1, message_ids: ['final-1'],
            tool_call_ids: [], status: 'completed',
          }],
          activity: [], current_assistant: null,
          final_answer: {
            message_id: 'final-1', internal_turn_id: 'turn-1', text: 'Done',
            status: 'completed', order: 4,
          },
          final_committed: true, hitl_interactions: [], active_interaction_id: null,
          agent_call_message_ids: [],
        }],
      },
    } as AnySSEFrame)

    render(<ComposerShell adapter={mockAdapter} />)
    expect(lifecycle.isSendGuardActive()).toBe(false)
    expect(screen.getByTestId('room-chat-input').getAttribute('data-processing')).toBe('false')
    expect(screen.getByTestId('room-chat-input').getAttribute('data-disabled')).toBe('false')
  })

  it('does not reactivate a settled guard for duplicate canonical run_started', async () => {
    const ts = '2030-01-01T00:00:00.000Z'
    const lifecycle = createProcessingLifecycle(() => {})
    lifecycle.startProcessing('user-1', 'client-1')
    lifecycle.setProcessing(false)
    const dispatcher = createSSEDispatcher({
      conversationId: 'test-room', lifecycle, getAgentName: vi.fn(), getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(), hitlRequestIndex: { current: new Map() },
      setCancelling: vi.fn(), requestSnapshotRef: { current: vi.fn() },
    })
    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-1', room_seq: 0 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 0, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1, turns: [],
      },
    } as AnySSEFrame)
    const runStarted = {
      type: 'run_event', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 1, event_id: 'event-started', run_id: 'run-1', seq: 1,
        type: 'run_started', correlation_id: 'client-1',
        payload: {
          hybro_turn_id: 'run-1', user_message_id: 'user-1',
          started_at: ts, mode: 'ultimate',
        },
      },
    }
    await dispatcher(runStarted as AnySSEFrame)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    await dispatcher({
      type: 'run_event', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 2, event_id: 'event-settled', run_id: 'run-1', seq: 2,
        type: 'run_settled', correlation_id: 'client-1',
        payload: {
          status: 'failed', started_at: ts, settled_at: ts, duration_ms: 0,
          failure_code: 'internal_error', error_summary: 'Failed',
        },
      },
    } as AnySSEFrame)
    expect(lifecycle.isSendGuardActive()).toBe(false)

    lifecycle.startProcessing('user-1', 'client-1')
    lifecycle.setProcessing(false)
    await dispatcher({
      ...runStarted,
      data: { ...runStarted.data, room_seq: 3, event_id: 'event-started-duplicate' },
    } as AnySSEFrame)

    expect(lifecycle.isSendGuardActive()).toBe(false)
  })

  it('does not stop unrelated legacy work for canonical run_settled', async () => {
    const ts = '2030-01-01T00:00:00.000Z'
    const lifecycle = createProcessingLifecycle(() => {})
    lifecycle.startProcessing('legacy-user', 'legacy-client')
    const dispatcher = createSSEDispatcher({
      conversationId: 'test-room', lifecycle, getAgentName: vi.fn(), getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(), hitlRequestIndex: { current: new Map() },
      setCancelling: vi.fn(), requestSnapshotRef: { current: vi.fn() },
    })
    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-1', room_seq: 0 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 0, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1, turns: [],
      },
    } as AnySSEFrame)
    await dispatcher({
      type: 'run_event', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 1, event_id: 'event-started', run_id: 'run-1', seq: 1,
        type: 'run_started', correlation_id: 'canonical-client',
        payload: {
          hybro_turn_id: 'run-1', user_message_id: 'canonical-user',
          started_at: ts, mode: 'ultimate',
        },
      },
    } as AnySSEFrame)
    await dispatcher({
      type: 'run_event', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 2, event_id: 'event-settled', run_id: 'run-1', seq: 2,
        type: 'run_settled', correlation_id: 'canonical-client',
        payload: {
          status: 'failed', started_at: ts, settled_at: ts, duration_ms: 0,
          failure_code: 'internal_error', error_summary: 'Failed',
        },
      },
    } as AnySSEFrame)

    expect(lifecycle.isSendGuardActive()).toBe(true)
    expect(lifecycle.getMessageId()).toBe('legacy-user')
    expect(lifecycle.getClientRequestId()).toBe('legacy-client')
  })

  it('preserves an unrelated active legacy guard in a mixed-room terminal snapshot', async () => {
    const lifecycle = createProcessingLifecycle((processing) => {
      useRoomUiStore.getState().setProcessing('test-room', processing)
    })
    lifecycle.startProcessing('legacy-user', 'legacy-client')
    const ts = '2030-01-01T00:00:00.000Z'
    const dispatcher = createSSEDispatcher({
      conversationId: 'test-room', lifecycle, getAgentName: vi.fn(), getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(), hitlRequestIndex: { current: new Map() },
      setCancelling: vi.fn(), requestSnapshotRef: { current: vi.fn() },
    })
    await dispatcher({
      type: 'connected', timestamp: ts, conversation_id: 'test-room',
      data: { connection_id: 'connection-1', room_seq: 2 },
    } as AnySSEFrame)
    await dispatcher({
      type: 'snapshot', timestamp: ts, conversation_id: 'test-room',
      data: {
        room_seq: 2, messages: [], tasks: [], streaming: {},
        hitl: { requests: [], resolved: [] }, turn_lifecycle_schema: 1,
        turns: [{
          hybro_turn_id: 'run-1', run_id: 'run-1', user_message_id: 'canonical-user',
          client_request_id: 'canonical-client', state: 'failed', started_at: ts,
          settled_at: '2030-01-01T00:00:01.000Z', duration_ms: 1000,
          terminal_code: 'internal_error', terminal_summary: 'Failed',
          internal_turns: [], activity: [], current_assistant: null, final_answer: null,
          final_committed: false, hitl_interactions: [], active_interaction_id: null,
          agent_call_message_ids: [],
        }],
      },
    } as AnySSEFrame)

    const legacyProcessing = useRoomUiStore.getState().rooms['test-room']?.processing ?? false
    render(<ComposerShell adapter={{ ...mockAdapter, isProcessing: legacyProcessing }} />)
    expect(lifecycle.isSendGuardActive()).toBe(true)
    expect(screen.getByTestId('room-chat-input').getAttribute('data-disabled')).toBe('true')
  })

  it('renders in normal mode', () => {
    render(<ComposerShell adapter={mockAdapter} />)
    expect(screen.getByTestId('room-chat-input')).toBeDefined()
    expect(screen.getByTestId('room-chat-input').getAttribute('data-target-mode')).toBe('room_default')
    expect(screen.getByTestId('room-chat-input').getAttribute('data-editor-disabled')).toBe('false')
    expect(screen.queryByTestId('setup-banner')).toBeNull()
  })

  it('disables the composer and links to settings while no provider works', () => {
    setupNotice = realSetupNotice({ state: 'unconfigured', image_size: 'auto' })
    try {
      render(<ComposerShell adapter={mockAdapter} />)
      expect(screen.getByTestId('setup-banner')).toHaveTextContent('Room Chat needs a model to plan and answer.')
      expect(screen.getByRole('link', { name: 'Set up provider' })).toHaveAttribute('href', '/room/test-room?settings=model-provider')
      expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
      expect(screen.getByTestId('room-chat-input').getAttribute('data-editor-disabled')).toBe('true')
      expect(screen.getByTestId('room-chat-input').getAttribute('data-placeholder')).toBe('Connect a model provider to start a chat')
    } finally {
      setupNotice = null
    }
  })

  it('keeps the composer usable for a non-blocking notice', () => {
    setupNotice = realSetupNotice({ state: 'configured', image_size: 'auto', last_error: 'config_invalid' })
    try {
      render(<ComposerShell adapter={mockAdapter} />)
      expect(screen.getByTestId('setup-banner')).toBeDefined()
      expect(screen.getByTestId('room-chat-input').getAttribute('data-editor-disabled')).toBe('false')
    } finally {
      setupNotice = null
    }
  })

  it('keeps active legacy processing authoritative after a canonical Turn completed', () => {
    installCompletedCanonicalTurn()
    render(<ComposerShell adapter={{ ...mockAdapter, isProcessing: true }} />)

    const input = screen.getByTestId('room-chat-input')
    expect(input.getAttribute('data-processing')).toBe('true')
    expect(input.getAttribute('data-disabled')).toBe('true')
  })

  it('restores the normal composer for a terminal file upload instruction', () => {
    const store = useMessageStore.getState()
    store.upsertMessage({
      id: 'user-upload',
      conversationId: 'test-room',
      messageType: 'user',
      content: 'Review my application',
      senderName: 'User',
      timestamp: new Date().toISOString(),
      turnTerminalStatus: 'completed',
    }, 'db')
    store.upsertMessage({
      id: 'agent-upload',
      conversationId: 'test-room',
      messageType: 'agent',
      content: 'Please upload the PDF in a new message.',
      senderName: 'Agent A',
      timestamp: new Date().toISOString(),
      relatedMessageId: 'user-upload',
      taskStatus: 'completed',
    }, 'db')

    render(<ComposerShell adapter={mockAdapter} />)

    expect(screen.getByTestId('room-chat-input')).toBeDefined()
    expect(screen.queryByTestId('hitl-response-frame')).toBeNull()
    expect(screen.getByTestId('room-chat-input').getAttribute('data-disabled')).toBe('false')
  })

  it('keeps active legacy HITL authoritative after a canonical Turn completed', () => {
    installCompletedCanonicalTurn()
    const store = useMessageStore.getState()
    store.upsertMessage({
      id: 'user-1',
      conversationId: 'test-room',
      messageType: 'user',
      content: 'hi',
      senderName: 'User',
      timestamp: new Date().toISOString(),
    }, 'db')
    store.upsertMessage({
      id: 'hitl-1',
      conversationId: 'test-room',
      messageType: 'agent',
      content: '',
      senderName: 'Agent A',
      timestamp: new Date().toISOString(),
      relatedMessageId: 'user-1',
      taskStatus: 'input-required' as any,
      hitlRequestId: 'h1',
      hitlPrompt: 'What color?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'h1',
      hitlInteractionStatus: 'open',
    }, 'db')

    render(<ComposerShell adapter={mockAdapter} />)
    expect(screen.getByText('What color?')).toBeDefined()
    expect(screen.getByTestId('hitl-response-frame')).toBeDefined()
    expect(screen.queryByTestId('room-chat-input')).toBeNull()
  })

  it('resets questionnaire state for a newer revision with reused aliases', () => {
    const store = useMessageStore.getState()
    store.upsertMessage({
      id: 'hitl-versioned',
      conversationId: 'test-room',
      messageType: 'agent',
      content: '',
      senderName: 'Agent A',
      timestamp: new Date().toISOString(),
      taskStatus: 'input-required' as any,
      hitlRequestId: 'same-request',
      hitlPrompt: 'First round?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'same-interaction',
      hitlInteractionVersion: 1,
      hitlInteractionStatus: 'open',
    }, 'db')

    render(<ComposerShell adapter={mockAdapter} />)
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), {
      target: { value: 'stale draft' },
    })
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLInputElement).value)
      .toBe('stale draft')

    act(() => {
      store.upsertMessage({
        id: 'hitl-versioned',
        conversationId: 'test-room',
        messageType: 'agent',
        content: '',
        senderName: 'Agent A',
        timestamp: new Date().toISOString(),
        hitlRequestId: 'same-request',
        hitlPrompt: 'Second round?',
        hitlPromptType: 'text',
        hitlResolved: false,
        hitlInteractionId: 'same-interaction',
        hitlInteractionVersion: 2,
        hitlInteractionStatus: 'open',
      }, 'sse')
    })

    expect(screen.getByText('Second round?')).toBeDefined()
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLInputElement).value)
      .toBe('')
  })

  it('re-renders when the same request changes lifecycle status only', () => {
    const store = useMessageStore.getState()
    store.upsertMessage({
      id: 'hitl-status',
      conversationId: 'test-room',
      messageType: 'agent',
      content: 'Which market?',
      senderName: 'Agent A',
      timestamp: new Date().toISOString(),
      taskStatus: 'input-required' as any,
      hitlRequestId: 'same-request',
      hitlPrompt: 'Which market?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'same-interaction',
      hitlInteractionStatus: 'open',
    }, 'db')

    render(<ComposerShell adapter={mockAdapter} />)
    expect(screen.getByText('Which market?')).toBeDefined()

    act(() => {
      store.upsertMessage({
        id: 'hitl-status',
        conversationId: 'test-room',
        messageType: 'agent',
        content: 'Which market?',
        senderName: 'Agent A',
        timestamp: new Date().toISOString(),
        hitlInteractionStatus: 'delivery_uncertain',
        hitlApplicationStatus: 'delivery_uncertain',
        taskError: 'Answer delivery is uncertain',
      }, 'sse')
    })

    expect(screen.getByText('Checking whether your answers were received')).toBeDefined()
  })
})


describe('ComposerShell HITL queuing', () => {
  beforeEach(() => {
    cleanup()
    const store = useMessageStore.getState()
    store.clearRoom()
    store.setRoom('test-room')
    useTurnStore.getState().clear()
  })

  it('does not count answered applying siblings as queued interactions', () => {
    const store = useMessageStore.getState()
    for (const [id, requestId, prompt, index] of [
      ['answered-1', 'security_training', 'Training?', 0],
      ['answered-2', 'cloud_providers', 'Cloud providers?', 1],
    ] as const) {
      store.upsertMessage({
        id,
        conversationId: 'test-room',
        messageType: 'agent',
        content: '',
        senderName: 'Broker',
        timestamp: new Date().toISOString(),
        taskStatus: 'input-required' as any,
        hitlRequestId: requestId,
        hitlPrompt: prompt,
        hitlPromptType: 'text',
        hitlResolved: false,
        hitlUserAnswer: index === 0 ? 'Yes' : 'AWS',
        hitlInteractionId: 'interaction-old',
        hitlInteractionStatus: 'applying',
        hitlApplicationStatus: 'applying',
        hitlGroupId: 'interaction-old',
        hitlGroupTotal: 2,
        hitlGroupIndex: index,
      }, 'optimistic')
    }
    store.upsertMessage({
      id: 'new-open',
      conversationId: 'test-room',
      messageType: 'agent',
      content: '',
      senderName: 'Broker',
      timestamp: new Date().toISOString(),
      taskStatus: 'input-required' as any,
      hitlRequestId: 'new-question',
      hitlPrompt: 'New question?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'interaction-new',
      hitlInteractionStatus: 'open',
    }, 'sse')

    render(<ComposerShell adapter={mockAdapter} />)

    expect(screen.getByText('New question?')).toBeDefined()
    expect(screen.queryByTestId('hitl-queue-note')).toBeNull()
  })

  it('notes queued interactions beyond the first while in HITL mode', () => {
    const store = useMessageStore.getState()
    store.upsertMessage({
      id: 'user-1',
      conversationId: 'test-room',
      messageType: 'user',
      content: 'hi',
      senderName: 'User',
      timestamp: new Date().toISOString(),
    }, 'db')
    store.upsertMessage({
      id: 'hitl-1',
      conversationId: 'test-room',
      messageType: 'agent',
      content: '',
      senderName: 'Agent A',
      timestamp: new Date().toISOString(),
      relatedMessageId: 'user-1',
      taskStatus: 'input-required' as any,
      hitlRequestId: 'h1',
      hitlPrompt: 'First question?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'interaction-1',
      hitlInteractionStatus: 'open',
    }, 'db')
    store.upsertMessage({
      id: 'hitl-2',
      conversationId: 'test-room',
      messageType: 'agent',
      content: '',
      senderName: 'Agent B',
      timestamp: new Date().toISOString(),
      relatedMessageId: 'user-1',
      taskStatus: 'input-required' as any,
      hitlRequestId: 'h2',
      hitlPrompt: 'Second question?',
      hitlPromptType: 'text',
      hitlResolved: false,
      hitlInteractionId: 'interaction-2',
      hitlInteractionStatus: 'open',
    }, 'db')

    render(<ComposerShell adapter={mockAdapter} />)

    expect(screen.getByText('First question?')).toBeDefined()
    expect(screen.getByTestId('hitl-queue-note')).toBeDefined()
    expect(screen.getByText('1 more input request is queued after this one.')).toBeDefined()
  })
})
