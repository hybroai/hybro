import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { McpConnectionDialog } from '@/components/networks/mcp-connection-dialog'
import { MCP_CONFIG, MCP_URL } from '@/lib/mcp'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mockStatus(status: string) {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ service: 'hybro-mcp', status }) })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

async function open() {
  render(<McpConnectionDialog />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect MCP' }))
  await screen.findByRole('dialog', { name: 'MCP Connection' })
}

describe('MCP connection panel', () => {
  it('fetches only when opened, shows English connection data, and copies both formats', async () => {
    const fetcher = mockStatus('ready')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<McpConnectionDialog />)
    expect(fetcher).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Connect MCP' }))
    await screen.findByText('Ready')
    expect(screen.getByText(MCP_URL)).toBeInTheDocument()
    expect(screen.getByText('Configuration', { exact: true })).toBeInTheDocument()
    expect(screen.queryByText('For clients on the machine running Hybro.')).not.toBeInTheDocument()
    expect(screen.queryByText('Transport')).not.toBeInTheDocument()
    expect(screen.queryByText('Streamable HTTP')).not.toBeInTheDocument()
    expect(screen.queryByText('Claude Code configuration')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'MCP Connection' })).not.toHaveAttribute('aria-describedby')
    fireEvent.click(screen.getByRole('button', { name: 'Copy URL' }))
    await screen.findByText('URL copied')
    expect(writeText).toHaveBeenLastCalledWith(MCP_URL)
    fireEvent.click(screen.getByRole('button', { name: 'Copy configuration' }))
    await screen.findByText('Configuration copied')
    expect(writeText).toHaveBeenLastCalledWith(MCP_CONFIG)
  })

  it.each([
    ['unavailable', 'Unavailable', 'Start services or check MCP logs in the Hybro TUI.'],
    ['unsupported_auth', 'Unavailable with Clerk', 'This local adapter does not support Clerk authentication.']
  ])('does not report ready for %s', async (state, label, message) => {
    mockStatus(state)
    await open()
    await screen.findByText(label)
    expect(screen.getByText(message)).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  })

  it('refreshes an unavailable service without starting it', async () => {
    const fetcher = mockStatus('unavailable')
    await open()
    await screen.findByText('Unavailable')
    fetcher.mockResolvedValue({ ok: true, json: async () => ({ service: 'hybro-mcp', status: 'ready' }) })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await screen.findByText('Ready')
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls.every(([url]) => url === '/hybro-mcp')).toBe(true)
  })

  it('offers a safe copy fallback and does not pretend a failed check succeeded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private diagnostics')))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('Denied')) } })
    await open()
    await screen.findByText('Unable to check status')
    fireEvent.click(screen.getByRole('button', { name: 'Copy URL' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to copy')
    expect(screen.getByText((_, element) => element?.tagName === 'PRE' && element.textContent === MCP_CONFIG)).toBeInTheDocument()
  })

  it('cancels a pending status request on close', async () => {
    const fetcher = vi.fn().mockImplementation(() => new Promise(() => {}))
    vi.stubGlobal('fetch', fetcher)
    await open()
    await waitFor(() => expect(fetcher).toHaveBeenCalledOnce())
    const signal = fetcher.mock.calls[0][1].signal as AbortSignal
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(signal.aborted).toBe(true)
  })
})
