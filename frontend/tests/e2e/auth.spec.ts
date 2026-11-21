import { test, expect } from '@playwright/test'

// The run is signed in as the owner (tests/e2e/owner-sign-in.ts).
test.describe('Owner sign-in', () => {
  test('keeps the signed-in owner on room routes', async ({ page }) => {
    await page.goto('/room/test-room')

    await expect(page).toHaveURL(/\/room\/test-room$/)
    await expect(page.getByText('Room not found', { exact: true })).toBeVisible({ timeout: 20000 })
    await expect(page).not.toHaveURL(/sign-in/)
  })
})

test.describe('Signed out', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  test('a portal page sends the browser to sign-in and back', async ({ page }) => {
    await page.goto('/chat')

    await expect(page).toHaveURL(/\/sign-in\?redirect_url=%2Fchat$/, { timeout: 20000 })
    await expect(page.getByLabel('Email')).toBeVisible()
    await expect(page.getByLabel('Password')).toBeVisible()
  })

  test('there is no sign-up page', async ({ page }) => {
    const response = await page.goto('/sign-up')

    expect(response?.status()).toBe(404)
  })
})

test.describe('Public Pages', () => {
  test('should load the about page', async ({ page }) => {
    await page.goto('/about')
    await expect(page.getByRole('heading', { name: /One network. Every agent./i })).toBeVisible()
  })

  test('should load the pricing page', async ({ page }) => {
    await page.goto('/pricing')
    await expect(page.getByRole('heading', { name: /Simple, Transparent Pricing Plans/i })).toBeVisible()
  })

  test('should load the network page', async ({ page }) => {
    await page.goto('/network')
    await expect(page.getByRole('heading', { name: 'Network' })).toBeVisible()
  })
})

test.describe('Navigation', () => {
  test('should navigate to the network page via sidebar', async ({ page }) => {
    // The sidebar only exists on /* routes — the root is a marketing landing page.
    await page.goto('/chat')

    // Wait for the owner's session so the sidebar fully renders.
    await page.waitForFunction(
      () => {
        const sidebar = document.querySelector('[data-slot="sidebar"]')
        if (!sidebar) return false
        return sidebar.querySelectorAll('.animate-pulse').length === 0
      },
      { timeout: 15000 },
    )

    // Match the canonical Network entry in the sidebar.
    const sidebar = page.locator('[data-slot="sidebar"]')
    const networkLink = sidebar.getByRole('link', { name: /^Network/ })
    await expect(sidebar.getByRole('button', { name: 'Manage' })).toHaveCount(0)
    await expect(networkLink).toBeVisible({ timeout: 5000 })
    await networkLink.click()
    await expect(page).toHaveURL(/\/network$/)
  })
})

test.describe('Sidebar owner identity', () => {
  test('renders the signed-in owner control and no sign-in action', async ({ page }) => {
    await page.goto('/chat')

    await expect(page.getByTestId('sidebar-sign-in')).toHaveCount(0)
    await expect(page).not.toHaveURL(/sign-in/)
  })
})

test.describe('Agent registration', () => {
  test('register page loads for the signed-in owner', async ({ page }) => {
    await page.goto('/network/agents/new')

    await page.waitForFunction(
      () => !document.querySelector('.animate-spin'),
      { timeout: 15000 },
    )

    // The page heading should be visible — confirms the page loaded correctly.
    await expect(page.getByRole('heading', { name: /register an agent/i })).toBeVisible({ timeout: 5000 })

    // The register button is only shown after a successful agent inspection.
    const registerBtn = page.getByRole('button', { name: /^register agent$/i })
    await expect(registerBtn).not.toBeVisible()
  })
})
