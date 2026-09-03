import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  useRoomUiStore,
  useRoomProcessing,
  useRoomSending,
  useRoomCancelling,
  useRoomSseEnabled,
} from '@/stores/room-ui-store'
import { useMessageStore } from '@/stores/message-store'
import { useAgentCatalog } from './useAgentCatalog'
import { useRoomData } from './useRoomData'
import { createProcessingLifecycle, type ProcessingLifecycle } from './processing-lifecycle'
import { createSSEDispatcher } from './sse-handlers/dispatch'
import { useRoomReset } from './useRoomReset'
import { useRoomHydration } from './useRoomHydration'
import { useProcessingRestore } from './useProcessingRestore'
import { useRoomSSEConnection } from './useRoomSSEConnection'
import { useSendMessage } from './useSendMessage'
import { useRoomActions } from './useRoomActions'
import type { UseRoomWebhookProps } from './types'

export function useRoomWebhook({ roomId, userId, userName, getToken }: UseRoomWebhookProps) {
  // Read per-room flags reactively through narrow selectors.
  const sending = useRoomSending(roomId)
  const cancelling = useRoomCancelling(roomId)
  const sseEnabled = useRoomSseEnabled(roomId)
  const processing = useRoomProcessing(roomId)

  // Bind stable action refs to current roomId
  const setRoomSending = useRoomUiStore(s => s.setSending)
  const setSending = useCallback((v: boolean) => setRoomSending(roomId, v), [roomId, setRoomSending])

  const setRoomProcessing = useRoomUiStore(s => s.setProcessing)
  const setProcessing = useCallback((v: boolean) => setRoomProcessing(roomId, v), [roomId, setRoomProcessing])

  const setRoomCancelling = useRoomUiStore(s => s.setCancelling)
  const setCancelling = useCallback((v: boolean) => setRoomCancelling(roomId, v), [roomId, setRoomCancelling])

  const setRoomSseEnabled = useRoomUiStore(s => s.setSseEnabled)
  const setSseEnabled = useCallback((v: boolean) => setRoomSseEnabled(roomId, v), [roomId, setRoomSseEnabled])

  const setRoomSseConnected = useRoomUiStore(s => s.setSseConnected)
  const setSseConnected = useCallback((v: boolean) => setRoomSseConnected(roomId, v), [roomId, setRoomSseConnected])

  const setRoomSseError = useRoomUiStore(s => s.setSseError)
  const setSseError = useCallback((v: string | null) => setRoomSseError(roomId, v), [roomId, setRoomSseError])

  const {
    availableAgents,
    allAgentsData,
    getAgentName,
    getAgentSource,
    primeAgentNameCache,
    resetAgentNameCache,
  } = useAgentCatalog(userId, getToken)

  const {
    room,
    loading,
    getSupervisorMode,
  } = useRoomData(roomId, getToken, primeAgentNameCache, allAgentsData)

  // Processing lifecycle: one instance per roomId, keyed in a Map.
  // Render only creates (idempotent for same roomId) — never disposes.
  // Disposal is deferred to effect cleanup (post-commit) so a discarded
  // concurrent render cannot kill the still-committed room's lifecycle.
  // After dispose(), stale async callbacks (e.g. SendMessage returning
  // after room switch) become no-ops instead of mutating the new room.
  const [lifecyclesMap] = useState(() => new Map<string, ProcessingLifecycle>())

  let lifecycle = lifecyclesMap.get(roomId)
  if (!lifecycle) {
    lifecycle = createProcessingLifecycle(setProcessing)
    lifecyclesMap.set(roomId, lifecycle)
  }

  // After commit: dispose lifecycles for rooms we no longer view
  // (previous rooms + orphans from discarded concurrent renders).
  // On unmount or before re-run: dispose current room's lifecycle.
  useEffect(() => {
    const map = lifecyclesMap
    for (const [id, lc] of map) {
      if (id !== roomId) {
        lc.dispose()
        map.delete(id)
      }
    }
    return () => {
      const lc = map.get(roomId)
      if (lc) {
        lc.dispose()
        map.delete(roomId)
      }
    }
  }, [roomId])

  // O(1) lookup index: maps HITL request_id → message entity id
  const hitlRequestIndex = useRef(new Map<string, string>())

  // Snapshot recovery surface (plan §4 rule 3): bound by useRoomSSEConnection
  // to a reconnect-with-?snapshot=1 callback.
  const requestSnapshotRef = useRef<(() => void) | null>(null)
  const requestCanonicalSnapshot = useCallback(
    () => requestSnapshotRef.current?.(),
    [],
  )

  // Room reset effect
  useRoomReset(roomId, lifecycle, hitlRequestIndex, resetAgentNameCache, setSending, setCancelling, setSseConnected, setSseError)

  // DB hydration
  const { reconcileWithDb } = useRoomHydration(
    roomId, userId, userName, getToken, room, hitlRequestIndex, getAgentName, getAgentSource,
  )

  // Backfill agentSource on entities once the agent catalog arrives.
  // DB hydration may run before the catalog query completes, leaving
  // agentSource undefined on hydrated entities. We depend on hydratedFromDb
  // so the effect also fires when hydration finishes after the catalog.
  const hydratedFromDb = useMessageStore(s => s.hydratedFromDb)
  useEffect(() => {
    if (!allAgentsData?.length || !hydratedFromDb) return
    const store = useMessageStore.getState()
    if (store.roomId !== roomId) return
    const patches: { id: string; agentSource: 'cloud' | 'local' | 'hub' }[] = []
    for (const entity of Object.values(store.entities)) {
      if (entity.messageType === 'agent' && entity.agentId && !entity.agentSource) {
        const src = getAgentSource(entity.agentId)
        if (src) patches.push({ id: entity.id, agentSource: src })
      }
    }
    if (patches.length === 0) return
    for (const { id, agentSource } of patches) {
      store.upsertMessage({
        id,
        roomId,
        messageType: 'agent',
        content: store.entities[id].content,
        senderName: store.entities[id].senderName,
        timestamp: store.entities[id].timestamp,
        agentSource,
      }, 'db')
    }
  }, [roomId, allAgentsData, getAgentSource, hydratedFromDb])

  // Processing restore
  useProcessingRestore(
    roomId,
    room,
    loading,
    lifecycle,
    getToken,
    reconcileWithDb,
  )

  // Handle SSE messages — delegates to pure dispatcher factory
  const handleSSEMessage = useMemo(
    () => createSSEDispatcher({
      roomId, lifecycle, getAgentName, getAgentSource, getToken,
      reconcileWithDb, hitlRequestIndex, setCancelling,
      requestSnapshotRef,
    }),
    [roomId, lifecycle, getAgentName, getAgentSource, getToken,
     reconcileWithDb, setCancelling]
  )

  // SSE connection
  const { sseConnected: sseConnectedFromSSE, sseConnecting, sseError: sseErrorFromSSE } = useRoomSSEConnection(
    roomId, getToken, sseEnabled, processing, lifecycle, handleSSEMessage,
    getAgentName, getAgentSource, hitlRequestIndex, reconcileWithDb,
    setSseConnected, setSseError, requestSnapshotRef,
  )

  // Send message
  const { sendUserMessage } = useSendMessage(
    roomId, userId, userName, room, getToken, sending, sseConnectedFromSSE,
    lifecycle, setSending, setCancelling, reconcileWithDb,
  )

  // Room actions
  const {
    cancelProcessing,
    respondToHitlBatch,
    cancelHitlRequest,
    refreshMessages,
    toggleSSE,
  } = useRoomActions(
    roomId, getToken, lifecycle, hitlRequestIndex,
    reconcileWithDb, setCancelling,
    sseEnabled, setSseEnabled,
    getAgentName, getAgentSource,
    requestCanonicalSnapshot,
  )

  return {
    // State
    room,
    loading,
    sending,
    processing,
    cancelling,

    // SSE State
    sseConnected: sseConnectedFromSSE,
    sseConnecting,
    sseError: sseErrorFromSSE,
    sseEnabled,

    // Supervisor Mode
    supervisorMode: getSupervisorMode(),

    // Actions
    sendUserMessage,
    cancelProcessing,
    respondToHitlBatch,
    cancelHitlRequest,
    refreshMessages,
    toggleSSE,
    availableAgents,
  }
}
