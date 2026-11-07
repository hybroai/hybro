import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type React from 'react'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { FIELDS, settingsView } from '../../fixtures/instance-settings'
import { toast } from 'sonner'
import { getApiUrl } from '@/lib/utils'
import { RESTART_POLL_MS } from '@/hooks/useInstanceSettings'
import { SettingsPanes } from '@/components/settings/settings-panes'
import type { SettingsView } from '@/lib/api/settings'
import { ConfigurationSection } from '@/components/settings/configuration-section'
import { RestartBanner } from '@/components/settings/restart-banner'
import { SystemSection } from '@/components/settings/system-section'
import { SettingsTabs } from '@/components/settings/settings-tabs'

const mockPathname = vi.fn(() => '/settings/configuration')
vi.mock('next/navigation', () => ({ usePathname: () => mockPathname() }))

// Every settings and setup route is mocked; nothing reaches a backend.
let view: SettingsView
let puts: unknown[]
let restarts: number

function renderWithClient(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>)
  return { ...result, queryClient }
}

const renderWith = (node: React.ReactNode) => renderWithClient(node)

beforeEach(() => {
  mockPathname.mockReturnValue('/settings/configuration')
  view = settingsView()
  puts = []
  restarts = 0
  stubFetchWithoutSignal()
  server.use(
    http.get(getApiUrl('settings'), () => HttpResponse.json(view)),
    http.put(getApiUrl('settings'), async ({ request }) => {
      const body = await request.json() as { values: Record<string, unknown> }
      puts.push(body)
      const changed = Object.keys(body.values)
      const restartRequired = changed.some(key => FIELDS.find(f => f.key === key)?.apply === 'restart')
      view = settingsView({ revision: 'rev-2', restart_pending: restartRequired }, FIELDS.map(f => (
        f.key in body.values ? { ...f, value: body.values[f.key] as never, source: 'config', restart_pending: f.apply === 'restart' } : f
      )))
      return HttpResponse.json({ changed, restart_required: restartRequired, settings: view })
    }),
    http.post(getApiUrl('settings/restart'), () => {
      restarts += 1
      view = { ...view, restart: { available: true, state: 'requested', error: null, updated_at: 1 } }
      return HttpResponse.json({ restart: view.restart }, { status: 202 })
    }),
    http.get(getApiUrl('setup/status'), () => HttpResponse.json({ state: 'configured', image_size: 'auto', last_error: null })),
  )
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const saveBar = () => screen.getByRole('region', { name: 'Unsaved changes' })

describe('ConfigurationSection', () => {
  it('lists the basic groups with tags and keeps Advanced folded', async () => {
    renderWith(<ConfigurationSection />)
    expect(await screen.findByRole('heading', { name: 'Runs & agents' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Logging' })).toBeInTheDocument()
    // Groups without fields are left out.
    expect(screen.queryByRole('heading', { name: 'Chat & memory' })).not.toBeInTheDocument()
    const interval = screen.getByLabelText('Health check interval (seconds)')
    expect(interval).toHaveValue('1800')
    expect(screen.getByText('Set in config.json · default 3600')).toBeInTheDocument()
    expect(within(screen.getByTestId('field-backend.log_level')).getByText('Live')).toBeInTheDocument()
    expect(within(screen.getByTestId('field-backend.agent_health_check_interval')).getByText('Restart')).toBeInTheDocument()

    const advanced = screen.getByRole('button', { name: /Advanced/ })
    expect(advanced).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByLabelText('Model call attempts')).not.toBeInTheDocument()
    fireEvent.click(advanced)
    expect(screen.getByLabelText('Model call attempts')).toHaveValue('2')
  })

  it('saves a live-only change without asking about a restart', async () => {
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    expect(within(saveBar()).getByText('1 unsaved change')).toBeInTheDocument()
    expect(within(saveBar()).getByText('It applies right away.')).toBeInTheDocument()

    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(puts).toEqual([{ revision: 'rev-1', values: { 'backend.log_level': 'DEBUG' } }]))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Unsaved changes' })).not.toBeInTheDocument())
  })

  it('offers Save or Save & restart when a change needs a restart', async () => {
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Health check interval (seconds)'), { target: { value: '600' } })
    fireEvent.click(screen.getByRole('switch', { name: 'Discover local agents' }))
    expect(within(saveBar()).getByText('2 unsaved changes')).toBeInTheDocument()
    expect(within(saveBar()).getByText('2 of them need a restart to take effect.')).toBeInTheDocument()

    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save 2 changes?' })
    expect(within(dialog).getByText('1800 → 600')).toBeInTheDocument()
    expect(within(dialog).getByText('Off → On')).toBeInTheDocument()
    expect(within(dialog).getByText(/A restart is needed for these changes to work/)).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save & restart' }))
    await waitFor(() => expect(restarts).toBe(1))
    expect(puts).toEqual([{ revision: 'rev-1', values: { 'backend.agent_health_check_interval': 600, 'backend.local_agent_discovery_enabled': true } }])
  })

  it('saves without restarting when asked', async () => {
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Health check interval (seconds)'), { target: { value: '600' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(restarts).toBe(0)
    expect(await screen.findByText('Restart pending')).toBeInTheDocument()
  })

  it('blocks saving while a value is invalid and shows the reason under the field', async () => {
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Health check interval (seconds)'), { target: { value: '0' } })
    expect(screen.getByText('Must be greater than 0.')).toBeInTheDocument()
    expect(screen.getByLabelText('Health check interval (seconds)')).toHaveAttribute('aria-invalid', 'true')
    expect(within(saveBar()).getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('shows the backend field errors from a refused save', async () => {
    server.use(http.put(getApiUrl('settings'), () => HttpResponse.json({
      detail: 'Some settings are invalid.',
      code: 'invalid_settings',
      errors: [{ field: 'backend.log_level', code: 'value_error', message: 'Not accepted here.' }],
    }, { status: 422 })))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Not accepted here.')).toBeInTheDocument()
    expect(within(saveBar()).getByText('1 unsaved change')).toBeInTheDocument()
  })

  it('explains a conflict and loads the new values on request', async () => {
    server.use(http.put(getApiUrl('settings'), () => HttpResponse.json({
      detail: 'config.json changed since it was read; reload and retry.',
      code: 'revision_conflict',
    }, { status: 409 })))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('config.json changed since you opened this page')
    view = settingsView({ revision: 'rev-9' })
    fireEvent.click(within(alert).getByRole('button', { name: 'Load new values' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Log level')).toHaveValue('INFO')
  })

  it('saves against the revision the edits started from, so a newer file is not overwritten', async () => {
    const { queryClient } = renderWithClient(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    // Another tab saves, and a focus refetch brings in its revision.
    view = settingsView({ revision: 'rev-5' })
    await queryClient.refetchQueries({ queryKey: ['settings', 'instance'] })
    await waitFor(() => expect(queryClient.getQueryData<SettingsView>(['settings', 'instance'])?.revision).toBe('rev-5'))
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(puts).toEqual([{ revision: 'rev-1', values: { 'backend.log_level': 'DEBUG' } }]))
  })

  it('clears the conflict notice after a later save succeeds', async () => {
    let refuse = true
    server.use(http.put(getApiUrl('settings'), () => (refuse
      ? HttpResponse.json({ detail: 'Changed.', code: 'revision_conflict' }, { status: 409 })
      : HttpResponse.json({ changed: ['backend.log_level'], restart_required: false, settings: view }))))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('config.json changed since you opened this page')
    refuse = false
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('reloads the view when the save finds config.json invalid', async () => {
    server.use(http.put(getApiUrl('settings'), () => {
      view = settingsView({ config_error: 'backend.log_level: bad value.' })
      return HttpResponse.json({ detail: 'config.json is invalid.', code: 'config_invalid' }, { status: 409 })
    }))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText(/config.json has an error/)).toBeInTheDocument()
    expect(screen.getByLabelText('Log level')).toBeDisabled()
  })

  it('shows refused-save errors that belong to no field on the page', async () => {
    const error = vi.spyOn(toast, 'error')
    server.use(http.put(getApiUrl('settings'), () => HttpResponse.json({
      detail: 'Some settings are invalid.',
      code: 'invalid_settings',
      errors: [
        { field: 'backend.log_level', code: 'value_error', message: 'Not accepted here.' },
        { field: null, code: 'unknown_field', message: 'backend.retired is not a setting.' },
      ],
    }, { status: 422 })))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Not accepted here.')).toBeInTheDocument()
    expect(error).toHaveBeenCalledWith('backend.retired is not a setting.')
  })

  it('starts a new edit from the current revision once earlier edits were undone', async () => {
    const { queryClient } = renderWithClient(<ConfigurationSection />)
    const level = await screen.findByLabelText('Log level')
    fireEvent.change(level, { target: { value: 'DEBUG' } })
    fireEvent.change(level, { target: { value: 'INFO' } })
    expect(screen.queryByRole('region', { name: 'Unsaved changes' })).not.toBeInTheDocument()
    view = settingsView({ revision: 'rev-5' })
    await queryClient.refetchQueries({ queryKey: ['settings', 'instance'] })
    await waitFor(() => expect(queryClient.getQueryData<SettingsView>(['settings', 'instance'])?.revision).toBe('rev-5'))
    fireEvent.change(level, { target: { value: 'WARNING' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(puts).toEqual([{ revision: 'rev-5', values: { 'backend.log_level': 'WARNING' } }]))
  })

  it('keeps the edits and the conflict notice when the new values cannot be loaded', async () => {
    server.use(http.put(getApiUrl('settings'), () => HttpResponse.json({ detail: 'Changed.', code: 'revision_conflict' }, { status: 409 })))
    renderWith(<ConfigurationSection />)
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    const alert = await screen.findByRole('alert')
    server.use(http.get(getApiUrl('settings'), () => HttpResponse.json({ detail: 'Down.', code: 'settings_unavailable' }, { status: 503 })))
    fireEvent.click(within(alert).getByRole('button', { name: 'Load new values' }))
    await waitFor(() => expect(within(alert).getByRole('button', { name: 'Load new values' })).toBeEnabled())
    expect(screen.getByRole('alert')).toHaveTextContent('config.json changed since you opened this page')
    expect(screen.getByLabelText('Log level')).toHaveValue('DEBUG')
  })

  it('keeps Advanced open while one of its fields has an unsaved change', async () => {
    renderWith(<ConfigurationSection />)
    const advanced = await screen.findByRole('button', { name: /Advanced/ })
    fireEvent.click(advanced)
    fireEvent.change(screen.getByLabelText('Model call attempts'), { target: { value: '3' } })
    expect(advanced).toHaveAttribute('aria-expanded', 'true')
    expect(advanced).toBeDisabled()
  })

  it('returns a field set in config.json to its default', async () => {
    renderWith(<ConfigurationSection />)
    await screen.findByLabelText('Health check interval (seconds)')
    fireEvent.click(screen.getByRole('button', { name: 'Reset Health check interval (seconds) to default' }))
    expect(screen.getByLabelText('Health check interval (seconds)')).toHaveValue('3600')
    fireEvent.click(within(saveBar()).getByRole('button', { name: 'Save changes' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts).toEqual([{ revision: 'rev-1', values: { 'backend.agent_health_check_interval': null } }]))
  })

  it('tells people who are not admins why the page is empty', async () => {
    server.use(http.get(getApiUrl('settings'), () => HttpResponse.json(
      { detail: 'Only administrators can change settings.', code: 'forbidden' }, { status: 403 },
    )))
    renderWith(<ConfigurationSection />)
    expect(await screen.findByText('Only an admin can view and change these settings.')).toBeInTheDocument()
  })

  it('reports an invalid config.json and does not offer to save', async () => {
    view = settingsView({ config_error: 'backend.log_level: Input should be DEBUG, INFO, WARNING, ERROR or CRITICAL.' })
    renderWith(<ConfigurationSection />)
    expect(await screen.findByText(/config.json has an error/)).toBeInTheDocument()
    expect(screen.getByLabelText('Log level')).toBeDisabled()
  })
})

describe('RestartBanner', () => {
  it('offers a restart for saved changes that wait for one', async () => {
    view = settingsView({ restart_pending: true }, FIELDS.map(f => (
      f.key === 'backend.agent_health_check_interval' ? { ...f, restart_pending: true } : f
    )))
    renderWith(<RestartBanner />)
    expect(await screen.findByText('Restart to apply 1 saved change')).toBeInTheDocument()
    expect(screen.getByText(/Health check interval \(seconds\) is saved/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart now' }))
    await waitFor(() => expect(restarts).toBe(1))
    expect(await screen.findByText('Restarting Hybro…')).toBeInTheDocument()
  })

  it('asks for a terminal restart where Hybro cannot restart itself', async () => {
    view = settingsView({ restart_pending: true, restart: { available: false, state: 'idle', error: null, updated_at: null } })
    renderWith(<RestartBanner />)
    expect(await screen.findByText(/Restart Hybro from the terminal/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart now' })).not.toBeInTheDocument()
  })

  it('reports a failed restart', async () => {
    view = settingsView({ restart: { available: true, state: 'failed', error: 'The frontend did not start.', updated_at: 5 } })
    renderWith(<RestartBanner />)
    expect(await screen.findByText(/The last restart failed: The frontend did not start\./)).toBeInTheDocument()
  })

  it('polls through a restart, keeps the last view while the backend is down, and announces each restart', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let down = false
      let failedPolls = 0
      server.use(http.get(getApiUrl('settings'), () => {
        if (down) {
          failedPolls += 1
          return HttpResponse.error()
        }
        return HttpResponse.json(view)
      }))
      view = settingsView({ restart_pending: true })
      const { queryClient } = renderWithClient(<RestartBanner />)

      const restartOnce = async () => {
        fireEvent.click(await screen.findByRole('button', { name: 'Restart now' }))
        expect(await screen.findByText('Restarting Hybro…')).toBeInTheDocument()
        // The backend is down for two polls; the last view stays and polling continues.
        down = true
        failedPolls = 0
        await vi.advanceTimersByTimeAsync(RESTART_POLL_MS * 2)
        expect(failedPolls).toBeGreaterThanOrEqual(2)
        expect(screen.getByText('Restarting Hybro…')).toBeInTheDocument()
        view = settingsView({ restart: { available: true, state: 'completed', error: null, updated_at: 2 } })
        down = false
        await vi.advanceTimersByTimeAsync(RESTART_POLL_MS)
        expect(await screen.findByText('Hybro restarted. All saved settings are in effect.')).toBeInTheDocument()
      }
      await restartOnce()

      // A dismissed confirmation comes back for the next restart.
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
      expect(screen.queryByText(/Hybro restarted/)).not.toBeInTheDocument()
      view = settingsView({ restart_pending: true, restart: { available: true, state: 'completed', error: null, updated_at: 2 } })
      await queryClient.refetchQueries({ queryKey: ['settings', 'instance'] })
      await restartOnce()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows a starting restart from any control, so it is not asked twice', async () => {
    let release: () => void = () => {}
    server.use(http.post(getApiUrl('settings/restart'), async () => {
      restarts += 1
      await new Promise<void>(resolve => { release = resolve })
      view = { ...view, restart: { available: true, state: 'requested', error: null, updated_at: 1 } }
      return HttpResponse.json({ restart: view.restart }, { status: 202 })
    }))
    view = settingsView({ restart_pending: true })
    renderWith(<><RestartBanner /><SystemSection /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart Hybro' }))
    expect(await screen.findByText('Restarting Hybro…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart now' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart Hybro' })).not.toBeInTheDocument()
    release()
    await waitFor(() => expect(restarts).toBe(1))
  })

  it('reports a refused restart request and keeps offering the restart', async () => {
    const error = vi.spyOn(toast, 'error')
    server.use(http.post(getApiUrl('settings/restart'), () => HttpResponse.json(
      { detail: 'The configuration is invalid.', code: 'config_invalid' }, { status: 409 },
    )))
    view = settingsView({ restart_pending: true })
    renderWith(<RestartBanner />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart now' }))
    await waitFor(() => expect(error).toHaveBeenCalledWith('The configuration is invalid.'))
    expect(await screen.findByRole('button', { name: 'Restart now' })).toBeInTheDocument()
    expect(screen.queryByText(/Hybro restarted/)).not.toBeInTheDocument()
  })

  it('follows a restart another tab started when this one is refused', async () => {
    server.use(http.post(getApiUrl('settings/restart'), () => {
      view = { ...view, restart: { available: true, state: 'restarting', error: null, updated_at: 3 } }
      return HttpResponse.json({ detail: 'A restart is already in progress.', code: 'restart_in_progress' }, { status: 409 })
    }))
    view = settingsView({ restart_pending: true })
    renderWith(<RestartBanner />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart now' }))
    expect(await screen.findByText('Restarting Hybro…')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Restart now' })).not.toBeInTheDocument())
  })

  it('asks for a page reload when the restart applied browser settings', async () => {
    const frontendField = { ...FIELDS[3], key: 'frontend.max_message_length', label: 'Maximum message length', kind: 'integer' as const, apply: 'restart' as const, options: null, default: 4000, value: 8000, source: 'config' as const, restart_pending: true }
    view = settingsView({ restart_pending: true }, [...FIELDS, frontendField])
    const { queryClient } = renderWithClient(<RestartBanner />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart now' }))
    expect(await screen.findByText('Restarting Hybro…')).toBeInTheDocument()
    view = settingsView({ restart: { available: true, state: 'completed', error: null, updated_at: 2 } }, [...FIELDS, { ...frontendField, restart_pending: false }])
    await queryClient.refetchQueries({ queryKey: ['settings', 'instance'] })
    expect(await screen.findByText(/Reload this page to use the new Maximum message length/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
  })

  it('stays quiet when nothing waits for a restart', async () => {
    const { container } = renderWith(<RestartBanner />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })
})

describe('SystemSection', () => {
  it('shows how the instance runs, its notices and a restart button', async () => {
    view = settingsView({ notices: [{ code: 'unlisted_fields', fields: ['backend.local_agent_discovery_excluded_ports'], message: 'These keys are set in config.json but cannot be edited on this page.' }] })
    renderWith(<SystemSection />)
    expect(await screen.findByText('Hybro 0.4.7')).toBeInTheDocument()
    expect(screen.getByText('Docker image')).toBeInTheDocument()
    expect(screen.getByText('3100')).toBeInTheDocument()
    expect(screen.getByText('This computer only')).toBeInTheDocument()
    expect(screen.getByText('~/.hybro/config.json')).toBeInTheDocument()
    expect(screen.getByText('backend.local_agent_discovery_excluded_ports')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart Hybro' }))
    await waitFor(() => expect(restarts).toBe(1))
  })

  it('has no restart button in a source checkout', async () => {
    view = settingsView({
      restart: { available: false, state: 'idle', error: null, updated_at: null },
      system: { ...settingsView().system, deployment: 'checkout', port: null, listen: null },
    })
    renderWith(<SystemSection />)
    expect(await screen.findByText('Source checkout')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart Hybro' })).not.toBeInTheDocument()
  })
})

describe('SettingsPanes', () => {
  it('keeps a visited section mounted, so switching tabs keeps unsaved edits and a sign-in in progress', async () => {
    mockPathname.mockReturnValue('/settings/model-provider')
    const { rerender, queryClient } = renderWithClient(<SettingsPanes />)
    const show = (path: string) => {
      mockPathname.mockReturnValue(path)
      rerender(<QueryClientProvider client={queryClient}><SettingsPanes /></QueryClientProvider>)
    }
    const provider = await screen.findByRole('region', { name: 'Model provider' })
    // A section not visited yet is not mounted.
    expect(screen.queryByRole('region', { name: 'Configuration', hidden: true })).not.toBeInTheDocument()

    show('/settings/configuration')
    fireEvent.change(await screen.findByLabelText('Log level'), { target: { value: 'DEBUG' } })
    expect(provider).not.toBeVisible()
    expect(provider).toBeInTheDocument()

    show('/settings/system')
    expect(await screen.findByText('Hybro 0.4.7')).toBeInTheDocument()

    show('/settings/configuration')
    expect(screen.getByLabelText('Log level')).toHaveValue('DEBUG')
    expect(provider).toBeInTheDocument()
  })
})

describe('SettingsTabs', () => {
  it('links the three sections and marks the current one', async () => {
    renderWith(<SettingsTabs />)
    const tabs = screen.getByRole('navigation', { name: 'Settings sections' })
    const links = within(tabs).getAllByRole('link')
    expect(links.map(link => link.getAttribute('href'))).toEqual(['/settings/model-provider', '/settings/configuration', '/settings/system'])
    expect(links[1]).toHaveAccessibleName('Configuration')
    expect(links[2]).toHaveAccessibleName('System')
    expect(links[1]).toHaveAttribute('aria-current', 'page')
    expect(links[0]).not.toHaveAttribute('aria-current')
  })

  it('names the status dots for screen readers', async () => {
    view = settingsView({ restart_pending: true })
    server.use(http.get(getApiUrl('setup/status'), () => HttpResponse.json({ state: 'unconfigured', image_size: 'auto', last_error: null })))
    renderWith(<SettingsTabs />)
    expect(await screen.findByRole('link', { name: 'Model provider (not set up)' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Configuration (restart pending)' })).toBeInTheDocument()
  })
})
