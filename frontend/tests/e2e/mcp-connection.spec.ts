import { expect, test } from '@playwright/test'

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`MCP connection stays readable and keyboard accessible at ${viewport.width}px`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.setViewportSize(viewport)
    await page.route('**/agentGroups**', route => route.fulfill({ json: { success: true, groups: [] } }))
    await page.route('**/getAllAgents**', route => route.fulfill({ json: { success: true, agents: [] } }))
    await page.route('**/agent/getAgent/me', route => route.fulfill({ json: { success: true, agents: [] } }))
    let status = 'ready'
    await page.route('**/hybro-mcp', route => route.fulfill({ json: { service: 'hybro-mcp', status } }))
    await page.goto('/networks')
    const trigger = page.getByRole('button', { name: 'Connect MCP' })
    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'MCP Connection' })
    await expect(dialog.getByText('Ready', { exact: true })).toBeVisible()
    await expect(dialog.getByText('http://127.0.0.1:8001/mcp', { exact: true })).toBeVisible()
    await expect(dialog.getByText('Configuration', { exact: true })).toBeVisible()
    await expect(dialog.getByText('For clients on the machine running Hybro.')).toHaveCount(0)
    await expect(dialog.getByText('Transport', { exact: true })).toHaveCount(0)
    await expect(dialog.getByText('Streamable HTTP')).toHaveCount(0)
    await expect(dialog.getByText('Claude Code configuration')).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Copy URL' }).click()
    await expect(dialog.getByText('URL copied')).toBeVisible()
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('http://127.0.0.1:8001/mcp')
    await dialog.getByRole('button', { name: 'Copy configuration' }).click()
    await expect(dialog.getByText('Configuration copied')).toBeVisible()
    expect(JSON.parse(await page.evaluate(() => navigator.clipboard.readText()))).toEqual({
      mcpServers: { hybro: { type: 'http', url: 'http://127.0.0.1:8001/mcp' } }
    })
    const bounds = await dialog.boundingBox()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width)
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height)
    await page.screenshot({ path: testInfo.outputPath(`mcp-${viewport.width}.png`) })
    status = 'unsupported_auth'
    await dialog.getByRole('button', { name: 'Refresh status' }).click()
    await expect(dialog.getByText('Unavailable with Clerk', { exact: true })).toBeVisible()
    await expect(dialog.getByText('Ready', { exact: true })).toHaveCount(0)
    status = 'unavailable'
    await dialog.getByRole('button', { name: 'Refresh status' }).click()
    await expect(dialog.getByText('Unavailable', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await expect(trigger).toBeFocused()
  })
}
