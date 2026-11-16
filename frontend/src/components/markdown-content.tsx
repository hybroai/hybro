'use client'

import React, { useCallback, useRef, useState } from 'react'
import { Streamdown, defaultRehypePlugins } from 'streamdown'
import type { PluggableList } from 'unified'
import rehypeHighlight from 'rehype-highlight'
import { AlertCircle, Check, ChevronRight, Code2, Copy, ImageIcon } from 'lucide-react'
import { cn, formatIfJson } from '@/lib/utils'
import { getPlainTextFromRange } from '@/lib/selection-plain-text'
import { preprocessConversationMarkdown } from '@/lib/markdown/normalize-conversation'
import { conversationRemarkPlugins } from '@/lib/markdown/conversation-remark-plugins'
import { isHashNumberedListItemText } from '@/lib/markdown/hash-numbered-list-item'
import { useRoomFile } from '@/hooks/useRoomFile'
import { ImageLightbox } from './image-lightbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from './ui/collapsible'

const MENTION_CLIPBOARD_MIME = 'application/x-hybro-mentions'

/** Streamdown replaces default rehype plugins when `rehypePlugins` is set — keep sanitize/harden. */
const streamdownRehypePlugins: PluggableList = [
  ...Object.values(defaultRehypePlugins),
  rehypeHighlight,
]

/** Conversation remark surgery needs the full document, not Streamdown block chunks. */
const parseConversationAsSingleBlock = (markdown: string) => [markdown]

/**
 * Extract plain text from React children tree (strips HTML / highlight spans).
 */
function extractText(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (!node) return ''
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (typeof node === 'object' && 'props' in node) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return extractText((node as React.ReactElement<any>).props.children)
  }
  return ''
}

/**
 * Code block with a copy-to-clipboard button in the top-right corner.
 */
function CodeBlockWithCopy({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleCopy = useCallback(() => {
    const text = extractText(children).replace(/\n$/, '')
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopied(false), 2000)
    }).catch(() => {})
  }, [children])

  return (
    <div className="relative group">
      <pre className="max-w-full bg-slate-100 dark:bg-slate-900 text-slate-700 dark:text-slate-200 p-3 pr-10 rounded-md overflow-x-auto border border-slate-200 dark:border-slate-700">
        <code className={className} {...props}>
          {children}
        </code>
      </pre>
      <button
        type="button"
        onClick={handleCopy}
        aria-label={copied ? 'Copied' : 'Copy'}
        className="absolute top-2 right-2 p-1 rounded-md text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors duration-150"
      >
        {copied ? (
          <Check className="h-4 w-4 text-green-500" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
      </button>
    </div>
  )
}

/**
 * Collapsible wrapper for JSON fenced code blocks in markdown.
 * Starts closed so large JSON responses don't flood the message view.
 */
function CollapsibleCodeBlock({
  className,
  children,
  lineCount,
}: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode; lineCount: number }) {
  const [open, setOpen] = useState(false)

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="my-1">
      <CollapsibleTrigger className="inline-flex cursor-pointer items-center gap-1 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors">
        <ChevronRight
          className="h-3.5 w-3.5 transition-transform duration-150 ease-in-out"
          style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)' }}
        />
        <Code2 className="h-3.5 w-3.5" />
        <span>JSON</span>
        <span className="text-muted-foreground/60">
          · {lineCount} {lineCount === 1 ? 'line' : 'lines'}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="data-[state=open]:animate-collapsible-down overflow-hidden">
        <div className="mt-1">
          <CodeBlockWithCopy className={className}>{children}</CodeBlockWithCopy>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}


function processMentions(content: string): string {
  return content.replace(
    /<@([^|]+)\|([^>]+)>/g,
    '[@$2](/network/agents/$1)'
  )
}

/** Supervisor prompts once asked for "4 spaces" indent; models sometimes echo that phrase. */
function stripLiteralFourSpacesPrefix(content: string): string {
  return content.replace(/^4 spaces /gm, '')
}

/** Check if a link href points to an agent profile (i.e. was an @mention). */
function isAgentMentionHref(href: string | undefined): boolean {
  return !!href && href.startsWith('/network/agents/')
}

export function copySelectionWithMentions(
  e: React.ClipboardEvent<HTMLElement>,
  container: HTMLElement
) {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return

  const range = selection.getRangeAt(0)
  if (!container.contains(range.commonAncestorContainer)) return

  const plainText = getPlainTextFromRange(range)

  let mentionStorageText = plainText
  const mentionEls = Array.from(container.querySelectorAll<HTMLElement>('.room-mention'))
    .filter((el) => {
      try {
        return range.intersectsNode(el)
      } catch {
        return false
      }
    })

  mentionEls.forEach((el) => {
    const agentId =
      el.dataset.id ||
      (el.getAttribute('href') || '').match(/\/agents\/([^/?#]+)/)?.[1]
    const mentionText = (el.textContent || '').trim()
    const agentName = mentionText.startsWith('@') ? mentionText.slice(1) : mentionText
    if (!agentId || !mentionText) return
    mentionStorageText = mentionStorageText.replace(mentionText, `<@${agentId}|${agentName}>`)
  })

  e.preventDefault()
  e.clipboardData.setData('text/plain', plainText)
  e.clipboardData.setData(MENTION_CLIPBOARD_MIME, mentionStorageText)
}

function isConversationMarkdownClass(className?: string): boolean {
  return !!className?.includes('conversation-markdown-body')
}

/** Tracks `<ol>` nesting depth so section counters apply only to top-level lists. */
const OlDepthContext = React.createContext(0)

function MarkdownOrderedList({
  children,
  start,
  className,
  conversationTypography,
  ...olProps
}: React.OlHTMLAttributes<HTMLOListElement> & {
  children?: React.ReactNode
  conversationTypography: boolean
}) {
  const depth = React.useContext(OlDepthContext)
  const isTopLevel = depth === 0
  return (
    <OlDepthContext.Provider value={depth + 1}>
      <ol
        className={className}
        style={
          conversationTypography && isTopLevel
            ? { counterReset: `conv-section-ol ${(start ?? 1) - 1}` }
            : undefined
        }
        {...olProps}
      >
        {children}
      </ol>
    </OlDepthContext.Provider>
  )
}

function MarkdownImage({
  src,
  alt,
  className,
  conversationTypography,
  ...props
}: React.ImgHTMLAttributes<HTMLImageElement> & {
  conversationTypography: boolean
}) {
  let fileId: string | undefined
  if (typeof src === 'string') {
    const roomFilePath = /^\/api\/v1\/files\/([a-zA-Z0-9_-]+)\/content$/
    // Path-relative only (not protocol-relative //host/...). Absolute URLs must
    // be same-origin — never treat bare localhost-shaped hosts as room files.
    if (src.startsWith('/') && !src.startsWith('//')) {
      const match = src.match(roomFilePath)
      if (match) {
        fileId = match[1]
      }
    } else {
      try {
        const url = new URL(src)
        const isSameOrigin =
          typeof window !== 'undefined' && url.origin === window.location.origin
        if (isSameOrigin) {
          const match = url.pathname.match(roomFilePath)
          if (match) {
            fileId = match[1]
          }
        }
      } catch {
        // Ignore invalid URLs
      }
    }
  }
  const { objectUrl, error } = useRoomFile(fileId, Boolean(fileId))

  if (fileId) {
    if (objectUrl) {
      return (
        <span className="my-1 inline-block">
          <ImageLightbox
            src={objectUrl}
            alt={alt || 'image'}
            caption={alt || undefined}
          />
        </span>
      )
    }
    if (error) {
      return (
        <span className="my-1 inline-flex items-center gap-2 rounded-md border border-dashed border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span>This image is no longer available</span>
        </span>
      )
    }
    return (
      <span className="my-1 inline-block h-48 w-full animate-pulse rounded-md bg-muted/50" />
    )
  }

  const externalHost = externalImageHost(src)
  if (externalHost) {
    return (
      <ExternalImage
        src={src as string}
        host={externalHost}
        alt={alt}
        className={className}
        conversationTypography={conversationTypography}
        {...props}
      />
    )
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt ?? ''}
      className={cn('max-w-full h-auto rounded-md', !conversationTypography && 'my-2', className)}
      {...props}
    />
  )
}

/**
 * Host of an image URL on another origin, or null for same-origin paths and
 * inline data/blob URLs. Loading a remote image tells its server the viewer's
 * IP address and when the message was read, so those wait for a click.
 */
function externalImageHost(src: unknown): string | null {
  if (typeof src !== 'string' || typeof window === 'undefined') return null
  try {
    const url = new URL(src, window.location.href)
    if (url.protocol === 'data:' || url.protocol === 'blob:') return null
    return url.origin === window.location.origin ? null : url.host
  } catch {
    return null
  }
}

function ExternalImage({
  src,
  host,
  alt,
  className,
  conversationTypography,
  ...props
}: React.ImgHTMLAttributes<HTMLImageElement> & {
  src: string
  host: string
  conversationTypography: boolean
}) {
  const [loaded, setLoaded] = useState(false)

  if (loaded) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        {...props}
        src={src}
        alt={alt ?? ''}
        referrerPolicy="no-referrer"
        className={cn('max-w-full h-auto rounded-md', !conversationTypography && 'my-2', className)}
      />
    )
  }

  return (
    <button
      type="button"
      onClick={(event) => {
        // A linked image puts this button inside an <a>; load instead of navigating.
        event.preventDefault()
        event.stopPropagation()
        setLoaded(true)
      }}
      title={src}
      className="my-1 inline-flex max-w-full items-center gap-2 rounded-md border border-dashed border-border bg-muted/50 px-3 py-2 text-left text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      <ImageIcon className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 break-all">
        {alt ? `${alt} · ` : ''}Load image from {host}
      </span>
    </button>
  )
}

/** Shared custom component overrides used by all Streamdown instances. */
function makeComponents(
  isStreaming: boolean,
  conversationTypography: boolean,
  collapseJsonCodeBlocks: boolean,
) {
  const blockSpacing = conversationTypography ? undefined : 'mb-2 last:mb-0'
  const listSpacing = conversationTypography ? undefined : 'mb-2 ml-4 list-disc'
  const orderedListSpacing = conversationTypography ? undefined : 'mb-2 ml-4 list-decimal'
  const listItemSpacing = conversationTypography ? undefined : 'mb-1'

  return {
  a: ({ children, href, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) => {
    if (isAgentMentionHref(href)) {
      return (
        <a
          className="prose room-mention mx-1 select-text hover:underline underline-offset-2 transition-opacity hover:opacity-80"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          {...props}
        >
          {children}
        </a>
      )
    }
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 hover:underline underline-offset-2 transition-colors duration-150 break-all"
        {...props}
      >
        {children}
      </a>
    )
  },
  code: ({ className, children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) => {
    const match = /language-(\w+)/.exec(className || '')
    // Fenced blocks without a language tag have no className, but they always
    // contain newlines. Inline backticks never do. Use this to distinguish them
    // so that unlabelled ``` blocks still get <pre> treatment.
    const isInline = !match && !extractText(children).includes('\n')
    if (isInline) {
      return (
        <code
          className={cn(
            'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-200 px-1.5 py-0.5 rounded font-mono',
            !conversationTypography && 'text-sm',
          )}
          {...props}
        >
          {children}
        </code>
      )
    }
    const isJson = match?.[1] === 'json'
    if (isJson && !isStreaming && collapseJsonCodeBlocks) {
      const text = extractText(children)
      const lineCount = text.split('\n').filter((l, i, a) => i < a.length - 1 || l.trim()).length
      return (
        <CollapsibleCodeBlock className={className} lineCount={lineCount}>
          {children}
        </CollapsibleCodeBlock>
      )
    }
    return (
      <CodeBlockWithCopy className={className} {...props}>
        {children}
      </CodeBlockWithCopy>
    )
  },
  p: ({ children }: { children?: React.ReactNode }) => <p className={blockSpacing}>{children}</p>,
  ul: ({ children }: { children?: React.ReactNode }) => (
    <ul className={listSpacing ?? (conversationTypography ? 'list-disc' : undefined)}>{children}</ul>
  ),
  ol: (props: React.OlHTMLAttributes<HTMLOListElement> & { children?: React.ReactNode }) => (
    <MarkdownOrderedList
      className={orderedListSpacing ?? (conversationTypography ? undefined : 'list-decimal')}
      conversationTypography={conversationTypography}
      {...props}
    />
  ),
  li: ({ children }: { children?: React.ReactNode }) => {
    const hashNumbered = conversationTypography
      && isHashNumberedListItemText(extractText(children))
    return (
      <li
        className={cn(
          listItemSpacing,
          hashNumbered && 'conv-hash-numbered-item',
        )}
      >
        {children}
      </li>
    )
  },
  h1: ({ children }: { children?: React.ReactNode }) => (
    <h1 className={conversationTypography ? undefined : 'text-lg font-bold mb-2'}>{children}</h1>
  ),
  h2: ({ children }: { children?: React.ReactNode }) => (
    <h2 className={conversationTypography ? undefined : 'text-base font-bold mb-2'}>{children}</h2>
  ),
  h3: ({ children }: { children?: React.ReactNode }) => (
    <h3 className={conversationTypography ? undefined : 'text-sm font-bold mb-1'}>{children}</h3>
  ),
  h4: ({ children }: { children?: React.ReactNode }) => (
    <h4 className={conversationTypography ? undefined : 'text-sm font-semibold mb-1'}>{children}</h4>
  ),
  h5: ({ children }: { children?: React.ReactNode }) => (
    <h5 className={conversationTypography ? undefined : 'text-xs font-semibold mb-1'}>{children}</h5>
  ),
  h6: ({ children }: { children?: React.ReactNode }) => (
    <h6 className={conversationTypography ? undefined : 'text-xs font-medium mb-1'}>{children}</h6>
  ),
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className={conversationTypography ? 'overflow-x-auto' : 'overflow-x-auto my-2'}>
      <table className={cn('min-w-full border-collapse', !conversationTypography && 'text-sm')}>{children}</table>
    </div>
  ),
  thead: ({ children }: { children?: React.ReactNode }) => (
    <thead className="bg-slate-100 dark:bg-slate-800">{children}</thead>
  ),
  th: ({ children }: { children?: React.ReactNode }) => (
    <th
      className={cn(
        'border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-left font-semibold',
        !conversationTypography && 'text-xs',
      )}
    >
      {children}
    </th>
  ),
  td: ({ children }: { children?: React.ReactNode }) => (
    <td
      className={cn(
        'border border-slate-200 dark:border-slate-700 px-3 py-1.5',
        !conversationTypography && 'text-xs',
      )}
    >
      {children}
    </td>
  ),
  img: ({ alt, src, className, ...props }: React.ImgHTMLAttributes<HTMLImageElement>) => (
    <MarkdownImage
      src={src}
      alt={alt}
      className={className}
      conversationTypography={conversationTypography}
      {...props}
    />
  ),
  }
}

/**
 * Shared markdown renderer for all message types.
 *
 * Uses Streamdown for both streaming and static rendering:
 *  - streaming: handles incomplete/unterminated markdown, shows a caret
 *  - static (mode="static"): same output, zero animation overhead
 *
 * @param content        - The raw text / markdown to render.
 * @param isStreaming    - Pass true while tokens are still arriving.
 * @param autoFormatJson - When true (default), raw JSON strings are wrapped in
 *                         a fenced code block for pretty-printing.
 * @param collapseJsonCodeBlocks - When true (default), JSON fenced code blocks
 *                                 get their own collapsible wrapper.
 */
export function MarkdownContent({
  content,
  isStreaming = false,
  autoFormatJson = true,
  collapseJsonCodeBlocks = true,
  className,
}: {
  content: string
  isStreaming?: boolean
  autoFormatJson?: boolean
  collapseJsonCodeBlocks?: boolean
  className?: string
}) {
  const contentRef = useRef<HTMLDivElement>(null)
  const conversationTypography = isConversationMarkdownClass(className)
  const processedContent = React.useMemo(() => {
    const formatted = autoFormatJson ? formatIfJson(content) : content
    const mentionProcessed = stripLiteralFourSpacesPrefix(processMentions(formatted))
    if (!conversationTypography) return mentionProcessed
    return preprocessConversationMarkdown(mentionProcessed, { streaming: isStreaming })
  }, [content, autoFormatJson, conversationTypography, isStreaming])
  // Memoize components by isStreaming to avoid Streamdown re-rendering on every render
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const components = React.useMemo(
    () => makeComponents(isStreaming, conversationTypography, collapseJsonCodeBlocks),
    [isStreaming, conversationTypography, collapseJsonCodeBlocks],
  )
  const handleCopy = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    const container = contentRef.current
    if (!container) return
    copySelectionWithMentions(e, container)
  }, [])

  return (
    <div
      ref={contentRef}
      onCopy={handleCopy}
      className={cn(
        'min-w-0 text-inherit',
        conversationTypography ? null : 'text-sm leading-relaxed',
        className,
      )}
    >
      <Streamdown
        mode={isStreaming ? 'streaming' : 'static'}
        caret={isStreaming ? 'block' : undefined}
        components={components}
        remarkPlugins={conversationTypography ? conversationRemarkPlugins : undefined}
        parseMarkdownIntoBlocksFn={
          conversationTypography ? parseConversationAsSingleBlock : undefined
        }
        rehypePlugins={streamdownRehypePlugins}
      >
        {processedContent}
      </Streamdown>
    </div>
  )
}
