"use client"

import { useState } from "react"
import Link from "next/link"
import { useUser } from "@/lib/auth"
import { Cpu, Info, KeyRound, MonitorSmartphone, RefreshCw, User, type LucideIcon } from "lucide-react"
import type { SettingsSection } from "@/lib/settings-url"

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import { ProfileSection } from "@/components/settings/profile-section"
import { PasswordSection } from "@/components/settings/password-section"
import { SessionsSection } from "@/components/settings/sessions-section"
import { DangerZoneSection } from "@/components/settings/danger-zone-section"
import { ModelProviderSection, PHASE_TEXT, type ProviderPhase } from "@/components/settings/model-provider-section"
import { SettingsCard } from "@/components/settings/settings-card"
import { cn } from "@/lib/utils"

type Pane = "model-provider" | "profile" | "password" | "sessions" | "about"

const PANES: { id: Pane; label: string; icon: LucideIcon }[] = [
  { id: "model-provider", label: "Model provider", icon: Cpu },
  { id: "profile", label: "Profile", icon: User },
  { id: "password", label: "Password", icon: KeyRound },
  { id: "sessions", label: "Sessions", icon: MonitorSmartphone },
  { id: "about", label: "About", icon: Info },
]

interface SettingsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Section to show when the dialog opens. */
  section?: SettingsSection | null
}

function AboutSection() {
  return (
    <SettingsCard title="About" description="The Hybro version running on this machine" spacing={4}>
      <p className="text-sm">Hybro {process.env.HYBRO_APP_VERSION}</p>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <Link href="/about" className="font-medium underline underline-offset-2">About Hybro</Link>
        <a href="https://github.com/hybroai/hybro" target="_blank" rel="noopener noreferrer" className="font-medium underline underline-offset-2">
          Source code on GitHub
        </a>
      </div>
    </SettingsCard>
  )
}

export function SettingsDialog({ open, onOpenChange, section = null }: SettingsDialogProps) {
  const { user, isLoaded } = useUser()
  const [pane, setPane] = useState<Pane>("model-provider")
  const [phase, setPhase] = useState<ProviderPhase | null>(null)

  // Opening with a requested section shows it (state adjusted during render, not in an effect).
  const [request, setRequest] = useState({ open, section })
  if (request.open !== open || request.section !== section) {
    setRequest({ open, section })
    if (open && section) setPane(section)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[880px] h-[min(760px,85vh)] flex flex-col p-0 gap-0">
        <DialogHeader className="border-b px-6 py-3.5">
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription className="sr-only">Manage your model provider and account</DialogDescription>
        </DialogHeader>

        {!isLoaded || !user ? (
          <div className="flex flex-col items-center justify-center gap-4 py-12">
            <RefreshCw className="h-8 w-8 animate-spin text-icon-action" />
            <span className="text-base font-medium text-muted-foreground">
              Loading settings...
            </span>
          </div>
        ) : (
          <div className="flex flex-1 flex-wrap items-start gap-6 overflow-y-auto p-6">
            <nav aria-label="Settings sections" className="flex max-w-[220px] flex-[1_1_180px] flex-col gap-0.5 sm:sticky sm:top-0">
              {PANES.map(({ id, label, icon: Icon }) => {
                const current = pane === id
                return (
                  <button
                    key={id}
                    type="button"
                    aria-current={current ? "page" : undefined}
                    onClick={() => setPane(id)}
                    className={cn(
                      "flex h-9 items-center gap-2 rounded-md px-3 text-left text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
                      current ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <Icon className="size-4 shrink-0" aria-hidden="true" />
                    {label}
                    {id === "model-provider" && phase ? (
                      <span aria-hidden="true" data-testid="model-provider-dot" className={cn("ml-auto size-1.5 rounded-full bg-current", PHASE_TEXT[phase])} />
                    ) : null}
                  </button>
                )
              })}
            </nav>

            {/* Every pane stays mounted, so a sign-in in progress survives switching panes. */}
            <div className="flex min-w-0 flex-[999_1_480px] flex-col">
              <div hidden={pane !== "model-provider"}><ModelProviderSection onPhaseChange={setPhase} /></div>
              <div hidden={pane !== "profile"} className="space-y-6">
                <ProfileSection user={user} />
                <DangerZoneSection user={user} />
              </div>
              <div hidden={pane !== "password"}><PasswordSection user={user} /></div>
              <div hidden={pane !== "sessions"}><SessionsSection user={user} /></div>
              <div hidden={pane !== "about"}><AboutSection /></div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
