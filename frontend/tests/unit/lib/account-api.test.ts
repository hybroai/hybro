import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import {
  accountErrorMessage,
  changePassword,
  getAuthStatus,
  listBrowsers,
  revokeBrowser,
  setupOwner,
  signIn,
  signOut,
  updateProfile,
} from '@/lib/api/account'
import { getClientAuthHeaders } from '@/lib/auth'
import { isPlainHttpFromAnotherMachine } from '@/lib/plain-http'

const OWNER = { user_id: 'user_local_developer', name: 'Ada', email: 'ada@example.com' }

describe('account API client', () => {
  it('reads the status', async () => {
    server.use(http.get(getApiUrl('auth/status'), () => HttpResponse.json({ account_exists: true, owner: OWNER })))
    await expect(getAuthStatus()).resolves.toEqual({ account_exists: true, owner: OWNER })
  })

  it('signs in with the email and password in the body', async () => {
    let body: unknown
    server.use(http.post(getApiUrl('auth/sign-in'), async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ owner: OWNER })
    }))
    await signIn('ada@example.com', 'fixture-password')
    expect(body).toEqual({ email: 'ada@example.com', password: 'fixture-password' })
  })

  it('sends the setup code with the new owner', async () => {
    let body: unknown
    server.use(http.post(getApiUrl('auth/setup'), async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ owner: OWNER }, { status: 201 })
    }))
    await setupOwner({ code: 'fixture-code', name: 'Ada', email: 'ada@example.com', password: 'fixture-password' })
    expect(body).toEqual({ code: 'fixture-code', name: 'Ada', email: 'ada@example.com', password: 'fixture-password' })
  })

  it('calls the account routes', async () => {
    const seen: string[] = []
    server.use(
      http.post(getApiUrl('auth/sign-out'), () => { seen.push('sign-out'); return new HttpResponse(null, { status: 204 }) }),
      http.patch(getApiUrl('auth/profile'), async ({ request }) => { seen.push(`profile ${JSON.stringify(await request.json())}`); return HttpResponse.json(OWNER) }),
      http.post(getApiUrl('auth/password'), async ({ request }) => { seen.push(`password ${JSON.stringify(await request.json())}`); return new HttpResponse(null, { status: 204 }) }),
      http.get(getApiUrl('auth/browsers'), () => { seen.push('browsers'); return HttpResponse.json({ items: [] }) }),
      http.delete(getApiUrl('auth/browsers/:id'), ({ params }) => { seen.push(`revoke ${params.id}`); return new HttpResponse(null, { status: 204 }) }),
    )
    await signOut()
    await updateProfile({ name: 'Ada L', email: 'ada@example.com' })
    await changePassword({ current_password: 'old-fixture', new_password: 'new-fixture' })
    await listBrowsers()
    await revokeBrowser('b-1')
    expect(seen).toEqual([
      'sign-out',
      'profile {"name":"Ada L","email":"ada@example.com"}',
      'password {"current_password":"old-fixture","new_password":"new-fixture"}',
      'browsers',
      'revoke b-1',
    ])
  })

  it('shows the backend message of a refusal, and a fallback otherwise', async () => {
    server.use(http.post(getApiUrl('auth/sign-in'), () => HttpResponse.json(
      { detail: { code: 'invalid_credentials', message: 'The email or the password is wrong.' } }, { status: 401 },
    )))
    const failure = await signIn('ada@example.com', 'wrong').catch((error: unknown) => error)
    expect(accountErrorMessage(failure, 'fallback')).toBe('The email or the password is wrong.')
    expect(accountErrorMessage(new Error('network'), 'fallback')).toBe('fallback')
  })

  it('sends no Authorization header: the sign-in cookie is the credential', async () => {
    const headers = await getClientAuthHeaders(async () => 'leftover-token')
    expect(headers).toEqual({ 'Content-Type': 'application/json' })
  })
})

describe('plain HTTP warning', () => {
  it('warns only on plain HTTP from a host that is not this machine', () => {
    expect(isPlainHttpFromAnotherMachine({ protocol: 'http:', hostname: 'hybro.lan' })).toBe(true)
    expect(isPlainHttpFromAnotherMachine({ protocol: 'http:', hostname: '192.168.1.5' })).toBe(true)
    expect(isPlainHttpFromAnotherMachine({ protocol: 'https:', hostname: 'hybro.example' })).toBe(false)
    for (const hostname of ['localhost', '127.0.0.1', '[::1]', 'app.localhost', '127.0.0.2']) {
      expect(isPlainHttpFromAnotherMachine({ protocol: 'http:', hostname })).toBe(false)
    }
  })
})
