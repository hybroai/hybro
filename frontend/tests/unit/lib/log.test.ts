import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api-client'

// The backend accepts an inbound X-Request-ID only when it matches this pattern.
const BACKEND_REQUEST_ID = /^[A-Za-z0-9._:-]{1,128}$/

async function freshLogModule() {
  vi.resetModules()
  return import('@/lib/log')
}

describe('log', () => {
  beforeEach(() => {
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    vi.spyOn(console, 'info').mockImplementation(() => undefined)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
  })

  it('appends {ts, level, event, fields} to the ring buffer', async () => {
    const { log, recentLogs } = await freshLogModule()

    log.info('room_hydrated', { conversation_id: 'room-1' })

    const entries = recentLogs()
    expect(entries).toHaveLength(1)
    expect(entries[0]).toEqual({
      ts: expect.any(String),
      level: 'info',
      event: 'room_hydrated',
      fields: { conversation_id: 'room-1' },
    })
    expect(Number.isNaN(Date.parse(entries[0].ts))).toBe(false)
    expect(console.info).toHaveBeenCalledWith('room_hydrated', { conversation_id: 'room-1' })
  })

  it('keeps only the last 500 entries', async () => {
    const { log, recentLogs } = await freshLogModule()

    for (let i = 0; i < 520; i++) {
      log.debug('ring_probe', { i })
    }

    const entries = recentLogs()
    expect(entries).toHaveLength(500)
    expect(entries[0].fields).toEqual({ i: 20 })
    expect(entries[499].fields).toEqual({ i: 519 })
  })

  it('returns a copy so callers cannot mutate the buffer', async () => {
    const { log, recentLogs } = await freshLogModule()
    log.warn('copy_probe')

    const snapshot = recentLogs() as unknown[]
    snapshot.length = 0

    expect(recentLogs()).toHaveLength(1)
  })

  it('buffers debug entries but keeps them off the console in production', async () => {
    vi.stubEnv('NODE_ENV', 'production')
    const { log, recentLogs } = await freshLogModule()

    log.debug('frame_ignored', { type: 'x' })
    log.info('room_hydrated')

    expect(recentLogs().map((entry) => entry.event)).toEqual(['frame_ignored', 'room_hydrated'])
    expect(console.debug).not.toHaveBeenCalled()
    expect(console.info).toHaveBeenCalledWith('room_hydrated')
  })

  it('prints debug entries outside production', async () => {
    vi.stubEnv('NODE_ENV', 'development')
    const { log } = await freshLogModule()

    log.debug('frame_ignored', { type: 'x' })

    expect(console.debug).toHaveBeenCalledWith('frame_ignored', { type: 'x' })
  })

  it('serialises Error fields to {name} plus V8 frames outside production', async () => {
    vi.stubEnv('NODE_ENV', 'development')
    const { log, recentLogs } = await freshLogModule()
    const error = new TypeError('SECRET boom')
    error.stack = 'TypeError: SECRET boom\n    at run (a.js:1:1)\n    at main (b.js:2:2)'

    log.error('room_hydration_failed', { conversation_id: 'room-1', error })

    const [entry] = recentLogs()
    expect(entry.level).toBe('error')
    expect(entry.fields.error).toEqual({
      name: 'TypeError',
      stack: '    at run (a.js:1:1)\n    at main (b.js:2:2)',
    })
    expect(entry.fields.conversation_id).toBe('room-1')
    expect(console.error).toHaveBeenCalledWith('room_hydration_failed', entry.fields)
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('SECRET')
  })

  it('serialises Error fields to {name} in production', async () => {
    vi.stubEnv('NODE_ENV', 'production')
    const { log, recentLogs } = await freshLogModule()

    log.error('room_hydration_failed', { error: new Error('SECRET boom') })

    expect(recentLogs()[0].fields.error).toEqual({ name: 'Error' })
  })
})

describe('log error redaction', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
  })

  function apiLikeError(): Error {
    return Object.assign(
      new Error('HTTP error! status: 422, message: {"input":"secret"}'),
      { name: 'ApiError', status: 422 },
    )
  }

  it('reduces errors with a numeric status to {name, status} in production', async () => {
    vi.stubEnv('NODE_ENV', 'production')
    const { log, recentLogs } = await freshLogModule()

    log.error('x_failed', { error: apiLikeError() })

    expect(recentLogs()[0].fields.error).toEqual({ name: 'ApiError', status: 422 })
  })

  it('never leaks the response body of a status error', async () => {
    vi.stubEnv('NODE_ENV', 'development')
    const { log, recentLogs } = await freshLogModule()

    log.error('x_failed', { error: apiLikeError() })

    const entry = recentLogs()[0]
    expect(entry.fields.error).toMatchObject({ name: 'ApiError', status: 422 })
    expect(JSON.stringify(recentLogs())).not.toContain('secret')
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('secret')
  })

  it('stores bounded strings for primitive non-Error error values', async () => {
    const { log, recentLogs } = await freshLogModule()

    log.error('x_failed', { error: 'y'.repeat(500) })
    log.error('x_failed', { error: 42 })

    expect(recentLogs()[0].fields.error).toBe('y'.repeat(200))
    expect(recentLogs()[1].fields.error).toBe('42')
  })

  it('stores only the type of object non-Error error values', async () => {
    const { log, recentLogs } = await freshLogModule()
    const payload = { input: 'secret' }

    log.error('x_failed', { error: payload })

    expect(recentLogs()[0].fields.error).toEqual({ type: 'object' })
    expect(JSON.stringify(recentLogs())).not.toContain('secret')
  })
})

describe('log error stacks', () => {
  beforeEach(() => {
    vi.stubEnv('NODE_ENV', 'development')
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
  })

  async function stackOf(error: Error): Promise<unknown> {
    const { log, recentLogs } = await freshLogModule()
    log.error('x_failed', { error })
    expect(JSON.stringify(recentLogs())).not.toContain('SECRET')
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('SECRET')
    return (recentLogs()[0].fields.error as { stack?: unknown }).stack
  }

  // Text that imitates V8 frames, as a response body or JSON input can.
  const FAKE_FRAMES = '\n    at handler (/srv/app.js:1)\napi_key=sk-SECRET'

  function expectRealFrames(stack: unknown): void {
    expect(typeof stack).toBe('string')
    for (const line of String(stack).split('\n')) expect(line).toMatch(/^\s+at \S/)
  }

  it('drops the stack when its header does not match name: message', async () => {
    const error = new Error('SECRET')
    error.stack = 'Wrapped SECRET context\n    at run (a.js:1:1)'

    expect(await stackOf(error)).toBeUndefined()
  })

  it('skips a message line that looks like a frame', async () => {
    expectRealFrames(await stackOf(new Error('boom\n    at SECRET')))
  })

  it('skips fake frames and the lines after them in a message', async () => {
    expectRealFrames(await stackOf(new Error(`boom${FAKE_FRAMES}`)))
  })

  it('skips fake frames in an ApiError response body', async () => {
    const error = new ApiError(500, `HTTP error! status: 500, message: {"detail":"x"}${FAKE_FRAMES}`)

    const { log, recentLogs } = await freshLogModule()
    log.error('x_failed', { error })

    const serialised = recentLogs()[0].fields.error as { name: string; status: number; stack?: unknown }
    expect(serialised).toMatchObject({ name: 'ApiError', status: 500 })
    expectRealFrames(serialised.stack)
    expect(JSON.stringify(recentLogs())).not.toContain('SECRET')
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('SECRET')
  })

  it('skips fake frames quoted by a JSON.parse SyntaxError', async () => {
    let parseError: Error | undefined
    try {
      JSON.parse(`x${FAKE_FRAMES}`)
    } catch (error) {
      parseError = error as Error
    }

    expect(parseError).toBeInstanceOf(SyntaxError)
    expectRealFrames(await stackOf(parseError as Error))
  })

  it('keeps frames for an empty message', async () => {
    const error = new Error('')
    error.stack = 'Error\n    at run (a.js:1:1)\n    at main (b.js:2:2)'

    expect(await stackOf(error)).toBe('    at run (a.js:1:1)\n    at main (b.js:2:2)')
  })

  it('drops every line of a multi-line message', async () => {
    const error = new Error('first line\nSECRET second line')
    error.stack = 'Error: first line\nSECRET second line\n    at run (a.js:1:1)'

    expect(await stackOf(error)).toBe('    at run (a.js:1:1)')
  })

  it('keeps only frame lines after the header', async () => {
    const error = new Error('boom')
    error.stack = 'Error: boom\n    at run (a.js:1:1)\nSECRET trailer'

    expect(await stackOf(error)).toBe('    at run (a.js:1:1)')
  })

  it('omits a stack without V8 frame lines', async () => {
    const error = new Error('SECRET')
    error.stack = 'run@a.js:1:1\nSECRET'

    expect(await stackOf(error)).toBeUndefined()
  })
})

describe('logToken', () => {
  it('keeps short identifier-like strings', async () => {
    const { logToken } = await freshLogModule()

    expect(logToken('rate_limit_exceeded')).toBe('rate_limit_exceeded')
    expect(logToken('agent.response-v1')).toBe('agent.response-v1')
  })

  it('replaces anything else with invalid', async () => {
    const { logToken } = await freshLogModule()

    expect(logToken('has space SECRET')).toBe('invalid')
    expect(logToken('x'.repeat(65))).toBe('invalid')
    expect(logToken('')).toBe('invalid')
    expect(logToken(42)).toBe('invalid')
    expect(logToken(undefined)).toBe('invalid')
    expect(logToken({ type: 'x' })).toBe('invalid')
  })
})

describe('request ids', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('newRequestId returns 32 lowercase hex characters from crypto.getRandomValues', async () => {
    const { newRequestId } = await freshLogModule()
    const spy = vi.spyOn(globalThis.crypto, 'getRandomValues')

    const id = newRequestId()

    expect(id).toMatch(/^[0-9a-f]{32}$/)
    expect(id).toMatch(BACKEND_REQUEST_ID)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(newRequestId()).not.toBe(id)
  })

  it('does not depend on crypto.randomUUID (plain-http LAN listeners)', async () => {
    const { newRequestId } = await freshLogModule()
    const randomUUID = vi.spyOn(globalThis.crypto, 'randomUUID')

    newRequestId()

    expect(randomUUID).not.toHaveBeenCalled()
  })

  it('requestHeaders returns the header and the id it carries', async () => {
    const { requestHeaders } = await freshLogModule()

    const { headers, requestId } = requestHeaders()

    expect(requestId).toMatch(/^[0-9a-f]{32}$/)
    expect(headers).toEqual({ 'X-Request-ID': requestId })
  })

  it('requestHeaders merges extra headers', async () => {
    const { requestHeaders } = await freshLogModule()

    const { headers, requestId } = requestHeaders({ Authorization: 'Bearer t' })

    expect(headers).toEqual({ 'X-Request-ID': requestId, Authorization: 'Bearer t' })
  })
})
