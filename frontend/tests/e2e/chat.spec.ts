import { test, expect } from '@playwright/test'

test.describe('Chat Flow', () => {
  test('should display the main page', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveTitle(/Hybro/i)
  })

  test('should have responsive layout on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await page.goto('/')
    await expect(page.locator('body')).toBeVisible()
  })

  test('should have responsive layout on desktop', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.goto('/')
    await expect(page.locator('body')).toBeVisible()
  })
})

test.describe('Chat Page for the signed-in owner', () => {
  test('should render chat page with quick-start templates', async ({ page }) => {
    await page.goto('/chat')

    // Wait for the local auth adapter to load (the spinner disappears).
    await page.waitForFunction(
      () => !document.querySelector('.animate-spin'),
      { timeout: 15000 },
    )

    // Quick-start templates should be visible
    const templates = page.locator('text=Travel Plan')
    await expect(templates.first()).toBeVisible({ timeout: 5000 })
  })

  test('submitting creates a room and navigates without a sign-in redirect', async ({ page }) => {
    await page.route('**/api/v1/rooms', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          room: { conversation_id: 'e2e-created-room' },
        }),
      })
    })
    await page.goto('/chat')

    await page.waitForFunction(
      () => !document.querySelector('.animate-spin'),
      { timeout: 15000 },
    )

    // The chat page uses a contenteditable div, not a textarea.
    const chatInput = page.locator('[contenteditable="true"]')
    await expect(chatInput).toBeVisible({ timeout: 5000 })

    // Type a message and submit
    await chatInput.click()
    await page.keyboard.type('Test message from local user')
    await page.keyboard.press('Enter')

    await expect(page).toHaveURL(/\/room\/e2e-created-room$/, { timeout: 10000 })
    await expect(page).not.toHaveURL(/sign-in/)
  })
})

test.describe('Page Content', () => {
  test('should render about page with main content area', async ({ page }) => {
    await page.goto('/about')

    // The portal layout renders a single <main>: SidebarInset.
    const contentMain = page.locator('main[data-slot="sidebar-inset"]')
    await expect(contentMain).toBeVisible()
  })
})

test.describe('Theme Toggle', () => {
  test('should toggle between light and dark themes', async ({ page }) => {
    // The sidebar (and theme toggle) only exists on /* routes, not the marketing homepage.
    await page.goto('/chat')

    // ThemeToggle renders in two places depending on auth state:
    //   - Unauthenticated: small button in the sidebar footer (always visible)
    //   - Authenticated: inside the user dropdown menu (must be opened first)
    // Wait for the owner's session so the sidebar finishes rendering.
    const authReady = await page.waitForFunction(
      () => {
        const sidebar = document.querySelector('[data-slot="sidebar"]')
        if (!sidebar) return false
        const skeletons = sidebar.querySelectorAll('.animate-pulse')
        return skeletons.length === 0
      },
      { timeout: 15000 },
    ).then(() => true).catch(() => false)

    if (!authReady) {
      test.fail(true, 'Local identity did not render within 15s')
      return
    }

    // Dismiss the cookie consent banner if present — it overlays the sidebar
    // footer and blocks pointer events on the theme toggle button.
    const declineBtn = page.getByRole('button', { name: 'Decline' })
    if (await declineBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await declineBtn.click()
    }

    const htmlElement = page.locator('html')
    const initialClass = await htmlElement.getAttribute('class')

    // Try the unauthenticated path: standalone toggle button in sidebar footer
    const themeToggle = page.getByRole('button', { name: 'Toggle theme' })
    const isDirectVisible = await themeToggle.isVisible({ timeout: 2000 }).catch(() => false)

    if (isDirectVisible) {
      await themeToggle.scrollIntoViewIfNeeded()
      await themeToggle.click()
    } else {
      // Authenticated path: open user dropdown, navigate to Theme submenu
      const userMenuTrigger = page.locator('[data-slot="sidebar"] button[title]').first()
      await userMenuTrigger.click()
      const themeMenuItem = page.getByRole('menuitem', { name: /theme/i })
      await expect(themeMenuItem).toBeVisible({ timeout: 5000 })
      await themeMenuItem.click()
      // Click one of the explicit theme options (Light or Dark)
      const htmlClass = await htmlElement.getAttribute('class')
      const isDark = htmlClass?.includes('dark')
      const targetOption = page.getByRole('menuitemradio', { name: isDark ? 'Light' : 'Dark' })
      await expect(targetOption).toBeVisible({ timeout: 3000 })
      await targetOption.click()
    }

    await page.waitForTimeout(500)

    const newClass = await htmlElement.getAttribute('class')
    expect(newClass).not.toBe(initialClass)
  })
})
