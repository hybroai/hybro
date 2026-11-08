import { expect, test, type Page, type Route } from '@playwright/test'
import { FIELDS, settingsView } from '../fixtures/instance-settings'
import type { SettingsView } from '../../src/lib/api/settings'

// The settings and setup routes are mocked in the browser; nothing reaches a backend.
async function mockSettings(page: Page) {
  let view: SettingsView = settingsView()
  let polls = 0
  const puts: unknown[] = []
  const json = (route: Route, body: unknown, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })

  await page.route(url => url.pathname.endsWith('/setup/status'), route => json(route, { state: 'configured', provider: 'deepseek', auth: 'api_key', text_model: 'deepseek-v4-flash', image_model: null, image_size: '1024x1024', last_error: null }))
  await page.route(url => /\/api\/v1\/settings(\/restart)?$/.test(url.pathname), async route => {
    const request = route.request()
    if (request.method() === 'PUT') {
      const body = request.postDataJSON() as { values: Record<string, never> }
      puts.push(body)
      view = settingsView({ revision: 'rev-2', restart_pending: true }, FIELDS.map(f => (
        f.key in body.values ? { ...f, value: body.values[f.key], source: 'config', restart_pending: f.apply === 'restart' } : f
      )))
      return json(route, { changed: Object.keys(body.values), restart_required: true, settings: view })
    }
    if (request.method() === 'POST') {
      view = { ...view, restart: { available: true, state: 'requested', error: null, updated_at: 1 } }
      return json(route, { restart: view.restart }, 202)
    }
    // The backend is down for two polls, then reports the finished restart.
    if (view.restart.state === 'requested') {
      polls += 1
      if (polls <= 2) return route.abort('connectionrefused')
      view = settingsView({ revision: 'rev-3', restart: { available: true, state: 'completed', error: null, updated_at: 2 } }, view.fields.map(f => ({ ...f, restart_pending: false })))
    }
    return json(route, view)
  })
  return { puts }
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`configuration saves and restarts in place at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    const { puts } = await mockSettings(page)
    await page.goto('/settings/configuration')

    const tabs = page.getByRole('navigation', { name: 'Settings sections' })
    await expect(tabs.getByRole('link', { name: 'Configuration' })).toHaveAttribute('aria-current', 'page')
    const interval = page.getByRole('textbox', { name: 'Health check interval (seconds)' })
    await expect(interval).toHaveValue('1800')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`configuration-${viewport.width}.png`), fullPage: true })

    await interval.fill('600')
    const bar = page.getByRole('region', { name: 'Unsaved changes' })
    await expect(bar).toContainText('1 unsaved change')
    await expect(bar).toBeInViewport()
    await bar.getByRole('button', { name: 'Save changes' }).click()

    const dialog = page.getByRole('dialog', { name: 'Save 1 change?' })
    await expect(dialog).toContainText('1800 → 600')
    await page.screenshot({ path: testInfo.outputPath(`configuration-dialog-${viewport.width}.png`) })
    await dialog.getByRole('button', { name: 'Save & restart' }).click()

    await expect(page.getByText('Restarting Hybro…')).toBeVisible()
    await expect(interval).toBeDisabled()
    await expect(page.getByText('Hybro restarted. All saved settings are in effect.')).toBeVisible({ timeout: 15_000 })
    await expect(interval).toBeEnabled()
    await expect(interval).toHaveValue('600')
    expect(puts).toEqual([{ revision: 'rev-1', values: { 'backend.agent_health_check_interval': 600 } }])
  })
}

test('system shows how the instance runs', async ({ page }) => {
  await mockSettings(page)
  await page.goto('/settings/system')
  await expect(page.getByText('Hybro 0.4.7')).toBeVisible()
  await expect(page.getByText('Docker image')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Restart Hybro' })).toBeVisible()
})

test('settings opens on the model provider', async ({ page }) => {
  await mockSettings(page)
  await page.goto('/settings')
  await expect(page).toHaveURL(/\/settings\/model-provider$/)
  await expect(page.getByRole('region', { name: 'Model provider' })).toBeVisible()
})
