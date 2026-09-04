import { test, expect } from './fixtures/auth'

test.describe('Authenticated flows', () => {
  test('send message and see agent response', async ({ roomPath, page }) => {
    await page.goto(roomPath)

    const input = page.locator('[data-testid="chat-input"]')
    await input.waitFor({ timeout: 15_000 })

    const bubblesBefore = await page.locator('.agent-message').count()

    await input.fill('Hello, test message')
    await input.press('Enter')

    await expect(page.locator('.agent-message')).toHaveCount(bubblesBefore + 1, {
      timeout: 30_000,
    })
  })

  test('cancel in-flight message', async ({ roomPath, page }) => {
    await page.goto(roomPath)

    const input = page.locator('[data-testid="chat-input"]')
    await input.waitFor({ timeout: 15_000 })

    const canceledBefore = await page.getByText('Request stopped', { exact: true }).count()

    await input.fill('Write a long essay about quantum computing')
    await input.press('Enter')

    const stopButton = page.locator('[data-testid="stop-processing"]')
    await stopButton.waitFor({ state: 'visible', timeout: 10_000 })
    await stopButton.click()

    await expect(stopButton).toBeHidden({ timeout: 10_000 })
    await expect(page.getByText('Request stopped', { exact: true })).toHaveCount(canceledBefore + 1, {
      timeout: 10_000,
    })
  })

  test('HITL reply flow', async ({ roomPath, page }) => {
    test.skip(true, 'Requires backend agent that triggers input_required — run manually')
    await page.goto(roomPath)

    const input = page.locator('[data-testid="chat-input"]')
    await input.waitFor({ timeout: 15_000 })
    await input.fill('trigger input_required state')
    await input.press('Enter')

    const hitlInput = page.locator('[data-testid="hitl-reply-input"]')
    await hitlInput.waitFor({ timeout: 30_000 })
    await hitlInput.fill('HITL reply content')
    await hitlInput.press('Enter')

    await expect(page.locator('[data-testid="hitl-reply-input"]')).toHaveCount(0, {
      timeout: 15_000,
    })
  })

  test('file attachment upload and send', async ({ roomPath, page }) => {
    await page.goto(roomPath)

    const fileName = `test-upload-${Date.now()}.txt`

    const input = page.locator('[data-testid="chat-input"]')
    await input.waitFor({ timeout: 15_000 })

    const attachTrigger = page.locator('button[title="Add attachments"]')
    await attachTrigger.click()

    const menuItem = page.locator('text=Add photos and files')
    await menuItem.click()

    const fileInput = page.locator('input[type="file"]')
    await fileInput.setInputFiles({
      name: fileName,
      mimeType: 'text/plain',
      buffer: Buffer.from('Test file content for upload'),
    })

    const preview = page.locator('[data-testid="attachment-preview"]')
    await expect(preview).toBeVisible({ timeout: 10_000 })
    await expect(page.locator(`text=${fileName}`)).toBeVisible()

    await input.fill('See attached file')
    await input.press('Enter')

    await expect(preview).toBeHidden({ timeout: 10_000 })

    const uploadedLink = page.locator(`a:has-text("${fileName}")`)
    await expect(uploadedLink).toHaveAttribute('href', /^(?!blob:).+/, {
      timeout: 15_000,
    })
  })
})
