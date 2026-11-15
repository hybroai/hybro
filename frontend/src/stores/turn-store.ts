import { create } from 'zustand'
import { useShallow } from 'zustand/react/shallow'
import type { CanonicalFoldEvent, TurnProjectionMap } from '@/lib/turn-lifecycle/fold'
import {
  foldCanonicalEvent,
  snapshotTurnToProjection,
  validateProjectionClosure,
} from '@/lib/turn-lifecycle/fold'
import type { RoomSnapshotTurn, TurnProjection } from '@/lib/turn-lifecycle/types'
import { useTurnPresentationStore } from './turn-presentation-store'

interface CanonicalRoomState {
  turns: TurnProjectionMap
  order: string[]
  protocolViolation?: string
}

interface TurnStore {
  rooms: Record<string, CanonicalRoomState>
  replaceSnapshot: (conversationId: string, turns: RoomSnapshotTurn[]) => { ok: true } | { ok: false; violation: string }
  applyEvent: (conversationId: string, event: CanonicalFoldEvent) => { handled: true; ok: boolean; violation?: string }
  clearRoom: (conversationId: string) => void
  clear: () => void
}

const emptyRoom = (): CanonicalRoomState => ({ turns: {}, order: [] })

export const useTurnStore = create<TurnStore>((set, get) => ({
  rooms: {},
  replaceSnapshot: (conversationId, snapshotTurns) => {
    const turns: TurnProjectionMap = {}
    const order: string[] = []
    for (const value of snapshotTurns) {
      const turn = snapshotTurnToProjection(conversationId, value)
      const violation = validateProjectionClosure(turn)
      if (violation) return { ok: false as const, violation }
      turns[turn.id] = turn
      order.push(turn.id)
    }
    set((state) => ({
      rooms: {
        ...state.rooms,
        [conversationId]: { turns, order },
      },
    }))
    const presentation = useTurnPresentationStore.getState()
    for (const turnId of order) presentation.ensure(turns[turnId], true)
    return { ok: true as const }
  },
  applyEvent: (conversationId, event) => {
    const current = get().rooms[conversationId] ?? emptyRoom()
    const result = foldCanonicalEvent(current.turns, conversationId, event)
    if (!result.ok) {
      set((state) => ({
        rooms: {
          ...state.rooms,
          [conversationId]: { ...current, protocolViolation: result.violation },
        },
      }))
      return { handled: true as const, ok: false, violation: result.violation }
    }
    if (!result.changed) return { handled: true as const, ok: true }
    const nextIds = Object.keys(result.turns)
    const order = [
      ...current.order.filter((id) => nextIds.includes(id)),
      ...nextIds.filter((id) => !current.order.includes(id)),
    ]
    set((state) => ({
      rooms: {
        ...state.rooms,
        [conversationId]: {
          turns: result.turns,
          order,
        },
      },
    }))
    for (const id of order) {
      if (!current.turns[id]) useTurnPresentationStore.getState().ensure(result.turns[id], false)
    }
    return { handled: true as const, ok: true }
  },
  clearRoom: (conversationId) => set((state) => {
    const rooms = { ...state.rooms }
    delete rooms[conversationId]
    return { rooms }
  }),
  clear: () => set({ rooms: {} }),
}))

export function useCanonicalTurns(conversationId: string): TurnProjection[] {
  return useTurnStore(useShallow((state) => {
    const room = state.rooms[conversationId]
    if (!room) return []
    return room.order.map((id) => room.turns[id]).filter(Boolean)
  }))
}

export interface CanonicalComposerAuthority {
  authoritative: boolean
  normalComposerBlocked: boolean
  awaitingInput: boolean
  processing: boolean
}

export function selectCanonicalComposerAuthority(room: CanonicalRoomState | undefined): CanonicalComposerAuthority {
  const current = room ?? emptyRoom()
  const unsettled = current.order.map((id) => current.turns[id]).filter((turn) => (
    turn && (turn.state === 'active' || turn.state === 'awaiting_input')
  ))
  const awaitingInput = unsettled.some((turn) => turn.state === 'awaiting_input')
  return {
    authoritative: true,
    normalComposerBlocked: unsettled.length > 0,
    awaitingInput,
    processing: unsettled.some((turn) => turn.state === 'active'),
  }
}

export function useCanonicalComposerAuthority(conversationId: string): CanonicalComposerAuthority {
  return useTurnStore(useShallow((state) => selectCanonicalComposerAuthority(state.rooms[conversationId])))
}

export function isCanonicalRoot(
  conversationId: string,
  clientRequestId: string | null | undefined,
  userMessageId: string | null | undefined,
): boolean {
  if (!clientRequestId || !userMessageId) return false
  const room = useTurnStore.getState().rooms[conversationId]
  return Boolean(room && Object.values(room.turns).some((turn) => (
    turn.clientRequestId === clientRequestId && turn.userMessageId === userMessageId
  )))
}
