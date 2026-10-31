/**
 * Agent Group API Client
 */

import { getApiUrl } from '../utils'
import { apiDelete, apiGet, apiPost, apiPut } from '../api-client'
import type {
  AgentGroupCreateRequest,
  AgentGroupUpdateRequest,
  AgentGroupResponse,
  AgentGroupListResponse
} from '../types/agent-group'

const API_BASE_URL = getApiUrl('agentGroups')

/**
 * Create a new agent group
 */
export async function createAgentGroup(
  request: AgentGroupCreateRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentGroupResponse> {
  return apiPost<AgentGroupResponse>(
    `${API_BASE_URL}`,
    request,
    getToken
  )
}

/**
 * List all agent groups for a user (including built-in groups)
 */
export async function listAgentGroups(
  owner_id?: string,
  getToken?: () => Promise<string | null>
): Promise<AgentGroupListResponse> {
  // A relative same-origin base cannot go through `new URL`.
  const query = owner_id ? `?${new URLSearchParams({ owner_id })}` : ''

  return apiGet<AgentGroupListResponse>(
    `${API_BASE_URL}${query}`,
    getToken
  )
}

/**
 * Get a specific agent group by ID
 */
export async function getAgentGroup(
  group_id: string,
  getToken?: () => Promise<string | null>
): Promise<AgentGroupResponse> {
  return apiGet<AgentGroupResponse>(
    `${API_BASE_URL}/${group_id}`,
    getToken
  )
}

/**
 * Update an agent group
 */
export async function updateAgentGroup(
  request: AgentGroupUpdateRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentGroupResponse> {
  const { group_id, ...updateFields } = request
  return apiPut<AgentGroupResponse>(
    `${API_BASE_URL}/${group_id}`,
    updateFields,
    getToken
  )
}

/**
 * Delete an agent group
 */
export async function deleteAgentGroup(
  group_id: string,
  getToken?: () => Promise<string | null>
): Promise<{ success: boolean; error?: string; status_code?: number }> {
  return apiDelete<{ success: boolean; error?: string; status_code?: number }>(
    `${API_BASE_URL}/${group_id}`,
    getToken
  )
}

