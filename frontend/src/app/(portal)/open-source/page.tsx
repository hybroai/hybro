'use client'

import React, { useState, useEffect } from "react"
import Link from "next/link"
import {
  ArrowRight,
  ArrowUp,
  AtSign,
  Check,
  Copy,
  Loader2,
  Paperclip,
  Terminal,
  ExternalLink,
  BookOpen,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { FadeInSection } from "@/components/fade-in-section"
import { VideoEmbed } from "@/components/video-embed"
import { GithubIcon, DiscordIcon } from "@/components/icons"
import { PortalFooter } from "@/components/portal/portal-footer"
import { routes } from "@/lib/routes"
import { useUser, useAuth } from "@/lib/auth"
import { useChatRoomCreation } from "@/hooks/useChatRoomCreation"
import { FRAMEWORKS } from "@/components/framework-badges"
import { TypingTerminal } from "@/components/open-source/typing-terminal"

// Steps mirror README "Quick Start" and install.sh. Ports come from
// docker-compose.yml; the env file names come from install.sh.
const AI_SETUP_PROMPT = `Set up Hybro AI on my machine and get it running locally.

Repo: https://github.com/hybroai/hybro
Requires: git, Docker, Docker Compose.

1. Clone the repo and cd into it.
2. Create the env files from their examples:
   backend/.env.example        -> backend/.env
   frontend/.env.example       -> frontend/.env.local
   default_agents/.env.example -> default_agents/.env
3. Ask me for my OPENAI_API_KEY, then set the same value in
   both backend/.env and default_agents/.env. The default agents
   register without it, but their calls fail until a valid key is set.
4. Run: docker compose up -d --build
5. Wait for the containers to come up, then verify:
   App  http://localhost:3000
   API  http://localhost:8000

If a container fails, show me its \`docker compose logs\` output and fix it.`

const QUICK_START_COMMANDS = {
  script: "curl -fsSL https://raw.githubusercontent.com/hybroai/hybro/main/install.sh | sh",
  docker: "git clone https://github.com/hybroai/hybro.git && cd hybro && docker compose up -d --build",
  ai: AI_SETUP_PROMPT,
}

const QUICK_START_TABS = [
  { key: "script", label: "CURL" },
  { key: "ai", label: "Agentic" },
  { key: "docker", label: "Docker" },
] as const

type QuickStartTab = (typeof QUICK_START_TABS)[number]["key"]

const HERO_EXAMPLE_PROMPTS = [
  "Plan a travel and calculate the budget for me",
  "Search creators across all social media platform",
]

const marqueeFrameworks = FRAMEWORKS

export default function OpenSourcePage() {
  const [activeTab, setActiveTab] = useState<QuickStartTab>("script")
  const [copied, setCopied] = useState(false)

  const handleCopy = () => {
    navigator.clipboard.writeText(QUICK_START_COMMANDS[activeTab])
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // Hero prompt input + typewriter placeholder animation
  const [heroInput, setHeroInput] = useState("")
  const [promptIndex, setPromptIndex] = useState(0)
  const [charIndex, setCharIndex] = useState(0)
  const [isDeleting, setIsDeleting] = useState(false)

  useEffect(() => {
    if (heroInput) return // pause animation while the user has typed something

    const current = HERO_EXAMPLE_PROMPTS[promptIndex]
    let delay = isDeleting ? 28 : 55
    if (!isDeleting && charIndex === current.length) delay = 1500
    if (isDeleting && charIndex === 0) delay = 300

    const timer = setTimeout(() => {
      if (!isDeleting && charIndex === current.length) {
        setIsDeleting(true)
      } else if (isDeleting && charIndex === 0) {
        setIsDeleting(false)
        setPromptIndex((p) => (p + 1) % HERO_EXAMPLE_PROMPTS.length)
      } else {
        setCharIndex((c) => c + (isDeleting ? -1 : 1))
      }
    }, delay)

    return () => clearTimeout(timer)
  }, [charIndex, isDeleting, promptIndex, heroInput])

  const { user } = useUser()
  const { getToken } = useAuth()

  const handleRequireAuth = () => {
    window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.pathname)}`
  }

  const { creating, createAndNavigate } = useChatRoomCreation({
    userId: user?.id,
    userName: user?.firstName || user?.username || "User",
    getToken,
    onRequireAuth: handleRequireAuth,
  })

  const handleHeroSend = async () => {
    if (creating) return
    // createAndNavigate only surfaces a banner when userId is missing, so the
    // sign-in redirect has to be triggered here (same guard as /chat).
    if (!user?.id) {
      handleRequireAuth()
      return
    }
    const text = heroInput.trim() || HERO_EXAMPLE_PROMPTS[promptIndex]
    await createAndNavigate(text)
  }

  const handleHeroKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault()
      handleHeroSend()
    }
  }

  return (
    <div className="relative min-h-screen bg-background text-foreground overflow-x-hidden">
      {/* Background Decorators */}
      <div aria-hidden="true" className="pointer-events-none fixed inset-0 z-0">
        <div className="absolute inset-x-0 top-0 h-[560px] bg-[radial-gradient(ellipse_at_50%_0%,hsl(var(--color-hybro-hy)/0.15),transparent_38%),radial-gradient(ellipse_at_80%_10%,hsl(var(--color-hybro-bro)/0.12),transparent_35%)]" />
        <div className="absolute left-[-14rem] top-[24rem] h-[32rem] w-[32rem] rounded-full bg-[hsl(var(--color-hybro-hy)/0.06)] blur-2xl md:blur-3xl" />
        <div className="absolute right-[-16rem] top-[44rem] h-[36rem] w-[36rem] rounded-full bg-[hsl(var(--color-hybro-bro)/0.06)] blur-2xl md:blur-3xl" />
        <div
          className="absolute inset-0 opacity-[0.12]"
          style={{
            backgroundImage:
              "linear-gradient(hsl(var(--color-border) / 0.3) 1px, transparent 1px), linear-gradient(90deg, hsl(var(--color-border) / 0.3) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
            maskImage: "linear-gradient(to bottom, black 0%, black 20%, transparent 60%, black 80%, transparent 100%)",
          }}
        />
      </div>

      <div className="relative z-10 max-w-7xl mx-auto px-4 sm:px-8">
        {/* Hero Section */}
        <section className="pt-16 md:pt-24 pb-12 text-center animate-fade-up">
          {/* Version tag */}
          <div className="relative inline-flex items-center gap-2.5 px-5 mb-7 font-mono text-[11px] tracking-[0.2em] uppercase text-muted-foreground/80">
            <span aria-hidden className="absolute inset-y-0 left-0 w-px bg-gradient-to-b from-transparent via-border to-transparent" />
            <span aria-hidden className="absolute inset-y-0 right-0 w-px bg-gradient-to-b from-transparent via-border to-transparent" />
            <span className="text-brand-gradient font-semibold">hybro core</span>
            <span>open source</span>
          </div>

          <h1 className="text-4xl sm:text-5xl md:text-6xl font-bold tracking-tight mb-6 max-w-4xl mx-auto leading-[1.15]">
            <span className="text-brand-gradient">The Interoperability Engine</span>
            <br />
            for AI Agents
          </h1>

          <p className="text-lg md:text-xl text-muted-foreground max-w-2xl mx-auto mb-10 leading-relaxed text-balance">
            Complete data privacy, zero configuration, and native A2A protocol support. All running on your machine.
          </p>

          {/* Hero Prompt Input */}
          <div
            className="max-w-xl mx-auto mb-8 rounded-xl bg-muted overflow-hidden text-left"
            style={{ border: "1px solid var(--conversation-border-light)" }}
          >
            {/* Fake mention chip — decorative only */}
            <div className="flex items-center px-4 pt-3">
              <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-md bg-primary/10 text-primary">
                <AtSign className="h-3 w-3" />
                Manager Agent
              </span>
            </div>

            <input
              type="text"
              value={heroInput}
              onChange={(e) => setHeroInput(e.target.value)}
              onKeyDown={handleHeroKeyDown}
              placeholder={`${HERO_EXAMPLE_PROMPTS[promptIndex].slice(0, charIndex)}▍`}
              aria-label="Ask Hybro to do something"
              className="w-full min-w-0 bg-transparent border-0 text-[15px] text-foreground placeholder:text-muted-foreground focus:outline-none px-4 py-2"
            />

            {/* Decorative toolbar — visual only, mirrors the real chat composer */}
            <div className="flex items-center justify-between px-3 py-2 border-t border-border/40">
              <div className="flex items-center gap-1">
                <span className="flex items-center justify-center size-8 rounded-md text-muted-foreground/70 cursor-default">
                  <Paperclip className="h-4 w-4" />
                </span>
                <span className="flex items-center justify-center size-8 rounded-md text-muted-foreground/70 cursor-default">
                  <AtSign className="h-4 w-4" />
                </span>
              </div>
              <button
                onClick={handleHeroSend}
                disabled={creating}
                aria-label="Send"
                className={`flex items-center justify-center size-8 rounded-full shrink-0 transition-colors ${
                  creating
                    ? "bg-primary/40 text-primary-foreground/70"
                    : "bg-primary text-primary-foreground hover:bg-primary/90"
                }`}
              >
                {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-4 w-4" />}
              </button>
            </div>
          </div>

          {/* Action CTAs */}
          <div className="flex flex-wrap items-center justify-center gap-4 mb-12">
            <Button size="lg" asChild className="btn-brand-gradient shadow-md px-6">
              <Link href={routes.chat}>
                Launch App
                <ArrowRight className="ml-2 h-4 w-4" />
              </Link>
            </Button>
            <Button size="lg" variant="outline" asChild className="px-6 border-border/80">
              <a href="https://github.com/hybroai/hybro" target="_blank" rel="noopener noreferrer">
                <GithubIcon className="mr-2 h-4 w-4" />
                GitHub Repo
                <ExternalLink className="ml-1.5 h-3.5 w-3.5 opacity-60" />
              </a>
            </Button>
            <Button size="lg" variant="brandTint" asChild className="px-6">
              <a href="https://docs.hybro.ai" target="_blank" rel="noopener noreferrer">
                <BookOpen className="mr-2 h-4 w-4" />
                Docs
              </a>
            </Button>
          </div>

          {/* Quick Start Terminal Widget */}
          <div className="max-w-3xl mx-auto rounded-xl border border-border/60 bg-card/80 backdrop-blur-md overflow-hidden text-left">
            <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 bg-muted/40 border-b border-border/50">
              <span className="text-xs font-mono text-muted-foreground flex items-center gap-1.5">
                <Terminal className="h-3.5 w-3.5 text-[hsl(var(--color-hybro-hy))]" />
                Quick Start
              </span>
              <div className="flex items-center gap-1 bg-background/60 p-1 rounded-lg border border-border/40 text-xs">
                {QUICK_START_TABS.map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => setActiveTab(tab.key)}
                    className={`px-3 py-1.5 rounded-md transition-all ${
                      activeTab === tab.key
                        ? "bg-[hsl(var(--color-hybro-hy))]/15 text-[hsl(var(--color-hybro-hy))] font-semibold shadow-sm ring-1 ring-[hsl(var(--color-hybro-hy))]/30"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
            </div>
            {/* The AI prompt is multi-line, so it wraps and the panel grows;
                the shell commands stay on a single scrollable line. */}
            <div className="flex items-start gap-2 p-4 bg-muted dark:bg-black/40 font-mono text-xs md:text-sm text-foreground">
              <div className="flex-1 min-w-0 overflow-x-auto">
                <code
                  className={`text-[hsl(var(--color-hybro-hy))] ${
                    activeTab === "ai"
                      ? "block whitespace-pre-wrap leading-relaxed"
                      : "whitespace-nowrap"
                  }`}
                >
                  {activeTab !== "ai" && <span className="text-muted-foreground select-none">$ </span>}
                  {QUICK_START_COMMANDS[activeTab]}
                </code>
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={handleCopy}
                className="h-8 px-2.5 text-muted-foreground hover:text-foreground shrink-0"
              >
                {copied ? <Check className="h-4 w-4 text-green-600 dark:text-green-400" /> : <Copy className="h-4 w-4" />}
                <span className="sr-only">
                  {activeTab === "ai" ? "Copy prompt" : "Copy command"}
                </span>
              </Button>
            </div>
          </div>
        </section>

        {/* Feature Grid */}
        <section className="py-20">
          <FadeInSection variant="wipe">
            <div className="text-center mb-12">
              {/* Two staggered lines, kept as one heading for semantics */}
              <h2 className="inline-block text-left text-3xl sm:text-4xl md:text-6xl font-bold tracking-tight leading-[1.1] mb-6">
                <span className="block">
                  <span className="text-hybro-hy">Private</span> by default.
                </span>
                <span className="block mt-1 md:mt-2 md:ml-28">
                  <span className="text-hybro-bro">Open</span> by design.
                </span>
              </h2>
              <p className="text-muted-foreground/90 max-w-3xl mx-auto text-balance">
                Everything you need to orchestrate autonomous AI agent workflows locally or at scale.
              </p>
            </div>
          </FadeInSection>

          {/* Pillar 1 — text left, live terminal right */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-10 md:gap-16 items-center py-10 md:py-14">
            <FadeInSection variant="left" delay={150}>
              <div>
                <h3 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
                  Local-First &amp; Private
                </h3>
                <p className="text-base md:text-lg text-muted-foreground leading-relaxed max-w-md">
                  Run completely offline. No telemetry, no tracking, no cloud lock-in. Your data never leaves your machine.
                </p>
              </div>
            </FadeInSection>
            <FadeInSection variant="right" delay={450}>
              <TypingTerminal />
            </FadeInSection>
          </div>

          {/* Pillar 2 — staggered logo wall left, text right */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-10 md:gap-16 items-center py-10 md:py-14">
            <FadeInSection variant="left" delay={450} className="order-2 md:order-1">
              <div className="group/wall flex flex-wrap justify-center gap-3 md:gap-4">
                {marqueeFrameworks.map((fw, i) => {
                  const offsets = ["md:translate-y-0", "md:translate-y-5", "md:-translate-y-3", "md:translate-y-3", "md:-translate-y-5", "md:translate-y-2", "md:-translate-y-1"]
                  const tile = (
                    <>
                      <span className={fw.color}>{fw.icon}</span>
                      <span className="text-[11px] font-medium text-muted-foreground whitespace-nowrap">
                        {fw.name}
                      </span>
                    </>
                  )
                  const tileClass = `flex flex-col items-center justify-center gap-2 w-[104px] h-[104px] transition-opacity duration-300 ease-out group-hover/wall:opacity-40 hover:opacity-100! ${offsets[i % offsets.length]}`

                  return fw.url ? (
                    <a key={fw.name} href={fw.url} target="_blank" rel="noopener noreferrer" className={tileClass}>
                      {tile}
                    </a>
                  ) : (
                    <div key={fw.name} className={`${tileClass} cursor-default`}>
                      {tile}
                    </div>
                  )
                })}
              </div>
            </FadeInSection>

            <FadeInSection variant="right" delay={150} className="order-1 md:order-2">
              <div>
                <h3 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
                  Works with any framework
                </h3>
                <p className="text-base md:text-lg text-muted-foreground leading-relaxed max-w-md">
                  Using the Agent2Agent protocol standard, agents talk to each other in one shared format.
                </p>
              </div>
            </FadeInSection>
          </div>

          {/* Supporting capabilities — hairline grid, no cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 pt-10 md:pt-14">
            {[
              {
                title: "Zero-Config Dev Mode",
                body: "Comes with mock credentials and environment templates. Run in one line.",
                tint: "hover:bg-[hsl(var(--color-hybro-bro)/0.03)]",
                rule: "bg-[hsl(var(--color-hybro-bro))]",
              },
              {
                title: "Multi-Agent Rooms",
                body: "Open a room for your agents to collaborate, debate, and solve multi-step problems.",
                tint: "hover:bg-[hsl(var(--color-hybro-hy)/0.03)]",
                rule: "bg-[hsl(var(--color-hybro-hy))]",
              },
              {
                title: "Modular Core Architecture",
                body: "Clean, layered architecture: FastAPI orchestration, Redis pub/sub and MongoDB persistence.",
                tint: "hover:bg-[hsl(var(--color-hybro-bro)/0.03)]",
                rule: "bg-[hsl(var(--color-hybro-bro))]",
              },
              {
                title: "Human-in-the-Loop",
                body: "Delivery is under your control. Approve, reject, or override before it ships.",
                tint: "hover:bg-[hsl(var(--color-hybro-hy)/0.03)]",
                rule: "bg-[hsl(var(--color-hybro-hy))]",
              },
            ].map((item, i) => (
              <FadeInSection key={item.title} variant="rise" delay={i * 160}>
                <div
                  className={`group/cap relative h-full px-6 py-8 md:px-10 md:py-10 transition-colors duration-300 ${item.tint} ${
                    i % 2 === 1 ? "md:border-l border-border/40" : ""
                  } ${i >= 2 ? "border-t border-border/40" : ""}`}
                >
                  <span
                    aria-hidden
                    className={`absolute left-0 top-8 bottom-8 w-px origin-top scale-y-0 group-hover/cap:scale-y-100 transition-transform duration-500 ease-out ${item.rule}`}
                  />
                  <h3 className="text-lg md:text-xl font-semibold mb-2 transition-transform duration-300 group-hover/cap:translate-x-1">
                    {item.title}
                  </h3>
                  <p className="text-sm text-muted-foreground leading-relaxed max-w-sm transition-transform duration-300 group-hover/cap:translate-x-1">
                    {item.body}
                  </p>
                </div>
              </FadeInSection>
            ))}
          </div>
        </section>

        {/* 3-Step Workflow */}
        <section className="py-16">
          <FadeInSection variant="wipe">
            <h2 className="text-2xl md:text-3xl font-bold mb-14 text-center">
              How to Get Started
            </h2>
          </FadeInSection>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-y-12 md:gap-x-10">
            {[
              {
                title: "Spin Up Engine",
                body: "Run the 1-liner script or Docker Compose to start the local backend and frontend services.",
                example: "docker compose up -d --build",
              },
              {
                title: "Register Agents",
                body: "Connect your local Python or remote A2A agents with the Agent2Agent adapter SDK.",
                example: "POST /api/v1/agent/registerAgent",
              },
              {
                title: "Orchestrate Rooms",
                body: "Create multi-agent rooms, prompt your cluster, and monitor live task collaboration.",
                example: "POST /api/v1/roomCenter/createNewRoom",
              },
            ].map((step, i) => {
              // Each numeral fills a consecutive slice of the bro → hy brand gradient,
              // so the three of them read as one continuous transition across the row.
              // n runs 0..3, mapping to 0%..100% — staying in range keeps color-mix valid.
              const stop = (n: number) =>
                `color-mix(in oklch, hsl(var(--color-hybro-bro)), hsl(var(--color-hybro-hy)) ${Math.round((n / 3) * 100)}%)`

              return (
                <FadeInSection key={step.title} variant="rise" delay={300 + i * 220}>
                  <div className="group/step relative flex flex-col items-center text-center px-2">
                    <div className="relative mb-3">
                      {/* Ghost numeral watermark, seated left of the title */}
                      <span
                        aria-hidden
                        className="font-spaceGrotesk text-[4rem] font-bold leading-none select-none pointer-events-none absolute right-full top-1/2 -translate-y-1/2 -mr-3 opacity-[0.17]"
                        style={{
                          backgroundImage: `linear-gradient(115deg, ${stop(i)}, ${stop(i + 1)})`,
                          WebkitBackgroundClip: "text",
                          backgroundClip: "text",
                          WebkitTextFillColor: "transparent",
                        }}
                      >
                        {i + 1}
                      </span>

                      <h3 className="relative text-2xl md:text-3xl font-semibold tracking-tight">{step.title}</h3>
                    </div>
                    <p className="relative text-sm text-muted-foreground leading-relaxed max-w-xs mb-6">
                      {step.body}
                    </p>

                    <code className="relative inline-flex items-center max-w-full overflow-x-auto whitespace-nowrap rounded-lg border border-border/40 bg-muted/30 px-3 py-1.5 font-mono text-xs text-muted-foreground/90">
                      <span className="text-[hsl(var(--color-hybro-hy))] select-none">$&nbsp;</span>
                      {step.example}
                    </code>
                  </div>
                </FadeInSection>
              )
            })}
          </div>
        </section>

        {/* Demo Video Section */}
        <section className="py-16 border-t border-border/40 animate-fade-up">
          <h2 className="text-xl font-semibold text-muted-foreground uppercase tracking-wider mb-8 text-center">
            See Hybro in Action
          </h2>
          <VideoEmbed
            videoId="P0kyUQAxnZg"
            title="HYBRO Core Demo - Multi-Agent Collaboration Engine"
            className="block max-w-4xl mx-auto"
          />
        </section>

        {/* Open Source License & Community Footer CTA */}
        <section className="py-16">
          <FadeInSection variant="rise">
            <div className="relative p-8 md:p-14 text-center max-w-5xl mx-auto">
              {/* Light falling from top center. Blurred blob rather than a
                  boxed gradient, so it has no edge to clip against. */}
              <span
                aria-hidden
                className="pointer-events-none absolute left-1/2 top-0 h-[34rem] w-[62rem] max-w-[150%] -translate-x-1/2 -translate-y-1/3 rounded-full blur-3xl"
                style={{
                  background:
                    "radial-gradient(ellipse at center, hsl(var(--color-hybro-hy) / 0.18), hsl(var(--color-hybro-bro) / 0.09) 45%, transparent 72%)",
                }}
              />

              <div className="relative">
                <h2 className="text-4xl md:text-5xl lg:text-[2.5rem] font-bold mb-5 tracking-tight leading-[1.15]">
                  Join the Open Source <span className="text-brand-gradient">Agent Community</span>
                </h2>
                <p className="text-base md:text-lg text-muted-foreground max-w-2xl mx-auto mb-8 leading-relaxed text-balance">
                  Hybro Core is released under the permissive Apache License 2.0. We welcome contributions, agent integrations, and feedback.
                </p>
                <div className="flex flex-wrap items-center justify-center gap-4 mb-10">
                  <Button size="lg" asChild className="btn-brand-gradient">
                    <a href="https://github.com/hybroai/hybro" target="_blank" rel="noopener noreferrer">
                      <GithubIcon className="mr-2 h-4 w-4" />
                      Star on GitHub
                    </a>
                  </Button>
                  <Button size="lg" variant="outline" asChild>
                    <a href="https://discord.gg/2S5pCKzUmJ" target="_blank" rel="noopener noreferrer">
                      <DiscordIcon className="mr-2 h-4 w-4 text-[#7289DA]" />
                      Join Discord
                    </a>
                  </Button>
                </div>
              </div>
            </div>
          </FadeInSection>
        </section>

        <PortalFooter />
      </div>
    </div>
  )
}
