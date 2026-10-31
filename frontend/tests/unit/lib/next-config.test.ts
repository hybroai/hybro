import { afterEach, describe, expect, it, vi } from 'vitest'
import publicConfig from '../../fixtures/public-config.json'

 describe('Next API proxy', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.resetModules() })

  it('uses the same validated public JSON prefix as file downloads', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify({ ...publicConfig, api_prefix: '/v1' }))
    vi.stubEnv('NEXT_PUBLIC_API_PREFIX', '/ignored-legacy')
    vi.resetModules()
    const { default: nextConfig } = await import('../../../next.config')
    expect(await nextConfig.rewrites?.()).toEqual([
      { source: '/v1/:path*', destination: 'http://127.0.0.1:8000/v1/:path*' },
      { source: '/mcp', destination: 'http://127.0.0.1:8001/mcp' },
      { source: '/mcp/:path*', destination: 'http://127.0.0.1:8001/mcp/:path*' },
    ])
  })

  it('compiles no rewrites without a projection, because Caddy routes the image', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', '')
    vi.resetModules()
    const { default: nextConfig } = await import('../../../next.config')
    expect(await nextConfig.rewrites?.()).toEqual([])
  })
})
