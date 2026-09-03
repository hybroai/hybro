/**
 * Phase 13: Consumer contract test.
 *
 * Verifies that the public API surface of useRoomWebhook hasn't drifted
 * from what the consumer (src/app/(portal)/room/[id]/page.tsx) expects. An exact
 * key set snapshot catches accidental additions/removals.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, cleanup, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import type { SSEMessage } from '@/lib/types/sse'

vi.mock('@/hooks/useRoomSSE', () => ({
  useRoomSSE: vi.fn((opts: { onMessage?: (msg: SSEMessage) => void }) => {
    return { connected: true, connecting: false, error: null }
  }),
}))

vi.mock('@/lib/api/room', () => ({
  inquiryRoomSetting: vi.fn().mockResolvedValue({
    success: true,
    room: { room_id: 'room-1', room_name: 'Test', room_agent_set: {} },
  }),
  SendMessage: vi.fn().mockResolvedValue({ success: true, message_id: 'msg-1' }),
  inquiryRoomMessagesByRoomId: vi.fn().mockResolvedValue({ success: true, message_list: [] }),
  updateRoomAgentSet: vi.fn().mockResolvedValue({ success: true }),
  updateRoomName: vi.fn().mockResolvedValue({ success: true }),
}))

vi.mock('@/lib/api/agent', () => ({
  getAllAgents: vi.fn().mockResolvedValue({ success: true, agents: [] }),
  getAllActiveAgents: vi.fn().mockResolvedValue({ success: true, agents: [] }),
}))

vi.mock('@/lib/api/sse', () => ({
  cancelMessage: vi.fn().mockResolvedValue({ success: true }),
  SSEConnection: vi.fn(),
}))

vi.mock('@/lib/api/hitl', () => ({
  respondToHitl: vi.fn().mockResolvedValue({ status: 'ok', request_id: 'req-1' }),
  fetchPendingHitlRequests: vi.fn().mockResolvedValue({ requests: [] }),
}))

vi.mock('@/components/ui/banner', () => ({
  banner: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children)
  }
}

describe('useRoomWebhook consumer contract', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useMessageStore.getState().clearRoom()
    useMessageStore.getState().setRoom('room-1')
    useMessageStore.getState().markDbSynced()
    useRoomUiStore.getState().resetAll()
  })

  afterEach(() => {
    cleanup()
  })

  it('returns the exact public API key set', async () => {
    const { useRoomWebhook } = await import('@/hooks/useRoomWebhook')
    const { result } = renderHook(
      () => useRoomWebhook({
        roomId: 'room-1',
        userId: 'u1',
        userName: 'Test',
        getToken: async () => 'token',
      }),
      { wrapper: createWrapper() }
    )

    await waitFor(() => {
      expect(result.current.room).toBeDefined()
    })

    // Exact key snapshot (sorted) — any added/removed key fails the test
    expect(Object.keys(result.current).sort()).toEqual([
      'availableAgents',
      'cancelHitlRequest',
      'cancelProcessing',
      'cancelling',
      'getAgentList',
      'getRoomFormData',
      'loading',
      'processing',
      'refreshMessages',
      'refreshRoomSetting',
      'respondToHitlBatch',
      'room',
      'sendUserMessage',
      'sending',
      'sseConnected',
      'sseConnecting',
      'sseEnabled',
      'sseError',
      'supervisorMode',
      'toggleSSE',
      'updateRoomSettings',
      'updatingRoom',
    ])
  })

  it('exposes all expected function-typed members', async () => {
    const { useRoomWebhook } = await import('@/hooks/useRoomWebhook')
    const { result } = renderHook(
      () => useRoomWebhook({
        roomId: 'room-1',
        userId: 'u1',
        userName: 'Test',
        getToken: async () => 'token',
      }),
      { wrapper: createWrapper() }
    )

    await waitFor(() => {
      expect(result.current.room).toBeDefined()
    })

    const functionKeys = [
      'sendUserMessage',
      'cancelProcessing',
      'updateRoomSettings',
      'refreshMessages',
      'refreshRoomSetting',
      'getAgentList',
      'getRoomFormData',
      'toggleSSE',
    ] as const

    for (const key of functionKeys) {
      expect(typeof result.current[key]).toBe('function')
    }
  })

  it('exposes all expected state-typed members', async () => {
    const { useRoomWebhook } = await import('@/hooks/useRoomWebhook')
    const { result } = renderHook(
      () => useRoomWebhook({
        roomId: 'room-1',
        userId: 'u1',
        userName: 'Test',
        getToken: async () => 'token',
      }),
      { wrapper: createWrapper() }
    )

    await waitFor(() => {
      expect(result.current.room).toBeTruthy()
    })

    // Boolean state flags
    expect(typeof result.current.sending).toBe('boolean')
    expect(typeof result.current.processing).toBe('boolean')
    expect(typeof result.current.cancelling).toBe('boolean')
    expect(typeof result.current.updatingRoom).toBe('boolean')
    expect(typeof result.current.sseEnabled).toBe('boolean')
    expect(typeof result.current.supervisorMode).toBe('boolean')

    // Loading can be boolean
    expect(typeof result.current.loading).toBe('boolean')

    // SSE state
    expect(typeof result.current.sseConnected).toBe('boolean')
    expect(typeof result.current.sseConnecting).toBe('boolean')

    // sseError is string | null, not a boolean
    expect(result.current.sseError === null || typeof result.current.sseError === 'string').toBe(true)

    // Room should be an object (loaded)
    expect(result.current.room).toBeTruthy()
    expect(typeof result.current.room).toBe('object')

    // availableAgents is an array
    expect(Array.isArray(result.current.availableAgents)).toBe(true)
  })
})
