/**
 * Browser logger: one structured event per call, a bounded in-memory ring
 * buffer, and per-HTTP-call request ids for backend correlation.
 *
 * Events are stable snake_case names (`room_hydration_failed`). Fields must
 * be flat primitives (ids, counts, reasons, flags); they are stored as given.
 * The only reduced values are Errors (under any key) and non-Error values
 * under `error`. Never put message text, tokens, or request/response bodies
 * in fields.
 *
 * The level comes from `NODE_ENV` only: `debug` is buffered but not printed
 * in production; other levels always print. This module is the only place in
 * `src/` that writes to `console`.
 */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'
export type LogFields = Record<string, unknown>

export interface LogEntry {
  ts: string
  level: LogLevel
  event: string
  fields: LogFields
}

export interface SerialisedError {
  name: string
  status?: number
  stack?: string
}

const RING_CAPACITY = 500
const MAX_ERROR_STRING = 200

// Module-level ring buffer; read through recentLogs(). No consumer yet.
const ring: LogEntry[] = []

function isProduction(): boolean {
  return process.env.NODE_ENV === 'production'
}

function hasStatus(error: Error): error is Error & { status: number } {
  return typeof (error as { status?: unknown }).status === 'number'
}

// A V8 stack frame line: leading whitespace, then "at ".
const FRAME_LINE = /^\s+at \S/
// The constructor name V8 writes at the start of a stack header. For a
// subclass such as ApiError it is the name at construction time ("Error").
const HEADER_NAME = /^[A-Za-z_$][\w$]*/

/**
 * The V8 frame lines of a stack, or undefined. The header is
 * `<name>: <message>` (just `<name>` for an empty message) and spans
 * 1 + (newlines in message) lines, which are skipped exactly, so a message
 * that imitates frames never survives. A stack whose header does not match
 * the message (rewritten or non-V8) is dropped; after the header only lines
 * matching the frame pattern are kept.
 */
function stackFrames(error: Error): string | undefined {
  const stack = error.stack
  if (!stack) return undefined
  const name = HEADER_NAME.exec(stack)?.[0]
  if (!name) return undefined
  const header = error.message ? `${name}: ${error.message}` : name
  if (stack !== header && !stack.startsWith(`${header}\n`)) return undefined
  const headerLines = 1 + (error.message.match(/\n/g)?.length ?? 0)
  const frames = stack.split('\n').slice(headerLines).filter((line) => FRAME_LINE.test(line))
  return frames.length > 0 ? frames.join('\n') : undefined
}

/**
 * Error messages are never logged: HTTP errors embed the response body, JSON
 * parse errors quote it, and wrappers such as `new Error(response.error)`
 * carry server text. An Error keeps `name` (plus a numeric `status`), and
 * outside production the frame lines of its stack.
 */
function serialiseError(error: Error): SerialisedError {
  const serialised: SerialisedError = { name: error.name }
  if (hasStatus(error)) serialised.status = error.status
  if (!isProduction()) {
    const stack = stackFrames(error)
    if (stack) serialised.stack = stack
  }
  return serialised
}

/**
 * Non-Error values thrown or rejected are untrusted: keep a bounded string for
 * primitives and only the type for objects, never a live reference.
 */
function serialiseThrown(value: unknown): unknown {
  if (value instanceof Error) return serialiseError(value)
  if (value === null || value === undefined) return value
  if (typeof value === 'object' || typeof value === 'function') return { type: typeof value }
  return String(value).slice(0, MAX_ERROR_STRING)
}

function serialiseFields(fields: LogFields): LogFields {
  const out: LogFields = {}
  for (const [key, value] of Object.entries(fields)) {
    out[key] = value instanceof Error || key === 'error' ? serialiseThrown(value) : value
  }
  return out
}

function write(level: LogLevel, event: string, fields?: LogFields): void {
  const entry: LogEntry = {
    ts: new Date().toISOString(),
    level,
    event,
    fields: fields ? serialiseFields(fields) : {},
  }
  ring.push(entry)
  if (ring.length > RING_CAPACITY) ring.splice(0, ring.length - RING_CAPACITY)

  if (level === 'debug' && isProduction()) return
  if (fields) {
    console[level](event, entry.fields)
  } else {
    console[level](event)
  }
}

export const log = {
  debug: (event: string, fields?: LogFields): void => write('debug', event, fields),
  info: (event: string, fields?: LogFields): void => write('info', event, fields),
  warn: (event: string, fields?: LogFields): void => write('warn', event, fields),
  error: (event: string, fields?: LogFields): void => write('error', event, fields),
}

const TOKEN = /^[a-z0-9_.-]{1,64}$/i

/**
 * A server- or wire-supplied identifier (frame type, error type) when it is a
 * short token, else `'invalid'`, so free text never reaches a field.
 */
export function logToken(value: unknown): string {
  return typeof value === 'string' && TOKEN.test(value) ? value : 'invalid'
}

/** The buffered entries, oldest first (a copy). */
export function recentLogs(): readonly LogEntry[] {
  return ring.slice()
}

/**
 * A per-HTTP-call id: 32 lowercase hex characters. Uses
 * `crypto.getRandomValues`, which, unlike `crypto.randomUUID`, also exists on
 * plain-http non-loopback origins.
 */
export function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** Headers carrying a fresh `X-Request-ID`, merged with `extra`, and that id. */
export function requestHeaders(
  extra?: Record<string, string>,
): { headers: Record<string, string>; requestId: string } {
  const requestId = newRequestId()
  return { headers: { 'X-Request-ID': requestId, ...extra }, requestId }
}
