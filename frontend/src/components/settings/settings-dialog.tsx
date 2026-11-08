"use client"

import { useState } from "react"
import { useUser } from "@/lib/auth"
import { KeyRound, MonitorSmartphone, RefreshCw, User, type LucideIcon } from "lucide-react"

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
import { cn } from "@/lib/utils"

type Pane = "profile" | "password" | "sessions"

const PANES: { id: Pane; label: string; icon: LucideIcon }[] = [
  { id: "profile", label: "Profile", icon: User },
  { id: "password", label: "Password", icon: KeyRound },
  { id: "sessions", label: "Sessions", icon: MonitorSmartphone },
]

interface SettingsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Account settings of the signed-in user; instance settings live on the Settings page. */
export function SettingsDialog({ open, onOpenChange }: SettingsDialogProps) {
  const { user, isLoaded } = useUser()
  const [pane, setPane] = useState<Pane>("profile")

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[820px] h-[min(720px,85vh)] flex flex-col p-0 gap-0">
        <DialogHeader className="border-b px-6 py-3.5">
          <DialogTitle>Account</DialogTitle>
          <DialogDescription className="sr-only">Manage your profile, password and sessions</DialogDescription>
        </DialogHeader>

        {!isLoaded || !user ? (
          <div className="flex flex-col items-center justify-center gap-4 py-12">
            <RefreshCw className="h-8 w-8 animate-spin text-icon-action" />
            <span className="text-base font-medium text-muted-foreground">
              Loading account...
            </span>
          </div>
        ) : (
          <div className="flex flex-1 flex-wrap items-start gap-6 overflow-y-auto p-6">
            <nav aria-label="Account sections" className="flex max-w-[200px] flex-[1_1_180px] flex-col gap-0.5 sm:sticky sm:top-0">
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
                  </button>
                )
              })}
            </nav>

            <div className="flex min-w-0 flex-[999_1_460px] flex-col">
              <div hidden={pane !== "profile"} className="space-y-6">
                <ProfileSection user={user} />
                <DangerZoneSection user={user} />
              </div>
              <div hidden={pane !== "password"}><PasswordSection user={user} /></div>
              <div hidden={pane !== "sessions"}><SessionsSection user={user} /></div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
