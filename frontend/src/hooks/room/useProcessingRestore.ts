import { useEffect } from 'react'
import type { ProcessingLifecycle } from './processing-lifecycle'
import { useMessageStore } from '@/stores/message-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { allAgentsTerminalForUserMessage } from '@/lib/room-timeline/turn-agent-terminal'
import { inquiryActiveRuns } from '@/lib/api/room'
import type { ActiveRunRefWire } from '@/lib/types/response'
import { isTerminalState } from '@/lib/types/sse'
import { isStale } from '@/lib/time'

interface ProcessingSnapshotRoom {
  active_runs?: ActiveRunRefWire[] | null
}

export function useProcessingRestore(
  conversationId: string,
  room: ProcessingSnapshotRoom | null,
  queryLoading: boolean,
  lifecycle: ProcessingLifecycle,
  getToken?: (() => Promise<string | null>) | undefined,
  reconcileWithDb?: ((conversationId: string) => Promise<unknown>) | undefined,
) {
  // Reactive subscription ensures the effect re-fires when hydration completes,
  // even if room/queryLoading settled first.
  const hydratedFromDb = useMessageStore(s => s.hydratedFromDb)

  useEffect(() => {
    // Only run when room data is loaded and not currently loading
    if (!room || queryLoading) return

    // Wait for DB hydration to complete before checking for task messages
    if (!hydratedFromDb) return
    const store = useMessageStore.getState()
    if (store.conversationId !== conversationId) return

    // Once SSE has resolved the live processing lifecycle, never re-add it —
    // the restore effect is only for page-load recovery.
    if (lifecycle.isProcessingResolved()) return

    const activeRuns = room.active_runs ?? []
    const activeRunTriggerMessageIds = activeRuns
      .map(run => run.trigger_message_id)
      .filter((id): id is string => typeof id === 'string' && id.length > 0)
    const activeRun = activeRuns[0]
    const activeRunTriggerMessageId = activeRun?.trigger_message_id ?? null
    const lifecycleMessageId = activeRunTriggerMessageId
    const hasActiveLifecycle = activeRuns.length > 0
    const cancellationPending = activeRun?.state === 'canceling'

    useRoomUiStore.getState().setActiveRunTriggerMessageIds(
      conversationId,
      activeRunTriggerMessageIds,
    )

    const finishStaleActiveRunIfTerminal = async () => {
      if (!lifecycleMessageId) return
      if (!allAgentsTerminalForUserMessage(store.entities, conversationId, lifecycleMessageId)) return

      let backendRunActive = true
      if (getToken) {
        try {
          const result = await inquiryActiveRuns(conversationId, getToken)
          if (result.success) {
            backendRunActive = (result.active_runs ?? []).some(
              run => run.trigger_message_id === lifecycleMessageId,
            )
          }
        } catch {
          backendRunActive = true
        }
      }

      if (backendRunActive) return

      const { sending } = useRoomUiStore.getState().rooms[conversationId] ?? {}
      if (!sending) {
        store.removeMessage(lifecycle.placeholderId(conversationId))
        lifecycle.stopProcessing()
      }
    }

    // Check if room has an active processing state
    if (hasActiveLifecycle) {
      const triggerMsg = lifecycleMessageId
        ? store.entities[lifecycleMessageId]
        : undefined

      // Restore correlation before any stale/task early return so Stop remains
      // attached to the same durable Run after refresh.
      lifecycle.startProcessing(
        lifecycleMessageId,
        triggerMsg?.clientRequestId ?? null,
      )
      useRoomUiStore.getState().setCancelling(conversationId, cancellationPending)

      // Cached active_runs may be stale — verify with backend before stopping processing.
      if (lifecycleMessageId) {
        void finishStaleActiveRunIfTerminal()
      }

      if (
        lifecycleMessageId
        && allAgentsTerminalForUserMessage(store.entities, conversationId, lifecycleMessageId)
      ) {
        return
      }

      if (!lifecycleMessageId || !triggerMsg) {
        return
      }

      // Check if the triggering user message is stale (> 2 min).
      const PLACEHOLDER_STALE_MS = 2 * 60 * 1000
      if (isStale(triggerMsg.timestamp, PLACEHOLDER_STALE_MS)) {
        return
      }

      // Check if any active (non-terminal) task entities already exist in the store
      const hasTaskEntities = Object.values(store.entities).some(
        e => e.conversationId === conversationId && e.taskStatus && !isTerminalState(e.taskStatus)
      )

      if (hasTaskEntities) {
        return
      }

      store.removeMessage(lifecycle.placeholderId(conversationId))
    } else {
      useRoomUiStore.getState().setCancelling(conversationId, false)
      // Backend truth says no active processing — clean up lifecycle state left
      // behind by missed terminal SSE without deleting per-turn update history.
      // But don't wipe it if a message send is still in flight (the SSE events
      // haven't arrived yet).
      const { sending } = useRoomUiStore.getState().rooms[conversationId] ?? {}
      if (sending) return
      if (lifecycle.getPendingRunEventAck()) return

      const liveLifecycleMessageId = lifecycle.getMessageId()
      const liveLifecycleMessage = liveLifecycleMessageId
        ? store.entities[liveLifecycleMessageId]
        : undefined
      if (
        liveLifecycleMessage?.conversationId === conversationId &&
        liveLifecycleMessage.messageType === 'user' &&
        !liveLifecycleMessage.turnTerminalStatus &&
        !allAgentsTerminalForUserMessage(store.entities, conversationId, liveLifecycleMessage.id)
      ) {
        if (getToken && reconcileWithDb) {
          void (async () => {
            try {
              const result = await inquiryActiveRuns(
                conversationId,
                getToken,
              )
              const backendRunActive = (result.active_runs ?? []).some(
                run => run.trigger_message_id === liveLifecycleMessage.id,
              )
              if (!result.success || backendRunActive) return

              await reconcileWithDb(conversationId)
              const refreshedStore = useMessageStore.getState()
              const refreshedMessage = refreshedStore.entities[liveLifecycleMessage.id]
              if (!refreshedMessage?.turnTerminalStatus) return

              const { sending: liveSending } =
                useRoomUiStore.getState().rooms[conversationId] ?? {}
              if (
                lifecycle.getMessageId() !== liveLifecycleMessage.id ||
                liveSending ||
                lifecycle.getPendingRunEventAck()
              ) {
                return
              }

              lifecycle.markProcessingResolved()
              refreshedStore.removeMessage(lifecycle.placeholderId(conversationId))
              lifecycle.stopProcessing()
            } catch {
              // Preserve the live lifecycle when backend truth cannot be confirmed.
            }
          })()
        }
        return
      }

      if (lifecycle.isPlaceholderDismissed()) return
      store.removeMessage(lifecycle.placeholderId(conversationId))
      lifecycle.stopProcessing()
    }
  }, [
    room,
    queryLoading,
    conversationId,
    lifecycle,
    hydratedFromDb,
    getToken,
    reconcileWithDb,
  ])
}
