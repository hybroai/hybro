// tests/e2e/functional-hitl-workflow.spec.ts
import { type APIRequestContext, type Page } from '@playwright/test'
import { expect, test } from './fixtures/auth'

const BACKEND_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000/api/v1'
const DEFAULT_TRAVEL_PLANNER_AGENT_ID = '575ee896f1e24823943a1e98aee111c9'

/**
 * Resolves active Travel Planner Agent ID dynamically.
 */
async function getTravelPlannerAgentId(request: APIRequestContext): Promise<string> {
  try {
    const resp = await request.get(`${BACKEND_URL}/agent/getAllAgents?active_only=true`)
    if (resp.ok()) {
      const data = await resp.json()
      const agents = data.agents || []
      for (const a of agents) {
        const name = (a.name || '').toLowerCase()
        if (name.includes('travel') || name.includes('planner')) {
          return a.agent_id || DEFAULT_TRAVEL_PLANNER_AGENT_ID
        }
      }
    }
  } catch {}
  return DEFAULT_TRAVEL_PLANNER_AGENT_ID
}

function messageText(msg: Record<string, unknown>): string {
  const content = msg.message_content as { message_text?: string } | undefined
  return (content?.message_text || '').trim()
}

function taskState(msg: Record<string, unknown>): string | undefined {
  const content = msg.message_content as { message_task?: { status?: { state?: string } } } | undefined
  return content?.message_task?.status?.state
}

function findTerminalSynthesis(
  messages: Record<string, unknown>[],
  clientRequestId: string,
): Record<string, unknown> | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const msg = messages[index]
    if (msg.message_type !== 'agent') continue
    if (msg.client_request_id !== clientRequestId) continue
    if (msg.agent_id !== 'system:hybro') continue
    const text = messageText(msg)
    if (text.length <= 30) continue
    const state = taskState(msg)
    if (state === undefined || state === 'completed') {
      return msg
    }
  }
  return undefined
}

async function pollPendingHitl(
  request: APIRequestContext,
  conversationId: string,
  timeoutMs = 45_000,
): Promise<Record<string, unknown>[]> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const pendingResp = await request.get(`${BACKEND_URL}/rooms/${conversationId}/hitl/pending`)
    expect(pendingResp.ok()).toBeTruthy()
    const data = await pendingResp.json()
    const requests = (data.requests || []) as Record<string, unknown>[]
    if (requests.length > 0) {
      return requests
    }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  return []
}


async function fetchRoomMessages(
  request: APIRequestContext,
  conversationId: string,
): Promise<Record<string, unknown>[]> {
  const historyResp = await request.get(`${BACKEND_URL}/rooms/${encodeURIComponent(conversationId)}/messages`)
  if (!historyResp.ok()) {
    return []
  }
  const data = await historyResp.json()
  return (data.message_list || []) as Record<string, unknown>[]
}

async function waitForTerminalSynthesis(
  request: APIRequestContext,
  conversationId: string,
  clientRequestId: string,
  // Live supervisor runs with real Agents regularly take over a minute.
  timeoutMs = 240_000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const messages = await fetchRoomMessages(request, conversationId)
    const synthesis = findTerminalSynthesis(messages, clientRequestId)
    if (synthesis) {
      return synthesis
    }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('Timed out waiting for terminal supervisor synthesis')
}

async function waitForNoPendingHitl(
  request: APIRequestContext,
  conversationId: string,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const pendingResp = await request.get(`${BACKEND_URL}/rooms/${conversationId}/hitl/pending`)
    if (pendingResp.ok()) {
      const data = await pendingResp.json()
      const requests = (data.requests || []) as Record<string, unknown>[]
      if (requests.length === 0) {
        return
      }
    }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('Timed out waiting for pending HITL queue to drain')
}

async function autoRespondHitlViaUiIfPresent(
  page: Page,
  answerText: string,
) {
  const hitlBar = page.locator('[data-testid="hitl-response-bar"]')
  if (await hitlBar.isVisible({ timeout: 1500 }).catch(() => false)) {
    const textInput = hitlBar.locator('textarea, input[type="text"]').first()
    if (await textInput.isVisible({ timeout: 1000 }).catch(() => false)) {
      await textInput.fill(answerText)
      const submitBtn = hitlBar
        .locator('button[type="submit"], button:has-text("Submit"), button:has-text("Send")')
        .first()
      if (await submitBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
        await submitBtn.click()
      }
    }
  }
}

function conversationTimeline(page: Page) {
  return page.locator('.conversation-turn-content')
}

function userMessageInTimeline(page: Page, text: string) {
  return page.locator('.conversation-turn .conversation-user-message-text').filter({ hasText: text })
}

function synthesisInTimeline(page: Page, synthesisText: string) {
  // Model wording varies per run, so match a slice of the persisted synthesis.
  // Markdown-formatted lines render across several elements, so pick a plain
  // line without inline markup.
  const plainLine = synthesisText
    .split('\n')
    // Drop list markers, which render as bullets rather than text.
    .map(line => line.trim().replace(/^(?:[-+]|\d+[.)])\s+/, ''))
    .filter(line => line.length >= 20 && !/[*_`#|\[\]<>]/.test(line))
    .pop() ?? synthesisText.trim()
  return conversationTimeline(page).getByText(plainLine.slice(0, 40), { exact: false }).first()
}

test.describe('Functional HITL & Timeline Hydration Flow', () => {
  test('creates room, automatically sends human input when requested, and persists across reload', async ({
    page,
    request,
  }) => {
    test.setTimeout(480_000)
    const travelAgentId = await getTravelPlannerAgentId(request)

    const createResp = await request.post(`${BACKEND_URL}/rooms`, {
      data: {
        room_name: 'E2E Functional Test Room',
        room_owner_name: 'Developer Local',
        membership_seed_input: 'manual',
        room_agent_ids: [travelAgentId],
        extend_info: { use_supervisor: true },
      },
    })
    expect(createResp.ok()).toBeTruthy()
    const roomData = await createResp.json()
    const conversationId = roomData.conversation_id
    expect(conversationId).toBeTruthy()

    const promptText = `Generate a travel plan e2e-${Date.now()}`
    const answerText = 'Kyoto, 3 days, $1500 budget'
    const clientRequestId = `e2e-req-${Date.now()}`
    const sendResp = await request.post(`${BACKEND_URL}/rooms/${encodeURIComponent(conversationId)}/messages`, {
      data: {
        user_input: promptText,
        message: {
          conversation_id: conversationId,
          message_id: '',
          message_type: 'user',
          message_content: {
            message_text: promptText,
          },
        },
        mode: 'supervisor',
        client_request_id: clientRequestId,
        agent_scope: {
          source: 'mention',
          agent_ids: [travelAgentId],
        },
      },
    })
    expect(sendResp.ok()).toBeTruthy()

    await page.goto(`/room/${conversationId}`)
    await expect(userMessageInTimeline(page, promptText)).toBeVisible({ timeout: 30000 })

    // Model-first HITL lets the supervisor answer an Agent's question itself,
    // so a user-facing question is not guaranteed; answer it when one appears.
    const pendingRequests = await pollPendingHitl(request, conversationId)
    // An interaction may ask several questions; answer each one as a single
    // batch, as the room UI does.
    if (pendingRequests.length === 0) {
      test.info().annotations.push({
        type: 'hitl',
        description: 'No user-facing question appeared; the supervisor answered the Agent itself.',
      })
    }
    // Group by interaction id and version, since a later round can reuse the id.
    const byInteraction = new Map<string, typeof pendingRequests>()
    for (const req of pendingRequests) {
      const key = `${String(req.interaction_id)}@${String(req.interaction_version)}`
      byInteraction.set(key, [...(byInteraction.get(key) ?? []), req])
    }
    for (const reqs of byInteraction.values()) {
      const interactionId = String(reqs[0].interaction_id)
      const submitResp = await request.post(`${BACKEND_URL}/rooms/${conversationId}/hitl/respond-batch`, {
        data: {
          interaction_id: interactionId,
          answers: reqs.map(req => ({ request_id: req.request_id, user_input: answerText })),
          client_request_id: reqs[0].client_request_id || clientRequestId,
        },
        timeout: 180_000,
      })
      expect(submitResp.ok(), `${submitResp.status()} ${await submitResp.text()}`).toBeTruthy()
      const submitBody = await submitResp.json()
      expect(submitBody.status).toMatch(/accepted|applied/)
    }
    await autoRespondHitlViaUiIfPresent(page, answerText)
    await waitForNoPendingHitl(request, conversationId)

    const synthesis = await waitForTerminalSynthesis(request, conversationId, clientRequestId)
    const synthesisText = messageText(synthesis)
    expect(synthesisText.length).toBeGreaterThan(30)

    await page.reload()
    await expect(userMessageInTimeline(page, promptText)).toBeVisible({ timeout: 30000 })
    await expect.poll(async () => {
      const messages = await fetchRoomMessages(request, conversationId)
      return findTerminalSynthesis(messages, clientRequestId)
    }, { timeout: 30_000 }).toBeTruthy()
    await expect(synthesisInTimeline(page, synthesisText)).toBeVisible({ timeout: 30_000 })
  })
})
