import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockUseUser = vi.fn()
vi.mock('@/lib/auth', () => ({
  useUser: () => mockUseUser(),
  useAuth: () => ({ getToken: async () => 'test-token' }),
}))

vi.mock('@/components/settings/profile-section', () => ({
  ProfileSection: () => <div data-testid="profile-section">Profile</div>,
}))

vi.mock('@/components/settings/password-section', () => ({
  PasswordSection: () => <div data-testid="password-section">Password</div>,
}))

vi.mock('@/components/settings/sessions-section', () => ({
  SessionsSection: () => <div data-testid="sessions-section">Sessions</div>,
}))

vi.mock('@/components/settings/danger-zone-section', () => ({
  DangerZoneSection: () => <div data-testid="danger-zone-section">Danger Zone</div>,
}))

let SettingsDialog: React.ComponentType<{
  open: boolean
  onOpenChange: (open: boolean) => void
}>

beforeEach(async () => {
  vi.clearAllMocks()
  mockUseUser.mockReturnValue({ isLoaded: true, user: { id: 'u1' } })
  const mod = await import('@/components/settings/settings-dialog')
  SettingsDialog = mod.SettingsDialog
})

afterEach(cleanup)

describe('SettingsDialog', () => {
  it('holds only account settings; instance settings live on the Settings page', () => {
    render(<SettingsDialog open={true} onOpenChange={vi.fn()} />)

    expect(screen.getByRole('dialog', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByTestId('profile-section')).toBeInTheDocument()
    expect(screen.getByTestId('password-section')).toBeInTheDocument()
    expect(screen.getByTestId('sessions-section')).toBeInTheDocument()
    expect(screen.getByTestId('danger-zone-section')).toBeInTheDocument()
    expect(screen.queryByText('Model provider')).not.toBeInTheDocument()
    expect(screen.queryByText('About')).not.toBeInTheDocument()
  })

  it('lists the sections with icons and shows one at a time, profile first', () => {
    render(<SettingsDialog open={true} onOpenChange={vi.fn()} />)

    const nav = screen.getByRole('navigation', { name: 'Account sections' })
    const items = within(nav).getAllByRole('button')
    expect(items.map(item => item.textContent)).toEqual(['Profile', 'Password', 'Sessions'])
    items.forEach(item => expect(item.querySelector('svg')).not.toBeNull())
    expect(items[0]).toHaveAttribute('aria-current', 'page')
    expect(screen.getByTestId('profile-section')).toBeVisible()
    expect(screen.getByTestId('password-section')).not.toBeVisible()

    fireEvent.click(items[2])
    expect(items[2]).toHaveAttribute('aria-current', 'page')
    expect(screen.getByTestId('sessions-section')).toBeVisible()
    expect(screen.getByTestId('profile-section')).not.toBeVisible()
  })

  it('shows loading state when user is not loaded', () => {
    mockUseUser.mockReturnValue({ isLoaded: false, user: null })

    render(<SettingsDialog open={true} onOpenChange={vi.fn()} />)

    expect(screen.getByText('Loading account...')).toBeInTheDocument()
  })
})
