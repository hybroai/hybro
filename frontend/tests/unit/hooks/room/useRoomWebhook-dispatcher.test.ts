/**
 * The room stream's ordering state lives in the dispatcher's reducer, so the
 * dispatcher must survive callback identity changes such as the agent catalog
 * loading after the stream has already received its snapshot.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { getAllAgents } from '@/lib/api/agent'
import { createSSEDispatcher } from '@/hooks/room/sse-handlers/dispatch'
import { createProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import type { SSEHandlerDeps } from '@/hooks/room/sse-handlers/types'
import type { AnySSEFrame } from '@/lib/types/sse'

const onMessageCalls: Array<((frame: AnySSEFrame) => void | Promise<void>) | undefined> = []

vi.mock('@/hooks/useRoomSSE', () => ({
  useRoomSSE: vi.fn((opts: { onMessage?: (frame: AnySSEFrame) => void | Promise<void> }) => {
    onMessageCalls.push(opts.onMessage)
    return { connected: true, connecting: false, error: null, reconnectWithSnapshot: vi.fn() }
  }),
}))

vi.mock('@/lib/api/room', () => ({
  inquiryRoomSetting: vi.fn().mockResolvedValue({
    success: true,
    room: { conversation_id: 'room-1', room_name: 'Test', room_agent_set: {} },
  }),
  SendMessage: vi.fn().mockResolvedValue({ success: true, message_id: 'msg-1' }),
  inquiryRoomMessagesByConversationId: vi.fn().mockResolvedValue({ success: true, message_list: [] }),
  inquiryActiveRuns: vi.fn().mockResolvedValue({ success: true, active_runs: [] }),
}))

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn(),
}))

vi.mock('@/lib/api/sse', () => ({
  cancelMessage: vi.fn().mockResolvedValue({ success: true }),
  SSEConnection: vi.fn(),
}))

vi.mock('@/lib/api/hitl', () => ({
  fetchPendingHitlRequests: vi.fn().mockResolvedValue({ requests: [] }),
}))

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

const timestamp = '2030-01-01T00:00:00.000Z'

function frame(type: string, data: Record<string, unknown>): AnySSEFrame {
  return { type, conversation_id: 'room-1', timestamp, data } as unknown as AnySSEFrame
}

function snapshot(roomSeq: number): AnySSEFrame {
  return frame('snapshot', {
    room_seq: roomSeq,
    messages: [],
    tasks: [],
    hitl: { requests: [], resolved: [] },
    streaming: {},
  })
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children)
  }
}

describe('useRoomWebhook stream dispatcher', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    onMessageCalls.length = 0
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useMessageStore.getState().markDbSynced()
    useRoomUiStore.getState().resetAll()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('keeps delta ordering after the agent catalog loads', async () => {
    type AgentsResponse = Awaited<ReturnType<typeof getAllAgents>>
    let resolveAgents: (value: AgentsResponse) => void = () => {}
    vi.mocked(getAllAgents).mockReturnValue(
      new Promise<AgentsResponse>((resolve) => { resolveAgents = resolve }),
    )

    const { useRoomWebhook } = await import('@/hooks/useRoomWebhook')
    const { result } = renderHook(
      () => useRoomWebhook({
        conversationId: 'room-1',
        userId: 'u1',
        userName: 'Test',
        getToken: async () => 'token',
      }),
      { wrapper: createWrapper() },
    )
    await waitFor(() => expect(result.current.room).toBeDefined())

    const firstHandler = onMessageCalls.at(-1)
    expect(firstHandler).toBeDefined()
    await act(async () => {
      await firstHandler!(frame('connected', { connection_id: 'c1', room_seq: 5 }))
      await firstHandler!(snapshot(5))
    })

    // The catalog arrives after the snapshot and changes the agent callbacks.
    await act(async () => {
      resolveAgents({
        success: true,
        agents: [{ agent_id: 'a1', name: 'Agent One', status: 'active', source: 'local' }],
      } as unknown as AgentsResponse)
    })
    await waitFor(() => expect(result.current.availableAgents).toHaveLength(1))

    const handler = onMessageCalls.at(-1)
    expect(handler).toBe(firstHandler)

    await act(async () => {
      // Already covered by the snapshot: discarded.
      await handler!(frame('error', { room_seq: 5, error: 'stale' }))
      // Out of order: held in the reorder window until 6 arrives.
      await handler!(frame('error', { room_seq: 7, error: 'seven' }))
      expect(toast.error).not.toHaveBeenCalled()
      await handler!(frame('error', { room_seq: 6, error: 'six' }))
    })

    expect(vi.mocked(toast.error).mock.calls.map((call) => call[0])).toEqual(['six', 'seven'])
  })
})

describe('createSSEDispatcher dispose', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('cancels the pending bootstrap snapshot request', async () => {
    vi.useFakeTimers()
    const requestSnapshot = vi.fn()
    const dispatch = createSSEDispatcher({
      conversationId: 'room-1',
      lifecycle: createProcessingLifecycle(() => {}),
      getAgentName: vi.fn().mockResolvedValue('Agent'),
      getAgentSource: vi.fn(),
      reconcileWithDb: vi.fn(),
      hitlRequestIndex: { current: new Map<string, string>() },
      setCancelling: vi.fn(),
      requestSnapshotRef: { current: requestSnapshot },
    } as unknown as SSEHandlerDeps)

    await dispatch(frame('connected', { connection_id: 'c1', room_seq: 1 }))
    dispatch.dispose()
    await vi.advanceTimersByTimeAsync(1000)

    expect(requestSnapshot).not.toHaveBeenCalled()
  })
})
