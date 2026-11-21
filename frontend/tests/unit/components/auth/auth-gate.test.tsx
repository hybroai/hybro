import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const replace = vi.fn()
const getAuthStatus = vi.fn()
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/sessions',
}))
vi.mock('@/lib/api/account', () => ({ getAuthStatus: () => getAuthStatus() }))

const OWNER = { user_id: 'user_local_developer', name: 'Ada Lovelace', email: 'ada@example.com' }

async function renderGate() {
  const { AuthProvider, AuthGate, useUser } = await import('@/lib/auth')
  function Who() {
    const { user } = useUser()
    return <p>{user?.fullName} {user?.primaryEmailAddress.emailAddress}</p>
  }
  render(<AuthProvider><AuthGate><Who /></AuthGate></AuthProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(cleanup)

describe('AuthGate', () => {
  it('renders the portal for the signed-in owner', async () => {
    getAuthStatus.mockResolvedValue({ account_exists: true, owner: OWNER })
    await renderGate()
    expect(await screen.findByText('Ada Lovelace ada@example.com')).toBeInTheDocument()
    expect(replace).not.toHaveBeenCalled()
  })

  it('sends a signed-out browser to sign-in, back to this page afterwards', async () => {
    getAuthStatus.mockResolvedValue({ account_exists: true, owner: null })
    await renderGate()
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/sign-in?redirect_url=%2Fsessions'))
    expect(screen.queryByText(/Ada/)).not.toBeInTheDocument()
  })

  it('sends a browser to setup while no account exists', async () => {
    getAuthStatus.mockResolvedValue({ account_exists: false, owner: null })
    await renderGate()
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/setup'))
  })

  it('keeps the query string of the page to come back to', async () => {
    window.history.replaceState(null, '', '/sessions?status=waiting_on_owner')
    getAuthStatus.mockResolvedValue({ account_exists: true, owner: null })
    await renderGate()
    await waitFor(() => expect(replace).toHaveBeenCalledWith(
      '/sign-in?redirect_url=%2Fsessions%3Fstatus%3Dwaiting_on_owner'))
    window.history.replaceState(null, '', '/')
  })

  it('goes to sign-in when the session ends while the page is open', async () => {
    const { reportSignedOut } = await import('@/lib/session-events')
    getAuthStatus.mockResolvedValueOnce({ account_exists: true, owner: OWNER })
    await renderGate()
    expect(await screen.findByText('Ada Lovelace ada@example.com')).toBeInTheDocument()
    getAuthStatus.mockResolvedValue({ account_exists: true, owner: null })
    reportSignedOut()
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/sign-in?redirect_url=%2Fsessions'))
  })

  it('keeps the portal when a later status reload fails', async () => {
    const { useAuthRefresh } = await import('@/lib/auth')
    getAuthStatus.mockResolvedValueOnce({ account_exists: true, owner: OWNER })
    function Grab() {
      const refresh = useAuthRefresh()
      return <button onClick={() => void refresh()}>Reload</button>
    }
    const { AuthProvider, AuthGate, useUser } = await import('@/lib/auth')
    function Who() {
      const { user } = useUser()
      return <p>{user?.fullName}</p>
    }
    render(<AuthProvider><Grab /><AuthGate><Who /></AuthGate></AuthProvider>)
    expect(await screen.findByText('Ada Lovelace')).toBeInTheDocument()
    getAuthStatus.mockRejectedValue(new Error('network'))
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    await waitFor(() => expect(getAuthStatus).toHaveBeenCalledTimes(2))
    // Let the failed reload settle before looking.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
    expect(screen.getByText('Ada Lovelace')).toBeInTheDocument()
    expect(screen.queryByText(/Hybro is not reachable/)).not.toBeInTheDocument()
  })

  it('says so when the status cannot be read, and renders nothing behind it', async () => {
    getAuthStatus.mockRejectedValue(new Error('network'))
    await renderGate()
    expect(await screen.findByText(/Hybro is not reachable/)).toBeInTheDocument()
    expect(replace).not.toHaveBeenCalled()
  })
})
