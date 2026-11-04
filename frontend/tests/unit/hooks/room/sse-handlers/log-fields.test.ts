import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

import { createSSEDispatcher } from '@/hooks/room/sse-handlers/dispatch'
import { handleError } from '@/hooks/room/sse-handlers/handlers/misc'
import { createProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import type { SSEHandlerDeps } from '@/hooks/room/sse-handlers/types'
import { recentLogs } from '@/lib/log'
import type { AnySSEFrame, RoomSSEFrameMap } from '@/lib/types/sse'

const timestamp = '2030-01-01T00:00:00.000Z'

function deps(): SSEHandlerDeps {
  return {
    conversationId: 'room-1',
    lifecycle: createProcessingLifecycle(() => {}),
    getAgentName: vi.fn().mockResolvedValue('Agent'),
    getAgentSource: vi.fn(),
    reconcileWithDb: vi.fn(),
    hitlRequestIndex: { current: new Map<string, string>() },
    setCancelling: vi.fn(),
    requestSnapshotRef: { current: vi.fn() },
  } as unknown as SSEHandlerDeps
}

function lastEvent(event: string, conversationId: string) {
  return recentLogs()
    .filter((entry) => entry.event === event && entry.fields.conversation_id === conversationId)
    .at(-1)
}

describe('SSE handler log fields', () => {
  beforeEach(() => {
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps an identifier-like unknown frame type', async () => {
    const dispatch = createSSEDispatcher(deps())

    await dispatch({ type: 'future_frame', conversation_id: 'type-ok', timestamp, data: {} } as unknown as AnySSEFrame)

    expect(lastEvent('sse_frame_ignored', 'type-ok')?.fields).toMatchObject({
      type: 'future_frame',
      reason: 'unknown_type',
    })
  })

  it('logs invalid for an unknown frame type that is not a short token', async () => {
    const dispatch = createSSEDispatcher(deps())

    await dispatch({
      type: 'free text with SECRET',
      conversation_id: 'type-bad',
      timestamp,
      data: {},
    } as unknown as AnySSEFrame)

    expect(lastEvent('sse_frame_ignored', 'type-bad')?.fields.type).toBe('invalid')
    expect(JSON.stringify(recentLogs())).not.toContain('SECRET')
  })

  it('keeps a token error_type and replaces anything else with invalid', () => {
    handleError(deps(), {
      type: 'error', conversation_id: 'err-ok', timestamp,
      data: { error: 'slow down', error_type: 'rate_limit_exceeded' },
    } as RoomSSEFrameMap['error'])
    handleError(deps(), {
      type: 'error', conversation_id: 'err-bad', timestamp,
      data: { error: 'x', error_type: 'Error: SECRET leaked text' },
    } as unknown as RoomSSEFrameMap['error'])
    handleError(deps(), {
      type: 'error', conversation_id: 'err-obj', timestamp,
      data: { error: 'x', error_type: { nested: 'SECRET' } },
    } as unknown as RoomSSEFrameMap['error'])

    expect(lastEvent('sse_error_frame', 'err-ok')?.fields.error_type).toBe('rate_limit_exceeded')
    expect(lastEvent('sse_error_frame', 'err-bad')?.fields.error_type).toBe('invalid')
    expect(lastEvent('sse_error_frame', 'err-obj')?.fields.error_type).toBe('invalid')
    expect(JSON.stringify(recentLogs())).not.toContain('SECRET')
  })
})
