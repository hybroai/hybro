import type { AnySSEFrame, SSEConnectionStatus } from '@/lib/types/sse'
import { hasSSEFrameEnvelope } from '@/lib/types/sse'
import { getApiUrl } from '../utils'
import { getClientAuthHeaders } from '../auth'

const API_BASE_URL = getApiUrl('sse')

// Re-export parsed frame type for convenience
export type { AnySSEFrame as SSEMessage }

export type SSECloseReason = 'manual' | 'permanent-failure'

export interface SSEConnectionOptions {
  roomId: string
  getToken?: () => Promise<string | null>
  onMessage?: (message: AnySSEFrame) => void | Promise<void>
  onError?: (error: Event) => void
  onOpen?: (event: Event) => void
  onClose?: (reason: SSECloseReason) => void
  reconnectJitterMs?: number
  randomFn?: () => number
  deterministicFirstReconnect?: boolean
  /** Force a fresh snapshot fold from the authoritative log (?snapshot=1). */
  snapshot?: boolean
}

// Connection state constants (mirrors EventSource.readyState values)
export const SSE_STATE = {
  CONNECTING: 0,
  OPEN: 1,
  CLOSED: 2,
} as const

const { CONNECTING, OPEN, CLOSED } = SSE_STATE

export class SSEConnection {
  private abortController: AbortController | null = null
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  private connectionState: number = CLOSED
  private roomId: string
  private options: SSEConnectionOptions
  private reconnectAttempts = 0
  private maxReconnectAttempts = 15
  private baseReconnectDelay = 1000
  private maxReconnectDelay = 30_000
  private reconnectJitterMs = 0
  private randomFn: () => number = Math.random
  private deterministicFirstReconnect = true
  private readTimeoutMs = 90_000 // 3x the backend's 30s heartbeat interval
  private isManualClose = false
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private connectCancelled = false

  constructor(options: SSEConnectionOptions) {
    this.roomId = options.roomId
    this.options = options
    this.reconnectJitterMs = Math.max(0, options.reconnectJitterMs ?? 0)
    this.randomFn = options.randomFn ?? Math.random
    this.deterministicFirstReconnect = options.deterministicFirstReconnect ?? true
  }

  async connect(): Promise<void> {
    return new Promise(async (resolve, reject) => {
      try {
        this.isManualClose = false
        this.connectCancelled = false
        this.connectionState = CONNECTING

        // Get auth token if available
        const token = this.options.getToken ? await this.options.getToken() : null

        // If a disconnect was requested while awaiting token, abort
        if (this.isManualClose || this.connectCancelled) {
          this.connectionState = CLOSED
          return resolve()
        }

        // Build URL without token in query string (security fix for issue 2.1)
        // ?snapshot=1 forces a fresh snapshot fold from the authoritative log
        // (Room Stream Snapshot plan §4 rule 3 gap recovery).
        const url = `${API_BASE_URL}/room/${this.roomId}/stream${this.options.snapshot ? '?snapshot=1' : ''}`

        // Send JWT via Authorization header instead of URL query parameter
        const headers: Record<string, string> = {
          'Accept': 'text/event-stream',
          'Cache-Control': 'no-cache',
        }
        if (token) {
          headers['Authorization'] = `Bearer ${token}`
        }

        this.abortController = new AbortController()

        const response = await fetch(url, {
          method: 'GET',
          headers,
          signal: this.abortController.signal,
        })

        if (!response.ok) {
          throw new Error(`SSE connection failed: HTTP ${response.status}`)
        }

        if (!response.body) {
          throw new Error('SSE response has no body')
        }

        // Connection established
        this.connectionState = OPEN
        this.reconnectAttempts = 0
        this.options.onOpen?.(new Event('open'))
        resolve()

        // Start reading the stream (runs until disconnect or error)
        this.reader = response.body.getReader()
        await this.readStream(this.reader)

      } catch (error) {
        // Handle abort (from disconnect())
        if (error instanceof DOMException && error.name === 'AbortError') {
          this.connectionState = CLOSED
          return resolve()
        }

        this.connectionState = CLOSED
        this.options.onError?.(new Event('error'))

        // If we're intentionally closing or cancelled, do not attempt reconnect
        if (this.isManualClose || this.connectCancelled) {
          return resolve()
        }

        this.attemptReconnect(reject)
      }
    })
  }

  private async readStream(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        // Race reader.read() against a timeout so silently-stalled connections
        // (proxy dropped TCP without FIN/RST) are detected. Backend sends a
        // heartbeat every 30s; 90s without any data means the stream is dead.
        const result = await Promise.race([
          reader.read(),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error('SSE read timeout')), this.readTimeoutMs)
          ),
        ])

        if (result.done) {
          break
        }

        buffer += decoder.decode(result.value, { stream: true })
        const { messages, remainder } = this.processSSEBuffer(buffer)
        buffer = remainder

        for (const data of messages) {
          try {
            const parsed: unknown = JSON.parse(data)

            if (!hasSSEFrameEnvelope(parsed)) {
              console.debug('Ignoring SSE payload without final frame envelope:', parsed)
              continue
            }

            // Preserve wire order across async folds. A later terminal frame
            // must never overtake an earlier start/update handler.
            await this.options.onMessage?.(parsed)
          } catch (frameError) {
            console.error('Failed to process SSE message:', frameError, data)
          }
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        return
      }
      // Read timeout or other stream errors fall through to finally block
    } finally {
      this.connectionState = CLOSED

      if (!this.isManualClose && !this.connectCancelled) {
        this.options.onError?.(new Event('error'))
        this.attemptReconnect()
      } else {
        this.options.onClose?.('manual')
      }
    }
  }

  private processSSEBuffer(buffer: string): { messages: string[]; remainder: string } {
    const messages: string[] = []
    const blocks = buffer.split('\n\n')

    // Last element is incomplete (no trailing \n\n) — keep as remainder
    const remainder = blocks.pop() ?? ''

    for (const block of blocks) {
      if (!block.trim()) continue

      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('data: ')) {
          data += (data ? '\n' : '') + line.slice(6)
        } else if (line.startsWith('data:')) {
          data += (data ? '\n' : '') + line.slice(5)
        }
        // Note: bare "data:" (no value) is treated as empty string per SSE spec,
        // handled by the slice(5) branch above. Comment lines (:), id:, event:,
        // retry: are ignored — not used by our backend.
      }

      if (data) {
        messages.push(data)
      }
    }

    return { messages, remainder }
  }

  private attemptReconnect(reject?: (reason: Error) => void): void {
    if (this.isManualClose || this.connectCancelled) return

    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++

      // Keep exponential backoff deterministic by default. Jitter is opt-in
      // and can be injected for production thundering-herd mitigation.
      const exponential = this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts - 1)
      const jitterCap = (this.deterministicFirstReconnect && this.reconnectAttempts === 1)
        ? 0
        : this.reconnectJitterMs
      const jitter = jitterCap > 0
        ? Math.floor(Math.max(0, this.randomFn()) * jitterCap)
        : 0
      const delay = Math.min(exponential + jitter, this.maxReconnectDelay)

      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
      }
      this.reconnectTimer = setTimeout(() => {
        if (!this.isManualClose && !this.connectCancelled) {
          this.connect().catch(console.error)
        }
      }, delay)
    } else {
      console.error(`❌ SSE reconnection failed after ${this.maxReconnectAttempts} attempts`)
      this.options.onError?.(new Event('error'))
      this.options.onClose?.('permanent-failure')
      reject?.(new Error('Max reconnection attempts reached'))
    }
  }

  disconnect(): void {
    this.isManualClose = true
    this.connectCancelled = true
    this.connectionState = CLOSED

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    if (this.abortController) {
      this.abortController.abort()
      this.abortController = null
    }

    if (this.reader) {
      this.reader.cancel().catch(() => { /* reader already released or stream closed */ })
      this.reader = null
    }
  }

  isConnected(): boolean {
    return this.connectionState === OPEN
  }

  getConnectionState(): number {
    return this.connectionState
  }
}

// Get SSE connection status
export async function getSSEStatus(
  roomId: string,
  getToken?: () => Promise<string | null>
): Promise<SSEConnectionStatus> {
  const url = `${API_BASE_URL}/room/${roomId}/status`
  
  const headers = await getClientAuthHeaders(getToken)
  const response = await fetch(url, { headers })
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`)
  }
  return await response.json() as SSEConnectionStatus
}

// Cancel message processing
export async function cancelMessage(
  messageId: string,
  getToken?: () => Promise<string | null>
): Promise<{
  success: boolean
  message_id: string
  message: string
  status?:
    | 'finalizing'
    | 'completed'
    | 'failed'
    | 'canceled'
    | 'budget_exhausted'
    | 'cancellation_pending'
  outcome?: 'canceled' | 'already_terminal' | 'pending_reconciliation'
}> {
  const url = `${API_BASE_URL}/message/${messageId}/cancel`
  
  const headers = await getClientAuthHeaders(getToken)
  const response = await fetch(url, {
    method: 'POST',
    headers
  })
  
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`)
  }
  
  return await response.json()
}
