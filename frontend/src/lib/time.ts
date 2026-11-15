/**
 * Timestamp utilities for consistent handling across the frontend.
 * Mirrors backend common/utils/time.py patterns.
 */

/**
 * Normalize a timestamp string to ISO 8601 format with UTC timezone.
 * Handles various input formats:
 * - ISO 8601 with/without timezone
 * - Space-separated datetime (e.g., "2024-01-15 10:30:00")
 * - Missing timezone (assumes UTC)
 *
 * @param value - The timestamp string to normalize
 * @param fallbackToNow - If true, returns current time for invalid/missing input (default: true)
 * @returns ISO 8601 string in UTC, or null if invalid and fallbackToNow is false
 */
function normalizeTimestamp(
  value?: string | null,
  fallbackToNow: boolean = true
): string | null {
  if (!value) {
    return fallbackToNow ? new Date().toISOString() : null
  }

  const trimmed = value.trim()

  // Check if already has timezone info (Z, +HH:MM, -HH:MM)
  const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(trimmed)

  // Ensure 'T' separator between date and time
  const withT = trimmed.includes('T') ? trimmed : trimmed.replace(' ', 'T')

  // Add UTC timezone if missing
  const candidate = hasZone ? withT : `${withT}Z`

  const parsed = new Date(candidate)

  if (Number.isNaN(parsed.getTime())) {
    return fallbackToNow ? new Date().toISOString() : null
  }

  return parsed.toISOString()
}

/**
 * Normalize a timestamp string, always returning a string (never null).
 * Convenience wrapper for cases where a fallback to current time is always desired.
 *
 * @param value - The timestamp string to normalize
 * @returns ISO 8601 string in UTC
 */
export function normalizeTimestampOrNow(value?: string | null): string {
  return normalizeTimestamp(value, true) as string
}

/**
 * Parse a timestamp string to a Date object with validation.
 * Uses normalizeTimestamp internally for consistent parsing.
 *
 * @param value - The timestamp string to parse
 * @returns Date object, or null if invalid
 */
function parseTimestamp(value?: string | null): Date | null {
  const normalized = normalizeTimestamp(value, false)
  return normalized ? new Date(normalized) : null
}

/**
 * Check if a timestamp is stale (older than threshold).
 *
 * @param timestamp - The timestamp to check
 * @param thresholdMs - Staleness threshold in milliseconds (default: 10 minutes)
 * @returns true if timestamp is older than threshold, or if timestamp is invalid/missing
 */
export function isStale(
  timestamp?: string | null,
  thresholdMs: number = 10 * 60 * 1000
): boolean {
  const parsed = parseTimestamp(timestamp)
  if (!parsed) return true // Treat missing/invalid as stale

  return Date.now() - parsed.getTime() > thresholdMs
}
