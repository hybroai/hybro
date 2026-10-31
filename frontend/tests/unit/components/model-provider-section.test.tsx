import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { getApiUrl } from '@/lib/utils'
import { AGENT_CATALOG_KEY } from '@/hooks/room/useAgentCatalog'
import { ModelProviderSection } from '@/components/settings/model-provider-section'

// Every setup route is mocked; nothing reaches a backend or a model provider.
const unconfigured = { state: 'unconfigured', image_size: '1024x1024', last_error: null }
const deepseek = { state: 'configured', provider: 'deepseek', auth: 'api_key', text_model: 'deepseek-v4-flash', image_model: null, image_size: '1024x1024', last_error: null }
const chatgpt = { state: 'configured', provider: 'openai', auth: 'oauth', text_model: 'gpt-6.1-sol', image_model: null, image_size: '1024x1024', last_error: null }
const pending = { login_id: 'login-1', state: 'pending', authorization_url: 'https://auth.openai.com/oauth/authorize?state=fixture', listener: true, expires_at: 0, error: null }

let status: Record<string, unknown>
let bodies: { path: string; body: unknown }[]

function record(path: string, reply: () => Record<string, unknown>) {
  return async ({ request }: { request: Request }) => {
    bodies.push({ path, body: await request.json().catch(() => null) })
    return HttpResponse.json(reply())
  }
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  render(<QueryClientProvider client={queryClient}><ModelProviderSection /></QueryClientProvider>)
  return { invalidate }
}

const statusCard = () => screen.getByTestId('model-provider-status')

beforeEach(() => {
  status = unconfigured
  stubFetchWithoutSignal()
  bodies = []
  vi.spyOn(window, 'open').mockReturnValue(null)
  server.use(
    http.get(getApiUrl('setup/status'), () => HttpResponse.json(status)),
    http.get(getApiUrl('setup/models'), ({ request }) => {
      const provider = new URL(request.url).searchParams.get('provider')
      return HttpResponse.json(provider === 'deepseek'
        ? { text: ['deepseek-v4-flash', 'deepseek-v4-pro'], image: [] }
        : { text: ['gpt-6.1-sol', 'gpt-6.1-mini'], image: [] })
    }),
    http.post(getApiUrl('setup/api-key'), record('api-key', () => ({ changed: true, status: deepseek }))),
    http.put(getApiUrl('setup/image-size'), record('image-size', () => ({ image_size: '1536x1024', restart_required: true }))),
  )
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ModelProviderSection', () => {
  it('connects with an API key, then enables the models card', async () => {
    const { invalidate } = renderSection()
    expect(await screen.findByText('Not set up')).toBeInTheDocument()
    expect(statusCard()).toHaveAttribute('data-phase', 'not_set_up')
    expect(screen.getByLabelText('Text model').closest('fieldset')).toBeDisabled()

    fireEvent.click(screen.getByRole('radio', { name: 'DeepSeek' }))
    await waitFor(() => expect(screen.getByLabelText('Text model')).toHaveValue('deepseek-v4-flash'))
    expect(screen.getByText('Image generation needs OpenAI. Image Generator stays off.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('DeepSeek API key'), { target: { value: 'sk-fixture' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check and save' }))

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: AGENT_CATALOG_KEY }))
    expect(bodies).toEqual([{ path: 'api-key', body: { provider: 'deepseek', api_key: 'sk-fixture', text_model: 'deepseek-v4-flash', image_model: null } }])
    expect(await screen.findByText('API key saved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Replace key' })).toBeInTheDocument()
    expect(statusCard()).toHaveTextContent('Connected')
    expect(statusCard()).toHaveTextContent('DeepSeek · API key')
    expect(screen.getByLabelText('Text model').closest('fieldset')).not.toBeDisabled()
  })

  it('saves models for a saved API key without sending the key again', async () => {
    status = deepseek
    renderSection()
    expect(await screen.findByText('API key saved')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('Text model')).toHaveValue('deepseek-v4-flash'))
    const save = screen.getByRole('button', { name: 'Save models' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Text model'), { target: { value: 'deepseek-v4-pro' } })
    fireEvent.click(save)
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ path: 'api-key', body: { provider: 'deepseek', api_key: null, text_model: 'deepseek-v4-pro', image_model: null } })
  })

  it('waits for the ChatGPT sign-in with a cancel and a paste fallback', async () => {
    server.use(
      http.post(getApiUrl('setup/oauth/start'), () => HttpResponse.json(pending)),
      http.get(getApiUrl('setup/oauth/login-1'), () => HttpResponse.json(pending)),
      http.delete(getApiUrl('setup/oauth/login-1'), () => new HttpResponse(null, { status: 204 })),
    )
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: /Sign in with ChatGPT/ }))

    const panel = await screen.findByTestId('oauth-pending')
    expect(panel).toHaveTextContent('Waiting for you to finish signing in on chatgpt.com…')
    expect(statusCard()).toHaveTextContent('Waiting for sign-in')
    expect(window.open).toHaveBeenCalled()
    // The blocked tab leaves a link to open the sign-in page by hand.
    expect(within(panel).getByRole('link', { name: /Open the sign-in page/ })).toHaveAttribute('href', pending.authorization_url)
    expect(within(panel).getByText('The sign-in tab ended on an error page?')).toBeInTheDocument()
    expect(within(panel).getByLabelText('Address from the sign-in tab')).toBeInTheDocument()

    fireEvent.click(within(panel).getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('button', { name: /Sign in with ChatGPT/ })).toBeInTheDocument()
    expect(statusCard()).toHaveTextContent('Not set up')
  })

  it('saves the recommended models as soon as the sign-in finishes', async () => {
    server.use(
      http.post(getApiUrl('setup/oauth/start'), () => HttpResponse.json(pending)),
      http.get(getApiUrl('setup/oauth/login-1'), () => HttpResponse.json({ ...pending, state: 'authenticated', authorization_url: null })),
      http.post(getApiUrl('setup/oauth/models'), record('oauth/models', () => ({ changed: true, status: chatgpt }))),
    )
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: /Sign in with ChatGPT/ }))
    await waitFor(() => expect(statusCard()).toHaveTextContent('OpenAI · ChatGPT account'), { timeout: 4000 })
    expect(bodies).toEqual([{ path: 'oauth/models', body: { login_id: 'login-1', text_model: 'gpt-6.1-sol', image_model: null } }])
    expect(screen.getByText('Signed in with a ChatGPT account')).toBeInTheDocument()
  })

  it('shows the expired sign-in in red with a sign-in button', async () => {
    status = { ...chatgpt, last_error: 'oauth_refresh_failed' }
    renderSection()
    expect(await screen.findByText('ChatGPT sign-in expired')).toBeInTheDocument()
    expect(statusCard()).toHaveAttribute('data-phase', 'expired')
    expect(within(statusCard()).getByRole('button', { name: 'Sign in again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sign in with ChatGPT/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Text model').closest('fieldset')).toBeDisabled()
  })
})
