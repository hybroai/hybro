'use client'

import { useRef, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

const COPY_FAILED = 'Unable to copy. Select and copy the text manually.'

/** Copy buttons, their feedback, and selectable blocks that a failed copy selects. */
export function useCopy() {
  const [feedback, setFeedback] = useState<{ id: string; failed: boolean } | null>(null)
  const [copying, setCopying] = useState(false)
  const blocks = useRef<Record<string, HTMLElement | null>>({})

  async function copy(id: string, value: string) {
    if (copying) return
    setCopying(true)
    try {
      await navigator.clipboard.writeText(value)
      setFeedback({ id, failed: false })
    } catch {
      setFeedback({ id, failed: true })
      const block = blocks.current[id]
      if (block) {
        block.focus()
        const range = document.createRange()
        range.selectNodeContents(block)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
      }
    } finally {
      setCopying(false)
    }
  }

  const button = (id: string, value: string, label: string, primary = false) => (
    <Button variant={primary ? 'default' : 'secondary'} size="sm" disabled={copying} onClick={() => { void copy(id, value) }}>
      {feedback?.id === id && !feedback.failed ? <Check data-icon="inline-start" aria-hidden="true" /> : <Copy data-icon="inline-start" aria-hidden="true" />}
      {label}
    </Button>
  )
  const state = (id: string, label: string): ReactNode => {
    if (feedback?.id !== id) return null
    return feedback.failed
      ? <Alert variant="destructive"><AlertDescription>{COPY_FAILED}</AlertDescription></Alert>
      : <p role="status" className="text-xs text-muted-foreground">{`${label} copied`}</p>
  }
  const block = (id: string, label: string, text: string) => (
    <pre
      ref={element => { blocks.current[id] = element }}
      tabIndex={0}
      aria-label={label}
      className="min-w-0 select-text overflow-x-auto rounded-lg border bg-muted/40 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap wrap-anywhere"
    >
      {text}
    </pre>
  )
  return { button, state, block }
}

export type Copier = ReturnType<typeof useCopy>
