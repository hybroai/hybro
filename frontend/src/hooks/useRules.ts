'use client'

import { useQuery } from '@tanstack/react-query'
import { listRules, type RuleList } from '@/lib/api/rules'

export const rulesQueryKey = ['rules'] as const

/** The owner's check rules, in order. With `paused`, set while a rule write
 * runs, no focus or reconnect refetch can replace a change not yet saved. */
export function useRules({ paused = false }: { paused?: boolean } = {}) {
  return useQuery<RuleList>({
    queryKey: rulesQueryKey,
    queryFn: ({ signal }) => listRules(signal),
    refetchOnWindowFocus: !paused,
    refetchOnReconnect: !paused,
    // Each rule write waits for its reload; a failed read shows Retry at once.
    retry: false,
  })
}
