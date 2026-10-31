import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import { cancelOAuth, getSetupModels, saveApiKey, setupError, setupErrorMessage, startOAuth } from '@/lib/api/setup'
import { setupNotice } from '@/lib/setup-notice'
import fixture from '../../fixtures/public-config.json'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('setup API client', () => {
  it('queries models for a provider and sign-in method', async () => {
    let seen = ''
    server.use(http.get(getApiUrl('setup/models'), ({ request }) => {
      seen = new URL(request.url).search
      return HttpResponse.json({ text: ['gpt-5-mini'], image: ['gpt-image-1'] })
    }))
    await expect(getSetupModels('openai', 'api_key')).resolves.toEqual({ text: ['gpt-5-mini'], image: ['gpt-image-1'] })
    expect(seen).toBe('?provider=openai&auth=api_key')
  })

  it('sends the API key only in the POST body', async () => {
    let body: unknown
    let query = ''
    server.use(http.post(getApiUrl('setup/api-key'), async ({ request }) => {
      body = await request.json()
      query = new URL(request.url).search
      return HttpResponse.json({ changed: true, status: { state: 'configured', image_size: 'auto' } })
    }))
    await saveApiKey({ provider: 'deepseek', api_key: 'sk-fixture', text_model: 'deepseek-v4-flash', image_model: null })
    expect(body).toEqual({ provider: 'deepseek', api_key: 'sk-fixture', text_model: 'deepseek-v4-flash', image_model: null })
    expect(query).toBe('')
  })

  it('accepts the empty 204 response of a cancelled login', async () => {
    server.use(http.delete(getApiUrl('setup/oauth/:id'), () => new HttpResponse(null, { status: 204 })))
    await expect(cancelOAuth('login-1')).resolves.toBeUndefined()
  })

  it('reads the safe error body, including a pending login to resume', async () => {
    server.use(http.post(getApiUrl('setup/oauth/start'), () => HttpResponse.json(
      { detail: 'A sign-in is already in progress.', code: 'login_pending', login_id: 'login-1' }, { status: 409 },
    )))
    const failure = await startOAuth().catch((error: unknown) => error)
    expect(setupError(failure)).toEqual({ detail: 'A sign-in is already in progress.', code: 'login_pending', login_id: 'login-1' })
    expect(setupErrorMessage(failure)).toBe('A sign-in is already in progress.')
    expect(setupErrorMessage(new Error('network'))).toMatch(/Check that Hybro is running/)
  })

  it('builds same-origin URLs when the API base URL is empty', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify({ ...fixture, api_base_url: '' }))
    vi.resetModules()
    const utils = await import('@/lib/utils')
    expect(utils.getApiUrl('setup/status')).toBe('/api/v1/setup/status')
  })
})

describe('setup notice', () => {
  it('is blocking for the two non-working states', () => {
    expect(setupNotice({ state: 'unconfigured', image_size: 'auto' })).toMatchObject({ code: 'unconfigured', blocking: true })
    expect(setupNotice({ state: 'credential_invalid', image_size: 'auto' })).toMatchObject({ code: 'credential_invalid', blocking: true })
  })

  it('treats an expired ChatGPT sign-in as a blocking red notice', () => {
    const expired = { tone: 'error', title: 'ChatGPT sign-in expired', action: 'Sign in again', blocking: true }
    expect(setupNotice({ state: 'configured', auth: 'oauth', image_size: 'auto', last_error: 'oauth_refresh_failed' })).toMatchObject({ code: 'oauth_refresh_failed', ...expired })
    expect(setupNotice({ state: 'credential_invalid', auth: 'oauth', image_size: 'auto' })).toMatchObject({ code: 'credential_invalid', ...expired })
    expect(setupNotice({ state: 'unconfigured', image_size: 'auto' })).toMatchObject({ tone: 'warning', title: 'Connect a model provider', action: 'Set up provider' })
  })

  it('reports the last error of a working provider without blocking', () => {
    expect(setupNotice({ state: 'configured', image_size: 'auto', last_error: 'config_invalid' })).toMatchObject({ code: 'config_invalid', tone: 'warning', blocking: false })
    expect(setupNotice({ state: 'configured', image_size: 'auto', last_error: 'something_new' })).toMatchObject({ code: 'something_new', blocking: false })
  })

  it('is absent while loading and when healthy', () => {
    expect(setupNotice(undefined)).toBeNull()
    expect(setupNotice({ state: 'configured', image_size: 'auto', last_error: null })).toBeNull()
  })
})

describe('MCP address', () => {
  it('uses the page origin behind one port', async () => {
    vi.stubEnv('HYBRO_FRONTEND_CONFIG', JSON.stringify({ ...fixture, api_base_url: '' }))
    vi.resetModules()
    const { mcpUrl } = await import('@/lib/mcp')
    expect(mcpUrl('http://localhost:8080')).toBe('http://localhost:8080/mcp')
  })
})
