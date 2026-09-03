import { BookOpen, Mail, Code2 } from "lucide-react"
import { DiscordIcon, GithubIcon } from "@/components/icons"
import Link from "next/link"

export function PortalFooter() {
  return (
    <footer className="py-10 px-4 md:px-8 border-t border-border/40">
      <div className="max-w-5xl mx-auto">
        <div className="flex flex-wrap items-center justify-center gap-x-6 gap-y-3 mb-6">
          <Link
            href="/core"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors py-2"
          >
            <Code2 className="h-4 w-4 text-[hsl(var(--color-hybro-hy))]" />
            Core
          </Link>
          <a
            href="https://github.com/hybroai/hybro"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors py-2"
          >
            <GithubIcon className="h-4 w-4" />
            hybro
          </a>
          <a
            href="https://docs.hybro.ai/"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors py-2"
          >
            <BookOpen className="h-4 w-4" />
            Docs
          </a>
          <a
            href="https://github.com/hybroai/a2a-adapter"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors py-2"
          >
            <GithubIcon className="h-4 w-4" />
            a2a-adapter
          </a>
          <a
            href="https://discord.gg/2S5pCKzUmJ"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-[#7289DA] transition-colors py-2"
          >
            <DiscordIcon className="h-4 w-4" />
            Discord
          </a>
        </div>
        <div className="flex flex-col sm:flex-row items-center justify-between gap-3 pt-4 border-t border-border/30">
          <a
            href="mailto:info@hybro.ai"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors py-2"
          >
            <Mail className="h-4 w-4" />
            info@hybro.ai
          </a>
          <p className="text-sm text-muted-foreground">
            &copy; {new Date().getFullYear()} Hybro AI. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  )
}
