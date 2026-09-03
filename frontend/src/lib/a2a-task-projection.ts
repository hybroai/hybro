/** Pure helpers for projecting persisted A2A task data into message state. */

import type { TaskState } from './types/sse'

export interface A2ATaskStatus {
  message_id: string
  status: TaskState
  task: {
    id: string
    contextId?: string
    status: {
      state: TaskState
      message?: {
        role: string
        parts: Array<{ text?: string }>
      }
      timestamp?: string
    }
    artifacts?: Array<{
      artifactId: string
      name: string
      parts: Array<{ text?: string }>
    }>
    history?: Array<{
      role: string
      parts: Array<{ text?: string }>
    }>
  }
  agent_name?: string
  agent_id?: string
  related_message_id?: string | null
  created_at: string
  updated_at: string
  retry_after_seconds: number | null
}

/**
 * Extract text content from task artifacts.
 */
export function extractTaskContent(task: A2ATaskStatus['task']): string | undefined {
  const texts: string[] = []

  if (task.artifacts) {
    for (const artifact of task.artifacts) {
      for (const part of artifact.parts || []) {
        // Handle both direct text and root-wrapped text (Pydantic RootModel)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const anyPart = part as any
        const text = anyPart.text || anyPart.root?.text
        if (text) {
          texts.push(text)
        }
      }
    }
  }

  return texts.length > 0 ? texts.join('') : undefined
}

/**
 * Extract error message from task status.
 */
export function extractTaskError(task: A2ATaskStatus['task']): string | undefined {
  const parts = task.status?.message?.parts
  if (!parts || parts.length === 0) return undefined
  // Handle both direct text and root-wrapped text (Pydantic RootModel)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const anyPart = parts[0] as any
  return anyPart.text || anyPart.root?.text
}
