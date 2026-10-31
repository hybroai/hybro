'use client'

import { useState, useEffect, useCallback, useRef } from "react"
import { useParams, useRouter } from "next/navigation"
import Link from "next/link"
import {
  ArrowLeft,
  Bot,
  SquareArrowOutUpRight,
  Lightbulb,
  Send,
  Reply,
  Zap,
  MessageCirclePlus,
  ShieldCheck,
  BarChart3,
  ChevronDown,
  ChevronRight,
  Link2,
  Tag,
  Sparkles,
  Share2,
  Trash2,
} from "lucide-react"
import { useAuth } from "@/lib/auth"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Separator } from "@/components/ui/separator"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  Breadcrumb,
  BreadcrumbList,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { toast } from "sonner"
import { deleteAgent, getAgent } from "@/lib/api/agent"
import { cn } from "@/lib/utils"
import type { AgentRouteResponse } from "@/lib/types"
import type { AgentCapabilitiesView, AgentSkillView } from "@/lib/types/agent"
import { routes } from '@/lib/routes'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { isSystemAgent, SYSTEM_AGENTS } from "@/lib/system-agents"
import { getModeIcon, getModeLabel } from "@/lib/agent-icon-utils"
import { AgentSourceBadge } from "@/components/agent-source-badge"
import { getAgentAvatarUri } from "@/lib/agent-avatar"
import { StatusIndicator } from "@/components/status-indicator"
import { agentStatusView, inactiveReasonText } from "@/lib/status"
import { AddToSubnetMenu } from "@/components/networks/add-to-subnet-menu"
import { SubnetDialog } from "@/components/subnets/subnet-dialog"
import { useNetworks } from "@/hooks/useNetworks"

const CAPABILITY_TOOLTIPS: Record<string, string> = {
  "Streaming": "Responses appear in real-time as they're generated",
  "Push Notifications": "Can send updates even after the conversation ends",
  "State History": "Remembers context across multiple interactions",
  "Extensions": "Supports additional protocol extensions",
}

function visibleCapabilities(caps: AgentCapabilitiesView): string[] {
  const all: [string, boolean][] = [
    ["Streaming", caps.streaming],
    ["Push Notifications", caps.push_notifications],
    ["State History", caps.state_transition_history],
    ["Extensions", caps.extensions],
  ]
  return all.filter(([, enabled]) => enabled).map(([label]) => label)
}

function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function collectQuickPrompts(skills: AgentSkillView[]): string[] {
  const prompts: string[] = []
  for (const skill of skills) {
    for (const example of skill.examples) {
      if (prompts.length >= 3) return prompts
      prompts.push(example)
    }
  }
  return prompts
}

export default function ConsumerAgentProfilePage() {
  const params = useParams()
  const router = useRouter()
  const queryClient = useQueryClient()
  const { getToken, userId } = useAuth()
  const agentId = decodeURIComponent(params.id as string)
  const [techOpen, setTechOpen] = useState(false)
  const [unregistering, setUnregistering] = useState(false)
  const ctaRef = useRef<HTMLDivElement>(null)
  const [showStickyBar, setShowStickyBar] = useState(false)
  const subnets = useNetworks()
  const [creatingSubnet, setCreatingSubnet] = useState(false)

  const isSystem = isSystemAgent(agentId)
  const { data: agentData, isLoading: loading } = useQuery<AgentRouteResponse>({
    queryKey: ["agent", agentId],
    enabled: !!agentId && !isSystem,
    queryFn: () => getAgent(agentId, undefined, getToken),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  useEffect(() => {
    const el = ctaRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      ([entry]) => setShowStickyBar(!entry.isIntersecting),
      { threshold: 0 },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [loading, agentData])

  const handleCopyLink = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(window.location.href)
      toast.success("Link copied to clipboard")
    } catch {
      toast.error("Failed to copy link")
    }
  }, [])

  const handleNativeShare = useCallback(async (name: string, description: string) => {
    try {
      await navigator.share({
        title: name,
        text: description,
        url: window.location.href,
      })
    } catch (e) {
      if (e instanceof Error && e.name !== "AbortError") {
        toast.error("Failed to share")
      }
    }
  }, [])

  const openShareWindow = useCallback((url: string) => {
    window.open(url, "_blank", "noopener,noreferrer,width=600,height=500")
  }, [])

  const handleUnregister = useCallback(async () => {
    try {
      setUnregistering(true)
      const response = await deleteAgent({ agent_id: agentId }, getToken)

      if (!response.success) {
        toast.error("Failed to unregister agent", {
          description: response.error || "An unexpected error occurred",
        })
        return
      }

      await queryClient.invalidateQueries({ queryKey: ["agents"] })
      toast.success("Agent unregistered")
      router.push(routes.network)
    } catch (error) {
      toast.error("Failed to unregister agent", {
        description: error instanceof Error ? error.message : "An unexpected error occurred",
      })
    } finally {
      setUnregistering(false)
    }
  }, [agentId, getToken, queryClient, router])

  /* ───────── Loading ───────── */

  if (loading) {
    return (
      <div className="page-container">
        <div className="page-content flex items-center justify-center min-h-[60vh]">
          <div className="flex flex-col items-center justify-center gap-4">
            <div className="relative">
              <div className="h-12 w-12 rounded-full border-4 border-muted motion-safe:animate-spin border-t-muted-foreground" />
              <Bot className="h-6 w-6 absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 text-muted-foreground" />
            </div>
            <span className="text-base font-medium text-muted-foreground">
              Loading agent profile…
            </span>
          </div>
        </div>
      </div>
    )
  }

  /* ───────── System agent ───────── */

  if (!agentData?.success || !agentData.agent) {
    if (isSystemAgent(agentId)) {
      const info = SYSTEM_AGENTS[agentId]
      return (
        <div className="page-container">
          <div className="page-content space-y-8">
            <Button
              variant="ghost"
              onClick={() => router.push(routes.network)}
              className="pl-0"
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              Back to Network
            </Button>

            <Card className="overflow-hidden">
              <CardHeader className="text-center space-y-4 pt-10 pb-6">
                <div className="mx-auto">
                  <Avatar className="h-24 w-24 mx-auto">
                    <AvatarImage src={getAgentAvatarUri(agentId)} alt={info.name} />
                    <AvatarFallback className="bg-primary/5 text-primary">
                      <ShieldCheck className="h-10 w-10" />
                    </AvatarFallback>
                  </Avatar>
                </div>
                <div className="space-y-2">
                  <CardTitle className="text-2xl font-semibold">{info.name}</CardTitle>
                  <Badge variant="secondary" className="gap-1.5">
                    <ShieldCheck className="h-3 w-3" />
                    Built-in System Agent
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="text-center pb-10 max-w-lg mx-auto space-y-4">
                <p className="text-muted-foreground leading-relaxed">{info.description}</p>
                <Separator />
                <p className="text-sm text-muted-foreground">
                  This is a built-in agent managed by Hybro. It does not have a
                  configurable profile, skills, or capabilities — it activates
                  automatically when a room has multiple agent responses.
                </p>
              </CardContent>
            </Card>
          </div>
        </div>
      )
    }

    /* ───────── Not found ───────── */

    return (
      <div className="page-container">
        <div className="page-content flex items-center justify-center min-h-[60vh]">
          <Card className="w-full max-w-md border-dashed">
            <CardHeader className="text-center">
              <div className="mx-auto bg-muted rounded-full p-3 w-fit mb-4">
                <Bot className="h-8 w-8 text-muted-foreground" />
              </div>
              <CardTitle className="text-xl">Agent Not Found</CardTitle>
              <CardDescription>
                The agent you are looking for does not exist or has been removed.
              </CardDescription>
            </CardHeader>
            <CardFooter className="flex justify-center">
              <Button onClick={() => router.push(routes.network)} variant="default">
                <ArrowLeft className="h-4 w-4 mr-2" />
                Back to Network
              </Button>
            </CardFooter>
          </Card>
        </div>
      </div>
    )
  }

  /* ───────── Main profile ───────── */

  const agent = agentData.agent
  const card = agent
  const memberOf = subnets.networks.filter(network => network.agents.includes(agent.agent_id))
  const isLocal = agent.source === "local"
  const waitingForSetup = agent.status === "inactive" ? inactiveReasonText(agent.inactive_reason) : null
  const canUnregister = !isLocal && userId === agent.provider_id
  const isVisibleLocal = isLocal ? agent.status === "active" : true

  if (!isVisibleLocal) {
    return (
      <div className="page-container">
        <div className="page-content flex items-center justify-center min-h-[60vh]">
          <Card className="w-full max-w-md border-dashed">
            <CardHeader className="text-center">
              <CardTitle className="text-xl">Local Agent Unavailable</CardTitle>
              <CardDescription>
                This agent is not currently discoverable on the local machine.
              </CardDescription>
            </CardHeader>
            <CardFooter className="flex justify-center">
              <Button onClick={() => router.push(routes.network)}>
                <ArrowLeft className="h-4 w-4 mr-2" />
                Back to Network
              </Button>
            </CardFooter>
          </Card>
        </div>
      </div>
    )
  }

  const enabledCaps = visibleCapabilities(card.capabilities)
  const isActive = agent.status === "active"
  const isChatDisabled = !isActive

  const callCount = agent.call_count ?? 0

  const hasStats = callCount > 0

  const quickPrompts = collectQuickPrompts(card.skills)
  const providerName = card.provider_organization || agent.provider_name || undefined
  const heroMeta = [card.version ? `v${card.version}` : null, providerName ? `Built by ${providerName}` : null]
    .filter(Boolean)
    .join(' · ')

  const navigateToChat = (prompt?: string) => {
    // Single-agent chat seeds room membership; it is not an @mention handoff.
    useRoomUiStore.getState().setPendingChatHandoff({
      draft: prompt?.trim() ? prompt.trim() : '',
      seedAgents: [agent],
    })
    router.push(routes.chat)
  }

  return (
    <div className="page-container">
      <div className="page-content space-y-10 pb-24 lg:pb-10">
        {/* ── Breadcrumb + actions ── */}
        <Breadcrumb>
          <BreadcrumbList>
            <BreadcrumbItem>
              <BreadcrumbLink asChild>
                <Link href={routes.network}>Network</Link>
              </BreadcrumbLink>
            </BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem>
              <BreadcrumbPage>{card.name}</BreadcrumbPage>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>

        {/* ── Hero ── */}
        <div className="space-y-5">
          <div className="flex items-start gap-5">
            <Avatar className={cn("h-24 w-24 shrink-0 rounded-xl", !isActive && "grayscale opacity-60")}>
              <AvatarImage src={card.icon_url || undefined} alt={card.name} />
              <AvatarFallback className="rounded-none p-0 overflow-hidden">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={getAgentAvatarUri(agentId)} alt={card.name} className="h-full w-full" />
              </AvatarFallback>
            </Avatar>

            <div className="space-y-2 min-w-0">
              <div className="flex flex-wrap items-start gap-3">
                <h1 className="text-2xl font-semibold tracking-tight">{card.name}</h1>
                <div className="flex flex-col items-start gap-1.5">
                  <StatusIndicator
                    view={agentStatusView(agent.status, agent.inactive_reason)}
                  />
                  <Badge variant="badgeMuted" className="h-6 gap-1.5 pl-1.5 pr-2.5 py-0 font-medium">
                    <AgentSourceBadge
                      source={agent.source}
                      className="h-3.5 w-3.5"
                    />
                    {isLocal ? "Local" : agent.source === "bundled" ? "Sample" : "Remote"}
                  </Badge>
                </div>
              </div>
              {heroMeta ? <p className="text-sm text-muted-foreground">{heroMeta}</p> : null}
            </div>
          </div>

          <p className="text-muted-foreground leading-relaxed max-w-2xl">{card.description}</p>
          {waitingForSetup ? <p className="text-sm text-status-warning">{waitingForSetup}</p> : null}

          {/* CTA inline with hero */}
          <div ref={ctaRef} className="flex flex-wrap items-center gap-3 pt-1">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span>
                    <Button
                      onClick={() => navigateToChat()}
                      disabled={isChatDisabled}
                    >
                      <MessageCirclePlus className="h-4 w-4 mr-2" />
                      Chat with this agent
                    </Button>
                  </span>
                </TooltipTrigger>
                {isChatDisabled && (
                  <TooltipContent>{waitingForSetup ?? "Agent is inactive"}</TooltipContent>
                )}
              </Tooltip>
            </TooltipProvider>

            {card.documentation_url && (
              <Button variant="secondary" asChild>
                <a
                  href={card.documentation_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <SquareArrowOutUpRight className="h-4 w-4 mr-2" />
                  Documentation
                </a>
              </Button>
            )}

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="secondary">
                  <Share2 className="h-4 w-4 mr-2" />
                  Share
                  <ChevronDown className="h-4 w-4" aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuItem onClick={handleCopyLink}>
                  <Link2 className="h-4 w-4" />
                  Copy Link
                </DropdownMenuItem>
                {typeof navigator !== "undefined" && "share" in navigator && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onClick={() => handleNativeShare(card.name, card.description ?? "")}>
                      <Share2 className="h-4 w-4" />
                      More Options…
                    </DropdownMenuItem>
                  </>
                )}
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => {
                    const url = encodeURIComponent(window.location.href)
                    const text = encodeURIComponent(`Check out ${card.name} on Hybro`)
                    openShareWindow(`https://x.com/intent/post?url=${url}&text=${text}`)
                  }}
                >
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" /></svg>
                  Share on X
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => {
                    const text = encodeURIComponent(`Check out ${card.name} on Hybro\n\n${window.location.href}`)
                    openShareWindow(`https://www.linkedin.com/feed/?shareActive=true&text=${text}`)
                  }}
                >
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" /></svg>
                  Share on LinkedIn
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => {
                    const url = encodeURIComponent(window.location.href)
                    const title = encodeURIComponent(card.name)
                    openShareWindow(`https://reddit.com/submit?url=${url}&title=${title}`)
                  }}
                >
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0zm5.01 4.744c.688 0 1.25.561 1.25 1.249a1.25 1.25 0 0 1-2.498.056l-2.597-.547-.8 3.747c1.824.07 3.48.632 4.674 1.488.308-.309.73-.491 1.207-.491.968 0 1.754.786 1.754 1.754 0 .716-.435 1.333-1.01 1.614a3.111 3.111 0 0 1 .042.52c0 2.694-3.13 4.87-7.004 4.87-3.874 0-7.004-2.176-7.004-4.87 0-.183.015-.366.043-.534A1.748 1.748 0 0 1 4.028 12c0-.968.786-1.754 1.754-1.754.463 0 .898.196 1.207.49 1.207-.883 2.878-1.43 4.744-1.487l.885-4.182a.342.342 0 0 1 .14-.197.35.35 0 0 1 .238-.042l2.906.617a1.214 1.214 0 0 1 1.108-.701zM9.25 12C8.561 12 8 12.562 8 13.25c0 .687.561 1.248 1.25 1.248.687 0 1.248-.561 1.248-1.249 0-.688-.561-1.249-1.249-1.249zm5.5 0c-.687 0-1.248.561-1.248 1.25 0 .687.561 1.248 1.249 1.248.688 0 1.249-.561 1.249-1.249 0-.687-.562-1.249-1.25-1.249zm-5.466 3.99a.327.327 0 0 0-.231.094.33.33 0 0 0 0 .463c.842.842 2.484.913 2.961.913.477 0 2.105-.056 2.961-.913a.361.361 0 0 0 .029-.463.33.33 0 0 0-.464 0c-.547.533-1.684.73-2.512.73-.828 0-1.979-.196-2.512-.73a.326.326 0 0 0-.232-.095z" /></svg>
                  Share on Reddit
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            {canUnregister && (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button variant="danger" disabled={unregistering}>
                    <Trash2 className="h-4 w-4 mr-2" />
                    {unregistering ? "Unregistering…" : "Unregister agent"}
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Unregister this agent?</AlertDialogTitle>
                    <AlertDialogDescription>
                      This removes &quot;{card.name}&quot; from Hybro&apos;s registry. It does
                      not stop or delete the remote A2A agent.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction asChild>
                      <Button variant="dangerSolid" onClick={handleUnregister}>Unregister agent</Button>
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}
          </div>

          {isLocal && (
            <p className="text-xs text-muted-foreground">
              Local agents are managed by automatic discovery and cannot be unregistered.
            </p>
          )}

          {/* Quick prompts */}
          {quickPrompts.length > 0 && !isChatDisabled && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-muted-foreground">Try asking:</span>
              {quickPrompts.map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => navigateToChat(prompt)}
                  className="text-xs px-3 py-1.5 rounded-full border border-border bg-muted/40 text-foreground/80 hover:bg-muted hover:text-foreground transition-colors cursor-pointer truncate max-w-[280px]"
                >
                  &ldquo;{prompt}&rdquo;
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Stats ── */}
        {hasStats && (
          <div className="flex flex-wrap items-center gap-6 text-sm text-muted-foreground">
            {callCount > 0 && (
              <span className="flex items-center gap-1.5">
                <BarChart3 className="h-4 w-4" />
                {`${formatCount(callCount)} room calls`}
              </span>
            )}

          </div>
        )}

        {/* ── Subnets ── */}
        <section aria-label="Subnets" aria-labelledby="agent-subnets-heading" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="agent-subnets-heading" className="text-[15px] font-semibold">Subnets</h2>
            <AddToSubnetMenu
              agentId={agent.agent_id}
              networks={subnets.networks}
              pending={subnets.pending}
              onSetMembers={subnets.setMembers}
              onCreateWithAgent={() => setCreatingSubnet(true)}
            />
          </div>
          {memberOf.length ? (
            <ul className="flex flex-col">
              {memberOf.map(network => (
                <li key={network.group_id}>
                  <Link
                    href={routes.networkWith(new URLSearchParams({ select: `subnet:${network.group_id}` }).toString())}
                    className="flex items-center justify-between gap-3 rounded-lg px-2 py-2 hover:bg-muted"
                  >
                    <span className="min-w-0">
                      <span className="block text-sm font-medium">{network.name}</span>
                      <span className="block truncate font-mono text-xs text-muted-foreground">{network.group_id}</span>
                    </span>
                    <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" />
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">Not in any subnet</p>
          )}
        </section>
        {creatingSubnet ? (
          <SubnetDialog
            open
            initialAgentIds={[agent.agent_id]}
            agents={subnets.agents}
            agentsLoading={subnets.agentsLoading}
            agentsError={subnets.agentsError}
            pending={subnets.pending}
            onOpenChange={open => { if (!open) setCreatingSubnet(false) }}
            onRetryAgents={() => { void subnets.refreshAgents() }}
            onSave={async values => { await subnets.create(values); setCreatingSubnet(false); toast.success('Subnet created') }}
          />
        ) : null}

        {/* ── Skills ── */}
        {card.skills.length > 0 && (
          <section className="space-y-4" data-testid="skills-section">
            <h2 className="text-[15px] font-semibold flex items-center gap-2">
              <Lightbulb className="h-4 w-4 text-muted-foreground" />
              What this agent can help with
            </h2>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {card.skills.map((skill) => (
                <Card key={skill.id} className="overflow-hidden rounded-xl border bg-card shadow-none">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-semibold">{skill.name}</CardTitle>
                    {skill.description && (
                      <CardDescription className="text-xs leading-relaxed">
                        {skill.description}
                      </CardDescription>
                    )}
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {skill.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {skill.tags.map((tag) => (
                          <Badge key={tag} variant="badgeMuted" className="gap-1 text-[10px]">
                            <Tag className="h-2.5 w-2.5" />
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    )}
                    {(skill.examples ?? []).length > 0 && (
                      <div className="space-y-1.5">
                        {(skill.examples ?? []).slice(0, 3).map((example) => (
                          <button
                            key={example}
                            onClick={() => !isChatDisabled && navigateToChat(example)}
                            disabled={isChatDisabled}
                            className="flex items-start gap-2 w-full text-left text-xs text-foreground/70 hover:text-foreground disabled:opacity-50 disabled:cursor-default transition-colors group"
                          >
                            <Sparkles className="h-3 w-3 mt-0.5 shrink-0 text-muted-foreground" />
                            <span className="wrap-break-word">{example}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          </section>
        )}

        {/* ── Technical details (collapsible) ── */}
        <Collapsible open={techOpen} onOpenChange={setTechOpen}>
          <section className="space-y-4" data-testid="technical-section">
            <CollapsibleTrigger className="flex items-center gap-2 group cursor-pointer w-full">
              <h2 className="text-[15px] font-semibold flex items-center gap-2">
                <Zap className="h-4 w-4 text-muted-foreground" />
                How this agent works
              </h2>
              <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform duration-200 ${techOpen ? "rotate-180" : ""}`} />
            </CollapsibleTrigger>

            <CollapsibleContent className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {/* You can send */}
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-sm font-semibold flex items-center gap-2">
                      <Send className="h-4 w-4 text-muted-foreground" />
                      You can send
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-2">
                      {card.default_input_modes.map((mode) => {
                        const Icon = getModeIcon(mode)
                        const label = getModeLabel(mode)
                        return (
                          <TooltipProvider key={mode}>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Badge
                                  variant="outline"
                                  className="gap-1.5 font-normal"
                                >
                                  <Icon className="h-3 w-3" />
                                  {label}
                                </Badge>
                              </TooltipTrigger>
                              {label !== mode && (
                                <TooltipContent className="font-mono text-xs">{mode}</TooltipContent>
                              )}
                            </Tooltip>
                          </TooltipProvider>
                        )
                      })}
                    </div>
                  </CardContent>
                </Card>

                {/* It returns */}
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-sm font-semibold flex items-center gap-2">
                      <Reply className="h-4 w-4 text-muted-foreground" />
                      It returns
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-2">
                      {card.default_output_modes.map((mode) => {
                        const Icon = getModeIcon(mode)
                        const label = getModeLabel(mode)
                        return (
                          <TooltipProvider key={mode}>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Badge
                                  variant="outline"
                                  className="gap-1.5 font-normal"
                                >
                                  <Icon className="h-3 w-3" />
                                  {label}
                                </Badge>
                              </TooltipTrigger>
                              {label !== mode && (
                                <TooltipContent className="font-mono text-xs">{mode}</TooltipContent>
                              )}
                            </Tooltip>
                          </TooltipProvider>
                        )
                      })}
                    </div>
                  </CardContent>
                </Card>
              </div>

              {/* Capabilities with tooltips */}
              {enabledCaps.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 pt-2" data-testid="capabilities">
                  <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                    Capabilities
                  </span>
                  <TooltipProvider>
                    {enabledCaps.map((cap) => (
                      <Tooltip key={cap}>
                        <TooltipTrigger asChild>
                          <Badge variant="secondary" className="font-normal cursor-default">
                            {cap}
                          </Badge>
                        </TooltipTrigger>
                        {CAPABILITY_TOOLTIPS[cap] && (
                          <TooltipContent>{CAPABILITY_TOOLTIPS[cap]}</TooltipContent>
                        )}
                      </Tooltip>
                    ))}
                  </TooltipProvider>
                </div>
              )}
            </CollapsibleContent>
          </section>
        </Collapsible>
      </div>

      {/* ── Sticky mobile CTA ── */}
      {showStickyBar && !isChatDisabled && (
        <div className="fixed bottom-0 inset-x-0 p-3 bg-background/95 backdrop-blur-sm border-t z-40 lg:hidden">
          <Button
            className="w-full"
            onClick={() => navigateToChat()}
          >
            <MessageCirclePlus className="h-4 w-4 mr-2" />
            Chat with this agent
          </Button>
        </div>
      )}
    </div>
  )
}
