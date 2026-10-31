import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { parsePublicConfig } from '@/lib/config-schema'
import fixture from '../../fixtures/public-config.json'

describe('public JSON configuration', () => {
  it('accepts the CLI projection as an immutable snapshot', () => {
    const result = parsePublicConfig(JSON.stringify(fixture))
    expect(result).toEqual(fixture)
    expect(Object.isFrozen(result)).toBe(true)
  })

  it('accepts an empty API base URL, meaning same origin, and the derived MCP switch', () => {
    const result = parsePublicConfig(JSON.stringify({ ...fixture, api_base_url: '', mcp_disabled: true }))
    expect(result.api_base_url).toBe('')
    expect(result.mcp_disabled).toBe(true)
  })

  it.each([undefined, '', '[]', '{invalid', JSON.stringify({ ...fixture, clerk_secret_key: 'private-marker' }), JSON.stringify({ ...fixture, max_message_length: -1 }), JSON.stringify({ ...fixture, enable_waitlist: 'true' }), JSON.stringify({ ...fixture, api_base_url: 'https://user:private-marker@example.org' }), JSON.stringify({ ...fixture, mcp_url: 'http://127.0.0.1:8001/mcp' }), JSON.stringify({ ...fixture, server_url: '' }), JSON.stringify({ ...fixture, mcp_disabled: 'true' })])('rejects invalid projections without echoing values', value => {
    expect(() => parsePublicConfig(value)).toThrow('frontend configuration')
    try { parsePublicConfig(value) } catch (error) {
      expect(String(error)).not.toContain('private-marker')
    }
  })
})

// A published image serves every deployment, so the projection must arrive at
// runtime. These cover the two sources the resolver accepts, and that an
// altered projection is still refused rather than trusted because it is local.
//
// Each case re-imports the module on purpose: the resolver caches its decision
// once per module instance, which is exactly the boundary under test.
describe('runtime configuration delivery', () => {
  // The delivery seam is a global the browser owns; this test world does not
  // have one, so it is installed and removed explicitly.
  const carrier = globalThis as unknown as {
    window?: { __HYBRO_PUBLIC_CONFIG__?: string }
  }

  const deliver = (projection: string | undefined) => {
    if (projection === undefined) delete carrier.window
    else carrier.window = { __HYBRO_PUBLIC_CONFIG__: projection }
  }

  // The shared test setup imports this module through the API mocks, so drop
  // that instance before each case to exercise fresh resolution.
  beforeEach(() => { vi.resetModules() })

  afterEach(() => {
    deliver(undefined)
    vi.resetModules()
    vi.unstubAllEnvs()
  })

  it('uses the projection the server delivered, not the build-time value', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify(fixture))
    deliver(JSON.stringify({ ...fixture, max_message_length: 777 }))
    const { publicConfig } = await import('@/lib/config')

    expect(publicConfig().max_message_length).toBe(777)
  })

  it('falls back to the environment where no document exists', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify({ ...fixture, api_prefix: '/v9' }))
    const { publicConfig } = await import('@/lib/config')

    expect(publicConfig().api_prefix).toBe('/v9')
  })

  it('still validates what the server delivered', async () => {
    deliver(JSON.stringify({ ...fixture, clerk_secret_key: 'private' }))
    const { publicConfig } = await import('@/lib/config')

    expect(() => publicConfig()).toThrow('frontend configuration')
  })
})
