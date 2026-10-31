import { expect, test, type Page, type Route } from '@playwright/test'

// Every setup route is mocked in the browser; nothing reaches a backend or a
// model provider.
type Status = Record<string, unknown>

const UNCONFIGURED: Status = { state: 'unconfigured', image_size: '1024x1024', last_error: null }

async function mockSetup(page: Page, initial: Status) {
  let status = initial
  const posted: { path: string; url: string; body: unknown }[] = []
  const json = (route: Route, body: unknown, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })

  await page.route('**/setup/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace(/^.*\/setup\//, '')
    const body = request.postData() ? request.postDataJSON() : null
    if (request.method() !== 'GET') posted.push({ path, url: request.url(), body })

    if (path === 'status') return json(route, status)
    if (path === 'models') {
      const choice = `${url.searchParams.get('provider')}/${url.searchParams.get('auth')}`
      const models: Record<string, { text: string[]; image: string[] }> = {
        'openai/api_key': { text: ['gpt-5-mini', 'gpt-4o-mini'], image: ['gpt-image-1'] },
        'openai/oauth': { text: ['gpt-6.1-sol', 'gpt-6-astra'], image: [] },
        'deepseek/api_key': { text: ['deepseek-v4-flash'], image: [] },
        'anthropic/api_key': { text: ['claude-haiku-4-5-20251001'], image: [] },
      }
      return json(route, models[choice])
    }
    if (path === 'api-key') {
      const request = body as { provider: string; text_model: string; image_model: string | null }
      status = { state: 'configured', provider: request.provider, auth: 'api_key', text_model: request.text_model, image_model: request.image_model, image_size: '1024x1024', last_error: null }
      return json(route, { changed: true, status })
    }
    if (path === 'oauth/start') {
      return json(route, { login_id: 'login-1', state: 'pending', authorization_url: 'https://auth.example.test/oauth/authorize?state=fixture', listener: true, expires_at: 300 })
    }
    if (path === 'oauth/complete') {
      return json(route, { login_id: 'login-1', state: 'authenticated', listener: true, expires_at: 300 })
    }
    if (path === 'oauth/login-1') {
      if (request.method() === 'DELETE') return route.fulfill({ status: 204 })
      return json(route, { login_id: 'login-1', state: 'pending', authorization_url: 'https://auth.example.test/oauth/authorize?state=fixture', listener: true, expires_at: 300 })
    }
    if (path === 'oauth/models') {
      const request = body as { text_model: string; image_model: string | null }
      status = { state: 'configured', provider: 'openai', auth: 'oauth', text_model: request.text_model, image_model: request.image_model, image_size: '1024x1024', last_error: null }
      return json(route, { changed: true, status })
    }
    if (path === 'image-size') {
      const request = body as { image_size: string }
      status = { ...status, image_size: request.image_size }
      return json(route, { image_size: request.image_size, restart_required: true })
    }
    return json(route, { detail: 'Not mocked.', code: 'invalid_request' }, 404)
  })
  return posted
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`banner opens Model provider settings and an API key save clears it at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    const posted = await mockSetup(page, UNCONFIGURED)
    await page.goto('/')

    const banner = page.getByTestId('setup-banner')
    await expect(banner).toContainText('No model provider is set up.')
    await banner.getByRole('link', { name: 'Open model provider settings' }).click()
    await expect(page).toHaveURL(/\?settings=model-provider$/)

    const dialog = page.getByRole('dialog', { name: 'Settings' })
    await expect(dialog.getByText('Model provider', { exact: true })).toBeVisible()
    await expect(dialog.getByTestId('model-provider-status')).toContainText('Not set up')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`model-provider-${viewport.width}.png`), fullPage: true })

    await dialog.getByRole('radio', { name: 'DeepSeek' }).click()
    await expect(dialog.getByLabel('Text model')).toHaveValue('deepseek-v4-flash')
    await expect(dialog.getByLabel('Image model')).toHaveCount(0)
    await dialog.getByLabel('DeepSeek API key').fill('sk-e2e-fixture')
    await dialog.getByRole('button', { name: 'Verify and save' }).click()

    await expect(dialog.getByTestId('model-provider-status')).toContainText('Set up — DeepSeek · API key · deepseek-v4-flash')
    await expect(dialog.getByLabel('DeepSeek API key')).toHaveValue('')
    const save = posted.find(entry => entry.path === 'api-key')!
    expect(save.body).toEqual({ provider: 'deepseek', api_key: 'sk-e2e-fixture', text_model: 'deepseek-v4-flash', image_model: null })
    expect(save.url).not.toContain('sk-e2e-fixture')

    await page.keyboard.press('Escape')
    await expect(page).not.toHaveURL(/settings=/)
    await expect(banner).toHaveCount(0)
  })
}

test('ChatGPT sign-in offers the paste fallback even with a listener, then saves models', async ({ page }) => {
  const posted = await mockSetup(page, UNCONFIGURED)
  await page.goto('/?settings=model-provider')
  const dialog = page.getByRole('dialog', { name: 'Settings' })

  await dialog.getByRole('radio', { name: 'ChatGPT sign-in' }).click()
  await dialog.getByRole('button', { name: 'Sign in with ChatGPT' }).click()
  await expect(dialog.getByTestId('oauth-pending')).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Open the sign-in page' })).toHaveAttribute('href', /^https:\/\/auth\.example\.test\//)
  await dialog.getByLabel('Address from the localhost:1455 page').fill('http://localhost:1455/auth/callback?code=fixture&state=fixture')
  await dialog.getByRole('button', { name: 'Finish sign-in' }).click()

  await expect(dialog.getByTestId('oauth-authenticated')).toBeVisible()
  await expect(dialog.getByLabel('Text model')).toHaveValue('gpt-6.1-sol')
  await dialog.getByLabel('Text model').selectOption('gpt-6-astra')
  await dialog.getByRole('button', { name: 'Verify and save' }).click()

  await expect(dialog.getByTestId('model-provider-status')).toContainText('Set up — OpenAI · ChatGPT sign-in · gpt-6-astra')
  expect(posted.find(entry => entry.path === 'oauth/complete')!.body).toEqual({ login_id: 'login-1', redirect_url: 'http://localhost:1455/auth/callback?code=fixture&state=fixture' })
  expect(posted.find(entry => entry.path === 'oauth/models')!.body).toEqual({ login_id: 'login-1', text_model: 'gpt-6-astra', image_model: null })

  // With a saved sign-in, models change without a new login.
  await expect(dialog.getByRole('button', { name: 'Sign in again' })).toBeVisible()
  await dialog.getByLabel('Image size').selectOption('auto')
  await dialog.getByRole('button', { name: 'Save image size' }).click()
  await expect(page.getByText('Image size saved. Restart Hybro to apply it.')).toBeVisible()
  expect(posted.find(entry => entry.path === 'image-size')!.body).toEqual({ image_size: 'auto' })
})

test('Room Chat keeps a permanent banner and a disabled composer while unconfigured', async ({ page }) => {
  await mockSetup(page, UNCONFIGURED)
  await page.goto('/chat')
  const banner = page.getByTestId('setup-banner')
  await expect(banner).toContainText('No model provider is set up.')
  await expect(banner.getByRole('button', { name: 'Dismiss' })).toHaveCount(0)
  await expect(page.locator('[contenteditable="false"]').first()).toBeVisible()
  await banner.getByRole('link', { name: 'Open model provider settings' }).click()
  await expect(page).toHaveURL(/\/chat\?settings=model-provider$/)
  await expect(page.getByRole('dialog', { name: 'Settings' })).toBeVisible()
})
