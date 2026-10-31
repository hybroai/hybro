"use client"

import { useMemo } from "react"
import type { UseCaseTemplate } from "@/lib/use-case-templates"
import type { Agent } from "@/lib/types/agent"
import { getAgentAvatarUri } from "@/lib/agent-avatar"
import { cn } from "@/lib/utils"

interface UseCaseCardProps {
  template: UseCaseTemplate
  catalog: Agent[]
  onClick: () => void
  disabled?: boolean
  /** Shown on the card, which is then disabled. */
  unavailableReason?: string
}

export function UseCaseCard({ template, catalog, onClick, disabled: busy, unavailableReason }: UseCaseCardProps) {
  const disabled = busy || unavailableReason !== undefined
  const { icon: Icon, title, description, agents, tag } = template

  const resolvedAvatars = useMemo(() => {
    const idMap = new Map(catalog.map((a) => [a.agent_id, a]))
    const nameMap = new Map(catalog.map((a) => [a.name.toLowerCase(), a]))
    return agents.map((ta) => {
      const found = idMap.get(ta.agentId) ?? nameMap.get(ta.agentName.toLowerCase())
      const iconUrl = found?.icon_url || getAgentAvatarUri(found?.agent_id ?? ta.agentId)
      return { ...ta, iconUrl }
    })
  }, [agents, catalog])

  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      className={cn(
        "group relative overflow-hidden rounded-[18px] p-6 text-left cursor-pointer",
        "aspect-[5/3] flex flex-col",
        "bg-gradient-to-br from-white to-slate-100 dark:from-[#111122] dark:to-[#0d0d1a]",
        "border border-slate-200/80 dark:border-transparent",
        "transition-all duration-250 ease-out",
        "disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none",
        "shadow-sm hover:bg-muted/40 dark:shadow-none",
      )}
    >
      {/* Glassmorphism border — dark mode only */}
      <div
        className="pointer-events-none absolute inset-0 rounded-[18px] p-px hidden dark:block"
        style={{
          background:
            "linear-gradient(160deg, rgba(255,255,255,0.1) 0%, rgba(255,255,255,0.02) 40%, rgba(255,255,255,0.06) 100%)",
          WebkitMask:
            "linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)",
          WebkitMaskComposite: "xor",
          maskComposite: "exclude",
        }}
      />

      {/* Shimmer sweep — light mode */}
      <div
        className={cn(
          "pointer-events-none absolute -top-1/2 -left-1/2 h-[200%] w-[200%]",
          "block dark:hidden",
          "transition-transform duration-[800ms] ease-out",
          "-translate-x-full group-hover:translate-x-[40%]",
        )}
        style={{
          background:
            "linear-gradient(115deg, transparent 30%, rgba(0,0,0,0.02) 45%, rgba(0,0,0,0.04) 50%, rgba(0,0,0,0.02) 55%, transparent 70%)",
        }}
      />

      {/* Shimmer sweep — dark mode */}
      <div
        className={cn(
          "pointer-events-none absolute -top-1/2 -left-1/2 h-[200%] w-[200%]",
          "hidden dark:block",
          "transition-transform duration-[800ms] ease-out",
          "-translate-x-full group-hover:translate-x-[40%]",
        )}
        style={{
          background:
            "linear-gradient(115deg, transparent 30%, rgba(255,255,255,0.03) 45%, rgba(255,255,255,0.06) 50%, rgba(255,255,255,0.03) 55%, transparent 70%)",
        }}
      />

      {/* Accent glow — light mode */}
      <div
        className="pointer-events-none absolute inset-0 rounded-[18px] opacity-0 transition-opacity duration-300 group-hover:opacity-100 block dark:hidden"
        style={{
          background: "radial-gradient(ellipse at 20% 50%, hsla(162,65%,38%,0.08) 0%, transparent 70%)",
        }}
      />

      {/* Accent glow — dark mode */}
      <div
        className="pointer-events-none absolute inset-0 rounded-[18px] opacity-0 transition-opacity duration-300 group-hover:opacity-100 hidden dark:block"
        style={{
          background: "radial-gradient(ellipse at 20% 50%, hsla(162,65%,38%,0.06) 0%, transparent 70%)",
        }}
      />

      {/* "New" corner tag */}
      {tag === "new" && (
        <span className="absolute -top-px -right-px z-10 rounded-bl-[10px] rounded-tr-[17px] border-b border-l border-primary/20 bg-primary/10 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-primary">
          New
        </span>
      )}

      {/* Top: Icon + Title */}
      <div className="relative z-[1] flex items-center gap-3">
        <div className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[11px] border border-teal-600/20 dark:border-[hsl(162,65%,58%)]/20 bg-teal-600/10 dark:bg-[hsl(162,65%,58%)]/10">
          <Icon className="h-[19px] w-[19px] text-teal-600 dark:text-[hsl(162,65%,58%)]" />
        </div>
        <span className="text-[15px] font-bold leading-tight text-slate-800 dark:text-[#f0f0f0]">
          {title}
        </span>
      </div>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Middle: Description */}
      <p className="relative z-[1] line-clamp-2 text-[13px] leading-relaxed text-slate-500 dark:text-[#999]">
        {unavailableReason ?? description}
      </p>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Bottom: Agent avatars */}
      <div className="relative z-[1] flex items-center gap-2.5">
        <div className="flex">
          {resolvedAvatars.map((agent, i) => (
            <div
              key={agent.agentId}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-full border-[2.5px]",
                "border-white shadow-[0_2px_8px_rgba(0,0,0,0.1)] dark:border-[#111122] dark:shadow-[0_2px_8px_rgba(0,0,0,0.3)]",
                i > 0 && "-ml-2",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={agent.iconUrl}
                alt={agent.agentName}
                className="h-full w-full rounded-full object-cover"
              />
            </div>
          ))}
        </div>
        <span className="text-[11px] text-slate-400 dark:text-[#666]">
          {agents.length} {agents.length === 1 ? "agent" : "agents"}
        </span>
      </div>
    </button>
  )
}
