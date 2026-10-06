import { afterEach, describe, expect, it, vi } from 'vitest'
import { GET } from '@/app/hybro-mcp/route'
import { MCP_CONFIG, MCP_URL, parseMcpStatus } from '@/lib/mcp'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('MCP connection status', () => {
  it('copies an explicit HTTP configuration rather than a stdio entry', () => {
    expect(JSON.parse(MCP_CONFIG)).toEqual({ mcpServers: { hybro: { type: 'http', url: MCP_URL } } })
  })

  it.each([null, [], { status: 'ready' }, { service: 'other', status: 'ready' }, { service: 'hybro-mcp', status: 'unknown' }])('rejects an invalid status payload', (body) => {
    expect(parseMcpStatus(body)).toBe('unavailable')
  })

  it.each(['ready', 'unsupported_auth', 'unavailable'])('probes only health and exposes only status: %s', async (status) => {
    vi.stubEnv('HYBRO_MCP_URL', 'http://mcp:8001')
    const fetcher = vi.fn().mockResolvedValue(Response.json({ service: 'hybro-mcp', status, private: 'do not forward' }))
    vi.stubGlobal('fetch', fetcher)
    const response = await GET()
    expect(await response.json()).toEqual({ service: 'hybro-mcp', status })
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(fetcher).toHaveBeenCalledWith('http://mcp:8001/health', {
      cache: 'no-store', redirect: 'error', signal: expect.any(AbortSignal)
    })
  })

  it('uses loopback for native development', async () => {
    vi.stubEnv('HYBRO_MCP_URL', undefined)
    const fetcher = vi.fn().mockRejectedValue(new Error('private diagnostics'))
    vi.stubGlobal('fetch', fetcher)
    const response = await GET()
    expect(await response.json()).toEqual({ service: 'hybro-mcp', status: 'unavailable' })
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8001/health')
  })

  it.each([new Response('private error', { status: 500 }), new Response('not JSON')])('handles invalid upstream replies safely', async (upstream) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(upstream))
    expect(await (await GET()).json()).toEqual({ service: 'hybro-mcp', status: 'unavailable' })
  })
})
