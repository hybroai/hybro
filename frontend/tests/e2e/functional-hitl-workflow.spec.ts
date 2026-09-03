// tests/e2e/functional-hitl-workflow.spec.ts
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

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
        const name = (a.agent_card?.name || '').toLowerCase()
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
  roomId: string,
  timeoutMs = 45_000,
): Promise<Record<string, unknown>[]> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const pendingResp = await request.get(`${BACKEND_URL}/rooms/${roomId}/hitl/pending`)
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
  roomId: string,
): Promise<Record<string, unknown>[]> {
  const historyResp = await request.post(`${BACKEND_URL}/roomCenter/inquiryRoomMessagesByRoomId`, {
    data: { room_id: roomId },
  })
  if (!historyResp.ok()) {
    return []
  }
  const data = await historyResp.json()
  return (data.message_list || []) as Record<string, unknown>[]
}

async function waitForTerminalSynthesis(
  request: APIRequestContext,
  roomId: string,
  clientRequestId: string,
  timeoutMs = 75_000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const messages = await fetchRoomMessages(request, roomId)
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
  roomId: string,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const pendingResp = await request.get(`${BACKEND_URL}/rooms/${roomId}/hitl/pending`)
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

function synthesisInTimeline(page: Page) {
  return conversationTimeline(page).getByText(/Day 1:|Custom Travel Plan/)
}

test.describe('Functional HITL & Timeline Hydration Flow', () => {
  test('creates room, automatically sends human input when requested, and persists across reload', async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000)
    const travelAgentId = await getTravelPlannerAgentId(request)

    const createResp = await request.post(`${BACKEND_URL}/roomCenter/createNewRoom`, {
      data: {
        room_name: 'E2E Functional Test Room',
        room_owner_name: 'Developer Local',
        room_agent_ids: [travelAgentId],
        extend_info: { use_supervisor: true },
      },
    })
    expect(createResp.ok()).toBeTruthy()
    const roomData = await createResp.json()
    const roomId = roomData.room_id
    expect(roomId).toBeTruthy()

    const promptText = `Generate a travel plan e2e-${Date.now()}`
    const answerText = 'Kyoto, 3 days, $1500 budget'
    const clientRequestId = `e2e-req-${Date.now()}`
    const sendResp = await request.post(`${BACKEND_URL}/roomCenter/sendMessage`, {
      data: {
        room_id: roomId,
        user_input: promptText,
        message: {
          room_id: roomId,
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

    await page.goto(`/room/${roomId}`)
    await expect(userMessageInTimeline(page, promptText)).toBeVisible({ timeout: 30000 })

    const pendingRequests = await pollPendingHitl(request, roomId)
    expect(pendingRequests.length).toBeGreaterThan(0)
    for (const req of pendingRequests) {
      const submitResp = await request.post(`${BACKEND_URL}/rooms/${roomId}/hitl/respond-batch`, {
        data: {
          interaction_id: req.interaction_id,
          answers: [{ request_id: req.request_id, user_input: answerText }],
          client_request_id: req.client_request_id || clientRequestId,
        },
      })
      expect(submitResp.ok()).toBeTruthy()
      const submitBody = await submitResp.json()
      expect(submitBody.status).toMatch(/accepted|applied/)
    }
    await autoRespondHitlViaUiIfPresent(page, answerText)
    await waitForNoPendingHitl(request, roomId)

    const synthesis = await waitForTerminalSynthesis(request, roomId, clientRequestId)
    const synthesisText = messageText(synthesis)
    expect(synthesisText.length).toBeGreaterThan(30)

    await page.reload()
    await expect(userMessageInTimeline(page, promptText)).toBeVisible({ timeout: 30000 })
    await expect.poll(async () => {
      const messages = await fetchRoomMessages(request, roomId)
      return findTerminalSynthesis(messages, clientRequestId)
    }, { timeout: 30_000 }).toBeTruthy()
    await expect(synthesisInTimeline(page)).toBeVisible({ timeout: 30_000 })
  })
})
