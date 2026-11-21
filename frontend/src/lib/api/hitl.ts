import { apiPost, apiGet } from '../api-client'
import { getApiUrl } from '../utils'

function hitlUrl(conversationId: string): string {
  return getApiUrl(`rooms/${conversationId}/hitl`)
}

export interface HitlPendingRequest {
  request_id: string
  message_id: string
  source: 'agent' | 'supervisor' | 'system'
  agent_id?: string
  agent_name?: string
  prompt: string
  prompt_type: import('@/lib/types/sse').HITLPromptType
  choices?: string[] | null
  header?: string
  required?: boolean
  allow_other?: boolean
  choice_options?: { value: string; label?: string; description?: string }[]
  status: 'pending'
  expires_at?: string
  created_at?: string
  interaction_id?: string | null
  interaction_status?: string | null
  interaction_version?: number | null
  application_status?: string | null
  application_error?: string | null
  question_count?: number
  question_index?: number
  related_message_id?: string | null
  client_request_id?: string | null
}

export interface HitlRespondResponse {
  status: string
  request_id: string
  interaction_id?: string
  client_request_id?: string | null
}

export interface HitlBatchAnswer {
  requestId: string
  answer: string
}

export interface HitlPendingResponse {
  requests: HitlPendingRequest[]
}

const HITL_RESPOND_TIMEOUT_MS = 180_000

export async function respondToHitlBatch(
  conversationId: string,
  interactionId: string,
  answers: HitlBatchAnswer[],
  clientRequestId: string | undefined,
  getToken?: () => Promise<string | null>,
): Promise<HitlRespondResponse> {
  return apiPost<HitlRespondResponse>(`${hitlUrl(conversationId)}/respond-batch`, {
    interaction_id: interactionId,
    answers: answers.map(answer => ({
      request_id: answer.requestId,
      user_input: answer.answer,
    })),
    client_request_id: clientRequestId,
  }, getToken, undefined, HITL_RESPOND_TIMEOUT_MS)
}

export async function cancelHitl(
  conversationId: string,
  interactionId: string,
  interactionVersion: number,
  clientRequestId: string,
  getToken?: () => Promise<string | null>,
): Promise<{ status: string; interaction_id: string; interaction_version: number }> {
  return apiPost(`${hitlUrl(conversationId)}/interactions/${interactionId}/cancel`, {
    interaction_id: interactionId,
    expected_interaction_version: interactionVersion,
    client_request_id: clientRequestId,
  }, getToken)
}

export async function fetchPendingHitlRequests(
  conversationId: string,
  getToken?: () => Promise<string | null>,
): Promise<HitlPendingResponse> {
  return apiGet<HitlPendingResponse>(`${hitlUrl(conversationId)}/pending`, getToken)
}
