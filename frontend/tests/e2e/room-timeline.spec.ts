// tests/e2e/room-timeline.spec.ts
import { expect, test } from '@playwright/test'

test.describe('Room Timeline', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    const conversationId = `room-timeline-${testInfo.workerIndex}-${testInfo.retry}`
    const now = new Date().toISOString()

    await page.route('**/api/v1/rooms', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          room: { conversation_id: conversationId },
        }),
      })
    })
    await page.route('**/api/v1/agent/getAllAgents*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, agents: [] }),
      })
    )
    await page.route('**/api/v1/agentGroups**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, groups: [] }),
      })
    )
    await page.route('**/api/v1/rooms/*', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          resolved_agents: [],
          active_runs: [],
          room: {
            conversation_id: conversationId,
            room_name: 'Timeline E2E Room',
            room_owner_id: 'user_local_developer',
            room_owner_name: 'Developer Local',
            room_agent_set: {
              'agent-one': 'Agent One',
              'agent-two': 'Agent Two',
            },
            room_created_at: now,
            extend_info: {},
          },
        }),
      })
    })
    await page.route('**/api/v1/rooms/*/messages*', async (route) => {
      if (route.request().method() === 'POST') {
        const payload = route.request().postDataJSON() as {
          client_request_id?: string
          user_input?: string
        }
        const content = payload.user_input ?? 'Timeline E2E message'
        const messageId = `message-${Date.now()}`

        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            success: true,
            conversation_id: conversationId,
            message_id: messageId,
            message: {
              conversation_id: conversationId,
              message_id: messageId,
              message_type: 'user',
              user_id: 'user_local_developer',
              client_request_id: payload.client_request_id,
              message_created_at: now,
              message_content: { message_text: content },
              extend_info: null,
            },
          }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          conversation_id: conversationId,
          message_list: [
            {
              conversation_id: conversationId,
              message_id: 'seed-user',
              message_type: 'user',
              user_id: 'user_local_developer',
              message_created_at: now,
              message_content: { message_text: 'Seeded multi-agent request' },
              extend_info: {
                orchestration_status: 'completed',
                turn_completion_kind: 'deterministic',
              },
            },
            {
              conversation_id: conversationId,
              message_id: 'seed-agent-one',
              message_type: 'agent',
              agent_id: 'agent-one',
              related_message_id: 'seed-user',
              message_created_at: now,
              task_updated_at: now,
              message_content: {
                message_text: 'Agent One response',
                message_task: {
                  id: 'seed-task-one',
                  status: { state: 'completed' },
                  metadata: { agent_id: 'agent-one' },
                },
              },
            },
            {
              conversation_id: conversationId,
              message_id: 'seed-agent-two',
              message_type: 'agent',
              agent_id: 'agent-two',
              related_message_id: 'seed-user',
              message_created_at: now,
              task_updated_at: now,
              message_content: {
                message_text: 'Agent Two response',
                message_task: {
                  id: 'seed-task-two',
                  status: { state: 'completed' },
                  metadata: { agent_id: 'agent-two' },
                },
              },
            },
          ],
        }),
      })
    })
    await page.route('**/api/v1/rooms/*/active-runs', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, conversation_id: conversationId, active_runs: [] }),
      })
    )
    await page.route('**/api/v1/rooms/*/hitl/pending', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ requests: [] }),
      })
    )
  })

  test('send message creates a turn', async ({ page }) => {
    // This test verifies the turn-based rendering after sending a message
    await page.goto('/chat')

    // Type a message
    const input = page.locator('[data-testid="chat-input"], [contenteditable="true"]').first()
    await input.fill('Hello from E2E test')

    // Send
    const sendButton = page.locator('button[aria-label*="Send"], button[type="submit"]').first()
    await sendButton.click()

    // Should navigate to a room and render a turn with the user prompt
    await page.waitForURL(/\/room\//)

    // The user message should appear as part of a turn
    await expect(page.getByText('Hello from E2E test')).toBeVisible({ timeout: 10000 })
  })

  test('renders history without a canonical root as a read-only transcript', async ({ page }, testInfo) => {
    await page.goto(`/room/room-timeline-${testInfo.workerIndex}-${testInfo.retry}`)

    const turn = page
      .locator('[data-turn-kind="transcript"]')
      .filter({ hasText: 'Seeded multi-agent request' })
    await expect(turn).toHaveCount(1)
    await expect(turn.getByText('Agent One response', { exact: true })).toBeVisible()
    await expect(turn.getByText('Agent Two response', { exact: true })).toBeVisible()
    await expect(turn.locator('.agent-index, .conversation-agent-card, [data-canonical-turn-trace]')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
})
