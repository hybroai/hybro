import { useEffect, useRef, useCallback, useState } from 'react'
import { SSEConnection } from '@/lib/api/sse'
import type { SSECloseReason } from '@/lib/api/sse'
import type { AnySSEFrame } from '@/lib/types/sse'
import { log } from '@/lib/log'

interface UseRoomSSEOptions {
  conversationId: string
  enabled?: boolean
  getToken?: () => Promise<string | null>
  onMessage?: (message: AnySSEFrame) => void | Promise<void>
  onConnectionChange?: (connected: boolean) => void
}

const RESURRECT_DELAY_MS = 45_000

export function useRoomSSE({ conversationId, enabled = true, getToken, onMessage, onConnectionChange }: UseRoomSSEOptions) {
  const [connected, setConnected] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const connectionRef = useRef<SSEConnection | null>(null)
  const resurrectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // ?snapshot=1 forced on the next connect (gap recovery, plan §4 rule 3).
  const snapshotRef = useRef(false)
  
  const onMessageRef = useRef(onMessage)
  const onConnectionChangeRef = useRef(onConnectionChange)
  const getTokenRef = useRef(getToken)
  
  useEffect(() => {
    onMessageRef.current = onMessage
  }, [onMessage])
  
  useEffect(() => {
    onConnectionChangeRef.current = onConnectionChange
  }, [onConnectionChange])
  
  useEffect(() => {
    getTokenRef.current = getToken
  }, [getToken])

  const handleMessage = useCallback((message: AnySSEFrame) => {
    return onMessageRef.current?.(message)
  }, [])

  const handleConnectionChange = useCallback((isConnected: boolean) => {
    setConnected(isConnected)
    onConnectionChangeRef.current?.(isConnected)
  }, [])

  const clearResurrectTimer = useCallback(() => {
    if (resurrectTimerRef.current) {
      clearTimeout(resurrectTimerRef.current)
      resurrectTimerRef.current = null
    }
  }, [])

  const connect = useCallback(async () => {
    if (!enabled || !conversationId || connectionRef.current?.isConnected()) {
      return
    }

    clearResurrectTimer()

    try {
      setConnecting(true)
      setError(null)

      if (connectionRef.current) {
        connectionRef.current.disconnect()
      }

      connectionRef.current = new SSEConnection({
        conversationId,
        getToken: () => getTokenRef.current?.() ?? Promise.resolve(null),
        onMessage: handleMessage,
        snapshot: snapshotRef.current,
        onOpen: () => {
          clearResurrectTimer()
          handleConnectionChange(true)
          setConnecting(false)
        },
        onError: () => {
          setError('Connection error')
          handleConnectionChange(false)
          setConnecting(false)
        },
        onClose: (reason: SSECloseReason) => {
          handleConnectionChange(false)
          setConnecting(false)

          if (reason === 'permanent-failure') {
            connectionRef.current = null
            clearResurrectTimer()
            resurrectTimerRef.current = setTimeout(() => {
              // Re-read enabled/conversationId from the closure at timer-fire time.
              // connect() has its own guards, so stale calls are harmless.
              connectRef.current()
            }, RESURRECT_DELAY_MS)
          }
        }
      })

      await connectionRef.current.connect()
    } catch (error) {
      // Any rejected connect (token, network, HTTP); SSEConnection logs the
      // HTTP status itself as sse_connect_failed.
      log.error('room_sse_connect_failed', { conversation_id: conversationId, error })
      setError(error instanceof Error ? error.message : 'Connection failed')
      setConnecting(false)
      handleConnectionChange(false)
    }
  }, [conversationId, enabled, handleMessage, handleConnectionChange, clearResurrectTimer])

  // Stable ref to `connect` so the resurrect timer can call the latest version
  const connectRef = useRef(connect)
  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  const disconnect = useCallback(() => {
    clearResurrectTimer()
    if (connectionRef.current) {
      connectionRef.current.disconnect()
      connectionRef.current = null
    }
    handleConnectionChange(false)
    setConnecting(false)
    setError(null)
  }, [handleConnectionChange, clearResurrectTimer])

  // Gap-recovery surface (plan §4 rule 3): close the stream and reconnect
  // to the same endpoint with ?snapshot=1 for a fresh fold.
  const reconnectWithSnapshot = useCallback(() => {
    snapshotRef.current = true
    disconnect()
    void connectRef.current()
  }, [disconnect])

  useEffect(() => {
    if (enabled && conversationId) {
      if (connectionRef.current) {
        connectionRef.current.disconnect()
        connectionRef.current = null
      }
      connect()
    } else {
      disconnect()
    }

    return () => {
      clearResurrectTimer()
      if (connectionRef.current) {
        connectionRef.current.disconnect()
        connectionRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, enabled])

  return {
    connected,
    connecting,
    error,
    connect,
    disconnect,
    reconnectWithSnapshot,
    isConnected: () => connectionRef.current?.isConnected() ?? false
  }
}
