import { test as base, expect } from '@playwright/test'
import { apiOrigin } from '../owner-sign-in'

export const test = base.extend<{ roomPath: string }>({
  // API calls carry the owner's cookie from the saved state, so they also send
  // the Origin the cross-site check expects. Client API calls send the client
  // token from HYBRO_TEST_CLIENT_TOKEN; the owner's routes ignore it.
  request: [
    async ({ playwright, storageState }, use) => {
      const token = process.env.HYBRO_TEST_CLIENT_TOKEN
      const context = await playwright.request.newContext({
        storageState,
        extraHTTPHeaders: { Origin: apiOrigin(), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
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
