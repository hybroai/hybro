/**
 * Centralized API client with built-in authentication
 * Automatically handles auth headers for all API requests
 */

import { getClientAuthHeaders } from './auth'
import { log, requestHeaders } from './log'

/**
 * Custom error class for API responses that carries the HTTP status code.
 * Allows callers to distinguish between client errors (4xx) and server errors (5xx).
 */
export class ApiError extends Error {
  public readonly status: number
  public readonly details?: unknown

  constructor(status: number, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.details = details
  }

  /** True for 4xx status codes (client/validation errors) */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }

  /** True for 5xx status codes (server errors) */
  get isServerError(): boolean {
    return this.status >= 500
  }
}

interface ApiClientOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
  body?: unknown
  headers?: HeadersInit
  getToken?: () => Promise<string | null>
  signal?: AbortSignal  // Support for request cancellation
  timeoutMs?: number  // Optional per-request timeout
}

// Default timeout for API requests (increased to avoid premature aborts on slow endpoints)
const DEFAULT_TIMEOUT_MS = 60000

/**
 * Make an authenticated API request
 * Automatically includes authentication headers
 */
export async function apiClient<T = unknown>(
  url: string,
  options: ApiClientOptions = {}
): Promise<T> {
  const { method = 'GET', body, headers: customHeaders, getToken, signal, timeoutMs } = options

  // Get auth headers (will use default getToken if not provided)
  const authHeaders = await getClientAuthHeaders(getToken)

  // Merge auth headers with custom headers; every call gets its own X-Request-ID.
  const { headers: requestIdHeader, requestId } = requestHeaders()
  const headers = {
    ...authHeaders,
    ...customHeaders,
    ...requestIdHeader,
  }

  // Support cancellation and timeout
  const controller = new AbortController()
  const timeout = timeoutMs ?? DEFAULT_TIMEOUT_MS
  let timedOut = false
  const timeoutId = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeout)

  // Tie external signal if provided
  if (signal) {
    signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  const fetchOptions: RequestInit = {
    method,
    headers,
    signal: controller.signal,  // Pass abort signal to fetch
  }

  // Add body for non-GET requests
  if (body && method !== 'GET') {
    fetchOptions.body = JSON.stringify(body)
  }

  try {
    const response = await fetch(url, fetchOptions)
    clearTimeout(timeoutId)

    if (!response.ok) {
      const errorText = await response.text()
      let details: unknown
      try {
        details = JSON.parse(errorText)
      } catch {
        details = undefined
      }
      throw new ApiError(
        response.status,
        `HTTP error! status: ${response.status}, message: ${errorText}`,
        details,
      )
    }

    // 204 No Content has no body to parse.
    if (response.status === 204) return undefined as T
    return await response.json()
  } catch (error) {
    clearTimeout(timeoutId)

    // Preserve AbortError so callers can treat it as cancellation.
    if (error instanceof Error && error.name === 'AbortError') {
      if (timedOut) {
        log.error('api_request_timed_out', { request_id: requestId, method, url, timeout_ms: timeout })
      } else {
        log.debug('api_request_aborted', { request_id: requestId, method, url })
      }
      // Re-throw the original AbortError (do not wrap) so upstream can detect it.
      throw error
    }

    // Use warn for client errors (4xx) since they are expected validation issues,
    // and error for server errors (5xx) or unexpected failures. The error
    // message embeds the response body, so only the status and name are logged.
    if (error instanceof ApiError && error.isClientError) {
      log.warn('api_request_client_error', { request_id: requestId, method, url, status: error.status })
    } else {
      log.error('api_request_failed', {
        request_id: requestId,
        method,
        url,
        // Only the name: a SyntaxError from response.json() quotes the body.
        ...(error instanceof ApiError
          ? { status: error.status }
          : { error_name: error instanceof Error ? error.name : typeof error }),
      })
    }
    throw error
  }
}

/**
 * Convenience method for GET requests
 */
export async function apiGet<T = unknown>(
  url: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  timeoutMs?: number
): Promise<T> {
  return apiClient<T>(url, { method: 'GET', getToken, signal, timeoutMs })
}

/**
 * Convenience method for POST requests
 */
export async function apiPost<T = unknown>(
  url: string,
  body?: unknown,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  timeoutMs?: number
): Promise<T> {
  return apiClient<T>(url, { method: 'POST', body, getToken, signal, timeoutMs })
}

/**
 * Convenience method for PUT requests
 */
export async function apiPut<T = unknown>(
  url: string,
  body?: unknown,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  timeoutMs?: number
): Promise<T> {
  return apiClient<T>(url, { method: 'PUT', body, getToken, signal, timeoutMs })
}

/**
 * Convenience method for DELETE requests
 */
export async function apiDelete<T = unknown>(
  url: string,
  getToken?: () => Promise<string | null>,
  signal?: AbortSignal,
  timeoutMs?: number
): Promise<T> {
  return apiClient<T>(url, { method: 'DELETE', getToken, signal, timeoutMs })
}
