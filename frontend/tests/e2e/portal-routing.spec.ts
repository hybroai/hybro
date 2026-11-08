import { expect, test } from '@playwright/test'

test.describe('Unified portal routing', () => {
  test('serves canonical routes and redirects legacy management paths', async ({ page, request }) => {
    for (const path of [
      '/',
      '/about',
      '/pricing',
      '/chat',
      '/network',
      '/network/agents/new',
      '/connect/mcp',
      '/connect/api',
      '/agents',
      '/agents/new',
      '/networks',
    ]) {
      const response = await request.get(path)
      expect(response.status(), `${path} should resolve`).toBe(200)
    }

    await page.goto('/manage')
    await expect(page).toHaveURL(/\/network$/)
  })

  for (const [from, to] of [
    ['/agents', /\/network$/],
    ['/agents/new', /\/network\/agents\/new$/],
    ['/networks', /\/network$/],
    ['/manage/agents/new', /\/network\/agents\/new$/],
    ['/access/api', /\/connect\/api$/],
  ] as const) {
    test(`redirects ${from}`, async ({ page }) => {
      await page.goto(from)
      await expect(page).toHaveURL(to)
    })
  }

  test('opens the Overview at the root', async ({ page }) => {
    await page.route('**/agent/getAllAgents**', route => route.fulfill({ json: { success: true, agents: [] } }))
    await page.route('**/agent/getAgent/me', route => route.fulfill({ json: { success: true, agents: [] } }))
    await page.route('**/agentGroups**', route => route.fulfill({ json: { success: true, groups: [] } }))
    await page.route('**/api/v1/rooms', route => route.request().method() === 'GET' ? route.fulfill({ json: { items: [] } }) : route.fallback())
    await page.goto('/')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeAttached()
    await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Recent chats' })).toBeVisible()
  })

  test('does not retain retired routes', async ({ request }) => {
    for (const path of [
      '/c',
      '/c/chat',
      '/core',
      '/d',
      '/d/agents',
      '/hub',
      '/manage/api-keys',
      '/manage/inspector',
    ]) {
      const response = await request.get(path)
      expect(response.status(), `${path} should be retired`).toBe(404)
    }
  })

  for (const width of [1440, 390]) {
    test(`uses the original shared menu on marketing and feature pages at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.route('**/agent/getAllAgents**', route => route.fulfill({ json: { success: true, agents: [] } }))
      await page.route('**/agent/getAgent/me', route => route.fulfill({ json: { success: true, agents: [] } }))
      await page.route('**/agentGroups**', route => route.fulfill({ json: { success: true, groups: [] } }))
      await page.route('**/api/v1/rooms', route => route.request().method() === 'GET' ? route.fulfill({ json: { items: [{
        conversation_id: 'navigation-history',
        title: 'Navigation history',
        last_activity_at: '2026-08-01T00:00:00Z',
        is_pinned: false,
        pin_order: null,
        status: 'idle',
      }] } }) : route.fallback())

      const openNavigation = async () => {
        if (width < 768) await page.getByRole('button', { name: 'Toggle Sidebar', exact: true }).click()
      }
      const modules = page.getByRole('navigation', { name: 'Modules', exact: true })
      await page.goto('/about')
      await openNavigation()
      await expect(page.locator('[data-sidebar="sidebar"]')).toHaveCount(1)
      await expect(page.getByRole('region', { name: 'History', exact: true })).toHaveCount(0)
      for (const name of ['Overview', 'Network', 'Chats', 'Inbox', 'Access', 'Settings']) {
        await expect(modules.getByRole('link', { name: new RegExp(`^${name}`) })).toHaveCount(1)
      }
      if (width >= 768) {
        await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click()
      }
      await modules.getByRole('link', { name: /^Network/ }).click()
      await expect(page).toHaveURL(/\/network$/)
      if (width < 768) await expect(page.getByRole('dialog')).toHaveCount(0)

      await openNavigation()
      await modules.getByRole('link', { name: /^Access/ }).click()
      await expect(page).toHaveURL(/\/connect\/mcp$/)
      await openNavigation()
      await expect(page.locator('[data-sidebar="sidebar"]')).toHaveCount(1)
      await expect(page.getByRole('region', { name: 'History', exact: true })).toHaveCount(0)
      await modules.getByRole('link', { name: /^Chats/ }).click()
      await expect(page).toHaveURL(/\/chat$/)
      await openNavigation()
      const history = page.getByRole('region', { name: 'History', exact: true })
      await expect(history).toBeVisible()
      if (width >= 768) {
        const primary = await page.locator('[data-sidebar="sidebar"]').boundingBox()
        const secondary = await page.getByRole('complementary', { name: 'Chat history', exact: true }).boundingBox()
        expect(secondary!.x).toBeGreaterThanOrEqual(primary!.x + primary!.width)
        // The primary sidebar is still collapsed from above; neither state hides the History column.
        await page.getByRole('button', { name: 'Expand sidebar', exact: true }).click()
        await expect(history).toBeVisible()
        await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click()
        await expect(history).toBeVisible()
      }
      const historyTrigger = history.getByRole('button', { name: 'History', exact: true })
      await historyTrigger.click()
      await expect(historyTrigger).toHaveAttribute('aria-expanded', 'false')
      await expect(history).toBeVisible()
      await historyTrigger.click()
      await expect(historyTrigger).toHaveAttribute('aria-expanded', 'true')
      if (width >= 768) {
        const column = page.getByRole('complementary', { name: 'Chat history' })
        const home = column.getByRole('link', { name: 'New chat', exact: true })
        const homeBox = await home.boundingBox()
        const historyBox = await historyTrigger.boundingBox()
        expect(homeBox!.y).toBeLessThan(historyBox!.y)
        await page.getByRole('link', { name: 'Navigation history', exact: true }).click()
        await expect(page).toHaveURL(/\/room\/navigation-history$/)
        await home.click()
        await expect(page).toHaveURL(/\/chat$/)
      }
      await expect(page.getByRole('link', { name: 'Navigation history', exact: true })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    })
  }
})
