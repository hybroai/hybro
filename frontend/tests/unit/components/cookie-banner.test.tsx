import type React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '../../utils/test-utils'
import { CookieBanner } from '@/components/cookie-banner'

const config = vi.hoisted(() => ({ measurementId: '' }))

vi.mock('@/lib/config', () => ({
  publicConfig: () => ({ analytics_measurement_id: config.measurementId }),
}))

// next/script injects tags outside the React tree; a marker shows what would load.
vi.mock('next/script', () => ({
  default: ({ src, id, children }: { src?: string; id?: string; children?: React.ReactNode }) => (
    <div data-testid="analytics-script" data-src={src ?? ''} data-id={id ?? ''}>{children}</div>
  ),
}))

const CONSENT_KEY = 'cookie_consent'
const scripts = () => screen.queryAllByTestId('analytics-script')

beforeEach(() => {
  localStorage.clear()
  config.measurementId = ''
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('CookieBanner', () => {
  it('shows no banner and loads no script without a measurement id', () => {
    localStorage.setItem(CONSENT_KEY, 'accepted')
    render(<CookieBanner />)

    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(scripts()).toHaveLength(0)
  })

  it('loads the script only after the user accepts', () => {
    config.measurementId = 'G-TEST1234'
    render(<CookieBanner />)

    expect(scripts()).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))

    expect(localStorage.getItem(CONSENT_KEY)).toBe('accepted')
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(scripts()[0].dataset.src).toBe('https://www.googletagmanager.com/gtag/js?id=G-TEST1234')
    expect(scripts()[1].textContent).toContain(`gtag('config', "G-TEST1234")`)
  })

  it('loads no script when the user declines', () => {
    config.measurementId = 'G-TEST1234'
    render(<CookieBanner />)

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))

    expect(localStorage.getItem(CONSENT_KEY)).toBe('declined')
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(scripts()).toHaveLength(0)
  })

  it('loads the script without a banner when consent was given before', () => {
    config.measurementId = 'G-TEST1234'
    localStorage.setItem(CONSENT_KEY, 'accepted')
    render(<CookieBanner />)

    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(scripts()).toHaveLength(2)
  })
})
