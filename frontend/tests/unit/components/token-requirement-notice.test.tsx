import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import userEvent from '@testing-library/user-event'
import { cleanup, render, screen } from '../../utils/test-utils'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { getApiUrl } from '@/lib/utils'
import { TokenRequirementNotice } from '@/components/connect/token-requirement'

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: React.ComponentProps<'a'>) => <a href={String(href)} {...props}>{children}</a>,
}))

const answer = (requirement: { required: boolean; started_off: boolean }) =>
  server.use(http.get(getApiUrl('clients/token-requirement'), () => HttpResponse.json({ ...requirement, updated_at: '2026-11-20T00:00:00Z' })))

const settle = () => new Promise(resolve => setTimeout(resolve, 20))

beforeEach(() => {
  window.localStorage.clear()
  stubFetchWithoutSignal()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('TokenRequirementNotice', () => {
  it('tells an install that started with the requirement off, once', async () => {
    answer({ required: false, started_off: true })
    const user = userEvent.setup()
    render(<TokenRequirementNotice />)
    const notice = await screen.findByTestId('token-requirement-notice')
    expect(notice).toHaveTextContent('before client tokens')
    expect(notice).toHaveTextContent('accepts requests without one')
    expect(notice).toHaveTextContent('Create a client for each app, assistant, or script on the Clients tab')
    expect(notice).toHaveTextContent('Require a client token')
    expect(screen.getByRole('link', { name: 'Open Clients' })).toHaveAttribute('href', '/connect/clients')
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('token-requirement-notice')).toBeNull()
    cleanup()
    render(<TokenRequirementNotice />)
    await settle()
    expect(screen.queryByTestId('token-requirement-notice')).toBeNull()
  })

  it.each([
    { required: true, started_off: true },
    { required: false, started_off: false },
    { required: true, started_off: false },
  ])('stays hidden for %o', async requirement => {
    answer(requirement)
    render(<TokenRequirementNotice />)
    await settle()
    expect(screen.queryByTestId('token-requirement-notice')).toBeNull()
  })
})
