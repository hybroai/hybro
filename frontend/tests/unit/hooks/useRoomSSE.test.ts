import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'

let mockConnectFn: ReturnType<typeof vi.fn>
let mockDisconnectFn: ReturnType<typeof vi.fn>
let mockIsConnectedFn: ReturnType<typeof vi.fn>
let capturedOptions: Record<string, unknown>

vi.mock('@/lib/api/sse', () => {
  return {
    SSEConnection: vi.fn().mockImplementation((options: Record<string, unknown>) => {
      capturedOptions = options
      return {
        connect: mockConnectFn,
        disconnect: mockDisconnectFn,
        isConnected: mockIsConnectedFn,
      }
    }),
  }
})

import { useRoomSSE } from '@/hooks/useRoomSSE'
import { recentLogs } from '@/lib/log'

describe('useRoomSSE', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    capturedOptions = {}
    mockConnectFn = vi.fn().mockResolvedValue(undefined)
    mockDisconnectFn = vi.fn()
    mockIsConnectedFn = vi.fn().mockReturnValue(false)
  })

  afterEach(() => {
    cleanup()
  })

  it('should start disconnected', () => {
    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: false })
    )
    expect(result.current.connected).toBe(false)
    expect(result.current.connecting).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('should auto-connect when enabled and conversationId are set', async () => {
    renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(mockConnectFn).toHaveBeenCalled()
    })
  })

  it('should not connect when enabled is false', () => {
    renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: false })
    )
    expect(mockConnectFn).not.toHaveBeenCalled()
  })

  it('should not connect when conversationId is empty', () => {
    renderHook(() =>
      useRoomSSE({ conversationId: '', enabled: true })
    )
    expect(mockConnectFn).not.toHaveBeenCalled()
  })

  it('should pass conversationId to SSEConnection options', async () => {
    renderHook(() =>
      useRoomSSE({ conversationId: 'room-42', enabled: true })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.conversationId).toBe('room-42')
    })
  })

  it('should set connected=true when onOpen fires', async () => {
    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onOpen).toBeDefined()
    })

    act(() => {
      ;(capturedOptions.onOpen as () => void)()
    })

    expect(result.current.connected).toBe(true)
    expect(result.current.connecting).toBe(false)
  })

  it('should set error when onError fires', async () => {
    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onError).toBeDefined()
    })

    act(() => {
      ;(capturedOptions.onError as (e: Event) => void)(new Event('error'))
    })

    expect(result.current.connected).toBe(false)
    expect(result.current.error).toBe('Connection error')
  })

  it('should set connected=false when onClose fires', async () => {
    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onOpen).toBeDefined()
    })

    act(() => {
      ;(capturedOptions.onOpen as () => void)()
    })
    expect(result.current.connected).toBe(true)

    act(() => {
      ;(capturedOptions.onClose as () => void)()
    })
    expect(result.current.connected).toBe(false)
  })

  it('should return the async onMessage result so the production SSE reader preserves order', async () => {
    let release: (() => void) | undefined
    const pending = new Promise<void>((resolve) => { release = resolve })
    const onMessage = vi.fn().mockReturnValue(pending)
    renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true, onMessage })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onMessage).toBeDefined()
    })

    const msg = { type: 'heartbeat', conversation_id: 'room-1', timestamp: new Date().toISOString() }
    const result = (capturedOptions.onMessage as (m: unknown) => Promise<void>)(msg)

    expect(onMessage).toHaveBeenCalledWith(msg)
    expect(result).toBe(pending)
    release?.()
    await result
  })

  it('should call onConnectionChange when connection state changes', async () => {
    const onConnectionChange = vi.fn()
    renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true, onConnectionChange })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onOpen).toBeDefined()
    })

    act(() => {
      ;(capturedOptions.onOpen as () => void)()
    })
    expect(onConnectionChange).toHaveBeenCalledWith(true)

    act(() => {
      ;(capturedOptions.onClose as () => void)()
    })
    expect(onConnectionChange).toHaveBeenCalledWith(false)
  })

  it('should disconnect on unmount', async () => {
    const { unmount } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(mockConnectFn).toHaveBeenCalled()
    })

    unmount()
    expect(mockDisconnectFn).toHaveBeenCalled()
  })

  it('should disconnect when enabled becomes false', async () => {
    const { rerender } = renderHook(
      (props: { enabled: boolean }) =>
        useRoomSSE({ conversationId: 'room-1', enabled: props.enabled }),
      { initialProps: { enabled: true } }
    )
    await vi.waitFor(() => {
      expect(mockConnectFn).toHaveBeenCalled()
    })

    rerender({ enabled: false })
    expect(mockDisconnectFn).toHaveBeenCalled()
  })

  it('should reconnect when conversationId changes', async () => {
    const { rerender } = renderHook(
      (props: { conversationId: string }) =>
        useRoomSSE({ conversationId: props.conversationId, enabled: true }),
      { initialProps: { conversationId: 'room-1' } }
    )
    await vi.waitFor(() => {
      expect(mockConnectFn).toHaveBeenCalledTimes(1)
    })

    rerender({ conversationId: 'room-2' })
    await vi.waitFor(() => {
      expect(mockConnectFn).toHaveBeenCalledTimes(2)
    })
  })

  it('should expose manual disconnect that clears state', async () => {
    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )
    await vi.waitFor(() => {
      expect(capturedOptions.onOpen).toBeDefined()
    })

    act(() => {
      ;(capturedOptions.onOpen as () => void)()
    })
    expect(result.current.connected).toBe(true)

    act(() => {
      result.current.disconnect()
    })
    expect(result.current.connected).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('should handle connect() throwing an error', async () => {
    mockConnectFn.mockRejectedValueOnce(new Error('Network down'))

    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'room-1', enabled: true })
    )

    await vi.waitFor(() => {
      expect(result.current.error).toBe('Network down')
    })
    expect(result.current.connected).toBe(false)
    expect(result.current.connecting).toBe(false)
  })

  it('logs its own event when connect() rejects, apart from the connection event', async () => {
    mockConnectFn.mockRejectedValueOnce(new Error('Network down'))

    const { result } = renderHook(() =>
      useRoomSSE({ conversationId: 'hook-log-room', enabled: true })
    )

    await vi.waitFor(() => {
      expect(result.current.error).toBe('Network down')
    })
    const events = recentLogs().filter((entry) => entry.fields.conversation_id === 'hook-log-room')
    expect(events.map((entry) => [entry.event, entry.level])).toEqual([
      ['room_sse_connect_failed', 'error'],
    ])
    expect(events[0].fields.error).toMatchObject({ name: 'Error' })
    expect(JSON.stringify(events[0])).not.toContain('Network down')
  })
})
