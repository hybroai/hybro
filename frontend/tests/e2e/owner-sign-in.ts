// Signs the e2e run in as the owner once, before any spec. The owner account
// must exist on the Hybro under test; CI creates it with `hybro owner create`.
// The browser pages and the API request fixture both start from the saved
// cookie. Without the two variables the run starts signed out, and only the
// pages that need no account can pass.
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { request } from '@playwright/test'

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000/api/v1'
export const OWNER_STATE = '.playwright/owner-state.json'
const EMAIL_ENV = 'HYBRO_TEST_OWNER_EMAIL'
const PASSWORD_ENV = 'HYBRO_TEST_OWNER_PASSWORD'

// The cross-site check wants Hybro's own address on a request that carries the
// sign-in cookie; the API request fixture calls the API's address directly.
export function apiOrigin(): string {
  return new URL(API_BASE_URL).origin
}

export default async function signInAsOwner(): Promise<void> {
  await mkdir(dirname(OWNER_STATE), { recursive: true })
  const email = process.env[EMAIL_ENV]
  const password = process.env[PASSWORD_ENV]
  if (!email || !password) {
    console.warn(`e2e: ${EMAIL_ENV} and ${PASSWORD_ENV} are not set; the run starts signed out.`)
    await writeFile(OWNER_STATE, JSON.stringify({ cookies: [], origins: [] }))
    return
  }
  const context = await request.newContext({ extraHTTPHeaders: { Origin: apiOrigin() } })
  try {
    const response = await context.post(`${API_BASE_URL}/auth/sign-in`, { data: { email, password } })
    if (!response.ok()) throw new Error(`e2e: signing in as the owner failed (HTTP ${response.status()})`)
    await context.storageState({ path: OWNER_STATE })
  } finally {
    await context.dispose()
  }
}
