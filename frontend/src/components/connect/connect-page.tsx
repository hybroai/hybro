'use client'

import { useRef, useState, type ReactNode } from 'react'
import { useRouter } from 'next/navigation'
import { Check, Copy } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { publicConfig } from '@/lib/config'
import { mcpUrl } from '@/lib/mcp'
import { routes } from '@/lib/routes'
import { usePageOrigin } from '@/hooks/usePageOrigin'
import { buildAccessMaterial, type AccessKind } from './access-material'
import { clientSetups } from './client-setups'

const COPY_FAILED = 'Unable to copy. Select and copy the text manually.'

function useCopy() {
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

function Features({ items }: { items: { title: string; detail: string; tool?: string }[] }) {
  return (
    <section aria-labelledby="features-heading" className="flex flex-col gap-3">
      <h2 id="features-heading" className="text-[15px] font-semibold">Features</h2>
      <ul className="grid gap-3 sm:grid-cols-2">
        {items.map(item => (
          <li key={item.title} className="rounded-xl border bg-card p-4">
            <p className="text-sm font-medium">{item.title}</p>
            {item.tool ? <code className="mt-1 inline-block font-mono text-xs text-muted-foreground">{item.tool}</code> : null}
            <p className="mt-1 text-sm text-muted-foreground">{item.detail}</p>
          </li>
        ))}
      </ul>
    </section>
  )
}

function AddressCard({ label, url, copy }: { label: string; url: string; copy: ReturnType<typeof useCopy> }) {
  return (
    <section aria-label={label} className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <p className="text-sm font-medium">{label}</p>
      <div className="flex flex-wrap items-center gap-3">
        <code className="min-w-0 break-all rounded-lg border bg-muted/40 px-3 py-2 font-mono text-sm">{url}</code>
        {copy.button('url', url, 'Copy address', true)}
        {copy.state('url', 'Address')}
      </div>
    </section>
  )
}

function McpPanel() {
  if (publicConfig().mcp_disabled) {
    return (
      <Alert>
        <AlertDescription>MCP is disabled on public listeners until MCP authentication exists. Connect from the machine that runs Hybro, or use the API.</AlertDescription>
      </Alert>
    )
  }
  return <McpSetup />
}

function McpSetup() {
  const copy = useCopy()
  const url = mcpUrl(usePageOrigin())
  const setups = clientSetups(url)
  return (
    <div className="flex flex-col gap-8">
      <AddressCard label="MCP address" url={url} copy={copy} />
      <section aria-labelledby="setup-heading" className="flex flex-col gap-3">
        <h2 id="setup-heading" className="text-[15px] font-semibold">Add Hybro to your assistant</h2>
        <Tabs defaultValue={setups[0].id} className="gap-3">
          <TabsList aria-label="Assistants" className="h-auto max-w-full flex-wrap justify-start">
            {setups.map(setup => (
              <TabsTrigger key={setup.id} value={setup.id} className="min-h-9 flex-none">{setup.label}</TabsTrigger>
            ))}
          </TabsList>
          {setups.map(setup => (
            <TabsContent key={setup.id} value={setup.id} className="flex flex-col gap-2 rounded-xl border bg-card p-5">
              <p className="text-xs font-medium text-muted-foreground">{setup.hint}</p>
              {copy.block(setup.id, `${setup.label} ${setup.kind}`, setup.snippet)}
              <div className="flex flex-wrap items-center gap-2">
                {copy.button(setup.id, setup.snippet, setup.kind === 'command' ? 'Copy command' : 'Copy configuration')}
                {copy.state(setup.id, setup.kind === 'command' ? 'Command' : 'Configuration')}
              </div>
            </TabsContent>
          ))}
        </Tabs>
      </section>
      <Features items={[
        { title: 'Find the right agent', tool: 'find_agents', detail: 'Your assistant describes the task and gets the best matching agents, ranked by their skills.' },
        { title: 'Browse subnets', tool: 'list_subnets', detail: 'Your assistant sees your subnets and can search within one team of agents.' },
        { title: 'Check an agent', tool: 'get_agent', detail: 'Your assistant reads an agent’s skills, examples, and accepted inputs before choosing.' },
        { title: 'Send tasks and follow up', tool: 'send_message', detail: 'Your assistant opens a session, hands a task to an agent, and continues the same session with any of your agents. Every turn is recorded in Hybro.' },
        { title: 'Wait for your approval', tool: 'check_session', detail: 'When an agent needs your decision, you approve or reject it in Hybro; your assistant checks the session for the outcome.' },
      ]} />
    </div>
  )
}

function ApiPanel() {
  const copy = useCopy()
  const config = publicConfig()
  const pageOrigin = usePageOrigin()
  // An empty base URL means the API is served on this page's origin.
  const origin = config.api_base_url || pageOrigin
  const material = buildAccessMaterial('api', origin, config.api_prefix)
  return (
    <div className="flex flex-col gap-8">
      <AddressCard label="API base URL" url={origin} copy={copy} />
      <section aria-labelledby="requests-heading" className="flex flex-col gap-3">
        <h2 id="requests-heading" className="text-[15px] font-semibold">Requests</h2>
        <div className="flex flex-col gap-2 rounded-xl border bg-card p-5">
          <p className="text-xs font-medium text-muted-foreground">Find agents for a task</p>
          {copy.block('search', 'Search request', material.searchRequest)}
          <div className="flex flex-wrap items-center gap-2">
            {copy.button('search', material.searchRequest, 'Copy search request')}
            {copy.state('search', 'Search request')}
          </div>
        </div>
        <div className="flex flex-col gap-2 rounded-xl border bg-card p-5">
          <p className="text-xs font-medium text-muted-foreground">Discover agents</p>
          {copy.block('discover', 'Discovery request', material.configuration)}
          <div className="flex flex-wrap items-center gap-2">
            {copy.button('discover', material.configuration, 'Copy discovery request')}
            {copy.state('discover', 'Discovery request')}
          </div>
        </div>
        <div className="flex flex-col gap-2 rounded-xl border bg-card p-5">
          <p className="text-xs font-medium text-muted-foreground">Send a message to an agent</p>
          {copy.block('send', 'Message request', material.toolCalls)}
          <div className="flex flex-wrap items-center gap-2">
            {copy.button('send', material.toolCalls, 'Copy message request')}
            {copy.state('send', 'Message request')}
          </div>
        </div>
      </section>
      <Features items={[
        { title: 'Find agents', tool: `GET ${config.api_prefix}/agents/search`, detail: 'Ranks agents for a capability query, optionally within one subnet.' },
        { title: 'List subnets', tool: `GET ${config.api_prefix}/agents/groups`, detail: 'Lists your subnets with their number of active agents.' },
        { title: 'Agent profiles', tool: `GET ${config.api_prefix}/agents/{agent_id}/profile`, detail: 'Shows an agent’s skills, examples, and accepted inputs.' },
        { title: 'Discover agents', tool: `GET ${config.api_prefix}/agents/discovery`, detail: 'Returns every agent visible to you with its public Agent Card fields.' },
        { title: 'Send messages', tool: `POST ${config.api_prefix}/agents/messages`, detail: 'Sends a task to one agent and returns its reply.' },
      ]} />
    </div>
  )
}

export function ConnectPage({ kind }: { kind: AccessKind }) {
  const router = useRouter()
  return (
    <div className="page-container">
      <div className="page-content flex min-w-0 flex-col gap-6">
        <PageHeader title="Connect" />
        <Tabs value={kind} onValueChange={value => { if (value !== kind) router.push(value === 'mcp' ? routes.connectMcp : routes.connectApi, { scroll: false }) }} className="gap-6">
          <TabsList aria-label="Connection methods">
            <TabsTrigger value="mcp" className="min-h-9">MCP</TabsTrigger>
            <TabsTrigger value="api" className="min-h-9">API</TabsTrigger>
          </TabsList>
          <TabsContent value="mcp">{kind === 'mcp' ? <McpPanel /> : null}</TabsContent>
          <TabsContent value="api">{kind === 'api' ? <ApiPanel /> : null}</TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
