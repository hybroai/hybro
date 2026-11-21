'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getTokenRequirement,
  listClients,
  setTokenRequirement,
  type ClientList,
  type TokenRequirement,
} from '@/lib/api/clients'

export const clientsQueryKey = ['clients'] as const
export const tokenRequirementQueryKey = ['clients', 'token-requirement'] as const

export function useClients(enabled = true) {
  return useQuery<ClientList>({
    queryKey: clientsQueryKey,
    queryFn: ({ signal }) => listClients(signal),
    enabled,
  })
}

/** The owner's switch; the Connect banner, the Overview notice and the Clients tab share it. */
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
