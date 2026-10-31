import { vi } from 'vitest'

/**
 * Lets MSW answer requests from component tests. Node's fetch rejects the
 * AbortSignal that jsdom provides, so the request never reaches MSW. The stub
 * drops the signal; undo it with `vi.unstubAllGlobals()`.
 */
export function stubFetchWithoutSignal(): void {
  const realFetch = globalThis.fetch
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => realFetch(input, { ...init, signal: undefined }))
}
