import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const signIn = vi.fn()
const setupOwner = vi.fn()
const assign = vi.fn()
vi.mock('@/lib/api/account', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/account')>()),
  signIn: (...args: unknown[]) => signIn(...args),
  setupOwner: (...args: unknown[]) => setupOwner(...args),
}))
vi.mock('@/lib/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/navigation')>()),
  goTo: (url: string) => assign(url),
}))

beforeEach(() => {
  vi.clearAllMocks()
  window.location.hash = ''
})
afterEach(cleanup)

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

describe('SignInForm', () => {
  it('signs in and returns to the page that asked', async () => {
    signIn.mockResolvedValue({ owner: {} })
    const { SignInForm } = await import('@/components/auth/sign-in-form')
    render(<SignInForm redirectUrl="/sessions" />)
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/sessions'))
    expect(signIn).toHaveBeenCalledWith('ada@example.com', 'fixture-password')
  })

  it('never redirects to another site', async () => {
    signIn.mockResolvedValue({ owner: {} })
    const { SignInForm } = await import('@/components/auth/sign-in-form')
    render(<SignInForm redirectUrl="//evil.example/x" />)
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
  })

  it('shows the refusal and keeps the email', async () => {
    const { ApiError } = await import('@/lib/api-client')
    signIn.mockRejectedValue(new ApiError(401, 'x', { detail: { code: 'invalid_credentials', message: 'The email or the password is wrong.' } }))
    const { SignInForm } = await import('@/components/auth/sign-in-form')
    render(<SignInForm redirectUrl="/" />)
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'wrong-fixture')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The email or the password is wrong.')
    expect(screen.getByLabelText(/^Email/)).toHaveValue('ada@example.com')
    expect(assign).not.toHaveBeenCalled()
  })
})

describe('SetupForm', () => {
  it('reads the code from the fragment and creates the owner', async () => {
    window.location.hash = '#fixture-code'
    setupOwner.mockResolvedValue({ owner: {} })
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    fill(/^Name/, 'Ada')
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fill(/^Confirm password/, 'fixture-password')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
    expect(setupOwner).toHaveBeenCalledWith({
      code: 'fixture-code', name: 'Ada', email: 'ada@example.com', password: 'fixture-password',
    })
  })

  it('without a code, asks for the setup link instead of the account form', async () => {
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    expect(screen.getByText('Open your setup link')).toBeInTheDocument()
    expect(screen.getByText(/prints a new link and replaces the earlier one/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create account' })).not.toBeInTheDocument()
  })

  it('uses a pasted setup link as the code', async () => {
    setupOwner.mockResolvedValue({ owner: {} })
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    fill(/^Setup link/, 'http://localhost:3100/setup#pasted-code')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText('Create your owner account')).toBeInTheDocument()
    expect(window.location.hash).toBe('#pasted-code')
    fill(/^Name/, 'Ada')
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fill(/^Confirm password/, 'fixture-password')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    await waitFor(() => expect(setupOwner).toHaveBeenCalledWith({
      code: 'pasted-code', name: 'Ada', email: 'ada@example.com', password: 'fixture-password',
    }))
  })

  it('refuses a link that has no setup code', async () => {
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    fill(/^Setup link/, 'http://localhost:3100/setup')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText(/no setup code/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create account' })).not.toBeInTheDocument()
  })

  it('with several administrators to choose from, points to hybro owner create', async () => {
    const { ApiError } = await import('@/lib/api-client')
    window.location.hash = '#fixture-code'
    setupOwner.mockRejectedValue(new ApiError(409, 'conflict', {
      detail: {
        code: 'owner_id_required',
        message: 'Choose which administrator the owner account takes over.',
        choices: ['user_a', 'user_b'],
      },
    }))
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    fill(/^Name/, 'Ada')
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fill(/^Confirm password/, 'fixture-password')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('hybro owner create --name <name> --email <email> --owner-id <id>')
    expect(alert).toHaveTextContent('user_a, user_b')
    expect(assign).not.toHaveBeenCalled()
  })

  it('does not send mismatched passwords', async () => {
    window.location.hash = '#fixture-code'
    const { SetupForm } = await import('@/components/auth/setup-form')
    render(<SetupForm />)
    fill(/^Name/, 'Ada')
    fill(/^Email/, 'ada@example.com')
    fill(/^Password/, 'fixture-password')
    fill(/^Confirm password/, 'other-fixture')
    expect(screen.getByRole('button', { name: 'Create account' })).toBeDisabled()
  })
})
