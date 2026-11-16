import type { Agent } from '@/lib/types/agent'
import type { IncomingMessage } from '@/stores/message-store/types'
import type { SSEMessage } from '@/lib/types/sse'
import { TASK_STATE } from '@/lib/types/sse'

let messageCounter = 0

export function resetCounters() {
  messageCounter = 0
}

export function createMessage(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  messageCounter++
  const id = overrides.id || `msg-${messageCounter}`
  return {
    id,
    conversationId: 'room-1',
    messageType: 'agent',
    content: `Test message ${messageCounter}`,
    senderName: 'Test Agent',
    timestamp: new Date(Date.now() + messageCounter * 1000).toISOString(),
    ...overrides,
  }
}

export function createUserMessage(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return createMessage({
    messageType: 'user',
    senderName: 'Test User',
    ...overrides,
  })
}

export function createAgentMessage(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return createMessage({
    messageType: 'agent',
    senderName: 'Test Agent',
    agentId: 'agent-1',
    ...overrides,
  })
}

export function createTaskMessage(
  taskStatus: typeof TASK_STATE[keyof typeof TASK_STATE],
  overrides: Partial<IncomingMessage> = {}
): IncomingMessage {
  return createAgentMessage({
    taskStatus,
    ...overrides,
  })
}

export function createWorkingTask(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return createTaskMessage(TASK_STATE.WORKING, overrides)
}

export function createCompletedTask(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return createTaskMessage(TASK_STATE.COMPLETED, {
    content: 'Task completed successfully',
    ...overrides,
  })
}

export function createEphemeralMessage(overrides: Partial<IncomingMessage> = {}): IncomingMessage {
  return createMessage({
    isEphemeral: true,
    ...overrides,
  })
}

export function createAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    agent_id: 'agent-1',
    name: 'Test Agent',
    description: '',
    version: '1.0.0',
    provider_organization: null,
    icon_url: null,
    documentation_url: null,
    skills: [],
    capabilities: {
      streaming: false,
      push_notifications: false,
      state_transition_history: false,
      extensions: false,
    },
    default_input_modes: ['text'],
    default_output_modes: ['text'],
    provider_id: 'provider-1',
    provider_name: null,
    status: 'active',
    source: 'cloud',
    is_public: true,
    public_url: null,
    call_count: 0,
    ...overrides,
  }
}

export function createSSEMessage(
  type: SSEMessage['type'],
  data: unknown = {}
): SSEMessage {
  return {
    type,
    conversation_id: 'room-1',
    timestamp: new Date().toISOString(),
    data,
  } as SSEMessage
}

export function createMessageBatch(count: number, baseOverrides: Partial<IncomingMessage> = {}): IncomingMessage[] {
  return Array.from({ length: count }, (_, i) =>
    createMessage({
      ...baseOverrides,
      timestamp: new Date(Date.now() + i * 1000).toISOString(),
    })
  )
}
