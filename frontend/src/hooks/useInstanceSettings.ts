'use client'

import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { getSettings, restartServices, settingsErrorMessage, type RestartState, type SettingsView } from '@/lib/api/settings'

export const instanceSettingsQueryKey = ['settings', 'instance'] as const

/** While a restart runs, the page polls until the services report back. */
export const RESTART_POLL_MS = 2000

const RUNNING: readonly RestartState[] = ['requested', 'restarting']

export function isRestarting(view: SettingsView | undefined): boolean {
  return view !== undefined && RUNNING.includes(view.restart.state)
}

export function useInstanceSettings() {
  return useQuery({
    queryKey: instanceSettingsQueryKey,
    queryFn: ({ signal }) => getSettings(signal),
    // Requests fail while the backend restarts; the last view stays and polling continues.
    refetchInterval: query => (isRestarting(query.state.data) ? RESTART_POLL_MS : false),
    refetchOnWindowFocus: true,
    retry: false,
  })
}

const restartMutationKey = ['settings', 'restart'] as const

/** True while any restart request on the page waits for its answer (the backend checks the configuration first). */
export function useRestartStarting(): boolean {
  return useIsMutating({ mutationKey: restartMutationKey }) > 0
}

/** Asks for an in-place restart; the settings view then reports its progress. */
export function useRestartServices() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: restartMutationKey,
    mutationFn: restartServices,
    // A view fetched before the restart was accepted must not overwrite the requested state.
    onMutate: () => queryClient.cancelQueries({ queryKey: instanceSettingsQueryKey }),
    onSuccess: async ({ restart }) => {
      await queryClient.cancelQueries({ queryKey: instanceSettingsQueryKey })
      queryClient.setQueryData<SettingsView>(instanceSettingsQueryKey, view => (view ? { ...view, restart } : view))
    },
    onError: error => {
      toast.error(settingsErrorMessage(error))
      // A refusal can mean another tab already restarts Hybro; the fresh view then shows and polls it.
      void queryClient.invalidateQueries({ queryKey: instanceSettingsQueryKey })
    },
  })
}
