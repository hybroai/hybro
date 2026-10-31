'use client'

import { useQuery } from '@tanstack/react-query'
import { getSetupStatus } from '@/lib/api/setup'
import { setupNotice, type SetupNotice } from '@/lib/setup-notice'

export const setupStatusQueryKey = ['setup', 'status'] as const

// The backend picks up saves from another instance and manual edits, so the
// status polls to keep banners and composer gating current.
export const SETUP_STATUS_POLL_MS = 30_000

export function useSetupStatus() {
  return useQuery({
    queryKey: setupStatusQueryKey,
    queryFn: ({ signal }) => getSetupStatus(signal),
    staleTime: 10_000,
    // A failed read (setup unavailable, or signed out) stops the polling.
    refetchInterval: query => (query.state.status === 'error' ? false : SETUP_STATUS_POLL_MS),
    refetchOnWindowFocus: true,
    retry: false,
  })
}

/** The current notice; null while loading or when the status cannot be read. */
export function useSetupNotice(): SetupNotice | null {
  return setupNotice(useSetupStatus().data)
}
