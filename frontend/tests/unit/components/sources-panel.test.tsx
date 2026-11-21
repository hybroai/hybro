import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import userEvent from '@testing-library/user-event'
import { cleanup, render, screen, waitFor, within } from '../../utils/test-utils'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { getApiUrl } from '@/lib/utils'
import { ConnectPage } from '@/components/connect/connect-page'
import type { SourceView } from '@/lib/api/source'

vi.mock('@/lib/config', () => ({ publicConfig: () => ({ api_base_url: 'https://hybro.example', api_prefix: '/api/v1' }) }))
const push = vi.fn()
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }))
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: React.ComponentProps<'a'>) => <a href={String(href)} {...props}>{children}</a>,
}))

// Obviously fake tokens, as the backend returns them once.
const TOKEN = 'hybro_src_fake-created-token-0001'
const ROTATED = 'hybro_src_fake-rotated-token-0002'
const CI: SourceView = {
  source_id: 'src_ci',
  name: 'CI bot',
  has_token: true,
  token_last4: 'c1c1',
  created_at: '2026-11-18T10:00:00Z',
  last_used_at: '2026-11-20T09:30:00Z',
  last_used_via: 'api',
  revoked_at: null,
}
const OLD: SourceView = { ...CI, source_id: 'src_old', name: 'Old laptop', has_token: false, token_last4: null, last_used_at: null, last_used_via: null, revoked_at: '2026-11-19T10:00:00Z' }
const CLAUDE: SourceView = { ...CI, source_id: 'src_claude', name: 'Claude Code', token_last4: 'cl4d', last_used_via: 'mcp' }

type Requirement = { required: boolean; started_off: boolean; updated_at: string }
const state = { sources: [CI, OLD] as SourceView[], requirement: { required: true, started_off: false, updated_at: '2026-11-20T00:00:00Z' } as Requirement, puts: [] as unknown[] }

beforeEach(() => {
  state.sources = [CI, OLD]
  state.requirement = { required: true, started_off: false, updated_at: '2026-11-20T00:00:00Z' }
  state.puts = []
  window.localStorage.clear()
  stubFetchWithoutSignal()
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
  server.use(
    http.get(getApiUrl('sources'), () => HttpResponse.json({ sources: state.sources })),
    http.get(getApiUrl('sources/token-requirement'), () => HttpResponse.json(state.requirement)),
    http.put(getApiUrl('sources/token-requirement'), async ({ request }) => {
      const body = await request.json() as { required: boolean }
      state.puts.push(body)
      state.requirement = { ...state.requirement, required: body.required, updated_at: '2026-11-20T12:00:00Z' }
      return HttpResponse.json(state.requirement)
    }),
    http.post(getApiUrl('sources'), async ({ request }) => {
      const { name } = await request.json() as { name: string }
      const created = { ...CI, source_id: 'src_new', name, token_last4: TOKEN.slice(-4), last_used_at: null }
      state.sources = [...state.sources, created]
      return HttpResponse.json({ ...created, token: TOKEN }, { status: 201 })
    }),
    http.post(getApiUrl('sources/src_ci/rotate'), () => HttpResponse.json({ ...CI, token_last4: ROTATED.slice(-4), token: ROTATED })),
    http.post(getApiUrl('sources/src_ci/revoke'), () => {
      state.sources = state.sources.map(source => source.source_id === 'src_ci' ? { ...source, has_token: false, token_last4: null } : source)
      return HttpResponse.json(state.sources[0])
    }),
    http.patch(getApiUrl('sources/src_ci'), async ({ request }) => {
      const { name } = await request.json() as { name: string }
      state.sources = state.sources.map(source => source.source_id === 'src_ci' ? { ...source, name } : source)
      return HttpResponse.json(state.sources[0])
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const table = () => screen.findByRole('table', { name: 'Sources' })

describe('Sources tab', () => {
  it('is the first tab, and the list shows name, token end, last use and creation', async () => {
    render(<ConnectPage kind="sources" />)
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Sources', 'MCP', 'API'])
    expect(screen.getByRole('tab', { name: 'Sources' })).toHaveAttribute('aria-selected', 'true')
    const list = await table()
    const ci = (await within(list).findByText('CI bot')).closest('tr')!
    expect(within(ci).getByLabelText('Token ending in c1c1')).toHaveTextContent('…c1c1')
    const times = [...ci.querySelectorAll('time')].map(time => time.getAttribute('datetime'))
    expect(times).toEqual([CI.last_used_at, CI.created_at])
    expect(within(ci).getByRole('link', { name: 'Sessions' })).toHaveAttribute('href', '/sessions?source=src_ci')
    const old = within(list).getByText('Old laptop').closest('tr')!
    expect(within(old).getAllByText('Revoked').length).toBeGreaterThan(0)
    expect(within(old).getByText('Never')).toBeInTheDocument()
  })

  it('shows how each source last connected, over the API or MCP', async () => {
    state.sources = [CI, CLAUDE, OLD]
    render(<ConnectPage kind="sources" />)
    const list = await table()
    expect(within(list).getByRole('columnheader', { name: 'Connects through' })).toBeInTheDocument()
    const cell = (name: string) => {
      const row = within(list).getByText(name).closest('tr')!
      return within(row).getByTestId('source-via')
    }
    await within(list).findByText('CI bot')
    expect(cell('CI bot')).toHaveTextContent('API')
    expect(cell('Claude Code')).toHaveTextContent('MCP')
    // Never used: nothing to show.
    expect(cell('Old laptop')).toHaveAccessibleName('Not known')
  })

  it('shows an empty state and a retryable error', async () => {
    state.sources = []
    render(<ConnectPage kind="sources" />)
    expect(await screen.findByText(/No sources yet/)).toBeInTheDocument()
    cleanup()
    server.use(http.get(getApiUrl('sources'), () => HttpResponse.json({ detail: 'x' }, { status: 500 })))
    render(<ConnectPage kind="sources" />)
    expect(await screen.findByText("Sources couldn't load.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('creates a source and shows its token once, with snippets that contain it', async () => {
    const user = userEvent.setup()
    // user-event installs its own clipboard; watch that one.
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(<ConnectPage kind="sources" />)
    await user.click(screen.getByRole('button', { name: 'Create source' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Nightly job')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    expect(await within(dialog).findByText('Token for Nightly job')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Source token')).toHaveTextContent(TOKEN)
    expect(within(dialog).getByLabelText('Claude Code command')).toHaveTextContent(`--header 'Authorization: Bearer ${TOKEN}'`)
    expect(within(dialog).getByLabelText('API request')).toHaveTextContent(`Authorization: Bearer ${TOKEN}`)
    await user.click(within(dialog).getByRole('button', { name: 'Copy token' }))
    expect(writeText).toHaveBeenLastCalledWith(TOKEN)
    // Never kept in the browser or the address.
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(TOKEN)
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain(TOKEN)
    expect(window.location.href).not.toContain(TOKEN)

    await user.click(within(dialog).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.body.textContent).not.toContain(TOKEN)
    expect(await within(await table()).findByText('Nightly job')).toBeInTheDocument()
    // Reopening starts empty: the token is gone.
    await user.click(screen.getByRole('button', { name: 'Create source' }))
    expect(within(await screen.findByRole('dialog')).getByLabelText('Name')).toHaveValue('')
    expect(document.body.textContent).not.toContain(TOKEN)
  })

  it('offers the MCP snippet on every listener', async () => {
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await user.click(screen.getByRole('button', { name: 'Create source' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Nightly job')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await within(dialog).findByLabelText('Source token')
    expect(within(dialog).getByLabelText('Claude Code command')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('API request')).toBeInTheDocument()
  })

  it('rotates after a confirmation and shows the new token once', async () => {
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await within(await table()).findByText('CI bot')
    await user.click(screen.getByRole('button', { name: 'Actions for CI bot' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Rotate token' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('stops working at once')
    await user.click(within(confirm).getByRole('button', { name: 'Rotate token' }))
    const shown = await screen.findByRole('dialog')
    expect(within(shown).getByLabelText('Source token')).toHaveTextContent(ROTATED)
    await user.click(within(shown).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(document.body.textContent).not.toContain(ROTATED))
  })

  it('cannot be closed while a create is in flight, so its token is never lost', async () => {
    let answer: () => void = () => {}
    const answered = new Promise<void>(resolve => { answer = resolve })
    server.use(http.post(getApiUrl('sources'), async () => {
      await answered
      return HttpResponse.json({ ...CI, source_id: 'src_new', name: 'Nightly job', token: TOKEN }, { status: 201 })
    }))
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await user.click(screen.getByRole('button', { name: 'Create source' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Nightly job')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled())
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    answer()
    expect(await within(dialog).findByLabelText('Source token')).toHaveTextContent(TOKEN)
  })

  it('cannot be closed while the rotation is in flight, so the new token is never lost', async () => {
    let answer: () => void = () => {}
    const answered = new Promise<void>(resolve => { answer = resolve })
    server.use(http.post(getApiUrl('sources/src_ci/rotate'), async () => {
      await answered
      return HttpResponse.json({ ...CI, token_last4: ROTATED.slice(-4), token: ROTATED })
    }))
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await within(await table()).findByText('CI bot')
    await user.click(screen.getByRole('button', { name: 'Actions for CI bot' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Rotate token' }))
    const confirm = await screen.findByRole('alertdialog')
    await user.click(within(confirm).getByRole('button', { name: 'Rotate token' }))
    await waitFor(() => expect(within(confirm).getByRole('button', { name: 'Cancel' })).toBeDisabled())
    await user.keyboard('{Escape}')
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    answer()
    expect(within(await screen.findByRole('dialog')).getByLabelText('Source token')).toHaveTextContent(ROTATED)
  })

  it('revokes after a confirmation that says it cannot be undone', async () => {
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await within(await table()).findByText('CI bot')
    await user.click(screen.getByRole('button', { name: 'Actions for CI bot' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Revoke' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('cannot be undone')
    await user.click(within(confirm).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    const row = (await within(await table()).findByText('CI bot')).closest('tr')!
    await waitFor(() => expect(within(row).getAllByText('Revoked').length).toBeGreaterThan(0))
  })

  it('renames a source', async () => {
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await within(await table()).findByText('CI bot')
    await user.click(screen.getByRole('button', { name: 'Actions for CI bot' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Rename' }))
    const dialog = await screen.findByRole('dialog')
    const field = within(dialog).getByLabelText('Name')
    await user.clear(field)
    await user.type(field, 'Release bot')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(await table()).findByText('Release bot')).toBeInTheDocument()
  })
})

describe('the token requirement', () => {
  it('asks before turning it off, states the consequence, and applies it', async () => {
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    const toggle = await screen.findByRole('switch', { name: 'Require a source token' })
    expect(toggle).toBeChecked()
    expect(toggle).toHaveAccessibleDescription('On: every API and MCP request needs a valid token.')
    await user.click(toggle)
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('Any program that can reach this Hybro can send work to your agents without a token.')
    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(state.puts).toEqual([])
    expect(toggle).toBeChecked()

    await user.click(toggle)
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Stop requiring' }))
    await waitFor(() => expect(state.puts).toEqual([{ required: false }]))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Require a source token' })).not.toBeChecked())
    expect(screen.getByRole('switch', { name: 'Require a source token' })).toHaveAccessibleDescription(
      'Off: requests without a token are accepted, so any program that can reach Hybro can use your agents.',
    )
    // The banner shows once the requirement is off.
    expect(await screen.findByTestId('token-requirement-banner')).toBeInTheDocument()
  })

  it('asks before turning it on, saying programs without a token are refused', async () => {
    state.requirement = { required: false, started_off: true, updated_at: '2026-11-20T00:00:00Z' }
    const user = userEvent.setup()
    render(<ConnectPage kind="sources" />)
    await user.click(await screen.findByRole('switch', { name: 'Require a source token' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('are refused')
    await user.click(within(confirm).getByRole('button', { name: 'Require tokens' }))
    await waitFor(() => expect(state.puts).toEqual([{ required: true }]))
    await waitFor(() => expect(screen.queryByTestId('token-requirement-banner')).toBeNull())
  })

  it('shows a dismissable banner on every Connect tab while off, with a dialog that holds the switch', async () => {
    state.requirement = { required: false, started_off: false, updated_at: '2026-11-20T00:00:00Z' }
    const user = userEvent.setup()
    render(<ConnectPage kind="mcp" />)
    const banner = await screen.findByTestId('token-requirement-banner')
    expect(banner).toHaveTextContent('Source tokens are not required')
    await user.click(within(banner).getByRole('button', { name: 'Learn more' }))
    const dialog = await screen.findByRole('dialog', { name: 'Requests without a source token' })
    expect(within(dialog).getByRole('switch', { name: 'Require a source token' })).not.toBeChecked()
    await user.keyboard('{Escape}')
    await user.click(within(screen.getByTestId('token-requirement-banner')).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('token-requirement-banner')).toBeNull()
    cleanup()
    render(<ConnectPage kind="api" />)
    await screen.findByRole('tab', { name: 'API' })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByTestId('token-requirement-banner')).toBeNull()
  })

  it('shows the banner again after the requirement is turned off again', async () => {
    state.requirement = { required: false, started_off: false, updated_at: '2026-11-20T00:00:00Z' }
    window.localStorage.setItem('hybro.token-requirement.banner-dismissed', '2026-11-19T00:00:00Z')
    render(<ConnectPage kind="api" />)
    expect(await screen.findByTestId('token-requirement-banner')).toBeInTheDocument()
  })

  it('shows no banner while tokens are required', async () => {
    render(<ConnectPage kind="mcp" />)
    await screen.findByRole('tab', { name: 'MCP' })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByTestId('token-requirement-banner')).toBeNull()
  })
})
