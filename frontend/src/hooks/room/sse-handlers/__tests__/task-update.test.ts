import { beforeEach, describe, expect, it, vi } from 'vitest'
import { handleTaskUpdate } from '../handlers/task-update'
import { createProcessingLifecycle } from '../../processing-lifecycle'
import { TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import type { RoomSSEFrameMap } from '@/lib/types/sse'
import type { SSEHandlerDeps } from '../types'

vi.mock('@/components/ui/banner', () => ({
  banner: {
    error: vi.fn(),
  },
}))

vi.mock('@/lib/room-timeline/event-log', () => ({
  appendEvent: vi.fn(),
}))

vi.mock('@/lib/room-timeline/stamp-live-turn-terminal', () => ({
  stampLiveTurnTerminalIfInferable: vi.fn(() => true),
}))

vi.mock('@/lib/room-timeline/turn-terminal-stamp', () => ({
  buildTurnForRecoveryHint: vi.fn(),
  scheduleTurnTerminalBackendTruthCheck: vi.fn(),
  shouldScheduleTurnTerminalRecovery: vi.fn(() => false),
}))

function makeDeps(): SSEHandlerDeps {
  const lifecycle = createProcessingLifecycle(() => {})
  return {
    roomId: 'room-1',
    lifecycle,
    getAgentName: vi.fn().mockResolvedValue('Agent One'),
    getAgentSource: vi.fn(() => 'cloud' as const),
    reconcileWithDb: vi.fn(),
    hitlRequestIndex: { current: new Map() },
    setCancelling: vi.fn(),
  }
}

function makeTaskUpdate(
  data: Partial<RoomSSEFrameMap['task_update']['data']>,
): RoomSSEFrameMap['task_update'] {
  return {
    type: 'task_update',
    timestamp: '2026-02-17T10:00:00Z',
    room_id: 'room-1',
    data: {
      message_id: 'agent-message-1',
      status: TASK_STATE.COMPLETED,
      client_request_id: 'client-request-1',
      ...data,
    },
  }
}

describe('handleTaskUpdate', () => {
  beforeEach(() => {
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
  })

  it('does not store arbitrary completed status_message as taskStatusMessage', async () => {
    const privateSentinel = 'PRIVATE_SENTINEL_completed_sse_status_message'

    await handleTaskUpdate(
      makeDeps(),
      makeTaskUpdate({
        status_message: privateSentinel,
        content: '',
      }),
      'req-1',
    )

    const entity = useMessageStore.getState().entities['agent-message-1']
    expect(entity.taskStatus).toBe(TASK_STATE.COMPLETED)
    expect(entity.taskStatusMessage).toBeUndefined()
    expect(JSON.stringify(entity)).not.toContain(privateSentinel)
  })

  it('stores non-terminal status_message as public status without raw task_content', async () => {
    const privateTaskContent = 'Evaluate the confidential renewal file and include the internal premium ceiling'

    await handleTaskUpdate(
      makeDeps(),
      makeTaskUpdate({
        status: TASK_STATE.WORKING,
        status_message: 'Requesting Insurer Agent',
        task_content: privateTaskContent,
      }),
      'req-1',
    )

    const entity = useMessageStore.getState().entities['agent-message-1']
    expect(entity.taskStatus).toBe(TASK_STATE.WORKING)
    expect(entity.clientRequestId).toBe('client-request-1')
    expect(entity.taskStatusMessage).toBe('Requesting Insurer Agent')
    expect(entity.taskContent).toBeUndefined()
    expect(JSON.stringify(entity)).not.toContain(privateTaskContent)
  })

  it('does not settle room cancellation from a canceled child task', async () => {
    const deps = makeDeps()
    const disarmCancelTimeout = vi.spyOn(deps.lifecycle, 'disarmCancelTimeout')

    await handleTaskUpdate(
      deps,
      makeTaskUpdate({ status: TASK_STATE.CANCELED }),
      'req-1',
    )

    expect(deps.setCancelling).not.toHaveBeenCalled()
    expect(disarmCancelTimeout).not.toHaveBeenCalled()
  })

  it('populates taskUpdatedAt so delayed task_update frames are rejected', async () => {
    await handleTaskUpdate(
      makeDeps(),
      makeTaskUpdate({
        status: TASK_STATE.WORKING,
        status_message: 'Working on request',
      }),
      'req-1',
    )

    await handleTaskUpdate(
      makeDeps(),
      {
        ...makeTaskUpdate({
          status: TASK_STATE.SUBMITTED,
          status_message: 'Submitted',
        }),
        timestamp: '2026-02-17T09:55:00Z',
      },
      'req-1',
    )

    const entity = useMessageStore.getState().entities['agent-message-1']
    expect(entity.taskStatus).toBe(TASK_STATE.WORKING)
    expect(entity.taskStatusMessage).toBe('Working on request')
    expect(entity.taskUpdatedAt).toBe('2026-02-17T10:00:00.000Z')
  })
})
