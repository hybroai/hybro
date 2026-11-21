import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import { apiClient } from '@/lib/api-client'
import { onSignedOut } from '@/lib/session-events'
import { redirectParam, safeRedirect } from '@/lib/navigation'

describe('safeRedirect', () => {
  it.each([
    ['/sessions?status=waiting_on_owner#top', '/sessions?status=waiting_on_owner#top'],
    ['/room/abc', '/room/abc'],
  ])('keeps a same-site path %s', (target, expected) => {
    expect(safeRedirect(target)).toBe(expected)
  })

  it.each([
    null,
    undefined,
    '',
    'https://evil.example/',
    '//evil.example',
    '/\\evil.example',
    '/\t/evil.example',
    '/\n/evil.example',
    '/\r\\evil.example',
    '\\\\evil.example',
    'javascript:alert(1)',
  ])('sends %j to the start page', (target) => {
    expect(safeRedirect(target)).toBe('/')
  })
})

describe('redirectParam', () => {
  it('takes the first value of a repeated parameter', () => {
    expect(redirectParam(['/a', '/b'])).toBe('/a')
    expect(redirectParam('/a')).toBe('/a')
    expect(redirectParam(undefined)).toBe('/')
  })
})

describe('a session that ends while the page is open', () => {
  it('reports a 401 not_signed_in from an owner route', async () => {
    const listener = vi.fn()
    const stop = onSignedOut(listener)
    server.use(http.get(getApiUrl('rooms'), () =>
      HttpResponse.json({ detail: { code: 'not_signed_in', message: 'Sign in.' } }, { status: 401 })))
    await expect(apiClient(getApiUrl('rooms'))).rejects.toMatchObject({ status: 401 })
    stop()
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('ignores any other 401', async () => {
    const listener = vi.fn()
    const stop = onSignedOut(listener)
    server.use(http.get(getApiUrl('agents/discovery'), () =>
      HttpResponse.json({ detail: { code: 'unknown_source_token', message: 'm' } }, { status: 401 })))
    await expect(apiClient(getApiUrl('agents/discovery'))).rejects.toMatchObject({ status: 401 })
    stop()
    expect(listener).not.toHaveBeenCalled()
  })
})

describe('raw fetches on owner routes', () => {
  it('report a 401 from a file download as the end of the session', async () => {
    const { fetchRoomFileBlob } = await import('@/lib/api/files')
    const listener = vi.fn()
    const stop = onSignedOut(listener)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: 'not_signed_in' } }), { status: 401 })))
    await expect(fetchRoomFileBlob('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')).rejects.toThrow()
    vi.unstubAllGlobals()
    stop()
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
