import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = {
  updateProfile: vi.fn(),
  changePassword: vi.fn(),
  listBrowsers: vi.fn(),
  revokeBrowser: vi.fn(),
}
const refresh = vi.fn()
vi.mock('@/lib/api/account', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/account')>()),
  updateProfile: (...a: unknown[]) => api.updateProfile(...a),
  changePassword: (...a: unknown[]) => api.changePassword(...a),
  listBrowsers: (...a: unknown[]) => api.listBrowsers(...a),
  revokeBrowser: (...a: unknown[]) => api.revokeBrowser(...a),
}))
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuthRefresh: () => refresh,
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const USER = {
  id: 'user_local_developer',
  fullName: 'Ada',
  firstName: 'Ada',
  username: null,
  primaryEmailAddress: { emailAddress: 'ada@example.com' },
  imageUrl: '',
  deleteSelfEnabled: false as const,
  delete: async () => undefined,
}

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('account panes', () => {
  it('profile saves the name and email', async () => {
    api.updateProfile.mockResolvedValue({ user_id: USER.id, name: 'Ada L', email: 'ada@example.com' })
    const { ProfileSection } = await import('@/components/settings/profile-section')
    render(<ProfileSection user={USER} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Ada L' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.updateProfile).toHaveBeenCalledWith({ name: 'Ada L', email: 'ada@example.com' }))
    expect(refresh).toHaveBeenCalled()
  })

  it('password change sends the current and the new password', async () => {
    api.changePassword.mockResolvedValue(undefined)
    const { PasswordSection } = await import('@/components/settings/password-section')
    render(<PasswordSection user={USER} />)
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-fixture-pw' } })
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'new-fixture-pw' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'new-fixture-pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    await waitFor(() => expect(api.changePassword).toHaveBeenCalledWith({
      current_password: 'old-fixture-pw', new_password: 'new-fixture-pw',
    }))
    expect(screen.getByLabelText('Current password')).toHaveValue('')
  })

  it('sessions lists the signed-in browsers and revokes another one', async () => {
    api.listBrowsers.mockResolvedValue({
      items: [
        { id: 'b-1', created_at: '2026-11-20T10:00:00Z', last_used_at: '2026-11-20T10:00:00Z', user_agent: 'Mozilla/5.0 (Macintosh) Firefox/140.0', current: true },
        { id: 'b-2', created_at: '2026-11-19T10:00:00Z', last_used_at: '2026-11-19T10:00:00Z', user_agent: 'Mozilla/5.0 (iPhone) Safari/604.1', current: false },
      ],
    })
    api.revokeBrowser.mockResolvedValue(undefined)
    const { SessionsSection } = await import('@/components/settings/sessions-section')
    render(<SessionsSection />)
    const rows = await screen.findAllByTestId('browser-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Current')).toBeInTheDocument()
    expect(within(rows[0]).queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()
    fireEvent.click(within(rows[1]).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(api.revokeBrowser).toHaveBeenCalledWith('b-2'))
    expect(api.listBrowsers).toHaveBeenCalledTimes(2)
  })
})
