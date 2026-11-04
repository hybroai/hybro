import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

import { useChatRoomCreation } from '@/hooks/useChatRoomCreation'
import { recentLogs } from '@/lib/log'

// Response bodies can echo user input; none of it may reach the logs.
const SECRET = 'caller-secret-text'
// V8 quotes only the first few characters of an invalid JSON body.
const SHORT_SECRET = 'zSECRETz'

const props = {
  userId: 'user-1',
  userName: 'Test User',
  getToken: async () => 'token',
}

// jsdom's AbortSignal is rejected by undici's Request, so MSW cannot intercept
// apiClient here; a fetch stub answers instead and records the URL.
function stubFetch(body: BodyInit, init?: ResponseInit) {
  const fetchMock = vi.fn<(url: string) => Promise<Response>>(async () => new Response(body, init))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function logged(): string {
  return JSON.stringify(recentLogs())
}

describe('useChatRoomCreation logging', () => {
  let consoleCalls: unknown[][]

  beforeEach(() => {
    consoleCalls = []
    for (const level of ['debug', 'info', 'warn', 'error'] as const) {
      vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
        consoleCalls.push(args)
      })
    }
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('logs a non-JSON 200 from agent suggestions without the body', async () => {
    const fetchMock = stubFetch(SHORT_SECRET, { status: 200 })
    const { result } = renderHook(() => useChatRoomCreation(props))

    let suggestions: unknown
    await act(async () => {
      suggestions = await result.current.getAgentSuggestions(`prompt ${SECRET}`)
    })

    expect(suggestions).toBeNull()
    expect(String(fetchMock.mock.calls[0][0])).toContain('/agent-suggestions')
    const failed = recentLogs().filter((entry) => entry.event === 'api_request_failed')
    expect(failed.at(-1)?.fields.error_name).toBe('SyntaxError')
    const suggestion = recentLogs().filter((entry) => entry.event === 'agent_suggestions_failed')
    expect(suggestion.at(-1)?.fields.error).toMatchObject({ name: 'SyntaxError' })
    for (const marker of [SECRET, SHORT_SECRET]) {
      expect(logged()).not.toContain(marker)
      expect(JSON.stringify(consoleCalls)).not.toContain(marker)
    }
  })

  it('logs a wrapped response error without its text', async () => {
    const fetchMock = stubFetch(JSON.stringify({ success: false, error: SECRET }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
    const { result } = renderHook(() => useChatRoomCreation(props))

    await act(async () => {
      await result.current.loadDefaultAgents()
    })

    expect(String(fetchMock.mock.calls[0][0])).toContain('/getAllAgents')
    const failed = recentLogs().filter((entry) => entry.event === 'agents_load_failed')
    expect(failed.at(-1)?.fields).toMatchObject({ source: 'chat_room_creation', error: { name: 'Error' } })
    expect(logged()).not.toContain(SECRET)
    expect(JSON.stringify(consoleCalls)).not.toContain(SECRET)
  })
})
