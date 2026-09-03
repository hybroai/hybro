// Room-related API functions
import type { 
  RoomCenterRoomSettingResponse, 
  RoomCenterActiveRunsResponse,
  RoomCenterUserMessageResponse,
  RoomCenterRoomMessageResponse
} from '@/lib/types/response'
import type {
  RoomCenterRoomSettingRequest,
  RoomCenterRoomMessageRequest,
  SendMessagePayload,
  AgentScopeInput,
  ExecutionMode,
} from '@/lib/types/request'
import { type RoomMembershipWriteInput } from '@/lib/types/agent-group'
import type { RoomQuoteWire } from '@/lib/types/quote'

import { getApiUrl } from '../utils'
import { apiClient, apiPost } from '../api-client'

const API_BASE_URL = getApiUrl('roomCenter')

export type RoomHistoryStatus = 'idle' | 'queued' | 'processing' | 'awaiting_input'

export interface RoomHistoryItem {
  room_id: string
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
  return apiClient<RoomHistoryResponse>(getApiUrl('roomCenter/history'), {
    getToken,
    signal,
  })
}

export function updateRoomHistoryItem(
  roomId: string,
  update: { title?: string; is_pinned?: boolean },
  getToken?: () => Promise<string | null>,
): Promise<RoomHistoryItem> {
  return apiClient<RoomHistoryItem>(getApiUrl(`roomCenter/history/${roomId}`), {
    method: 'PATCH',
    body: update,
    getToken,
  })
}

export function reorderPinnedRooms(
  roomIds: string[],
  getToken?: () => Promise<string | null>,
): Promise<{ success: boolean }> {
  return apiClient<{ success: boolean }>(getApiUrl('roomCenter/history/pinned-order'), {
    method: 'PUT',
    body: { room_ids: roomIds },
    getToken,
  })
}

export function deleteRoomHistoryItem(
  roomId: string,
  getToken?: () => Promise<string | null>,
): Promise<{ success: boolean }> {
  return apiClient<{ success: boolean }>(getApiUrl(`roomCenter/history/${roomId}`), {
    method: 'DELETE',
    getToken,
  })
}

export interface CreateRoomParams {
  room_name: string
  room_owner_id: string
  room_owner_name: string
  getToken?: () => Promise<string | null>
  extend_info?: { [k: string]: unknown } | null
  membership?: RoomMembershipWriteInput
  /** @deprecated Use membership instead. */
  room_agent_set?: { [k: string]: string }
  /** @deprecated Use membership.seed_group_id instead. */
  applied_from_group?: string
}

export async function createNewRoom(
  room_name: string,
  room_owner_id: string,
  room_owner_name: string,
  getToken?: () => Promise<string | null>,
  room_agent_set?: { [k: string]: string },
  extend_info?: { [k: string]: unknown } | null,
  applied_from_group?: string,
  membership?: RoomMembershipWriteInput,
): Promise<RoomCenterRoomSettingResponse> {
  const requestData: RoomCenterRoomSettingRequest = {
    room_name,
    room_owner_id,
    room_owner_name,
    extend_info,
    // Legacy fields (still sent during rollout)
    room_agent_set,
    applied_from_group,
  }

  // Overlay canonical membership fields when provided
  if (membership) {
    requestData.membership_seed_input = membership.membership_seed_input
    if ('room_agent_ids' in membership) {
      requestData.room_agent_ids = membership.room_agent_ids
    }
    if ('seed_group_id' in membership) {
      requestData.seed_group_id = membership.seed_group_id
    }
    if ('seed_all_current_agents' in membership) {
      requestData.seed_all_current_agents = membership.seed_all_current_agents
    }
  }

  return apiPost<RoomCenterRoomSettingResponse>(
    `${API_BASE_URL}/createNewRoom`,
    requestData,
    getToken
  )
}

// Inquiry room setting
export async function inquiryRoomSetting(
  room_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal
): Promise<RoomCenterRoomSettingResponse> {
  const requestData: RoomCenterRoomSettingRequest = {
    room_id
  }

  return apiPost<RoomCenterRoomSettingResponse>(
    `${API_BASE_URL}/inquiryRoomSetting`,
    requestData,
    getToken,
    signal
  )
}

export async function inquiryActiveRuns(
  room_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  trigger_message_id?: string,
): Promise<RoomCenterActiveRunsResponse> {
  const requestData: RoomCenterRoomSettingRequest & { trigger_message_id?: string } = {
    room_id,
    ...(trigger_message_id ? { trigger_message_id } : {}),
  }

  return apiPost<RoomCenterActiveRunsResponse>(
    `${API_BASE_URL}/inquiryActiveRuns`,
    requestData,
    getToken,
    signal
  )
}

export async function updateRoomAgentSet(
  room_id: string,
  room_agent_set: { [k: string]: string },
  getToken?: () => Promise<string | null>,
  membership?: RoomMembershipWriteInput,
): Promise<RoomCenterRoomSettingResponse> {
  const requestData: RoomCenterRoomSettingRequest = {
    room_id,
    room_agent_set,
  }

  if (membership) {
    requestData.membership_seed_input = membership.membership_seed_input
    if ('room_agent_ids' in membership) {
      requestData.room_agent_ids = membership.room_agent_ids
    }
    if ('seed_group_id' in membership) {
      requestData.seed_group_id = membership.seed_group_id
    }
    if ('seed_all_current_agents' in membership) {
      requestData.seed_all_current_agents = membership.seed_all_current_agents
    }
  }

  return apiPost<RoomCenterRoomSettingResponse>(
    `${API_BASE_URL}/updateRoomAgentSet`,
    requestData,
    getToken
  )
}

// Update room name
export async function updateRoomName(
  room_id: string,
  room_name: string,
  getToken?: () => Promise<string | null>
): Promise<RoomCenterRoomSettingResponse> {
  const requestData: RoomCenterRoomSettingRequest = {
    room_id,
    room_name
  }

  return apiPost<RoomCenterRoomSettingResponse>(
    `${API_BASE_URL}/updateRoomName`,
    requestData,
    getToken
  )
}

// Query room messages
export interface RoomTimelinePagination {
  limit?: number
  cursor?: string | null
}

export async function inquiryRoomMessagesByRoomId(
  room_id: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  pagination?: RoomTimelinePagination
): Promise<RoomCenterRoomMessageResponse> {
  const requestData: RoomCenterRoomMessageRequest = {
    room_id,
    ...(pagination?.limit !== undefined ? { limit: pagination.limit } : {}),
    ...(pagination?.cursor !== undefined ? { cursor: pagination.cursor } : {})
  }

  return apiPost<RoomCenterRoomMessageResponse>(
    `${API_BASE_URL}/inquiryRoomMessagesByRoomId`,
    requestData,
    getToken,
    signal
  )
}


export interface SendMessageParams {
  roomId: string
  userInput: string
  getToken?: () => Promise<string | null>
  userId?: string
  userName?: string
  relatedMessageId?: string | null
  quotedText?: string | null
  quotedSenderName?: string | null
  attachments?: Array<{ file_id: string }>
  mode: ExecutionMode
  agentScope: AgentScopeInput
  clientRequestId: string
  structuredQuote?: RoomQuoteWire | null
}

type SendMessageRequestBody = SendMessagePayload & {
  room_id: string
  user_id: string
  user_name: string
  user_input: string
  attachments?: Array<{ file_id: string }>
}

export async function SendMessage(params: SendMessageParams): Promise<RoomCenterUserMessageResponse> {
  const {
    roomId,
    userInput,
    getToken,
    userId,
    userName,
    relatedMessageId,
    quotedText,
    quotedSenderName,
    attachments,
    mode,
    agentScope,
    clientRequestId,
    structuredQuote,
  } = params

  const message: Record<string, unknown> = {
    room_id: roomId,
    message_id: "",
    message_type: "user",
    related_message_id: relatedMessageId || null,
    message_content: {
      message_text: userInput
    },
    user_id: userId || "",
    extend_info: null as Record<string, unknown> | null,
  }

  if (structuredQuote) {
    message.quote = structuredQuote
    message.extend_info = {
      quoted_text: structuredQuote.text,
      quoted_sender_name: structuredQuote.sender_display_name ?? null,
    }
  } else if (quotedText) {
    message.extend_info = { quoted_text: quotedText, quoted_sender_name: quotedSenderName || null }
  }

  const requestData: SendMessageRequestBody = {
    room_id: roomId,
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
      `${API_BASE_URL}/sendMessage`,
      requestData,
      getToken
    )
    return result
  } catch (error) {
    console.error('SendMessage API Error:', error)
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
    `${API_BASE_URL}/suggestAgents`,
    { message_text, top_k },
    getToken
  )
}
