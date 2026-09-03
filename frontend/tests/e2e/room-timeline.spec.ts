// tests/e2e/room-timeline.spec.ts
import { expect, test } from '@playwright/test'

test.describe('Room Timeline', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    const roomId = `room-timeline-${testInfo.workerIndex}-${testInfo.retry}`
    const now = new Date().toISOString()

    await page.route('**/roomCenter/createNewRoom', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          room: { room_id: roomId },
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
    await page.route('**/api/v1/roomCenter/inquiryRoomSetting', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          resolved_agents: [],
          active_runs: [],
          room: {
            room_id: roomId,
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
    )
    await page.route('**/api/v1/roomCenter/inquiryRoomMessagesByRoomId', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          room_id: roomId,
          message_list: [
            {
              room_id: roomId,
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
              room_id: roomId,
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
              room_id: roomId,
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
    )
    await page.route('**/api/v1/roomCenter/inquiryActiveRuns', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, room_id: roomId, active_runs: [] }),
      })
    )
    await page.route('**/api/v1/rooms/*/hitl/pending', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ requests: [] }),
      })
    )
    await page.route('**/api/v1/roomCenter/sendMessage', async (route) => {
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
          room_id: roomId,
          message_id: messageId,
          message: {
            room_id: roomId,
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
    })
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

  test('multiple agents are grouped in a single turn', async ({ page }) => {
    // This test requires a room with multiple agent responses
    // Navigate to an existing room with messages (or create via API)
    // For now, verify the structural elements exist
    await page.goto('/chat')

    const input = page.locator('[data-testid="chat-input"], [contenteditable="true"]').first()
    await input.fill('Test multi-agent grouping')

    const sendButton = page.locator('button[aria-label*="Send"], button[type="submit"]').first()
    await sendButton.click()

    await page.waitForURL(/\/room\//)

    const completedTurn = page
      .locator('.conversation-turn')
      .filter({ hasText: 'Seeded multi-agent request' })
    await expect(completedTurn).toHaveCount(1)

    const activityToggle = completedTurn.locator('.agent-index button').first()
    await expect(activityToggle).toHaveAttribute('aria-expanded', 'false')
    await activityToggle.click()

    await expect(
      completedTurn.getByRole('link', { name: 'Agent One' })
    ).toBeVisible()
    await expect(
      completedTurn.getByRole('link', { name: 'Agent Two' })
    ).toBeVisible()
  })

  test('collapse and expand a completed multi-agent activity strip', async ({ page }) => {
    // Navigate to a room with at least 2 completed turns
    // This test is structural — verifies the collapse/expand interaction
    await page.goto('/chat')

    const input = page.locator('[data-testid="chat-input"], [contenteditable="true"]').first()
    await input.fill('First question for collapse test')

    const sendButton = page.locator('button[aria-label*="Send"], button[type="submit"]').first()
    await sendButton.click()

    await page.waitForURL(/\/room\//)

    const completedTurn = page
      .locator('.conversation-turn')
      .filter({ hasText: 'Seeded multi-agent request' })
    const activityToggle = completedTurn.locator('.agent-index button').first()
    const agentOne = completedTurn.getByRole('link', { name: 'Agent One' })

    await expect(activityToggle).toHaveAttribute('aria-expanded', 'false')
    await expect(agentOne).toBeHidden()

    await activityToggle.click()
    await expect(activityToggle).toHaveAttribute('aria-expanded', 'true')
    await expect(agentOne).toBeVisible()

    await activityToggle.click()
    await expect(activityToggle).toHaveAttribute('aria-expanded', 'false')
    await expect(agentOne).toBeHidden()
  })
})
