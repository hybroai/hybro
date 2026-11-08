// Form logic of the Configuration page. The backend validates again on save;
// these checks only catch typing mistakes and the bounds the field reports.
import type { FieldValue, SettingsField } from '@/lib/api/settings'

/**
 * What an input holds: switches keep a boolean, everything else the typed text.
 * `null` means "reset to default": the key is removed from config.json.
 */
export type DraftValue = boolean | string | null

/** The input's content for a value; `value` defaults to the saved one. */
export function toDraft(field: SettingsField, value: FieldValue = field.value): boolean | string {
  if (field.kind === 'boolean') return value === true
  return value === null ? '' : String(value)
}

export function parseDraft(field: SettingsField, draft: DraftValue): { value: FieldValue } | { error: string } {
  if (draft === null) return { value: null }
  if (field.kind === 'boolean') return { value: draft === true }
  const text = String(draft).trim()
  if (field.kind === 'enum' || field.kind === 'url') return { value: text }
  // An empty number is saved as null, which returns the field to its default.
  if (text === '') return { value: null }
  const number = Number(text)
  if (!Number.isFinite(number)) return { error: 'Enter a number.' }
  if (field.kind === 'integer' && !Number.isInteger(number)) return { error: 'Enter a whole number.' }
  if (field.exclusive_minimum !== null && number <= field.exclusive_minimum) return { error: `Must be greater than ${field.exclusive_minimum}.` }
  if (field.minimum !== null && number < field.minimum) return { error: `Must be at least ${field.minimum}.` }
  if (field.maximum !== null && number > field.maximum) return { error: `Must be at most ${field.maximum}.` }
  return { value: number }
}

export interface PendingChanges {
  /** Keys that differ from the saved value, valid or not, in field order. */
  keys: string[]
  /** Valid changes, ready for `saveSettings`. */
  values: Record<string, FieldValue>
  errors: Record<string, string>
  /** Changed keys that take effect only after a restart. */
  restartKeys: string[]
}

export function pendingChanges(fields: SettingsField[], drafts: Record<string, DraftValue>): PendingChanges {
  const changes: PendingChanges = { keys: [], values: {}, errors: {}, restartKeys: [] }
  for (const field of fields) {
    if (!(field.key in drafts)) continue
    const parsed = parseDraft(field, drafts[field.key])
    if ('error' in parsed) {
      changes.errors[field.key] = parsed.error
    } else if (parsed.value === field.value || (parsed.value === null && field.source === 'default')) {
      continue
    } else {
      changes.values[field.key] = parsed.value
    }
    changes.keys.push(field.key)
    if (field.apply === 'restart') changes.restartKeys.push(field.key)
  }
  return changes
}

export function formatValue(field: SettingsField, value: FieldValue): string {
  if (field.kind === 'boolean') return value ? 'On' : 'Off'
  if (value === null) return 'none'
  if (value === '') return 'empty'
  return String(value)
}
