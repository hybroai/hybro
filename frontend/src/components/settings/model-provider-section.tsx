"use client"

import { useEffect, useRef, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ExternalLink, Loader2 } from "lucide-react"
import { toast } from "sonner"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { SegmentedControl } from "@/components/ui/segmented-control"
import { LoadingButton } from "@/components/settings/loading-button"
import { FormGroup } from "@/components/settings/form-group"
import { setupStatusQueryKey, useSetupStatus } from "@/hooks/useSetupStatus"
import { AGENT_CATALOG_KEY } from "@/hooks/room/useAgentCatalog"
import {
  IMAGE_SIZES,
  cancelOAuth,
  completeOAuth,
  getOAuthLogin,
  getSetupModels,
  saveApiKey,
  saveImageSize,
  saveOAuthModels,
  setupError,
  setupErrorMessage,
  startOAuth,
  type AuthMethod,
  type ImageSize,
  type OAuthLogin,
  type ProviderId,
  type SetupStatus,
} from "@/lib/api/setup"
import { isSignInExpired } from "@/lib/setup-notice"
import { cn } from "@/lib/utils"

const PROVIDERS: { value: ProviderId; label: string; note: string }[] = [
  { value: "openai", label: "OpenAI", note: "ChatGPT account or API key" },
  { value: "anthropic", label: "Anthropic", note: "API key" },
  { value: "deepseek", label: "DeepSeek", note: "API key" },
]
const AUTH_OPTIONS: { value: AuthMethod; label: string }[] = [
  { value: "oauth", label: "ChatGPT account" },
  { value: "api_key", label: "API key" },
]
const IMAGE_SIZE_LABELS: Record<ImageSize, string> = {
  "1024x1024": "1024 × 1024",
  "1536x1024": "1536 × 1024",
  "1024x1536": "1024 × 1536",
  auto: "Auto",
}
const LOGIN_POLL_MS = 2000

const SELECT_CLASS = cn(
  "border-input dark:bg-input/30 flex h-9 w-full min-w-0 rounded-md border bg-transparent px-3 py-1 text-base shadow-xs outline-none md:text-sm",
  "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50",
)

/** What the status card says, from the saved status and any sign-in in progress here. */
export type ProviderPhase = "not_set_up" | "signing_in" | "connected" | "expired"

export function providerPhase(status: SetupStatus, login: OAuthLogin | null): ProviderPhase {
  if (login?.state === "pending") return "signing_in"
  if (isSignInExpired(status)) return "expired"
  return status.state === "configured" ? "connected" : "not_set_up"
}

/** Text color of each phase, shared with the dot on the Settings menu. */
export const PHASE_TEXT: Record<ProviderPhase, string> = {
  not_set_up: "text-status-warning",
  signing_in: "text-icon-action",
  connected: "text-status-online",
  expired: "text-status-error",
}
const PHASE_FRAME: Record<ProviderPhase, string> = {
  not_set_up: "border-status-warning/35 bg-status-warning/[0.07]",
  signing_in: "border-icon-action/35 bg-icon-action/[0.06]",
  connected: "bg-card",
  expired: "border-status-error/40 bg-status-error/[0.07]",
}

function providerLabel(id: ProviderId): string {
  return PROVIDERS.find(provider => provider.value === id)?.label ?? id
}

function phaseCopy(phase: ProviderPhase, status: SetupStatus): { title: string; body: string } {
  switch (phase) {
    case "not_set_up":
      return { title: "Not set up", body: "Room Chat and 4 sample agents are off until you connect a provider." }
    case "signing_in":
      return { title: "Waiting for sign-in", body: "Finish signing in on chatgpt.com. This page updates by itself." }
    case "expired":
      return { title: "ChatGPT sign-in expired", body: "Room Chat and 4 sample agents are paused. Sign in again to turn them back on." }
    case "connected": {
      const method = status.auth === "oauth" ? "ChatGPT account" : "API key"
      const scope = status.image_model
        ? "Room Chat and all sample agents are on."
        : "Room Chat and the text sample agents are on. Image Generator is off."
      return { title: "Connected", body: [status.provider ? providerLabel(status.provider) : null, method, scope].filter(Boolean).join(" · ") }
    }
  }
}

function StepCard({ step, title, hint, disabled = false, children }: {
  step: number
  title: string
  hint: string
  disabled?: boolean
  children: React.ReactNode
}) {
  const id = `setup-step-${step}`
  return (
    <section aria-labelledby={id} className={cn("rounded-xl border bg-card p-5", disabled && "opacity-55")}>
      <h3 id={id} className="mb-1 flex items-baseline gap-2 text-[15px] font-semibold">
        <span aria-hidden="true" className="inline-flex size-5 items-center justify-center rounded-full bg-muted text-xs font-semibold text-muted-foreground">{step}</span>
        {title}
      </h3>
      <p className="mb-3 ml-7 text-[13px] text-muted-foreground">{hint}</p>
      {children}
    </section>
  )
}

function ConnectedRow({ label, action }: { label: string; action: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-[10px] border bg-background px-3.5 py-3" data-testid="setup-connected">
      <Check className="size-4 text-status-online" aria-hidden="true" />
      <span className="flex-[1_1_200px] text-sm">{label}</span>
      {action}
    </div>
  )
}

/** Picks the saved model when it is still offered for this choice, else the recommendation. */
function initialModel(choices: readonly string[], saved: string | null | undefined, sameChoice: boolean): string {
  if (sameChoice && saved && choices.includes(saved)) return saved
  return choices[0] ?? ""
}

export function ModelProviderSection({ onPhaseChange }: { onPhaseChange?: (phase: ProviderPhase | null) => void }) {
  const queryClient = useQueryClient()
  const status = useSetupStatus()
  const current = status.data

  // Drafts stay null until the user changes them, so they follow the saved status.
  const [providerDraft, setProviderDraft] = useState<ProviderId | null>(null)
  const [authDraft, setAuthDraft] = useState<AuthMethod | null>(null)
  const provider = providerDraft ?? current?.provider ?? "openai"
  const auth: AuthMethod = provider === "openai" ? authDraft ?? (current?.provider === "openai" ? current.auth : null) ?? "oauth" : "api_key"
  const sameChoice = current?.provider === provider && current?.auth === auth

  const models = useQuery({
    queryKey: ["setup", "models", provider, auth],
    queryFn: ({ signal }) => getSetupModels(provider, auth, signal),
    enabled: status.isSuccess,
    staleTime: Infinity,
    retry: false,
  })
  const [textDraft, setTextDraft] = useState<{ key: string; value: string } | null>(null)
  const [imageDraft, setImageDraft] = useState<{ key: string; value: string } | null>(null)
  const choiceKey = `${provider}/${auth}`
  const textModel = textDraft?.key === choiceKey ? textDraft.value : initialModel(models.data?.text ?? [], current?.text_model, sameChoice)
  const imageModel = imageDraft?.key === choiceKey
    ? imageDraft.value
    : sameChoice ? (current?.image_model ?? "") : (models.data?.image[0] ?? "")
  const selectedImage = imageModel === "" ? null : imageModel

  const [apiKey, setApiKey] = useState("")
  const [replacingKey, setReplacingKey] = useState(false)
  const [login, setLogin] = useState<OAuthLogin | null>(null)
  const [tabBlocked, setTabBlocked] = useState(false)
  const [pasted, setPasted] = useState("")
  const [imageSizeDraft, setImageSizeDraft] = useState<ImageSize | null>(null)
  const imageSize = imageSizeDraft ?? current?.image_size ?? "1024x1024"
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<{ step: 2 | 3; message: string } | null>(null)

  const phase = current ? providerPhase(current, login) : null
  useEffect(() => {
    onPhaseChange?.(phase)
  }, [phase, onPhaseChange])

  const loginId = login?.state === "pending" ? login.login_id : null
  useEffect(() => {
    if (!loginId) return
    const timer = setInterval(() => {
      getOAuthLogin(loginId).then(setLogin).catch((failure: unknown) => {
        if (setupError(failure)?.code === "login_not_found") setLogin(null)
      })
    }, LOGIN_POLL_MS)
    return () => clearInterval(timer)
  }, [loginId])

  async function run(action: string, step: 2 | 3, work: () => Promise<void>) {
    setBusy(action)
    setError(null)
    try {
      await work()
    } catch (failure) {
      setError({ step, message: setupErrorMessage(failure) })
    } finally {
      setBusy(null)
    }
  }

  function saved(status: SetupStatus, message: string) {
    queryClient.setQueryData(setupStatusQueryKey, status)
    // The save re-gated the sample agents, so their status changed.
    void queryClient.invalidateQueries({ queryKey: AGENT_CATALOG_KEY })
    setProviderDraft(null)
    setAuthDraft(null)
    setTextDraft(null)
    setImageDraft(null)
    toast.success(message)
  }

  function choose(next: { provider?: ProviderId; auth?: AuthMethod }) {
    if (next.provider) setProviderDraft(next.provider)
    if (next.auth) setAuthDraft(next.auth)
    setReplacingKey(false)
    setError(null)
  }

  const submitApiKey = () => run("api-key", 2, async () => {
    const result = await saveApiKey({ provider, api_key: apiKey.trim(), text_model: textModel, image_model: selectedImage })
    setApiKey("")
    setReplacingKey(false)
    saved(result.status, "Model provider connected")
  })

  const beginLogin = () => {
    // Open the tab inside the click, before awaiting, so the browser allows it.
    const tab = window.open("", "_blank")
    if (tab) tab.opener = null
    setTabBlocked(!tab)
    setProviderDraft("openai")
    setAuthDraft("oauth")
    return run("oauth-start", 2, async () => {
      setPasted("")
      let started: OAuthLogin
      try {
        started = await startOAuth()
      } catch (failure) {
        // One login at a time: resume the pending one instead of failing.
        const pending = setupError(failure)
        if (pending?.code !== "login_pending" || !pending.login_id) {
          tab?.close()
          throw failure
        }
        started = await getOAuthLogin(pending.login_id)
      }
      if (tab && started.authorization_url) tab.location.href = started.authorization_url
      else tab?.close()
      setLogin(started)
    })
  }

  const submitPasted = (id: string) => run("oauth-complete", 2, async () => {
    setLogin(await completeOAuth(id, pasted.trim()))
    setPasted("")
  })

  const cancelLogin = (id: string) => run("oauth-cancel", 2, async () => {
    await cancelOAuth(id)
    setLogin(null)
  })

  const authenticatedId = login?.state === "authenticated" ? login.login_id : null

  async function saveModels(loginId: string | null) {
    const result = loginId !== null || auth === "oauth"
      ? await saveOAuthModels({ login_id: loginId, text_model: textModel, image_model: selectedImage })
      : await saveApiKey({ provider, api_key: null, text_model: textModel, image_model: selectedImage })
    if (loginId) setLogin(null)
    let status = result.status
    if (imageSize !== status.image_size) {
      status = { ...status, image_size: (await saveImageSize(imageSize)).image_size }
      setImageSizeDraft(null)
    }
    return status
  }

  const submitModels = () => run("models", 3, async () => {
    const sizeChanged = imageSize !== current?.image_size
    const status = await saveModels(authenticatedId)
    saved(status, sizeChanged ? "Models saved. Restart Hybro to apply the image size." : "Models saved")
  })

  // A finished sign-in saves the recommended models at once, so it ends connected.
  const finishing = useRef<string | null>(null)
  useEffect(() => {
    if (!authenticatedId || !models.data || finishing.current === authenticatedId) return
    finishing.current = authenticatedId
    void run("oauth-finish", 2, async () => {
      saved(await saveModels(authenticatedId), "Signed in with ChatGPT")
    })
    // saveModels reads the current drafts; only a new authenticated login should trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authenticatedId, models.data])

  const header = (
    <div>
      <h2 className="mb-1 text-lg font-semibold">Model provider</h2>
      <p className="text-sm text-muted-foreground">Room Chat and the model-backed sample agents use this provider. MCP and the Echo agent work without one.</p>
    </div>
  )

  if (status.isPending) {
    return <div className="flex flex-col gap-4">{header}<p className="text-sm text-muted-foreground">Loading model provider…</p></div>
  }
  if (status.isError || !current || !phase) {
    return (
      <div className="flex flex-col gap-4">
        {header}
        <Alert variant="destructive"><AlertDescription>{setupErrorMessage(status.error)}</AlertDescription></Alert>
      </div>
    )
  }

  const copy = phaseCopy(phase, current)
  // Connected for the provider and method shown, or signed in and choosing models.
  const connectedHere = (phase === "connected" && sameChoice) || authenticatedId !== null
  const imageChoices = models.data?.image ?? []
  const imageHint = imageChoices.length > 0
    ? "Optional. Turns on the Image Generator agent."
    : provider === "openai"
      ? "Image generation needs an OpenAI API key. Image Generator stays off."
      : "Image generation needs OpenAI. Image Generator stays off."
  const modelsDirty = authenticatedId !== null
    || !sameChoice
    || textModel !== current.text_model
    || selectedImage !== current.image_model
    || imageSize !== current.image_size
  const stepError = (step: 2 | 3) => error?.step === step
    ? <Alert variant="destructive" className="mt-3"><AlertDescription>{error.message}</AlertDescription></Alert>
    : null

  let connect: React.ReactNode
  if (auth === "oauth") {
    if (login?.state === "pending") {
      connect = (
        <div className="flex flex-col gap-3 rounded-[10px] border border-icon-action/35 bg-icon-action/[0.06] p-4" data-testid="oauth-pending">
          <div className="flex flex-wrap items-center gap-3">
            <Loader2 className="size-[18px] animate-spin text-icon-action" aria-hidden="true" />
            <span className="flex-[1_1_200px] text-sm">Waiting for you to finish signing in on chatgpt.com…</span>
            <LoadingButton variant="outline" loading={busy === "oauth-cancel"} disabled={busy !== null} onClick={() => { void cancelLogin(login.login_id) }}>
              Cancel
            </LoadingButton>
          </div>
          {tabBlocked && login.authorization_url ? (
            <a href={login.authorization_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2">
              The browser blocked the new tab. Open the sign-in page<ExternalLink className="size-3.5" aria-hidden="true" />
            </a>
          ) : null}
          <details className="text-[13px] text-muted-foreground" open={!login.listener || undefined}>
            <summary className="cursor-pointer text-foreground/80">The sign-in tab ended on an error page?</summary>
            <form className="mt-2.5 flex flex-col gap-2" onSubmit={event => { event.preventDefault(); void submitPasted(login.login_id) }}>
              <p className="leading-[18px]">This happens when Hybro runs on another machine. Copy the full address from that tab and paste it here.</p>
              <Input aria-label="Address from the sign-in tab" autoComplete="off" placeholder="http://localhost:1455/auth/callback?code=…" value={pasted} onChange={event => setPasted(event.target.value)} />
              <div>
                <LoadingButton type="submit" variant="outline" loading={busy === "oauth-complete"} disabled={busy !== null || !pasted.trim()}>
                  Finish sign-in
                </LoadingButton>
              </div>
            </form>
          </details>
        </div>
      )
    } else if (connectedHere) {
      connect = (
        <ConnectedRow
          label="Signed in with a ChatGPT account"
          action={<LoadingButton variant="outline" loading={busy === "oauth-start"} disabled={busy !== null} onClick={() => { void beginLogin() }}>Sign in again</LoadingButton>}
        />
      )
    } else {
      connect = (
        <div className="flex flex-col gap-3">
          {login && (login.state === "failed" || login.state === "expired") ? (
            <Alert variant="destructive"><AlertDescription>{login.error ?? (login.state === "expired" ? "The sign-in expired." : "The sign-in failed.")}</AlertDescription></Alert>
          ) : null}
          <div className="flex flex-wrap items-center gap-3">
            <LoadingButton loading={busy === "oauth-start" || busy === "oauth-finish"} disabled={busy !== null} onClick={() => { void beginLogin() }}>
              Sign in with ChatGPT<ExternalLink className="size-3.5" aria-hidden="true" />
            </LoadingButton>
            <span className="text-[13px] text-muted-foreground">Opens chatgpt.com in a new tab. Come back here when you finish.</span>
          </div>
        </div>
      )
    }
  } else if (connectedHere && !replacingKey) {
    connect = (
      <ConnectedRow
        label="API key saved"
        action={<Button variant="outline" onClick={() => { setReplacingKey(true); setError(null) }}>Replace key</Button>}
      />
    )
  } else {
    const label = providerLabel(provider)
    connect = (
      <form onSubmit={event => { event.preventDefault(); void submitApiKey() }}>
        <FormGroup id="setup-api-key" label={`${label} API key`} hint={`Hybro checks the key with ${label}, then stores it in auth.json on this machine. It is never shown again.`}>
          <div className="flex flex-wrap gap-2">
            <Input
              id="setup-api-key"
              type="password"
              autoComplete="off"
              placeholder={provider === "anthropic" ? "sk-ant-…" : "sk-…"}
              className="w-auto flex-[1_1_260px]"
              value={apiKey}
              onChange={event => setApiKey(event.target.value)}
            />
            <LoadingButton type="submit" loading={busy === "api-key"} disabled={busy !== null || !apiKey.trim() || !textModel}>
              Check and save
            </LoadingButton>
            {replacingKey ? <Button type="button" variant="ghost" onClick={() => setReplacingKey(false)}>Cancel</Button> : null}
          </div>
        </FormGroup>
      </form>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      {header}

      <div role="status" data-testid="model-provider-status" data-phase={phase} className={cn("flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3.5", PHASE_FRAME[phase])}>
        <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full bg-current", PHASE_TEXT[phase])} />
        <span className="flex flex-[1_1_240px] flex-col gap-0.5">
          <span className={cn("text-sm font-semibold", PHASE_TEXT[phase])}>{copy.title}</span>
          <span className="text-[13px] leading-[18px] text-muted-foreground">{copy.body}</span>
        </span>
        {phase === "expired" ? (
          <LoadingButton loading={busy === "oauth-start"} disabled={busy !== null} onClick={() => { void beginLogin() }}>Sign in again</LoadingButton>
        ) : null}
      </div>

      <StepCard step={1} title="Provider" hint="Pick the service that runs your models.">
        <div role="radiogroup" aria-label="Provider" className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-2">
          {PROVIDERS.map(option => {
            const selected = option.value === provider
            return (
              <label
                key={option.value}
                className={cn(
                  "flex cursor-pointer items-start gap-2.5 rounded-[10px] border p-3",
                  selected ? "border-icon-action bg-icon-action/[0.06]" : "bg-background",
                )}
              >
                <input
                  type="radio"
                  name="setup-provider"
                  className="mt-0.5 accent-[hsl(var(--color-icon-action))]"
                  checked={selected}
                  aria-label={option.label}
                  onChange={() => choose({ provider: option.value })}
                />
                <span className="flex flex-col gap-0.5">
                  <span className="text-sm font-semibold">{option.label}</span>
                  <span className="text-xs text-muted-foreground">{option.note}</span>
                </span>
              </label>
            )
          })}
        </div>
      </StepCard>

      <StepCard
        step={2}
        title="Connect"
        hint={provider === "openai" ? "Use your ChatGPT plan, or an API key billed by usage." : `Paste an API key from your ${providerLabel(provider)} account.`}
      >
        {provider === "openai" ? (
          <SegmentedControl className="mb-4" label="Sign-in method" value={auth} options={AUTH_OPTIONS} onValueChange={value => choose({ auth: value })} />
        ) : null}
        {connect}
        {stepError(2)}
      </StepCard>

      <StepCard
        step={3}
        title="Models"
        hint={connectedHere ? "Changes apply right away, without a restart." : "Connect a provider first. The model lists come from it."}
        disabled={!connectedHere}
      >
        <form onSubmit={event => { event.preventDefault(); void submitModels() }}>
          <fieldset disabled={!connectedHere} className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-4">
            <FormGroup
              id="setup-text-model"
              label="Text model"
              hint="Used by Room Chat and the text agents."
              error={models.isError ? setupErrorMessage(models.error) : undefined}
            >
              <select id="setup-text-model" className={SELECT_CLASS} value={textModel} disabled={!models.data} onChange={event => setTextDraft({ key: choiceKey, value: event.target.value })}>
                {(models.data?.text ?? []).map((model, index) => (
                  <option key={model} value={model}>{index === 0 ? `${model} (recommended)` : model}</option>
                ))}
              </select>
            </FormGroup>
            <div className="flex flex-col gap-4">
              <FormGroup id="setup-image-model" label="Image model" hint={imageHint}>
                <select
                  id="setup-image-model"
                  className={SELECT_CLASS}
                  value={imageModel}
                  disabled={imageChoices.length === 0}
                  onChange={event => setImageDraft({ key: choiceKey, value: event.target.value })}
                >
                  {imageChoices.length > 0 ? (
                    <>
                      <option value="">None (Image Generator stays off)</option>
                      {imageChoices.map(model => <option key={model} value={model}>{model}</option>)}
                    </>
                  ) : (
                    <option value="">{provider === "openai" ? "Needs an API key" : "Needs OpenAI"}</option>
                  )}
                </select>
              </FormGroup>
              <FormGroup id="setup-image-size" label="Image size" hint="Takes effect after Hybro restarts.">
                <select
                  id="setup-image-size"
                  className={SELECT_CLASS}
                  value={imageSize}
                  disabled={imageChoices.length === 0}
                  onChange={event => setImageSizeDraft(event.target.value as ImageSize)}
                >
                  {IMAGE_SIZES.map(size => <option key={size} value={size}>{IMAGE_SIZE_LABELS[size]}</option>)}
                </select>
              </FormGroup>
            </div>
          </fieldset>
          <div className="mt-5 flex justify-end">
            <LoadingButton type="submit" loading={busy === "models"} disabled={!connectedHere || busy !== null || !textModel || !modelsDirty}>
              Save models
            </LoadingButton>
          </div>
        </form>
        {stepError(3)}
      </StepCard>
    </div>
  )
}
