// tests/e2e/sessions.spec.ts
// A client opens a session through the API; the owner sees it in the sidebar,
// approves the agent's question on the session page, and the client reads the
// completed reply. Requires the live stack (./scripts/hybro start).
import { type APIRequestContext, type Locator } from '@playwright/test'
import { expect, test } from './fixtures/auth'

const BACKEND_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000/api/v1'
const ANSWER = 'Kyoto, Japan, 3 days starting next Monday, budget $1500, mid-range hotels, temples and food'

async function travelPlannerId(request: APIRequestContext): Promise<string> {
  const resp = await request.get(`${BACKEND_URL}/agent/getAllAgents?active_only=true`)
  expect(resp.ok()).toBeTruthy()
  const agents = ((await resp.json()).agents || []) as { agent_id: string; name?: string }[]
  const agent = agents.find(a => /travel|planner/i.test(a.name || ''))
  if (!agent) throw new Error('Travel Planner Agent is not registered')
  return agent.agent_id
}

// Answer every question on the card: the first suggested choice, or the
// prepared text for free-form questions, then send.
async function answerQuestions(card: Locator): Promise<void> {
  for (let step = 0; step < 6; step += 1) {
    const text = card.getByPlaceholder('Type your answer…')
    if (await text.isVisible()) await text.fill(ANSWER)
    else await card.locator('fieldset:not([hidden])').getByRole('radio').or(card.locator('fieldset:not([hidden])').getByRole('checkbox')).first().check()
    const send = card.getByRole('button', { name: 'Send' })
    if (await send.isVisible()) {
      await send.click()
      // The card leaves its questions once the answers are accepted.
      await expect(card.locator('fieldset:not([hidden])')).toHaveCount(0, { timeout: 30_000 })
      return
    }
    await card.getByRole('button', { name: 'Next' }).click()
  }
  throw new Error('The question card never offered Send')
}

async function sessionState(request: APIRequestContext, sessionId: string): Promise<Record<string, unknown>> {
  const resp = await request.get(`${BACKEND_URL}/sessions/${sessionId}`)
  expect(resp.ok()).toBeTruthy()
  return resp.json()
}

test.describe('Inbox', () => {
  test.setTimeout(180_000)

  test('owner approves a client session in the app', async ({ page, request }) => {
    const agentId = await travelPlannerId(request)
    const label = `E2E client ${Date.now()}`
    const send = await request.post(`${BACKEND_URL}/sessions/messages`, {
      data: {
        agent_id: agentId,
        text: 'Generate a travel plan',
        client_request_id: crypto.randomUUID(),
        client_label: label,
      },
      timeout: 60_000,
    })
    expect(send.ok()).toBeTruthy()
    const sent = await send.json()
    const sessionId = sent.session_id as string

    await expect.poll(async () => (await sessionState(request, sessionId)).state, { timeout: 60_000 })
      .toBe('waiting_on_owner')

    await page.goto('/sessions')
    await expect(page.getByRole('link', { name: /^Inbox, \d+ need input/ })).toBeVisible({ timeout: 30_000 })
    // The ticket list: filter by status, search, and open the ticket.
    await page.getByRole('button', { name: 'Filter by status' }).click()
    await page.getByRole('menuitemcheckbox', { name: 'Needs you' }).click()
    await page.keyboard.press('Escape')
    await page.getByRole('searchbox', { name: 'Search sessions' }).fill(label)
    const table = page.getByRole('table', { name: 'Inbox' })
    await expect(table.getByRole('row')).toHaveCount(2)
    await expect(table.getByRole('cell', { name: label, exact: true })).toBeVisible()
    await table.getByRole('link', { name: 'Generate a travel plan' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Generate a travel plan' })).toBeVisible()
    await expect(page.getByRole('complementary', { name: 'Details' }).getByText(label)).toBeVisible()
    // The session record is not a chat: no message composer.
    await expect(page.getByPlaceholder(/message/i)).toHaveCount(0)

    // The pending question uses the chat question card at the end of the record.
    const decision = page.getByRole('region', { name: /needs your input/ })
    await expect(decision).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('list', { name: 'Session record' })).toBeVisible()
    await answerQuestions(decision)

    // The agent may ask a follow-up round before it replies. Right after an
    // answer the state can still read waiting_on_owner, so wait for either
    // completion or a fresh question card that can be answered.
    const openQuestion = decision.locator('fieldset:not([hidden])')
    for (let round = 0; round < 3; round += 1) {
      let outcome = ''
      await expect.poll(async () => {
        if ((await sessionState(request, sessionId)).state === 'completed') outcome = 'completed'
        else if (await openQuestion.count() > 0) outcome = 'question'
        else outcome = ''
        return outcome
      }, { timeout: 120_000 }).not.toBe('')
      if (outcome === 'completed') break
      await answerQuestions(decision)
    }
    // The client's view: its own messages and the agent's reply, no owner decisions.
    const client = await sessionState(request, sessionId)
    expect(client.state).toBe('completed')
    const requests = client.history as { text: string; reply: string | null }[]
    expect(requests.at(-1)?.reply).toBeTruthy()
    expect(JSON.stringify(client)).not.toContain(ANSWER)
    const thread = page.getByRole('list', { name: 'Session record' })
    await expect(thread.getByText('answered').first()).toBeVisible({ timeout: 30_000 })
    await expect(thread.getByText(/^replied/)).toBeVisible({ timeout: 30_000 })

    // Back returns to the same filtered list. Scope to the page: the sidebar
    // link is also named "Inbox" once no session waits on the owner.
    await page.getByRole('main').getByRole('link', { name: 'Inbox', exact: true }).click()
    await expect(page.getByRole('searchbox', { name: 'Search sessions' })).toHaveValue(label)
    await expect(page).toHaveURL(/status=waiting_on_owner/)

    // The chat history excludes the session container.
    const record = await (await request.get(`${BACKEND_URL}/sessions/${sessionId}/record`)).json()
    const history = await (await request.get(`${BACKEND_URL}/rooms`)).json()
    expect((history.items as { conversation_id: string }[]).map(item => item.conversation_id)).not.toContain(record.conversation_id)
  })
})
