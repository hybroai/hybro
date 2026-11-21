import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import {
  createSource,
  getTokenRequirement,
  listSources,
  renameSource,
  revokeSource,
  rotateSource,
  setTokenRequirement,
  sourceErrorMessage,
} from '@/lib/api/source'
import { ApiError } from '@/lib/api-client'

// An obviously fake token, as the backend would return it once.
const TOKEN = 'hybro_src_fake-token-for-tests'
const VIEW = {
  source_id: 'src_1',
  name: 'CI bot',
  has_token: true,
  token_last4: 'ests',
  created_at: '2026-11-20T10:00:00Z',
  last_used_at: null,
  revoked_at: null,
}
const REQUIREMENT = { required: false, started_off: true, updated_at: '2026-11-20T10:00:00Z' }

describe('source API client', () => {
  it('lists the sources', async () => {
    server.use(http.get(getApiUrl('sources'), () => HttpResponse.json({ sources: [VIEW] })))
    await expect(listSources()).resolves.toEqual({ sources: [VIEW] })
  })

  it('creates a source with its name in the body, and returns the token', async () => {
    let body: unknown
    let url = ''
    server.use(http.post(getApiUrl('sources'), async ({ request }) => {
      body = await request.json()
      url = request.url
      return HttpResponse.json({ ...VIEW, token: TOKEN }, { status: 201 })
    }))
    await expect(createSource('CI bot')).resolves.toMatchObject({ token: TOKEN })
    expect(body).toEqual({ name: 'CI bot' })
    expect(url).not.toContain('CI')
  })

  it('renames, rotates and revokes by an encoded id', async () => {
    const seen: string[] = []
    server.use(
      http.patch(getApiUrl('sources/src%2F1'), async ({ request }) => {
        seen.push(`PATCH ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ ...VIEW, name: 'Nightly' })
      }),
      http.post(getApiUrl('sources/src%2F1/rotate'), () => {
        seen.push('rotate')
        return HttpResponse.json({ ...VIEW, token: TOKEN })
      }),
      http.post(getApiUrl('sources/src%2F1/revoke'), () => {
        seen.push('revoke')
        return HttpResponse.json({ ...VIEW, has_token: false, token_last4: null })
      }),
    )
    await renameSource('src/1', 'Nightly')
    await rotateSource('src/1')
    await revokeSource('src/1')
    expect(seen).toEqual(['PATCH {"name":"Nightly"}', 'rotate', 'revoke'])
  })

  it('reads and sets the token requirement', async () => {
    let body: unknown
    server.use(
      http.get(getApiUrl('sources/token-requirement'), () => HttpResponse.json(REQUIREMENT)),
      http.put(getApiUrl('sources/token-requirement'), async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ...REQUIREMENT, required: true })
      }),
    )
    await expect(getTokenRequirement()).resolves.toEqual(REQUIREMENT)
    await expect(setTokenRequirement(true)).resolves.toMatchObject({ required: true })
    expect(body).toEqual({ required: true })
  })

  it('turns a coded refusal into its message, and anything else into fixed text', () => {
    const coded = new ApiError(409, 'x', { detail: { code: 'source_revoked', message: 'This source is revoked.' } })
    expect(sourceErrorMessage(coded)).toBe('This source is revoked.')
    expect(sourceErrorMessage(new ApiError(500, 'boom'))).toBe('Something went wrong. Try again.')
    expect(sourceErrorMessage(new Error('network'))).toBe('Something went wrong. Try again.')
  })
})
