import { getApiUrl } from '../utils'
import { publicConfig } from '../config'
import { requestHeaders } from '../log'

const API_BASE_URL = getApiUrl('files')

function getAuthenticatedFilePath(): string {
  const configuredPrefix = publicConfig().api_prefix
  const normalizedPrefix = `/${configuredPrefix.replace(/^\/+|\/+$/g, '')}`
  return `${normalizedPrefix}/files`
}

export interface FileUploadResponse {
  file_id: string
  file_url: string
  mime_type: string
  file_name: string
  size_bytes: number
}

const FILE_ID_PATTERN = /^[0-9a-f]{32}$/

export async function fetchRoomFileBlob(
  fileId: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<Blob> {
  if (!FILE_ID_PATTERN.test(fileId)) {
    throw new Error('Invalid file id')
  }
  const token = await getToken?.()
  const { headers } = requestHeaders()
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(`${getAuthenticatedFilePath()}/${fileId}/content`, {
    headers,
    signal,
  })
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}))
    const message = errorBody.detail || errorBody.error
      || `File download failed (${response.status})`
    throw new Error(message)
  }
  return response.blob()
}

export async function uploadFile(
  file: File,
  conversationId: string,
  getToken?: () => Promise<string | null>,
): Promise<FileUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('conversation_id', conversationId)

  const { headers } = requestHeaders()
  if (getToken) {
    const token = await getToken()
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    }
  }

  const response = await fetch(`${API_BASE_URL}/upload`, {
    method: 'POST',
    headers,
    body: formData,
  })

  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}))
    const message = errorBody.detail || errorBody.error || `Upload failed (${response.status})`
    throw new Error(message)
  }

  return response.json()
}
