import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchRoomFileBlob, uploadFile } from '@/lib/api/files'
import { getApiUrl } from '@/lib/utils'
import publicConfig from '../../fixtures/public-config.json'

const REQUEST_ID = expect.stringMatching(/^[0-9a-f]{32}$/)

describe('fetchRoomFileBlob', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('fetches only the constructed room-file endpoint with bearer auth', async () => {
    const response = new Response(new Blob(['hello'], { type: 'text/plain' }), {
      status: 200,
    })
    const fetchMock = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal

    const blob = await fetchRoomFileBlob(
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      async () => 'token-1',
      signal,
    )

    expect(await blob.text()).toBe('hello')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/files/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/content',
      {
        headers: { Authorization: 'Bearer token-1', 'X-Request-ID': REQUEST_ID },
        signal,
      },
    )
  })

  it('rejects unvalidated ids before issuing a request', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      fetchRoomFileBlob('../external', async () => 'token', undefined),
    ).rejects.toThrow('Invalid file id')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('uses the configured JSON API prefix', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify({ ...publicConfig, api_prefix: '/v1' }))
    vi.resetModules()
    const { fetchRoomFileBlob } = await import('@/lib/api/files')
    const response = new Response(new Blob(['hello']), { status: 200 })
    const fetchMock = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)

    await fetchRoomFileBlob(
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      async () => 'token-1',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/files/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/content',
      {
        headers: { Authorization: 'Bearer token-1', 'X-Request-ID': REQUEST_ID },
        signal: undefined,
      },
    )
  })
})

describe('uploadFile', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends X-Request-ID with bearer auth', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      file_id: 'f1',
      file_url: '/api/v1/files/f1/content',
      mime_type: 'text/plain',
      file_name: 'a.txt',
      size_bytes: 5,
    }))
    vi.stubGlobal('fetch', fetchMock)

    await uploadFile(new File(['hello'], 'a.txt', { type: 'text/plain' }), 'room-1', async () => 'token-1')

    expect(fetchMock).toHaveBeenCalledWith(
      `${getApiUrl('files')}/upload`,
      expect.objectContaining({
        method: 'POST',
        headers: { Authorization: 'Bearer token-1', 'X-Request-ID': REQUEST_ID },
      }),
    )
  })
})
