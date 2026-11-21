'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getTokenRequirement,
  listSources,
  setTokenRequirement,
  type SourceList,
  type TokenRequirement,
} from '@/lib/api/source'

export const sourcesQueryKey = ['sources'] as const
export const tokenRequirementQueryKey = ['sources', 'token-requirement'] as const

export function useSources(enabled = true) {
  return useQuery<SourceList>({
    queryKey: sourcesQueryKey,
    queryFn: ({ signal }) => listSources(signal),
    enabled,
  })
}

/** The owner's switch; the Connect banner, the Overview notice and the Sources tab share it. */
export function useTokenRequirement(enabled = true) {
  return useQuery<TokenRequirement>({
    queryKey: tokenRequirementQueryKey,
    queryFn: ({ signal }) => getTokenRequirement(signal),
    enabled,
    staleTime: 30_000,
  })
}

export function useSetTokenRequirement() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (required: boolean) => setTokenRequirement(required),
    onSuccess: requirement => queryClient.setQueryData(tokenRequirementQueryKey, requirement),
  })
}
