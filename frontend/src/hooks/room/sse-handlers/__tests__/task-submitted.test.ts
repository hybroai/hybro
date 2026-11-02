import { beforeEach, describe, expect, it, vi } from 'vitest'
import { handleTaskSubmitted } from '../handlers/task-submitted'
import { createProcessingLifecycle } from '../../processing-lifecycle'
import { TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import type { RoomSSEFrameMap } from '@/lib/types/sse'
import type { SSEHandlerDeps } from '../types'

function makeDeps(): SSEHandlerDeps {
  const lifecycle = createProcessingLifecycle(() => {})
  return {
    conversationId: 'room-1',
    lifecycle,
    getAgentName: vi.fn().mockResolvedValue('Agent One'),
    getAgentSource: vi.fn(() => 'cloud' as const),
    reconcileWithDb: vi.fn(),
    hitlRequestIndex: { current: new Map() },
    setCancelling: vi.fn(),
  }
}

function makeTaskSubmitted(
  data: Partial<RoomSSEFrameMap['task_submitted']['data']>,
): RoomSSEFrameMap['task_submitted'] {
  return {
    type: 'task_submitted',
    timestamp: '2026-02-17T10:00:00Z',
    conversation_id: 'room-1',
    data: {
      message_id: 'agent-message-1',
      task_id: 'task-1',
      agent_name: 'Insurer Agent',
      agent_id: 'agent-1',
      status: TASK_STATE.WORKING,
      related_message_id: 'user-message-1',
      client_request_id: 'client-request-1',
      ...data,
    },
  }
}

describe('handleTaskSubmitted', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
  })

  it('does not store raw SSE task_content on the message entity', async () => {
    const privateTaskContent = 'Evaluate the confidential renewal file and include the internal premium ceiling'

    await handleTaskSubmitted(
      makeDeps(),
      makeTaskSubmitted({
        task_content: privateTaskContent,
      }),
      'req-1',
    )

    const entity = useMessageStore.getState().entities['agent-message-1']
    expect(entity.taskStatus).toBe(TASK_STATE.WORKING)
    expect(entity.clientRequestId).toBe('client-request-1')
    expect(entity.relatedMessageId).toBe('user-message-1')
    expect(entity.taskContent).toBeUndefined()
    expect(JSON.stringify(entity)).not.toContain(privateTaskContent)
  })
})
