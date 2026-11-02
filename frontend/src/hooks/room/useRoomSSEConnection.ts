import { useEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'
import type { AnySSEFrame } from '@/lib/types/sse'
import { inquiryActiveRuns } from '@/lib/api/room'
import { hydrateRoomFromDb } from '@/lib/room-sync/hydrate-room'
import { useRoomSSE } from '../useRoomSSE'
import type { ProcessingLifecycle } from './processing-lifecycle'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { useMessageStore } from '@/stores/message-store'
import { allAgentsTerminalForUserMessage } from '@/lib/room-timeline/turn-agent-terminal'
import { findProcessingStatusUserEntity } from './processing-status-log'

function hasTerminalEvidenceForCurrentTurn(conversationId: string, lifecycle: ProcessingLifecycle): boolean {
  const userEntity = findProcessingStatusUserEntity(conversationId, {
    messageId: lifecycle.getMessageId(),
    clientRequestId: lifecycle.getPendingRunEventAck(),
    latestUser: true,
  })
  const userMessageId = lifecycle.getMessageId() ?? userEntity?.id
  if (!userMessageId) return false

  const store = useMessageStore.getState()
  if (store.conversationId !== conversationId) return false

  const latestUser = store.entities[userMessageId] ?? userEntity
  if (latestUser?.conversationId === conversationId && latestUser.messageType === 'user' && latestUser.turnTerminalStatus) {
    return true
  }

  return allAgentsTerminalForUserMessage(store.entities, conversationId, userMessageId)
}

export function useRoomSSEConnection(
  conversationId: string,
  getToken: (() => Promise<string | null>) | undefined,
  sseEnabled: boolean,
  processing: boolean,
  lifecycle: ProcessingLifecycle,
  handleSSEMessage: (message: AnySSEFrame) => void,
  getAgentName: (agentId: string) => Promise<string>,
  getAgentSource: (agentId: string | undefined) => 'cloud' | 'local' | 'hub' | undefined,
  hitlRequestIndex: MutableRefObject<Map<string, string>>,
  reconcileWithDb: (conversationId: string) => Promise<void>,
  setSseConnected: (v: boolean) => void,
  setSseError: (v: string | null) => void,
  requestSnapshotRef?: MutableRefObject<(() => void) | null>,
) {
  // Initialize SSE connection
  const {
    connected: sseConnected,
    connecting: sseConnecting,
    error: sseError,
    reconnectWithSnapshot,
  } = useRoomSSE({
    conversationId,
    enabled: sseEnabled && !!conversationId,
    getToken,
    onMessage: handleSSEMessage,
  })

  // Expose the gap-recovery surface to the SSE dispatcher's reducer
  // (plan §4 rule 3: reconnect to the same endpoint with ?snapshot=1).
  useEffect(() => {
    if (requestSnapshotRef) {
      requestSnapshotRef.current = reconnectWithSnapshot
    }
    return () => {
      if (requestSnapshotRef && requestSnapshotRef.current === reconnectWithSnapshot) {
        requestSnapshotRef.current = null
      }
    }
  }, [requestSnapshotRef, reconnectWithSnapshot])

  // Sync SSE state to zustand
  useEffect(() => {
    setSseConnected(!!sseConnected)
    setSseError(sseError ? String(sseError) : null)
  }, [sseConnected, sseError, setSseConnected, setSseError])

  // Track SSE disconnections during active processing.
  // If SSE drops while agents are working, we may have missed events and need
  // to reconcile with DB after processing completes.
  // On reconnect, restore any pending HITL requests and reconcile messages.
  const prevSseConnectedRef = useRef(false)
  const hasBeenConnectedRef = useRef(false)
  useEffect(() => {
    const backendHasActiveLifecycle = async (): Promise<boolean | null> => {
      if (!conversationId) return null
      try {
        const result = await inquiryActiveRuns(conversationId, getToken)
        if (!result.success) return null
        return (result.active_runs?.length ?? 0) > 0
      } catch {
        return null
      }
    }

    if (!sseConnected && processing) {
      lifecycle.markSseDisconnection()
    }

    const justReconnected = sseConnected && !prevSseConnectedRef.current && conversationId
    const isReconnection = justReconnected && hasBeenConnectedRef.current

    if (sseConnected) {
      hasBeenConnectedRef.current = true
    }

    // HITL reconnect catch-up: restore pending HITL requests after SSE reconnects
    if (justReconnected) {
      hydrateRoomFromDb({
        conversationId,
        phase: 'hitl_overlay',
        getToken,
        getAgentName,
        getAgentSource,
        hitlRequestIndex,
      }).catch((err) => {
        console.error('[HITL] Failed to restore pending requests on reconnect:', err)
      })
    }

    // Any reconnection after a gap may have missed messages. Reconcile
    // unconditionally so the UI is fresh, regardless of processing state.
    // Also check whether the backend is still active: if it isn't, clear any
    // stuck spinner even when processing=false (e.g. page refresh during a turn,
    // or terminal SSE events were sent before this reconnect was established).
    if (isReconnection) {
      reconcileWithDb(conversationId)
      // Capture the current message ID before the async check so we can detect
      // if the user starts a new turn before the response comes back (race guard).
      const messageIdAtReconnect = lifecycle.getMessageId()
      backendHasActiveLifecycle().then(async hasActive => {
        const { sending } = useRoomUiStore.getState().rooms[conversationId] ?? {}
        if (hasActive === false && lifecycle.getMessageId() === messageIdAtReconnect && !sending) {
          await reconcileWithDb(conversationId)
          if (lifecycle.getMessageId() !== messageIdAtReconnect) return
          if (!hasTerminalEvidenceForCurrentTurn(conversationId, lifecycle)) {
            return
          }
          lifecycle.stopProcessing()
          lifecycle.clearSseDisconnection()
        }
      }).catch(() => { /* ignore — safety-net, not critical */ })
    }

    // Phase 3 (Room Stream Snapshot plan §8): the fixed 5 s safety-net poll
    // is removed. Missed events are recovered by the reducer's gap self-heal
    // (snapshot re-request) — the only recovery path.

    prevSseConnectedRef.current = sseConnected
  }, [sseConnected, processing, conversationId, getToken, getAgentName, getAgentSource, hitlRequestIndex, lifecycle, reconcileWithDb])

  return { sseConnected, sseConnecting, sseError }
}
