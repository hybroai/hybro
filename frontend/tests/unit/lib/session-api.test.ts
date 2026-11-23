import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import { closeSession, getSessionRecord, getSessionSteps, listSessions } from '@/lib/api/session'
import { sessionClientLabel, sessionClientName } from '@/lib/session-format'

const base = getApiUrl('sessions')

afterEach(() => server.resetHandlers())

describe('session api', () => {
  it('lists the owner sessions', async () => {
    const result = await listSessions()
    expect(result.sessions[0]).toMatchObject({ session_id: 'sess-1', state: 'waiting_on_owner' })
  })

  it('reads the owner record with the conversation id', async () => {
    const record = await getSessionRecord('sess-1')
    expect(record.conversation_id).toBe('conv-1')
    expect(record.client_label).toBe('Buyer agent')
  })

  it('escapes the session id and posts the owner close', async () => {
    const seen: string[] = []
    server.use(http.post(`${base}/:id/record/close`, ({ params, request }) => {
      seen.push(`${request.method} ${String(params.id)} ${new URL(request.url).pathname}`)
      return HttpResponse.json({ session_id: params.id, status: 'closed' })
    }))
    const closed = await closeSession('a/b')
    expect(closed.status).toBe('closed')
    expect(seen[0]).toContain('/sessions/a%2Fb/record/close')
  })

  it('names the session by its client and keeps the label apart', () => {
    const client = { client_id: 'client_1', name: 'CI bot', has_token: true }
    expect(sessionClientName({ client })).toBe('CI bot')
    expect(sessionClientLabel({ client_label: 'Laptop', client })).toBe('Laptop')
    expect(sessionClientLabel({ client_label: null, client })).toBeNull()
    expect(sessionClientLabel({ client_label: ' CI bot ', client })).toBeNull()
  })

  it('reads the session steps from the owner route', async () => {
    const seen: string[] = []
    server.use(http.get(`${base}/:id/steps`, ({ params, request }) => {
      seen.push(new URL(request.url).pathname)
      return HttpResponse.json({ session_id: params.id, steps: [] })
    }))

    const result = await getSessionSteps('a/b')

    expect(result).toEqual({ session_id: 'a/b', steps: [] })
    expect(seen[0]).toContain('/sessions/a%2Fb/steps')
  })
})
