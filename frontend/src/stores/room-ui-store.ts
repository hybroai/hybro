import { create } from 'zustand'
import type { Agent } from '@/lib/types/agent'
import type { PendingAttachment } from '@/lib/types/attachments'
import type { AgentScopeInput, ExecutionMode } from '@/lib/types/request'
import type { ConversationScrollSnapshot } from '@/lib/conversation/conversation-scroll'

/** One-shot handoff from an Agent detail page to the new-chat composer. */
export interface PendingChatHandoff {
  /** Prefill text only — not an @mention. */
  draft: string
  /** Seed room membership with these agents (single-agent chat ≠ mention). */
  seedAgents?: Agent[]
}

type ConversationId = string

/** Max detail-pane scroll snapshots retained (LRU by last access). */
const MAX_DETAIL_SCROLL_SNAPSHOTS = 32

function touchDetailScrollSnapshot(
  map: Record<string, ConversationScrollSnapshot>,
  messageId: string,
  snapshot: ConversationScrollSnapshot,
): Record<string, ConversationScrollSnapshot> {
  const next: Record<string, ConversationScrollSnapshot> = { ...map }
  delete next[messageId]
  next[messageId] = snapshot
  const keys = Object.keys(next)
  if (keys.length <= MAX_DETAIL_SCROLL_SNAPSHOTS) return next

  const trimmed: Record<string, ConversationScrollSnapshot> = {}
  const dropCount = keys.length - MAX_DETAIL_SCROLL_SNAPSHOTS
  for (let i = dropCount; i < keys.length; i += 1) {
    const key = keys[i]
    trimmed[key] = next[key]
  }
  return trimmed
}

interface PendingRoomData {
  initialMessage: string
  mode?: ExecutionMode
  agentScope?: AgentScopeInput
  clientRequestId?: string
  attachments?: PendingAttachment[]
  handoffMode?: "autosend" | "prefill"
}

export interface RoomFlags {
  sending: boolean
  processing: boolean
  cancelling: boolean
  sseEnabled: boolean
  sseConnected: boolean
  sseError: string | null
  /** User message ids that still have an open backend room run. */
  activeRunTriggerMessageIds: string[]
}

export const DEFAULT_ROOM_FLAGS: RoomFlags = {
  sending: false,
  processing: false,
  cancelling: false,
  sseEnabled: true,
  sseConnected: false,
  sseError: null,
  activeRunTriggerMessageIds: [],
}

function patchRoom(rooms: Record<ConversationId, RoomFlags>, conversationId: ConversationId, patch: Partial<RoomFlags>): Record<ConversationId, RoomFlags> {
  return { ...rooms, [conversationId]: { ...(rooms[conversationId] ?? DEFAULT_ROOM_FLAGS), ...patch } }
}

interface RoomUiState {
  rooms: Record<ConversationId, RoomFlags>
  /** Pending initial messages for rooms (replaces sessionStorage) */
  pendingRoomData: Record<ConversationId, PendingRoomData>
  /** One-shot handoff from an Agent detail page to the new-chat composer. */
  pendingChatHandoff: PendingChatHandoff | null
  localSendSeqByRoom: Record<ConversationId, number>
  initialHydrationSeqByRoom: Record<ConversationId, number>
  conversationScrollByRoom: Record<ConversationId, ConversationScrollSnapshot>
  detailScrollByMessageId: Record<string, ConversationScrollSnapshot>
  selectedAgentMessageIdByRoom: Record<ConversationId, string | undefined>

  // Per-room flag setters (conversationId, value)
  setSending: (conversationId: ConversationId, v: boolean) => void
  setProcessing: (conversationId: ConversationId, v: boolean) => void
  setCancelling: (conversationId: ConversationId, v: boolean) => void
  setSseEnabled: (conversationId: ConversationId, v: boolean) => void
  setSseConnected: (conversationId: ConversationId, v: boolean) => void
  setSseError: (conversationId: ConversationId, v: string | null) => void
  setActiveRunTriggerMessageIds: (conversationId: ConversationId, ids: string[]) => void

  // Non-reactive getter for getState() callers
  getRoomFlags: (conversationId: ConversationId) => RoomFlags
  // Delete a single room's entry (falls back to defaults on next read)
  resetRoom: (conversationId: ConversationId) => void
  resetAll: () => void

  /** Store a pending initial message + target group for a room */
  setPendingRoomData: (conversationId: ConversationId, data: PendingRoomData) => void
  /** Consume (read + delete) pending data for a room */
  consumePendingRoomData: (conversationId: ConversationId) => PendingRoomData | null
  setPendingChatHandoff: (handoff: PendingChatHandoff) => void
  clearPendingChatHandoff: () => void
  markLocalSend: (conversationId: ConversationId) => void
  markInitialHydrated: (conversationId: ConversationId) => void
  saveConversationScroll: (conversationId: ConversationId, snapshot: ConversationScrollSnapshot) => void
  getConversationScroll: (conversationId: ConversationId) => ConversationScrollSnapshot | undefined
  saveDetailPaneScroll: (messageId: string, snapshot: ConversationScrollSnapshot) => void
  getDetailPaneScroll: (messageId: string) => ConversationScrollSnapshot | undefined
  openAgentDetail: (conversationId: ConversationId, messageId: string) => void
  closeAgentDetail: (conversationId: ConversationId) => void
}

export const useRoomUiStore = create<RoomUiState>((set, get) => ({
  rooms: {},
  pendingRoomData: {},
  pendingChatHandoff: null,
  localSendSeqByRoom: {},
  initialHydrationSeqByRoom: {},
  conversationScrollByRoom: {},
  detailScrollByMessageId: {},
  selectedAgentMessageIdByRoom: {},

  setSending: (conversationId, v) => set(s => ({ rooms: patchRoom(s.rooms, conversationId, { sending: v }) })),
  setProcessing: (conversationId, v) => set(s => ({
    rooms: patchRoom(s.rooms, conversationId, v
      ? { processing: true }
      : { processing: false, activeRunTriggerMessageIds: [] }),
  })),
  setCancelling: (conversationId, v) => set(s => ({ rooms: patchRoom(s.rooms, conversationId, { cancelling: v }) })),
  setSseEnabled: (conversationId, v) => set(s => ({ rooms: patchRoom(s.rooms, conversationId, { sseEnabled: v }) })),
  setSseConnected: (conversationId, v) => set(s => ({ rooms: patchRoom(s.rooms, conversationId, { sseConnected: v }) })),
  setSseError: (conversationId, v) => set(s => ({ rooms: patchRoom(s.rooms, conversationId, { sseError: v }) })),
  setActiveRunTriggerMessageIds: (conversationId, ids) => set(s => ({
    rooms: patchRoom(s.rooms, conversationId, { activeRunTriggerMessageIds: ids }),
  })),

  getRoomFlags: (conversationId) => get().rooms[conversationId] ?? DEFAULT_ROOM_FLAGS,

  resetRoom: (conversationId) =>
    set(s => {
      const rooms = { ...s.rooms }
      delete rooms[conversationId]
      const localSendSeqByRoom = { ...s.localSendSeqByRoom }
      delete localSendSeqByRoom[conversationId]
      const initialHydrationSeqByRoom = { ...s.initialHydrationSeqByRoom }
      delete initialHydrationSeqByRoom[conversationId]
      const selectedAgentMessageIdByRoom = { ...s.selectedAgentMessageIdByRoom }
      delete selectedAgentMessageIdByRoom[conversationId]
      return { rooms, localSendSeqByRoom, initialHydrationSeqByRoom, selectedAgentMessageIdByRoom }
    }),

  resetAll: () =>
    set({
      rooms: {},
      pendingRoomData: {},
      pendingChatHandoff: null,
      localSendSeqByRoom: {},
      initialHydrationSeqByRoom: {},
      conversationScrollByRoom: {},
      detailScrollByMessageId: {},
      selectedAgentMessageIdByRoom: {},
    }),

  setPendingRoomData: (conversationId, data) =>
    set((state) => ({
      pendingRoomData: {
        ...state.pendingRoomData,
        [conversationId]: data,
      },
    })),
  consumePendingRoomData: (conversationId) => {
    const data = get().pendingRoomData[conversationId] || null
    if (data) {
      set((state) => {
        const copy = { ...state.pendingRoomData }
        delete copy[conversationId]
        return { pendingRoomData: copy }
      })
    }
    return data
  },
  setPendingChatHandoff: (handoff) => set({ pendingChatHandoff: handoff }),
  clearPendingChatHandoff: () => set({ pendingChatHandoff: null }),
  markLocalSend: (conversationId) =>
    set((state) => ({
      localSendSeqByRoom: {
        ...state.localSendSeqByRoom,
        [conversationId]: (state.localSendSeqByRoom[conversationId] ?? 0) + 1,
      },
    })),
  markInitialHydrated: (conversationId) =>
    set((state) => ({
      initialHydrationSeqByRoom: {
        ...state.initialHydrationSeqByRoom,
        [conversationId]: (state.initialHydrationSeqByRoom[conversationId] ?? 0) + 1,
      },
    })),
  saveConversationScroll: (conversationId, snapshot) =>
    set((state) => ({
      conversationScrollByRoom: {
        ...state.conversationScrollByRoom,
        [conversationId]: snapshot,
      },
    })),
  getConversationScroll: (conversationId) => get().conversationScrollByRoom[conversationId],
  saveDetailPaneScroll: (messageId, snapshot) =>
    set((state) => ({
      detailScrollByMessageId: touchDetailScrollSnapshot(
        state.detailScrollByMessageId,
        messageId,
        snapshot,
      ),
    })),
  getDetailPaneScroll: (messageId) => get().detailScrollByMessageId[messageId],
  openAgentDetail: (conversationId, messageId) =>
    set((state) => ({
      selectedAgentMessageIdByRoom: {
        ...state.selectedAgentMessageIdByRoom,
        [conversationId]: messageId,
      },
    })),
  closeAgentDetail: (conversationId) =>
    set((state) => {
      const selectedAgentMessageIdByRoom = { ...state.selectedAgentMessageIdByRoom }
      delete selectedAgentMessageIdByRoom[conversationId]
      return { selectedAgentMessageIdByRoom }
    }),
}))

/** Narrow selector: room processing lifecycle flag only. */
export function useRoomProcessing(conversationId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[conversationId] ?? DEFAULT_ROOM_FLAGS).processing)
}

export function useRoomSending(conversationId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[conversationId] ?? DEFAULT_ROOM_FLAGS).sending)
}

export function useRoomCancelling(conversationId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[conversationId] ?? DEFAULT_ROOM_FLAGS).cancelling)
}

export function useRoomSseEnabled(conversationId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[conversationId] ?? DEFAULT_ROOM_FLAGS).sseEnabled)
}

export function useLocalSendSeq(conversationId: string): number {
  return useRoomUiStore(s => s.localSendSeqByRoom[conversationId] ?? 0)
}

export function useInitialHydrationSeq(conversationId: string): number {
  return useRoomUiStore(s => s.initialHydrationSeqByRoom[conversationId] ?? 0)
}

export function useSelectedAgentMessageId(conversationId: string): string | undefined {
  return useRoomUiStore(s => s.selectedAgentMessageIdByRoom[conversationId])
}
