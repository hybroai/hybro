import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { cleanup, fireEvent, render, screen, waitFor } from '../../utils/test-utils'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { getApiUrl } from '@/lib/utils'
import { OverviewSetupBanner } from '@/components/setup/setup-banner'

vi.mock('next/navigation', () => ({ usePathname: () => '/' }))

const DISMISSED_KEY = 'hybro.setup-notice.dismissed'
let status: Record<string, unknown>

beforeEach(() => {
  localStorage.clear()
  stubFetchWithoutSignal()
  status = { state: 'unconfigured', image_size: 'auto', last_error: null }
  server.use(http.get(getApiUrl('setup/status'), () => HttpResponse.json(status)))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Overview setup banner', () => {
  it('links to the model provider settings and can be dismissed', async () => {
    render(<OverviewSetupBanner />)
    const banner = await screen.findByTestId('setup-banner')
    expect(banner).toHaveTextContent('Connect a model provider')
    expect(banner).toHaveTextContent('Room Chat and 4 sample agents need one. MCP and the Echo agent already work.')
    expect(banner).toHaveAttribute('data-tone', 'warning')
    expect(screen.getByRole('link', { name: 'Set up provider' })).toHaveAttribute('href', '/?settings=model-provider')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('setup-banner')).toBeNull()
    expect(localStorage.getItem(DISMISSED_KEY)).toBe('unconfigured')
  })

  it('stays dismissed for the same error code in this browser', async () => {
    localStorage.setItem(DISMISSED_KEY, 'unconfigured')
    render(<OverviewSetupBanner />)
    await waitFor(() => expect(screen.queryByTestId('setup-banner')).toBeNull())
  })

  it('shows again when the status reports a new error code', async () => {
    localStorage.setItem(DISMISSED_KEY, 'unconfigured')
    status = { state: 'credential_invalid', image_size: 'auto', last_error: null }
    render(<OverviewSetupBanner />)
    expect(await screen.findByTestId('setup-banner')).toHaveTextContent('API key is missing')
  })

  it('shows the red sign-in banner when the ChatGPT sign-in expired', async () => {
    status = { state: 'configured', provider: 'openai', auth: 'oauth', text_model: 'gpt-6.1-sol', image_size: 'auto', last_error: 'oauth_refresh_failed' }
    render(<OverviewSetupBanner />)
    const banner = await screen.findByTestId('setup-banner')
    expect(banner).toHaveTextContent('ChatGPT sign-in expired')
    expect(banner).toHaveTextContent('Room Chat and 4 sample agents are paused until you sign in again.')
    expect(banner).toHaveAttribute('data-tone', 'error')
    expect(screen.getByRole('link', { name: 'Sign in again' })).toHaveAttribute('href', '/?settings=model-provider')
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('clears the dismissal once the provider is healthy', async () => {
    localStorage.setItem(DISMISSED_KEY, 'unconfigured')
    status = { state: 'configured', provider: 'openai', auth: 'api_key', text_model: 'gpt-5-mini', image_size: 'auto', last_error: null }
    render(<OverviewSetupBanner />)
    await waitFor(() => expect(localStorage.getItem(DISMISSED_KEY)).toBeNull())
    expect(screen.queryByTestId('setup-banner')).toBeNull()
  })

  it('shows nothing when the status cannot be read', async () => {
    server.use(http.get(getApiUrl('setup/status'), () => HttpResponse.json({ detail: 'Only administrators can change the model provider.', code: 'forbidden' }, { status: 403 })))
    render(<OverviewSetupBanner />)
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(screen.queryByTestId('setup-banner')).toBeNull()
  })
})
