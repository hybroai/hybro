import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToString } from 'react-dom/server'
import { cleanup, render, screen } from '../../utils/test-utils'
import { ConnectPage } from '@/components/connect/connect-page'

// Behind one port the projection has an empty API base URL.
vi.mock('@/lib/config', () => ({ publicConfig: () => ({ api_base_url: '', api_prefix: '/api/v1' }) }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }))

afterEach(cleanup)

describe('Connect page with a same-origin API', () => {
  it('server-renders the API tab before the page origin is known', () => {
    const html = renderToString(<ConnectPage kind="api" />)
    expect(html).toContain('/api/v1/agents/discovery')
  })

  it('shows the page origin as the API base URL and the MCP address', () => {
    render(<ConnectPage kind="api" />)
    expect(screen.getByLabelText('API base URL')).toHaveTextContent(window.location.origin)
    cleanup()
    render(<ConnectPage kind="mcp" />)
    expect(screen.getByText(`${window.location.origin}/mcp`)).toBeInTheDocument()
  })
})
