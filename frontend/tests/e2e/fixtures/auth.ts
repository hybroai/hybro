import { test as base, expect } from '@playwright/test'
import { apiOrigin } from '../owner-sign-in'

export const test = base.extend<{ roomPath: string }>({
  // API calls carry the owner's cookie from the saved state, so they also send
  // the Origin the cross-site check expects. Source API calls send no token.
  request: [
    async ({ playwright, storageState }, use) => {
      const context = await playwright.request.newContext({
        storageState,
        extraHTTPHeaders: { Origin: apiOrigin() },
      })
      await use(context)
      await context.dispose()
    },
    { scope: 'test' },
  ],
  roomPath: [
    async ({}, use, testInfo) => {
      const roomPath = process.env.E2E_TEST_ROOM_PATH
      if (!roomPath) {
        testInfo.skip(true, 'E2E_TEST_ROOM_PATH not set — skipping room-dependent test')
        return
      }
      await use(roomPath)
    },
    { auto: false },
  ],
})

export { expect }
