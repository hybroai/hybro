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

type RoomId = string

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

export interface PendingTurnSkeleton {
  text: string
  attachments?: PendingAttachment[]
}

export interface RoomFlags {
  sending: boolean
  processing: boolean
  cancelling: boolean
  sseEnabled: boolean
  sseConnected: boolean
  sseError: string | null
  turnBasedTimeline: boolean
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
  turnBasedTimeline: false,
  activeRunTriggerMessageIds: [],
}

function patchRoom(rooms: Record<RoomId, RoomFlags>, roomId: RoomId, patch: Partial<RoomFlags>): Record<RoomId, RoomFlags> {
  return { ...rooms, [roomId]: { ...(rooms[roomId] ?? DEFAULT_ROOM_FLAGS), ...patch } }
}

interface RoomUiState {
  rooms: Record<RoomId, RoomFlags>
  /** Pending initial messages for rooms (replaces sessionStorage) */
  pendingRoomData: Record<RoomId, PendingRoomData>
  /** One-shot handoff from an Agent detail page to the new-chat composer. */
  pendingChatHandoff: PendingChatHandoff | null
  pendingTurnSkeletons: Record<RoomId, PendingTurnSkeleton | undefined>
  localSendSeqByRoom: Record<RoomId, number>
  initialHydrationSeqByRoom: Record<RoomId, number>
  conversationScrollByRoom: Record<RoomId, ConversationScrollSnapshot>
  detailScrollByMessageId: Record<string, ConversationScrollSnapshot>
  selectedAgentMessageIdByRoom: Record<RoomId, string | undefined>

  // Per-room flag setters (roomId, value)
  setSending: (roomId: RoomId, v: boolean) => void
  setProcessing: (roomId: RoomId, v: boolean) => void
  setCancelling: (roomId: RoomId, v: boolean) => void
  setSseEnabled: (roomId: RoomId, v: boolean) => void
  setSseConnected: (roomId: RoomId, v: boolean) => void
  setSseError: (roomId: RoomId, v: string | null) => void
  setTurnBasedTimeline: (roomId: RoomId, v: boolean) => void
  setActiveRunTriggerMessageIds: (roomId: RoomId, ids: string[]) => void

  // Non-reactive getter for getState() callers
  getRoomFlags: (roomId: RoomId) => RoomFlags
  // Delete a single room's entry (falls back to defaults on next read)
  resetRoom: (roomId: RoomId) => void
  resetAll: () => void

  /** Store a pending initial message + target group for a room */
  setPendingRoomData: (roomId: RoomId, data: PendingRoomData) => void
  /** Consume (read + delete) pending data for a room */
  consumePendingRoomData: (roomId: RoomId) => PendingRoomData | null
  setPendingChatHandoff: (handoff: PendingChatHandoff) => void
  clearPendingChatHandoff: () => void
  setPendingTurnSkeleton: (roomId: RoomId, value?: PendingTurnSkeleton) => void
  markLocalSend: (roomId: RoomId) => void
  markInitialHydrated: (roomId: RoomId) => void
  saveConversationScroll: (roomId: RoomId, snapshot: ConversationScrollSnapshot) => void
  getConversationScroll: (roomId: RoomId) => ConversationScrollSnapshot | undefined
  saveDetailPaneScroll: (messageId: string, snapshot: ConversationScrollSnapshot) => void
  getDetailPaneScroll: (messageId: string) => ConversationScrollSnapshot | undefined
  openAgentDetail: (roomId: RoomId, messageId: string) => void
  closeAgentDetail: (roomId: RoomId) => void
}

export const useRoomUiStore = create<RoomUiState>((set, get) => ({
  rooms: {},
  pendingRoomData: {},
  pendingChatHandoff: null,
  pendingTurnSkeletons: {},
  localSendSeqByRoom: {},
  initialHydrationSeqByRoom: {},
  conversationScrollByRoom: {},
  detailScrollByMessageId: {},
  selectedAgentMessageIdByRoom: {},

  setSending: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { sending: v }) })),
  setProcessing: (roomId, v) => set(s => ({
    rooms: patchRoom(s.rooms, roomId, v
      ? { processing: true }
      : { processing: false, activeRunTriggerMessageIds: [] }),
  })),
  setCancelling: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { cancelling: v }) })),
  setSseEnabled: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { sseEnabled: v }) })),
  setSseConnected: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { sseConnected: v }) })),
  setSseError: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { sseError: v }) })),
  setTurnBasedTimeline: (roomId, v) => set(s => ({ rooms: patchRoom(s.rooms, roomId, { turnBasedTimeline: v }) })),
  setActiveRunTriggerMessageIds: (roomId, ids) => set(s => ({
    rooms: patchRoom(s.rooms, roomId, { activeRunTriggerMessageIds: ids }),
  })),

  getRoomFlags: (roomId) => get().rooms[roomId] ?? DEFAULT_ROOM_FLAGS,

  resetRoom: (roomId) =>
    set(s => {
      const rooms = { ...s.rooms }
      delete rooms[roomId]
      const localSendSeqByRoom = { ...s.localSendSeqByRoom }
      delete localSendSeqByRoom[roomId]
      const initialHydrationSeqByRoom = { ...s.initialHydrationSeqByRoom }
      delete initialHydrationSeqByRoom[roomId]
      const selectedAgentMessageIdByRoom = { ...s.selectedAgentMessageIdByRoom }
      delete selectedAgentMessageIdByRoom[roomId]
      return { rooms, localSendSeqByRoom, initialHydrationSeqByRoom, selectedAgentMessageIdByRoom }
    }),

  resetAll: () =>
    set({
      rooms: {},
      pendingRoomData: {},
      pendingChatHandoff: null,
      pendingTurnSkeletons: {},
      localSendSeqByRoom: {},
      initialHydrationSeqByRoom: {},
      conversationScrollByRoom: {},
      detailScrollByMessageId: {},
      selectedAgentMessageIdByRoom: {},
    }),

  setPendingRoomData: (roomId, data) =>
    set((state) => ({
      pendingRoomData: {
        ...state.pendingRoomData,
        [roomId]: data,
      },
    })),
  consumePendingRoomData: (roomId) => {
    const data = get().pendingRoomData[roomId] || null
    if (data) {
      set((state) => {
        const copy = { ...state.pendingRoomData }
        delete copy[roomId]
        return { pendingRoomData: copy }
      })
    }
    return data
  },
  setPendingChatHandoff: (handoff) => set({ pendingChatHandoff: handoff }),
  clearPendingChatHandoff: () => set({ pendingChatHandoff: null }),
  setPendingTurnSkeleton: (roomId, value) =>
    set((state) => {
      const copy = { ...state.pendingTurnSkeletons }
      if (!value) delete copy[roomId]
      else copy[roomId] = value
      return { pendingTurnSkeletons: copy }
    }),
  markLocalSend: (roomId) =>
    set((state) => ({
      localSendSeqByRoom: {
        ...state.localSendSeqByRoom,
        [roomId]: (state.localSendSeqByRoom[roomId] ?? 0) + 1,
      },
    })),
  markInitialHydrated: (roomId) =>
    set((state) => ({
      initialHydrationSeqByRoom: {
        ...state.initialHydrationSeqByRoom,
        [roomId]: (state.initialHydrationSeqByRoom[roomId] ?? 0) + 1,
      },
    })),
  saveConversationScroll: (roomId, snapshot) =>
    set((state) => ({
      conversationScrollByRoom: {
        ...state.conversationScrollByRoom,
        [roomId]: snapshot,
      },
    })),
  getConversationScroll: (roomId) => get().conversationScrollByRoom[roomId],
  saveDetailPaneScroll: (messageId, snapshot) =>
    set((state) => ({
      detailScrollByMessageId: touchDetailScrollSnapshot(
        state.detailScrollByMessageId,
        messageId,
        snapshot,
      ),
    })),
  getDetailPaneScroll: (messageId) => get().detailScrollByMessageId[messageId],
  openAgentDetail: (roomId, messageId) =>
    set((state) => ({
      selectedAgentMessageIdByRoom: {
        ...state.selectedAgentMessageIdByRoom,
        [roomId]: messageId,
      },
    })),
  closeAgentDetail: (roomId) =>
    set((state) => {
      const selectedAgentMessageIdByRoom = { ...state.selectedAgentMessageIdByRoom }
      delete selectedAgentMessageIdByRoom[roomId]
      return { selectedAgentMessageIdByRoom }
    }),
}))

/** Narrow selector: room processing lifecycle flag only. */
export function useRoomProcessing(roomId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[roomId] ?? DEFAULT_ROOM_FLAGS).processing)
}

export function useRoomSending(roomId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[roomId] ?? DEFAULT_ROOM_FLAGS).sending)
}

export function useRoomCancelling(roomId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[roomId] ?? DEFAULT_ROOM_FLAGS).cancelling)
}

export function useRoomSseEnabled(roomId: string): boolean {
  return useRoomUiStore((s) => (s.rooms[roomId] ?? DEFAULT_ROOM_FLAGS).sseEnabled)
}

export function useLocalSendSeq(roomId: string): number {
  return useRoomUiStore(s => s.localSendSeqByRoom[roomId] ?? 0)
}

export function useInitialHydrationSeq(roomId: string): number {
  return useRoomUiStore(s => s.initialHydrationSeqByRoom[roomId] ?? 0)
}

export function useSelectedAgentMessageId(roomId: string): string | undefined {
  return useRoomUiStore(s => s.selectedAgentMessageIdByRoom[roomId])
}
