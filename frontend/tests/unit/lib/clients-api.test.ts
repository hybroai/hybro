import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import {
  createClient,
  getTokenRequirement,
  listClients,
  renameClient,
  revokeClient,
  rotateClient,
  setTokenRequirement,
  clientErrorMessage,
} from '@/lib/api/clients'
import { ApiError } from '@/lib/api-client'

// An obviously fake token, as the backend would return it once.
const TOKEN = 'hybro_client_fake-token-for-tests'
const VIEW = {
  client_id: 'client_1',
  name: 'CI bot',
  has_token: true,
  answer_grant: false,
  token_last4: 'ests',
  created_at: '2026-11-20T10:00:00Z',
  last_used_at: null,
  revoked_at: null,
}
const REQUIREMENT = { required: false, started_off: true, updated_at: '2026-11-20T10:00:00Z' }

describe('client API client', () => {
  it('lists the clients', async () => {
    server.use(http.get(getApiUrl('clients'), () => HttpResponse.json({ clients: [VIEW] })))
    await expect(listClients()).resolves.toEqual({ clients: [VIEW] })
  })

  it('creates a client with its name in the body, and returns the token', async () => {
    let body: unknown
    let url = ''
    server.use(http.post(getApiUrl('clients'), async ({ request }) => {
      body = await request.json()
      url = request.url
      return HttpResponse.json({ ...VIEW, token: TOKEN }, { status: 201 })
    }))
    await expect(createClient('CI bot')).resolves.toMatchObject({ token: TOKEN })
    expect(body).toEqual({ name: 'CI bot' })
    expect(url).not.toContain('CI')
  })

  it('renames, rotates and revokes by an encoded id', async () => {
    const seen: string[] = []
    server.use(
      http.patch(getApiUrl('clients/client%2F1'), async ({ request }) => {
        seen.push(`PATCH ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ ...VIEW, name: 'Nightly' })
      }),
      http.post(getApiUrl('clients/client%2F1/rotate'), () => {
        seen.push('rotate')
        return HttpResponse.json({ ...VIEW, token: TOKEN })
      }),
      http.post(getApiUrl('clients/client%2F1/revoke'), () => {
        seen.push('revoke')
        return HttpResponse.json({ ...VIEW, has_token: false, token_last4: null })
      }),
    )
    await renameClient('client/1', 'Nightly')
    await rotateClient('client/1')
    await revokeClient('client/1')
    expect(seen).toEqual(['PATCH {"name":"Nightly"}', 'rotate', 'revoke'])
  })

  it('reads and sets the token requirement', async () => {
    let body: unknown
    server.use(
      http.get(getApiUrl('clients/token-requirement'), () => HttpResponse.json(REQUIREMENT)),
      http.put(getApiUrl('clients/token-requirement'), async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ...REQUIREMENT, required: true })
      }),
    )
    await expect(getTokenRequirement()).resolves.toEqual(REQUIREMENT)
    await expect(setTokenRequirement(true)).resolves.toMatchObject({ required: true })
    expect(body).toEqual({ required: true })
  })

  it('turns a coded refusal into its message, and anything else into fixed text', () => {
    const coded = new ApiError(409, 'x', { detail: { code: 'client_revoked', message: 'This client is revoked.' } })
    expect(clientErrorMessage(coded)).toBe('This client is revoked.')
    expect(clientErrorMessage(new ApiError(500, 'boom'))).toBe('Something went wrong. Try again.')
    expect(clientErrorMessage(new Error('network'))).toBe('Something went wrong. Try again.')
  })
})
