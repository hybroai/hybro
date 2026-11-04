// Room-related API functions
import type { 
  RoomCenterRoomSettingResponse, 
  RoomCenterActiveRunsResponse,
  RoomCenterUserMessageResponse,
  RoomCenterRoomMessageResponse
} from '@/lib/types/response'
import type {
  CreateRoomRequest,
  SendMessagePayload,
  AgentScopeInput,
  ExecutionMode,
} from '@/lib/types/request'
import { type RoomMembershipWriteInput } from '@/lib/types/agent-group'
import type { RoomQuoteWire } from '@/lib/types/quote'

import { getApiUrl } from '../utils'
import { apiClient, apiGet, apiPost } from '../api-client'
import { log } from '@/lib/log'

const ROOMS_URL = getApiUrl('rooms')

function roomUrl(conversationId: string, suffix = ''): string {
  return `${ROOMS_URL}/${encodeURIComponent(conversationId)}${suffix}`
}

export type RoomHistoryStatus = 'idle' | 'queued' | 'processing' | 'awaiting_input'

export interface RoomHistoryItem {
  conversation_id: string
  title: string
  last_activity_at: string
  is_pinned: boolean
  pin_order: number | null
  status: RoomHistoryStatus
}

export interface RoomHistoryResponse {
  items: RoomHistoryItem[]
}

export function listRoomHistory(
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<RoomHistoryResponse> {
  return apiClient<RoomHistoryResponse>(ROOMS_URL, {
    getToken,
    signal,
  })
}

export function updateRoomHistoryItem(
  conversationId: string,
  update: { title?: string; is_pinned?: boolean },
  getToken?: () => Promise<string | null>,
): Promise<RoomHistoryItem> {
  return apiClient<RoomHistoryItem>(roomUrl(conversationId), {
    method: 'PATCH',
    body: update,
    getToken,
  })
}

export function reorderPinnedRooms(
  conversationIds: string[],
  getToken?: () => Promise<string | null>,
): Promise<{ success: boolean }> {
  return apiClient<{ success: boolean }>(`${ROOMS_URL}/pinned-order`, {
    method: 'PUT',
    body: { conversation_ids: conversationIds },
    getToken,
  })
}

export function deleteRoomHistoryItem(
  conversationId: string,
  getToken?: () => Promise<string | null>,
): Promise<{ success: boolean }> {
  return apiClient<{ success: boolean }>(roomUrl(conversationId), {
    method: 'DELETE',
    getToken,
  })
}

export async function createNewRoom(
  room_name: string,
  room_owner_name: string,
  getToken?: () => Promise<string | null>,
  extend_info?: { [k: string]: unknown } | null,
  membership?: RoomMembershipWriteInput,
): Promise<RoomCenterRoomSettingResponse> {
  const requestData: CreateRoomRequest = {
    room_name,
    room_owner_name,
    extend_info,
    ...membership,
  }

  return apiPost<RoomCenterRoomSettingResponse>(ROOMS_URL, requestData, getToken)
}

// Inquiry room setting
export async function inquiryRoomSetting(
  conversation_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal
): Promise<RoomCenterRoomSettingResponse> {
  return apiGet<RoomCenterRoomSettingResponse>(roomUrl(conversation_id), getToken, signal)
}

export async function inquiryActiveRuns(
  conversation_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<RoomCenterActiveRunsResponse> {
  return apiGet<RoomCenterActiveRunsResponse>(
    roomUrl(conversation_id, '/active-runs'),
    getToken,
    signal
  )
}

// Query room messages
export interface RoomTimelinePagination {
  limit?: number
  cursor?: string | null
}

export async function inquiryRoomMessagesByConversationId(
  conversation_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  pagination?: RoomTimelinePagination
): Promise<RoomCenterRoomMessageResponse> {
  const query = new URLSearchParams()
  if (pagination?.limit !== undefined) query.set('limit', String(pagination.limit))
  if (pagination?.cursor) query.set('cursor', pagination.cursor)
  const search = query.toString()

  return apiGet<RoomCenterRoomMessageResponse>(
    roomUrl(conversation_id, `/messages${search ? `?${search}` : ''}`),
    getToken,
    signal
  )
}


export interface SendMessageParams {
  conversationId: string
  userInput: string
  getToken?: () => Promise<string | null>
  userId?: string
  userName?: string
  attachments?: Array<{ file_id: string }>
  mode: ExecutionMode
  agentScope: AgentScopeInput
  clientRequestId: string
  /** The only way to send a quote; the backend derives the display fields. */
  structuredQuote?: RoomQuoteWire | null
}

type SendMessageRequestBody = SendMessagePayload & {
  user_id: string
  user_name: string
  user_input: string
  attachments?: Array<{ file_id: string }>
}

export async function SendMessage(params: SendMessageParams): Promise<RoomCenterUserMessageResponse> {
  const {
    conversationId,
    userInput,
    getToken,
    userId,
    userName,
    attachments,
    mode,
    agentScope,
    clientRequestId,
    structuredQuote,
  } = params

  const message: Record<string, unknown> = {
    conversation_id: conversationId,
    message_id: "",
    message_type: "user",
    related_message_id: null,
    message_content: {
      message_text: userInput
    },
    user_id: userId || "",
    extend_info: null,
  }

  if (structuredQuote) {
    message.quote = structuredQuote
  }

  const requestData: SendMessageRequestBody = {
    user_id: userId || "",
    user_name: userName || "",
    user_input: userInput,
    message,
    client_request_id: clientRequestId,
    mode,
    agent_scope: agentScope,
  }

  if (attachments && attachments.length > 0) {
    requestData.attachments = attachments
  }

  try {
    const result = await apiPost<RoomCenterUserMessageResponse>(
      roomUrl(conversationId, '/messages'),
      requestData,
      getToken
    )
    return result
  } catch (error) {
    log.error('send_request_failed', { conversation_id: conversationId, client_request_id: clientRequestId, error })
    throw error
  }
}

// Agent suggestion response type
export interface SuggestAgentsResponse {
  success: boolean
  routing_strategy?: "single" | "parallel" | "sequential"
  reasoning?: string
  needs_debate?: boolean
  suggested_agents?: Array<{
    agent_id: string
    name: string
    reason: string
  }>
  error?: string
  status_code?: number
}

// Suggest agents for a message (preview for "All Agents" group)
export async function suggestAgents(
  message_text: string,
  top_k: number = 3,
  getToken?: () => Promise<string | null>
): Promise<SuggestAgentsResponse> {
  return apiPost<SuggestAgentsResponse>(
    getApiUrl('agent-suggestions'),
    { message_text, top_k },
    getToken
  )
}
