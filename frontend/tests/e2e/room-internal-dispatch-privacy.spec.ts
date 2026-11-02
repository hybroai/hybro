import { expect, test, type Page } from '@playwright/test'

const ROOM_ID = 'privacy-room'
const USER_MESSAGE_ID = 'user-msg-stream'
const AGENT_MESSAGE_ID = 'agent-msg-stream'
const INTERNAL_TEXT = 'INTERNAL DISPATCH TASK: include private planner context'
const PUBLIC_LABEL = 'Requesting Insurer'
const USER_TEXT = 'Get a quote'
const STREAM_PUBLIC_TEXT = 'Public streaming update applied'

type LeakWindow = typeof window & {
  __internalPromptLeakCount?: number
  __internalPromptLeakSamples?: string[]
}

async function installInternalPromptLeakWatcher(page: Page) {
  await page.addInitScript((internalText: string) => {
    const leakWindow = window as LeakWindow
    leakWindow.__internalPromptLeakCount = 0
    leakWindow.__internalPromptLeakSamples = []

    const recordIfLeaked = () => {
      const bodyText = document.body?.textContent ?? ''
      if (!bodyText.includes(internalText)) return
      leakWindow.__internalPromptLeakCount = (leakWindow.__internalPromptLeakCount ?? 0) + 1
      leakWindow.__internalPromptLeakSamples = [
        ...(leakWindow.__internalPromptLeakSamples ?? []),
        bodyText.slice(0, 500),
      ].slice(-5)
    }

    const start = () => {
      recordIfLeaked()
      const target = document.body ?? document.documentElement
      new MutationObserver(recordIfLeaked).observe(target, {
        childList: true,
        subtree: true,
        characterData: true,
      })
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', start, { once: true })
    } else {
      start()
    }
  }, INTERNAL_TEXT)
}

async function expectInternalPromptNeverRendered(page: Page) {
  const leaks = await page.evaluate(() => {
    const leakWindow = window as LeakWindow
    return {
      count: leakWindow.__internalPromptLeakCount ?? 0,
      samples: leakWindow.__internalPromptLeakSamples ?? [],
    }
  })
  expect(leaks).toEqual({ count: 0, samples: [] })
  await expect(page.getByText(INTERNAL_TEXT)).toHaveCount(0)
}

function agentFixture() {
  return {
    agent_id: 'agent-1',
    provider_id: 'provider-1',
    provider_name: 'Example Provider',
    status: 'active',
    source: 'cloud',
    is_public: true,
    call_count: 0,
    name: 'Insurer Agent',
    description: 'Quotes insurance submissions',
    version: '1.0.0',
    skills: [],
    capabilities: {
      streaming: false,
      push_notifications: false,
      state_transition_history: false,
      extensions: false,
    },
    default_input_modes: ['text/plain'],
    default_output_modes: ['text/plain'],
  }
}

test('streaming agent turn never displays internal dispatch prompt', async ({ page }) => {
  const now = new Date().toISOString()
  let sseRelease!: (clientRequestId: string) => void
  const releaseSse = new Promise<string>(resolve => {
    sseRelease = resolve
  })
  let sseRequestCount = 0
  let sseTaskUpdateClientRequestId: string | undefined

  await installInternalPromptLeakWatcher(page)

  await page.route('**/api/v1/agent/getAllAgents*', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ success: true, agents: [agentFixture()] }),
    })
  })

  await page.route('**/api/v1/agentGroups**', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ success: true, groups: [] }),
    })
  })

  await page.route(`**/api/v1/rooms/${ROOM_ID}`, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        conversation_id: ROOM_ID,
        resolved_agents: [],
        active_runs: [],
        room: {
          conversation_id: ROOM_ID,
          room_name: 'Privacy Room',
          room_owner_id: 'user-1',
          room_owner_name: 'User',
          room_agent_set: { 'agent-1': 'Insurer Agent' },
          room_created_at: now,
          extend_info: { use_supervisor: true },
        },
      }),
    })
  })


  await page.route(`**/api/v1/rooms/${ROOM_ID}/active-runs`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        conversation_id: ROOM_ID,
        active_runs: [],
      }),
    })
  })

  await page.route(`**/api/v1/rooms/${ROOM_ID}/hitl/pending`, async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ requests: [] }),
    })
  })

  await page.route(`**/api/v1/rooms/${ROOM_ID}/messages*`, async route => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          conversation_id: ROOM_ID,
          message_list: [],
        }),
      })
      return
    }

    const payload = route.request().postDataJSON() as { client_request_id?: unknown }
    const clientRequestId = typeof payload.client_request_id === 'string'
      ? payload.client_request_id
      : ''

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        conversation_id: ROOM_ID,
        message_id: USER_MESSAGE_ID,
        message: {
          conversation_id: ROOM_ID,
          message_id: USER_MESSAGE_ID,
          message_type: 'user',
          user_id: 'user_local_developer',
          client_request_id: clientRequestId,
          message_created_at: now,
          message_content: { message_text: USER_TEXT },
          extend_info: null,
        },
      }),
    })
  })

  await page.route(`**/api/v1/sse/room/${ROOM_ID}/stream`, async route => {
    sseRequestCount += 1
    if (sseRequestCount > 1) {
      await route.fulfill({
        status: 503,
        contentType: 'text/plain',
        body: 'stream closed for test',
      })
      return
    }

    const clientRequestId = await releaseSse
    sseTaskUpdateClientRequestId = clientRequestId
    const frames = [
      {
        type: 'connected',
        conversation_id: ROOM_ID,
        timestamp: now,
        data: { status: 'connected' },
      },
      {
        type: 'task_update',
        conversation_id: ROOM_ID,
        timestamp: now,
        data: {
          message_id: AGENT_MESSAGE_ID,
          related_message_id: USER_MESSAGE_ID,
          agent_id: 'agent-1',
          agent_name: 'Insurer Agent',
          status: 'working',
          content: STREAM_PUBLIC_TEXT,
          task_content: INTERNAL_TEXT,
          status_message: PUBLIC_LABEL,
          client_request_id: clientRequestId,
        },
      },
    ]

    await route.fulfill({
      status: 200,
      headers: {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
      },
      body: frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join(''),
    })
  })

  await page.goto(`/room/${ROOM_ID}`)

  await expect(page.getByTestId('chat-input')).toBeVisible()
  await expectInternalPromptNeverRendered(page)

  await page.getByTestId('chat-input').fill(USER_TEXT)
  await expect(page.getByTestId('send-button')).toBeEnabled()
  await expectInternalPromptNeverRendered(page)

  const sendRequestPromise = page.waitForRequest(
    request => request.method() === 'POST'
      && new URL(request.url()).pathname.endsWith(`/api/v1/rooms/${ROOM_ID}/messages`),
  )
  await page.getByTestId('send-button').click()
  const sendRequest = await sendRequestPromise
  const sendPayload = sendRequest.postDataJSON() as { client_request_id?: unknown }
  const clientRequestId = sendPayload.client_request_id

  expect(typeof clientRequestId).toBe('string')
  expect(clientRequestId).not.toBe('')
  await expect(page.getByText(USER_TEXT)).toBeVisible()
  await expectInternalPromptNeverRendered(page)

  await expect(page.getByTestId('stop-processing')).toBeVisible()
  await expectInternalPromptNeverRendered(page)

  sseRelease(clientRequestId as string)

  await expect(page.getByText(STREAM_PUBLIC_TEXT)).toBeVisible()
  expect(sseTaskUpdateClientRequestId).toBe(clientRequestId)
  await expectInternalPromptNeverRendered(page)
})
