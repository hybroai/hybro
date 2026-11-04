import { create } from 'zustand'
import type { ArtifactData } from '@/stores/message-store/types'
import { extractStreamTextFromArtifacts } from '@/stores/message-store/upsert'
import { mergeStreamArtifacts } from './merge-stream-artifacts'
import { log } from '@/lib/log'

// Buffers not updated within this window are considered orphaned (backend crash
// before task_update was sent). They are evicted on the next append() call.
// 5 minutes is generous enough to cover slow streaming agents.
const STALE_BUFFER_TTL_MS = 5 * 60_000

export type StreamBufferMetadata = {
  clientRequestId?: string
  userMessageId?: string
}

/**
 * Ephemeral display buffer for a single streaming message.
 * Lives only in this store — never written to messageStore.
 * Discarded when task_update (checkpoint) or DB reconcile fires.
 */
export interface StreamBuffer {
  /** Accumulated text for live render, derived from artifacts via extractStreamTextFromArtifacts. */
  text: string
  /** Accumulated artifacts for live render (non-text artifact cards). */
  artifacts: ArtifactData[]
  /** True after last_chunk=true is received. Render uses this for isStreaming flag. */
  isComplete: boolean
  /**
   * Room this buffer belongs to. Set on first append and used by clearRoom()
   * to prevent operations on one room from accidentally clearing buffers
   * that belong to a different room.
   */
  conversationId: string
  clientRequestId?: string
  userMessageId?: string
  /**
   * Monotonic timestamp (Date.now()) updated on every append and markComplete.
   * Used for TTL-based eviction of orphaned buffers (backend crash before
   * task_update was delivered).
   */
  lastUpdatedAt: number
}

/** Message-scoped live buffer lookup — never merges by client_request_id. */
export function resolveStreamBuffer(
  buffers: Record<string, StreamBuffer>,
  messageId?: string,
): StreamBuffer | undefined {
  return messageId ? buffers[messageId] : undefined
}

interface StreamingState {
  buffers: Record<string, StreamBuffer>

  /**
   * Append an incoming artifact chunk to the buffer for message_id.
   * Creates the buffer if it does not yet exist.
   * Uses mergeStreamArtifacts + extractStreamTextFromArtifacts for live display.
   *
   * Also evicts stale buffers (older than STALE_BUFFER_TTL_MS) whose
   * task_update was never received (e.g. backend crash mid-stream).
   */
  append: (
    id: string,
    conversationId: string,
    chunk: ArtifactData,
    isAppend: boolean,
    metadata?: StreamBufferMetadata,
  ) => void

  /** Mark the buffer complete after last_chunk=true arrives. */
  markComplete: (id: string) => void

  /**
   * Clear the buffer for a single message_id.
   * Called by the task_update SSE handler after writing the checkpoint to messageStore.
   */
  clear: (id: string) => void

  /** Clear all buffers tagged with the given client_request_id. */
  clearByClientRequestId: (clientRequestId: string) => void

  /**
   * Clear all buffers that belong to the given conversationId.
   * Use for room-switch cleanup (useRoomReset) where ALL buffers for the
   * room must be discarded unconditionally.
   * Room affiliation is enforced via StreamBuffer.conversationId — buffers from
   * other rooms are never touched.
   */
  clearRoom: (conversationId: string) => void

  /**
   * Clear only the buffers whose message_id appears in the provided set.
   * Use after reconcileWithDb (useRoomHydration) where only messages that
   * are confirmed-persisted in the DB should have their buffers superseded.
   * Buffers for messages still actively streaming (not yet in DB) are preserved.
   */
  clearByMessageIds: (messageIds: ReadonlySet<string>) => void
}

export const useStreamingStore = create<StreamingState>()((set) => ({
  buffers: {},

  append: (id, conversationId, chunk, isAppend, metadata) => set((state) => {
    const now = Date.now()

    // Evict orphaned buffers — those not updated within STALE_BUFFER_TTL_MS.
    // These are left over from backend crashes that never sent task_update.
    // Eviction happens passively on each append so no separate timer is needed.
    let evicted = false
    const afterEviction: Record<string, StreamBuffer> = {}
    for (const [bufferId, buf] of Object.entries(state.buffers)) {
      if (bufferId !== id && now - buf.lastUpdatedAt > STALE_BUFFER_TTL_MS) {
        evicted = true
        log.debug('stream_buffer_evicted', {
          message_id: bufferId,
          conversation_id: buf.conversationId,
          idle_s: Math.round((now - buf.lastUpdatedAt) / 1000),
        })
      } else {
        afterEviction[bufferId] = buf
      }
    }
    const base = evicted ? afterEviction : state.buffers

    const existing = base[id]
    const prevArtifacts = existing?.artifacts ?? []
    const nextArtifacts = mergeStreamArtifacts(prevArtifacts, chunk, isAppend)
    const text = extractStreamTextFromArtifacts(nextArtifacts)
    return {
      buffers: {
        ...base,
        [id]: {
          text,
          artifacts: nextArtifacts,
          isComplete: existing?.isComplete ?? false,
          conversationId,
          clientRequestId: metadata?.clientRequestId ?? existing?.clientRequestId,
          userMessageId: metadata?.userMessageId ?? existing?.userMessageId,
          lastUpdatedAt: now,
        },
      },
    }
  }),

  markComplete: (id) => set((state) => {
    const existing = state.buffers[id]
    if (!existing || existing.isComplete) return state
    return {
      buffers: {
        ...state.buffers,
        [id]: { ...existing, isComplete: true, lastUpdatedAt: Date.now() },
      },
    }
  }),

  clear: (id) => set((state) => {
    if (!(id in state.buffers)) return state
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { [id]: _removed, ...rest } = state.buffers
    return { buffers: rest }
  }),

  clearByClientRequestId: (clientRequestId) => set((state) => {
    const nextBuffers: Record<string, StreamBuffer> = {}
    let changed = false
    for (const [id, buf] of Object.entries(state.buffers)) {
      if (buf.clientRequestId === clientRequestId) {
        changed = true
      } else {
        nextBuffers[id] = buf
      }
    }
    return changed ? { buffers: nextBuffers } : state
  }),

  clearRoom: (conversationId) => set((state) => {
    const nextBuffers: Record<string, StreamBuffer> = {}
    let changed = false
    for (const [id, buf] of Object.entries(state.buffers)) {
      if (buf.conversationId === conversationId) {
        changed = true
      } else {
        nextBuffers[id] = buf
      }
    }
    return changed ? { buffers: nextBuffers } : state
  }),

  clearByMessageIds: (messageIds) => set((state) => {
    const nextBuffers: Record<string, StreamBuffer> = {}
    let changed = false
    for (const [id, buf] of Object.entries(state.buffers)) {
      if (messageIds.has(id)) {
        changed = true
      } else {
        nextBuffers[id] = buf
      }
    }
    return changed ? { buffers: nextBuffers } : state
  }),
}))
