import { expect, test } from '@playwright/test'

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`Access page stays readable and copyable at ${viewport.width}px`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.setViewportSize(viewport)
    await page.goto('/connect/mcp')
    await expect(page.getByRole('heading', { level: 1, name: 'Access' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'MCP' })).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('button', { name: 'Copy address' }).click()
    await expect(page.getByText('Address copied', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('http://127.0.0.1:8001/mcp')

    await expect(page.getByLabel('Claude Code command', { exact: true })).toContainText("claude mcp add --transport http hybro http://127.0.0.1:8001/mcp --header 'Authorization: Bearer <client token>'")
    await page.getByRole('tab', { name: 'Other' }).click()
    await page.getByRole('button', { name: 'Copy configuration' }).click()
    await expect(page.getByText('Configuration copied', { exact: true })).toBeVisible()
    expect(JSON.parse(await page.evaluate(() => navigator.clipboard.readText()))).toEqual({
      mcpServers: { hybro: { type: 'http', url: 'http://127.0.0.1:8001/mcp', headers: { Authorization: 'Bearer <client token>' } } }
    })
    await expect(page.getByRole('heading', { name: 'Features' })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`mcp-${viewport.width}.png`), fullPage: true })

    await page.getByRole('tab', { name: 'API' }).click()
    await expect(page).toHaveURL(/\/connect\/api$/)
    await page.getByRole('button', { name: 'Copy discovery request' }).click()
    await expect(page.getByText('Discovery request copied', { exact: true })).toBeVisible()
    const request = await page.evaluate(() => navigator.clipboard.readText())
    expect(request).toContain('/agents/discovery')
    // Only the placeholder, never a real token.
    expect(request).toContain("--header 'Authorization: Bearer <client token>'")
    expect(request).not.toMatch(/hybro_client_|access_token|group_id/i)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`api-${viewport.width}.png`), fullPage: true })
  })
}
