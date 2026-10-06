'use client'

import { useEffect, useState } from 'react'
import { Copy, Plug, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/dialog'
import { MCP_CONFIG, MCP_URL, parseMcpStatus, type McpStatus } from '@/lib/mcp'

const statusLabels = {
  checking: 'Checking…',
  ready: 'Ready',
  unavailable: 'Unavailable',
  unsupported_auth: 'Unavailable with Clerk',
  error: 'Unable to check status'
}

export function McpConnectionDialog() {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<McpStatus | 'checking' | 'error'>('checking')
  const [refresh, setRefresh] = useState(0)
  const [copyMessage, setCopyMessage] = useState('')
  const [copyFailed, setCopyFailed] = useState(false)

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    const timeout = setTimeout(() => {
      setStatus('error')
      controller.abort()
    }, 6000)
    async function checkStatus() {
      try {
        const response = await fetch('/hybro-mcp', { cache: 'no-store', signal: controller.signal })
        if (!response.ok) throw new Error('Status unavailable')
        const body: unknown = await response.json()
        if (!controller.signal.aborted) setStatus(parseMcpStatus(body))
      } catch {
        if (!controller.signal.aborted) setStatus('error')
      } finally {
        clearTimeout(timeout)
      }
    }
    void checkStatus()
    return () => {
      clearTimeout(timeout)
      controller.abort()
    }
  }, [open, refresh])

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopyFailed(false)
      setCopyMessage(`${label} copied`)
    } catch {
      setCopyFailed(true)
      setCopyMessage('Unable to copy. Select and copy the text manually.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => {
      setOpen(next)
      setStatus('checking')
      setCopyMessage('')
    }}>
      <DialogTrigger asChild>
        <Button variant="outline" className="w-full">
          <Plug data-icon="inline-start" aria-hidden="true" />
          Connect MCP
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-md" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle>MCP Connection</DialogTitle>
        </DialogHeader>
        <dl className="flex min-w-0 flex-col gap-4 text-sm">
          <div className="flex items-center justify-between gap-3">
            <dt className="text-muted-foreground">Status</dt>
            <dd className="flex items-center gap-2">
              <span role="status">{statusLabels[status]}</span>
              <Button variant="ghost" size="icon" aria-label="Refresh status" disabled={status === 'checking'} onClick={() => {
                setStatus('checking')
                setRefresh((value) => value + 1)
              }}>
                <RefreshCw aria-hidden="true" />
              </Button>
            </dd>
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            <dt className="text-muted-foreground">Server URL</dt>
            <dd><code className="select-text break-all">{MCP_URL}</code></dd>
          </div>
        </dl>
        {status === 'unavailable' ? (
          <p className="text-sm text-muted-foreground">Start services or check MCP logs in the Hybro TUI.</p>
        ) : null}
        {status === 'unsupported_auth' ? (
          <p className="text-sm text-muted-foreground">This local adapter does not support Clerk authentication.</p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => { void copy(MCP_URL, 'URL') }}>
            <Copy data-icon="inline-start" aria-hidden="true" />
            Copy URL
          </Button>
          <Button variant="outline" onClick={() => { void copy(MCP_CONFIG, 'Configuration') }}>
            <Copy data-icon="inline-start" aria-hidden="true" />
            Copy configuration
          </Button>
        </div>
        <details className="min-w-0 text-sm">
          <summary className="cursor-pointer text-muted-foreground">Configuration</summary>
          <pre className="mt-2 overflow-x-auto rounded-md bg-muted p-3 text-xs" tabIndex={0}>{MCP_CONFIG}</pre>
        </details>
        {copyMessage ? (
          <p role={copyFailed ? 'alert' : 'status'} className="text-sm text-muted-foreground">{copyMessage}</p>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}
