"use client"

import { useCallback, useEffect, useState } from "react"
import { Globe, Loader2, Monitor, Shield, Smartphone } from "lucide-react"
import { toast } from "sonner"
import { accountErrorMessage, listBrowsers, revokeBrowser, type BrowserSession } from "@/lib/api/account"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

import { SettingsCard } from "@/components/settings/settings-card"

const SESSION_ROW_CLASS = "flex items-center gap-3 rounded-lg border p-3"

function formatRelativeTime(date: Date): string {
  const diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000)
  const diffMinutes = Math.floor(diffSeconds / 60)
  const diffHours = Math.floor(diffMinutes / 60)
  const diffDays = Math.floor(diffHours / 24)
  if (diffSeconds < 60) return "Just now"
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 30) return `${diffDays}d ago`
  return date.toLocaleDateString()
}

/** A short name for a user agent string; the full string is the title. */
export function describeBrowser(userAgent: string): string {
  const browser =
    /Edg\//.test(userAgent) ? "Edge"
      : /Firefox\//.test(userAgent) ? "Firefox"
        : /Chrome\//.test(userAgent) ? "Chrome"
          : /Safari\//.test(userAgent) ? "Safari"
            : null
  const device =
    /iPhone|iPad|Android/.test(userAgent) ? "mobile"
      : /Macintosh|Windows|Linux|X11/.test(userAgent) ? "desktop"
        : null
  if (!browser) return userAgent ? "Browser" : "Unknown browser"
  return device ? `${browser} on ${device}` : browser
}

function DeviceIcon({ userAgent }: { userAgent: string }) {
  if (/iPhone|iPad|Android/.test(userAgent)) return <Smartphone className="h-5 w-5 text-icon-neutral" />
  if (userAgent) return <Monitor className="h-5 w-5 text-icon-neutral" />
  return <Globe className="h-5 w-5 text-icon-neutral" />
}

/** The browsers signed in as the owner; any but this one can be signed out. */
export function SessionsSection() {
  const [browsers, setBrowsers] = useState<BrowserSession[]>([])
  const [loading, setLoading] = useState(true)
  const [revokingId, setRevokingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const result = await listBrowsers()
      setBrowsers(result.items)
    } catch (err: unknown) {
      toast.error(accountErrorMessage(err, "Failed to load signed-in browsers"))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function handleRevoke(browser: BrowserSession) {
    try {
      setRevokingId(browser.id)
      await revokeBrowser(browser.id)
      await load()
      toast.success("Browser signed out")
    } catch (err: unknown) {
      toast.error(accountErrorMessage(err, "Failed to sign out the browser"))
    } finally {
      setRevokingId(null)
    }
  }

  return (
    <SettingsCard
      title={
        <span className="flex items-center gap-2">
          <Shield className="h-5 w-5 text-icon-action" />
          Signed-in browsers
        </span>
      }
      description="Every browser signed in as the owner. Sign out one you do not recognize."
      spacing={4}
    >
      {loading ? (
        <div className="space-y-3">
          {[1, 2].map((i) => (
            <div key={i} className={SESSION_ROW_CLASS}>
              <Skeleton className="h-10 w-10 rounded-full" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-3 w-24" />
              </div>
            </div>
          ))}
        </div>
      ) : browsers.length === 0 ? (
        <p className="py-4 text-center text-sm text-muted-foreground">No signed-in browsers found.</p>
      ) : (
        browsers.map((browser) => (
          <div key={browser.id} className={SESSION_ROW_CLASS} data-testid="browser-row">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <DeviceIcon userAgent={browser.user_agent} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium" title={browser.user_agent || undefined}>
                  {describeBrowser(browser.user_agent)}
                </span>
                {browser.current && (
                  <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                    Current
                  </span>
                )}
              </div>
              <p className="truncate text-xs text-muted-foreground">
                Last used {formatRelativeTime(new Date(browser.last_used_at))} &middot; signed in{" "}
                {new Date(browser.created_at).toLocaleDateString()}
              </p>
            </div>
            {!browser.current && (
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={() => handleRevoke(browser)}
                disabled={revokingId !== null}
                aria-label={revokingId === browser.id ? "Revoking" : "Revoke"}
              >
                {revokingId === browser.id ? (
                  <Loader2 className="h-4 w-4 animate-spin text-icon-action" />
                ) : (
                  "Revoke"
                )}
              </Button>
            )}
          </div>
        ))
      )}
    </SettingsCard>
  )
}
