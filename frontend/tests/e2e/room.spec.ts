import { test, expect } from '@playwright/test'

async function waitForRoomLoad(page: import('@playwright/test').Page, timeoutMs = 20000) {
  await page.waitForFunction(
    () => {
      const text = document.body.innerText
      return !text.includes('Loading room')
    },
    { timeout: timeoutMs },
  )
}

test.describe('Room for the signed-in owner', () => {
  test('keeps the room route and does not redirect to sign-in', async ({ page }) => {
    await page.goto('/room/test-room-id')

    await waitForRoomLoad(page)

    await expect(page).toHaveURL(/\/room\/test-room-id$/)
    await expect(page).not.toHaveURL(/sign-in/)
    await expect(page.getByText('Room not found', { exact: true })).toBeVisible()
  })

  test('room page should not throw JS errors', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    await page.goto('/room/test-room-id')

    await waitForRoomLoad(page)

    expect(errors).toHaveLength(0)
  })
})

test.describe('Agent Selection', () => {
  test('should display the network page with title', async ({ page }) => {
    await page.goto('/network')
    await expect(page).toHaveTitle(/Hybro/i)
    await expect(page.locator('body')).toBeVisible()
  })
})

test.describe('Unknown room subpath', () => {
  test('returns a 404 instead of treating the subpath as a settings route', async ({ request }) => {
    const response = await request.get('/room/test-room/settings')
    expect(response.status()).toBe(404)
  })
})
