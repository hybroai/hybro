'use client'

import { useState, useRef, useEffect, useMemo } from 'react'
import Image from 'next/image'
import { ArrowUp, Square, AtSign, Maximize2, Minimize2, X, Quote, ChevronsUpDown } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { GroupSelector } from '@/components/group-selector'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import type { AgentGroup, MessageDispatchInput, TargetModeDispatchInput } from '@/lib/types/agent-group'
import { BUILTIN_GROUP_ALL_AGENTS, resolveSelectedGroupDispatch } from '@/lib/types/agent-group'
import { cn } from '@/lib/utils'
import type { QuoteData } from '@/lib/types/quote'
import type { PendingAttachment } from '@/lib/types/attachments'
import { FileAttachmentButton, ACCEPTED_MIME_SET, MAX_FILE_SIZE, MAX_ATTACHMENTS } from './file-attachment-button'
import { ModeSelector } from './mode-selector'
import type { ChatMode } from '@/lib/types/chat-mode'
import { DEFAULT_CHAT_MODE } from '@/lib/types/chat-mode'
import { AttachmentPreview } from './attachment-preview'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { mentionColor } from '@/lib/mention-color'

const _parsed = parseInt(process.env.NEXT_PUBLIC_MAX_MESSAGE_LENGTH || '10000', 10)
export const MAX_MESSAGE_LENGTH = Number.isNaN(_parsed) || _parsed < 1 ? 10000 : _parsed
const COUNTER_VISIBLE_THRESHOLD = Math.floor(MAX_MESSAGE_LENGTH * 0.95)
const WARNING_THRESHOLD = Math.floor(MAX_MESSAGE_LENGTH * 0.99)
const MENTION_CLIPBOARD_MIME = 'application/x-hybro-mentions'

export interface RoomChatInputAgent {
  id: string
  name: string
  iconUrl?: string | null
}

interface RoomChatInputProps {
  onSubmit: (message: string, dispatch: MessageDispatchInput, quote?: QuoteData | null, attachments?: PendingAttachment[]) => void
  /**
   * When true, the editor itself is disabled (read-only).
   * For normal sending-state control, prefer using disableSend.
   */
  disabled?: boolean
  /**
   * When true, the Send button is disabled, but typing is still allowed.
   */
  disableSend?: boolean
  /**
   * When true, shows spinner (message is being created/parsed, cancellation won't work)
   */
  sending?: boolean
  /**
   * When true, shows Stop button instead of Send button and allows cancelling
   */
  processing?: boolean
  /**
   * When true, shows a disabled spinner button indicating cancellation is in progress
   */
  cancelling?: boolean
  /**
   * Callback when user clicks Stop button to cancel ongoing processing
   */
  onCancel?: () => void
  agents: RoomChatInputAgent[]
  /** Agent IDs belonging to the current room (for filtering mentions when room_team is selected). */
  roomAgentIds?: string[]
  // Group selector props
  groups?: AgentGroup[]
  loadingGroups?: boolean
  selectedGroup?: string
  selectedGroupName?: string
  roomMembershipLabel?: string
  selectedGroupDispatch?: TargetModeDispatchInput
  onGroupChange?: (groupId: string) => void
  onCreateGroup?: () => void
  onEditGroup?: (group: AgentGroup) => void
  onDeleteGroup?: (group: AgentGroup) => void
  showGroupSelector?: boolean
  disableGroupSelector?: boolean
  /**
   * External value to set in the editor (for quick start templates etc.)
   * When this changes to a non-empty value, it updates the editor content.
   */
  externalValue?: string
  /**
   * Callback when external value has been consumed (set in editor).
   * Call this to reset externalValue to empty after it's been applied.
   */
  onExternalValueConsumed?: () => void
  /**
   * When true, externalValue updates re-apply on every change (for demo/typewriter UIs).
   * Does not call onExternalValueConsumed or steal focus.
   */
  continuousExternalUpdate?: boolean
  /** Fired when the editor receives focus (e.g. to pause demo animations). */
  onEditorFocus?: () => void
  /** Currently quoted message (shown as preview above editor). */
  quote?: QuoteData | null
  /** Callback to clear the current quote. */
  onClearQuote?: () => void
  /**
   * Content above the editor (e.g. HITL Questions panel).
   */
  topSlot?: React.ReactNode
  /** Current chat mode. */
  chatMode?: ChatMode
  /** Callback when the user changes the chat mode. */
  onChatModeChange?: (mode: ChatMode) => void
  /** When true, keep the mode selector visible but non-interactive. */
  disableModeSelector?: boolean
  /** When true, keep the attach button visible but non-clickable. */
  disableAttachmentButton?: boolean
  /** When true, keep the mention button visible but non-clickable. */
  disableMentionButton?: boolean
}

export function RoomChatInput({
  onSubmit,
  disabled = false,
  disableSend = false,
  sending = false,
  processing = false,
  cancelling = false,
  onCancel,
  agents,
  roomAgentIds = [],
  groups = [],
  loadingGroups = false,
  selectedGroup = BUILTIN_GROUP_ALL_AGENTS,
  selectedGroupName,
  roomMembershipLabel,
  selectedGroupDispatch,
  onGroupChange,
  onCreateGroup,
  onEditGroup,
  onDeleteGroup,
  showGroupSelector = true,
  disableGroupSelector = false,
  externalValue,
  onExternalValueConsumed,
  continuousExternalUpdate = false,
  onEditorFocus,
  quote,
  onClearQuote,
  topSlot,
  chatMode,
  onChatModeChange,
  disableModeSelector = false,
  disableAttachmentButton = false,
  disableMentionButton = false,
}: RoomChatInputProps) {
  const [message, setMessage] = useState('') // Storage format: <@id|name>
  const [showAgentSuggestions, setShowAgentSuggestions] = useState(false)
  const [mentionQuery, setMentionQuery] = useState('')
  const [selectedAgentIndex, setSelectedAgentIndex] = useState(0)
  const [isEditorExpanded, setIsEditorExpanded] = useState(false)
  const [isOverflowing, setIsOverflowing] = useState(false)
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [plainTextLength, setPlainTextLength] = useState(0)
  const attachmentsRef = useRef<PendingAttachment[]>([])
  const editorRef = useRef<HTMLDivElement>(null)
  const suggestionsRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const focusEditorAtEnd = () => {
    setTimeout(() => {
      if (editorRef.current) {
        editorRef.current.focus()
        try {
          const range = document.createRange()
          const selection = window.getSelection()
          range.selectNodeContents(editorRef.current)
          range.collapse(false)
          selection?.removeAllRanges()
          selection?.addRange(range)
        } catch (err) {
          console.error('Error setting caret position:', err)
        }
      }
    }, 50)
  }

  const handleGroupChange = (groupId: string) => {
    onGroupChange?.(groupId)
    focusEditorAtEnd()
  }

  const handleChatModeChange = (mode: ChatMode) => {
    onChatModeChange?.(mode)
    focusEditorAtEnd()
  }

  const scopedAgents = useMemo(() => {
    const targetMode = selectedGroupDispatch
      ?? resolveSelectedGroupDispatch(selectedGroup)
    if (targetMode.message_target_mode === 'all_agents') return agents
    if (targetMode.message_target_mode === 'room_default') {
      if (roomAgentIds.length === 0) return agents
      const idSet = new Set(roomAgentIds)
      return agents.filter(a => idSet.has(a.id))
    }
    const group = groups.find(g => g.group_id === targetMode.target_group_id)
    if (!group) return agents
    const idSet = new Set(group.agents)
    return agents.filter(a => idSet.has(a.id))
  }, [agents, selectedGroup, selectedGroupDispatch, groups, roomAgentIds])

  const filteredAgents = scopedAgents.filter(agent =>
    agent.name.toLowerCase().includes(mentionQuery.toLowerCase())
  )

  const agentNameMap = useMemo(
    () => Object.fromEntries(agents.map(a => [a.id, a.name])),
    [agents]
  )

  const addFiles = (files: File[]) => {
    const valid = files.filter(f => f.size <= MAX_FILE_SIZE && ACCEPTED_MIME_SET.has(f.type))
    if (valid.length === 0) return
    setAttachments(prev => {
      const remaining = MAX_ATTACHMENTS - prev.length
      if (remaining <= 0) {
        toast.warning(`Maximum ${MAX_ATTACHMENTS} attachments allowed`)
        return prev
      }
      const toAdd = valid.slice(0, remaining)
      if (toAdd.length < valid.length) {
        toast.warning(`Only ${remaining} more attachment${remaining === 1 ? '' : 's'} allowed — ${valid.length - toAdd.length} skipped`)
      }
      const pending: PendingAttachment[] = toAdd.map(file => {
        const needsPreview = file.type.startsWith('image/') || file.type.startsWith('audio/') || file.type.startsWith('video/')
        return {
          id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          file,
          previewUrl: needsPreview ? URL.createObjectURL(file) : null,
          status: 'pending' as const,
        }
      })
      const next = [...prev, ...pending]
      attachmentsRef.current = next
      return next
    })
  }

  const removeAttachment = (id: string) => {
    setAttachments(prev => {
      const target = prev.find(a => a.id === id)
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl)
      const next = prev.filter(a => a.id !== id)
      attachmentsRef.current = next
      return next
    })
  }

  useEffect(() => {
    return () => {
      attachmentsRef.current.forEach(a => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl) })
    }
  }, [])

  // Reset selected index when filtered agents change
  useEffect(() => {
    setSelectedAgentIndex(0)
  }, [mentionQuery])

  // Scroll selected item into view
  useEffect(() => {
    if (showAgentSuggestions && listRef.current) {
      const selectedElement = listRef.current.children[selectedAgentIndex] as HTMLElement
      if (selectedElement) {
        selectedElement.scrollIntoView({
          block: 'nearest',
          behavior: 'smooth'
        })
      }
    }
  }, [selectedAgentIndex, showAgentSuggestions])

  // Close mention dropdown on outside click
  useEffect(() => {
    if (!showAgentSuggestions) return
    const handleMouseDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        suggestionsRef.current?.contains(target) ||
        editorRef.current?.contains(target)
      ) return
      setShowAgentSuggestions(false)
      setSelectedAgentIndex(0)
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [showAgentSuggestions])

  // Convert storage format to display HTML
  const convertToDisplayHTML = (content: string) => {
    // Escape HTML to prevent XSS
    const escapeHtml = (text: string) => {
      const div = document.createElement('div')
      div.textContent = text
      return div.innerHTML
    }

    // Split by mentions and rebuild with HTML
    const parts: string[] = []
    let lastIndex = 0
    const mentionRegex = /<@([^|]+)\|([^>]+)>/g
    let match

    while ((match = mentionRegex.exec(content)) !== null) {
      // Add text before mention
      if (match.index > lastIndex) {
        parts.push(escapeHtml(content.slice(lastIndex, match.index)))
      }

      // Add mention span with avatar
      const id = match[1]
      const name = match[2]
      const agent = agents.find(a => a.id === id)
      const avatarSrc = agent?.iconUrl || getAgentAvatarUri(id)
      const color = mentionColor(id)
      parts.push(
        `<span class="room-mention" data-id="${escapeHtml(id)}" data-name="${escapeHtml(name)}" contenteditable="false" style="--mention-color:${color};user-select:text;-webkit-user-select:text;"><img src="${escapeHtml(avatarSrc)}" alt="" class="room-mention-avatar" />${escapeHtml(name)}</span>`
      )

      lastIndex = match.index + match[0].length
    }

    // Add remaining text
    if (lastIndex < content.length) {
      parts.push(escapeHtml(content.slice(lastIndex)))
    }

    return parts.join('')
  }

  // Handle external value changes (e.g., quick start templates or demo typewriter)
  useEffect(() => {
    if (!externalValue) return

    const applyExternalValue = (stealFocus: boolean) => {
      setMessage(externalValue)
      const displayLength = externalValue.replace(/<@[^|]+\|([^>]+)>/g, '@$1').length
      setPlainTextLength(displayLength)
      if (editorRef.current) {
        editorRef.current.innerHTML = convertToDisplayHTML(externalValue)
        if (stealFocus) {
          const range = document.createRange()
          const selection = window.getSelection()
          range.selectNodeContents(editorRef.current)
          range.collapse(false)
          selection?.removeAllRanges()
          selection?.addRange(range)
          editorRef.current.focus()
        }
      }
    }

    if (continuousExternalUpdate) {
      applyExternalValue(false)
      return
    }

    if (externalValue !== message) {
      applyExternalValue(true)
    }

    // Consume even when the same template is selected twice. Otherwise the
    // retained external value would overwrite the user's next edit.
    onExternalValueConsumed?.()
  }, [externalValue, message, onExternalValueConsumed, continuousExternalUpdate])

  // Get plain text from editor (display format)
  const getEditorText = (): string => {
    if (!editorRef.current) return ''

    let text = ''
    const traverse = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent || ''
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const element = node as HTMLElement
        if (element.classList.contains('room-mention')) {
          text += `@${element.dataset.name || ''}`
        } else if (element.tagName === 'BR') {
          text += '\n'
        } else {
          node.childNodes.forEach(traverse)
        }
      }
    }

    editorRef.current.childNodes.forEach(traverse)
    return text
  }

  // Convert editor content to storage format
  const convertToStorageFormat = (): string => {
    if (!editorRef.current) return ''

    let storage = ''
    const traverse = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        storage += node.textContent || ''
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const element = node as HTMLElement
        if (element.classList.contains('room-mention')) {
          const id = element.dataset.id || ''
          const name = element.dataset.name || ''
          storage += `<@${id}|${name}>`
        } else if (element.tagName === 'BR') {
          storage += '\n'
        } else {
          node.childNodes.forEach(traverse)
        }
      }
    }

    editorRef.current.childNodes.forEach(traverse)
    return storage
  }

  // Serialize DOM nodes into plain text + storage mention format.
  const serializeNodes = (nodes: NodeListOf<ChildNode> | ChildNode[]) => {
    let plain = ''
    let storage = ''

    const traverse = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = node.textContent || ''
        plain += text
        storage += text
        return
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return

      const element = node as HTMLElement
      if (element.classList.contains('room-mention')) {
        const id = element.dataset.id || ''
        const name = element.dataset.name || ''
        plain += `@${name}`
        storage += `<@${id}|${name}>`
        return
      }
      if (element.tagName === 'BR') {
        plain += '\n'
        storage += '\n'
        return
      }
      element.childNodes.forEach(traverse)
    }

    Array.from(nodes).forEach(traverse)
    return { plain, storage }
  }

  // Get cursor position in text
  const getCursorPosition = (): number => {
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0 || !editorRef.current) return 0

    const range = selection.getRangeAt(0)
    const preCaretRange = range.cloneRange()
    preCaretRange.selectNodeContents(editorRef.current)
    preCaretRange.setEnd(range.endContainer, range.endOffset)

    let position = 0
    const traverse = (node: Node): boolean => {
      if (node === range.endContainer) {
        position += range.endOffset
        return true
      }

      if (node.nodeType === Node.TEXT_NODE) {
        position += node.textContent?.length || 0
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const element = node as HTMLElement
        if (element.classList.contains('room-mention')) {
          position += `@${element.dataset.name || ''}`.length
        } else {
          for (const child of Array.from(node.childNodes)) {
            if (traverse(child)) return true
          }
        }
      }
      return false
    }

    for (const child of Array.from(editorRef.current.childNodes)) {
      if (traverse(child)) break
    }

    return position
  }

  // Set cursor position
  const setCursorPosition = (position: number) => {
    if (!editorRef.current) return

    const selection = window.getSelection()
    const range = document.createRange()

    let charCount = 0
    let found = false

    const traverse = (node: Node): boolean => {
      if (found) return true

      if (node.nodeType === Node.TEXT_NODE) {
        const textLength = node.textContent?.length || 0
        if (charCount + textLength >= position) {
          range.setStart(node, Math.min(position - charCount, textLength))
          range.collapse(true)
          found = true
          return true
        }
        charCount += textLength
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const element = node as HTMLElement
        if (element.classList.contains('room-mention')) {
          const mentionLength = `@${element.dataset.name || ''}`.length
          if (charCount + mentionLength >= position) {
            range.setStartAfter(element)
            range.collapse(true)
            found = true
            return true
          }
          charCount += mentionLength
        } else {
          for (const child of Array.from(node.childNodes)) {
            if (traverse(child)) return true
          }
        }
      }
      return false
    }

    for (const child of Array.from(editorRef.current.childNodes)) {
      if (traverse(child)) break
    }

    if (!found) {
      range.selectNodeContents(editorRef.current)
      range.collapse(false)
    }

    selection?.removeAllRanges()
    selection?.addRange(range)
  }

  // Handle input changes with debouncing to preserve cursor
  const handleInput = () => {
    if (!editorRef.current) return

    const cursorPos = getCursorPosition()
    const storageFormat = convertToStorageFormat()

    // Update storage format
    setMessage(storageFormat)

    // Get text for mention detection
    const text = getEditorText()
    setPlainTextLength(text.length)

    // Check for @ mentions
    const beforeCursor = text.slice(0, cursorPos)
    const mentionMatch = beforeCursor.match(/@(\w*)$/)

    if (mentionMatch) {
      setMentionQuery(mentionMatch[1])
      setShowAgentSuggestions(true)
    } else {
      setShowAgentSuggestions(false)
      setMentionQuery('')
      setSelectedAgentIndex(0)
    }

    // Detect if editor content overflows the visible area
    if (editorRef.current) {
      setIsOverflowing(editorRef.current.scrollHeight > editorRef.current.clientHeight)
    }
  }

  // Paste plain text preserving newlines and whitespace
  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    const items = Array.from(e.clipboardData.items)
    const fileItems = items
      .filter(item => item.kind === 'file')
      .map(item => item.getAsFile())
      .filter((f): f is File => f !== null)

    if (fileItems.length > 0) {
      e.preventDefault()
      addFiles(fileItems)
      return
    }

    e.preventDefault()
    const mentionStorageText = e.clipboardData.getData(MENTION_CLIPBOARD_MIME)
    const text = mentionStorageText || e.clipboardData.getData('text/plain')
    if (!editorRef.current) return

    // Enforce message size limit on paste
    const currentText = getEditorText()
    const selectionLength = window.getSelection()?.toString().length ?? 0
    const availableChars = MAX_MESSAGE_LENGTH - (currentText.length - selectionLength)

    if (availableChars <= 0) {
      toast.warning(`Message is at the ${MAX_MESSAGE_LENGTH.toLocaleString()} character limit`)
      return
    }

    let textToInsert = text
    if (text.length > availableChars) {
      textToInsert = text.slice(0, availableChars)
      const truncated = text.length - availableChars
      toast.warning(`Pasted text truncated: ${truncated.toLocaleString()} characters removed to stay within the ${MAX_MESSAGE_LENGTH.toLocaleString()} character limit`)
    }

    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0) return

    // Delete any selected content
    selection.deleteFromDocument()

    const range = selection.getRangeAt(0)

    let lastNode: Node | null = null
    if (mentionStorageText) {
      const template = document.createElement('template')
      template.innerHTML = convertToDisplayHTML(textToInsert)
      const fragment = template.content
      const insertedNodes = Array.from(fragment.childNodes)
      range.insertNode(fragment)
      if (insertedNodes.length > 0) {
        lastNode = insertedNodes[insertedNodes.length - 1]
      }
    } else {
      // Split by newlines and insert text nodes with <br> elements between them
      const lines = textToInsert.split('\n')
      lines.forEach((line, index) => {
        if (index > 0) {
          // Insert a <br> for each newline
          const br = document.createElement('br')
          range.insertNode(br)
          range.setStartAfter(br)
          range.collapse(true)
          lastNode = br
        }
        if (line.length > 0) {
          const textNode = document.createTextNode(line)
          range.insertNode(textNode)
          range.setStartAfter(textNode)
          range.collapse(true)
          lastNode = textNode
        }
      })
    }

    // Move cursor to after the last inserted node
    if (lastNode) {
      range.setStartAfter(lastNode)
      range.collapse(true)
    }
    selection.removeAllRanges()
    selection.addRange(range)

    handleInput()

    // Scroll editor so the cursor (bottom of content) is visible
    if (editorRef.current) {
      editorRef.current.scrollTop = editorRef.current.scrollHeight
    }
  }

  const handleCopy = (e: React.ClipboardEvent<HTMLDivElement>) => {
    if (!editorRef.current) return
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return

    const range = selection.getRangeAt(0)
    if (!editorRef.current.contains(range.commonAncestorContainer)) return

    const fragment = range.cloneContents()
    const { plain, storage } = serializeNodes(fragment.childNodes)
    e.preventDefault()
    e.clipboardData.setData('text/plain', plain)
    e.clipboardData.setData(MENTION_CLIPBOARD_MIME, storage)
  }

  const handleCut = (e: React.ClipboardEvent<HTMLDivElement>) => {
    if (!editorRef.current) return
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return

    const range = selection.getRangeAt(0)
    if (!editorRef.current.contains(range.commonAncestorContainer)) return

    const fragment = range.cloneContents()
    const { plain, storage } = serializeNodes(fragment.childNodes)
    e.preventDefault()
    e.clipboardData.setData('text/plain', plain)
    e.clipboardData.setData(MENTION_CLIPBOARD_MIME, storage)
    selection.deleteFromDocument()
    handleInput()
  }
  const insertMention = (agent: RoomChatInputAgent) => {
    if (!editorRef.current) return

    // Get current storage format
    const currentStorage = convertToStorageFormat()
    const text = getEditorText()
    const cursorPos = getCursorPosition()

    // Find @ position in text
    const beforeCursor = text.slice(0, cursorPos)
    const atIndex = beforeCursor.lastIndexOf('@')
    if (atIndex === -1) return

    // Build new text
    const beforeMention = text.slice(0, atIndex)

    // Find corresponding position in storage format
    let textPos = 0
    let storageBeforeMention = ''

    const storageRegex = /<@([^|]+)\|([^>]+)>/g
    let lastStorageIndex = 0
    let match

    while ((match = storageRegex.exec(currentStorage)) !== null) {
      // Text before this mention
      const textBeforeMention = currentStorage.slice(lastStorageIndex, match.index)
      const displayLength = textBeforeMention.length

      if (textPos + displayLength >= atIndex) {
        // The @ is in plain text before this mention
        storageBeforeMention = currentStorage.slice(0, lastStorageIndex) +
          textBeforeMention.slice(0, atIndex - textPos)
        break
      }

      textPos += displayLength

      // This mention
      const mentionDisplayLength = `@${match[2]}`.length
      if (textPos + mentionDisplayLength >= atIndex) {
        // The @ is inside a mention (shouldn't happen, but handle it)
        storageBeforeMention = currentStorage.slice(0, match.index + match[0].length)
        break
      }

      textPos += mentionDisplayLength
      lastStorageIndex = match.index + match[0].length
    }

    if (!storageBeforeMention) {
      // The @ is in the remaining text
      const remainingText = currentStorage.slice(lastStorageIndex)
      const offsetInRemaining = atIndex - textPos
      storageBeforeMention = currentStorage.slice(0, lastStorageIndex) +
        remainingText.slice(0, offsetInRemaining)
    }

    // Get storage format of after cursor
    let storageAfterCursor = ''
    textPos = 0
    lastStorageIndex = 0
    storageRegex.lastIndex = 0

    while ((match = storageRegex.exec(currentStorage)) !== null) {
      const textBeforeMention = currentStorage.slice(lastStorageIndex, match.index)
      const displayLength = textBeforeMention.length

      if (textPos + displayLength >= cursorPos) {
        const offsetInText = cursorPos - textPos
        storageAfterCursor = currentStorage.slice(lastStorageIndex + offsetInText)
        break
      }

      textPos += displayLength
      const mentionDisplayLength = `@${match[2]}`.length

      if (textPos + mentionDisplayLength >= cursorPos) {
        storageAfterCursor = currentStorage.slice(match.index + match[0].length)
        break
      }

      textPos += mentionDisplayLength
      lastStorageIndex = match.index + match[0].length
    }

    if (!storageAfterCursor && cursorPos < text.length) {
      const offsetInRemaining = cursorPos - textPos
      storageAfterCursor = currentStorage.slice(lastStorageIndex + offsetInRemaining)
    }

    // Build new storage format
    const newStorage = storageBeforeMention + `<@${agent.id}|${agent.name}> ` + storageAfterCursor

    setMessage(newStorage)
    const displayLength = newStorage.replace(/<@[^|]+\|([^>]+)>/g, '@$1').length
    setPlainTextLength(displayLength)

    // Update editor HTML
    editorRef.current.innerHTML = convertToDisplayHTML(newStorage)

    // Set cursor after the mention
    const newCursorPos = beforeMention.length + `@${agent.name} `.length
    setTimeout(() => {
      setCursorPosition(newCursorPos)
      editorRef.current?.focus()
    }, 0)

    setShowAgentSuggestions(false)
    setMentionQuery('')
    setSelectedAgentIndex(0)
  }

  // Auto-focus editor when a quote is set
  useEffect(() => {
    if (quote) {
      editorRef.current?.focus()
    }
  }, [quote])

  // Update editor when message changes externally
  useEffect(() => {
    if (editorRef.current && message === '') {
      editorRef.current.innerHTML = ''
    }
  }, [message])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (showAgentSuggestions) {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault()
          if (filteredAgents.length > 0) {
            setSelectedAgentIndex(prev =>
              prev < filteredAgents.length - 1 ? prev + 1 : 0
            )
          }
          break
        case 'ArrowUp':
          e.preventDefault()
          if (filteredAgents.length > 0) {
            setSelectedAgentIndex(prev =>
              prev > 0 ? prev - 1 : filteredAgents.length - 1
            )
          }
          break
        case 'Enter':
          e.preventDefault()
          if (filteredAgents[selectedAgentIndex]) {
            insertMention(filteredAgents[selectedAgentIndex])
          }
          break
        case 'Escape':
          e.preventDefault()
          setShowAgentSuggestions(false)
          setSelectedAgentIndex(0)
          break
        case 'Tab':
          e.preventDefault()
          if (filteredAgents[selectedAgentIndex]) {
            insertMention(filteredAgents[selectedAgentIndex])
          }
          break
        default:
          break
      }
    } else {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        // Allow typing but block submit
        if (!disableSend && !disabled) {
          handleSubmit()
        }
      }
    }
  }

  // Extract mentioned agents from the message
  const mentionedAgents = useMemo(() => {
    const mentionPattern = /<@([^|]+)\|([^>]+)>/g
    const mentions: { id: string; name: string }[] = []
    let match
    while ((match = mentionPattern.exec(message)) !== null) {
      mentions.push({ id: match[1], name: match[2] })
    }
    return mentions
  }, [message])

  const handleSubmit = () => {
    const trimmedMessage = message.trim()
    if ((!trimmedMessage && attachments.length === 0) || disableSend || disabled) {
      return
    }

    if (trimmedMessage.length > MAX_MESSAGE_LENGTH || getEditorText().length > MAX_MESSAGE_LENGTH) {
      toast.warning(`Message exceeds the ${MAX_MESSAGE_LENGTH.toLocaleString()} character limit`)
      return
    }

    if (trimmedMessage || attachments.length > 0) {
      const dispatch: MessageDispatchInput = mentionedAgents.length > 0
        ? { mentioned_agent_ids: [mentionedAgents[0].id, ...mentionedAgents.slice(1).map((agent) => agent.id)] }
        : selectedGroupDispatch
          ?? resolveSelectedGroupDispatch(selectedGroup ?? BUILTIN_GROUP_ALL_AGENTS)
      const submittedAttachments = attachments.length > 0 ? attachments : undefined

      setMessage('')
      setPlainTextLength(0)
      setAttachments([])
      attachmentsRef.current = []

      if (editorRef.current) {
        editorRef.current.innerHTML = ''
      }
      setShowAgentSuggestions(false)
      setSelectedAgentIndex(0)
      onClearQuote?.()

      // Submitted attachment blob URLs are NOT revoked here — they may
      // still be needed by the new-room handoff flow.  Instead,
      // sendUserMessage revokes them after the optimistic swap replaces
      // blob URLs with server URLs in the message store.
      onSubmit(trimmedMessage, dispatch, quote, submittedAttachments)
    }
  }

  // Determine if message is ready to send — gate on trimmed storage format length
  // (onSubmit receives message.trim(), so measure the same string everywhere)
  const trimmedStorageLength = message.trim().length
  const isDisplayOverLimit = plainTextLength > MAX_MESSAGE_LENGTH
  const isStorageOverLimit = trimmedStorageLength > MAX_MESSAGE_LENGTH
  const isOverLimit = isDisplayOverLimit || isStorageOverLimit
  const isReadyToSend = (message.trim() || attachments.length > 0) && !disableSend && !disabled && !isOverLimit
  const actionButtonClass = "size-8 rounded-[var(--chat-input-radius)] p-0"

  return (
    <div className="relative">
      {/* Agent suggestions dropdown */}
      {showAgentSuggestions && (
        <div
          ref={suggestionsRef}
          className={cn(
            "absolute bottom-full left-[var(--conversation-body-inset)] right-[var(--conversation-body-inset)] -mb-px z-50",
            "bg-popover backdrop-blur-xl",
            "border border-border/50 shadow-xl rounded-t-xl rounded-b-none",
            "animate-in fade-in slide-in-from-bottom-3 duration-300"
          )}
        >
          {/* Header */}
          <div className="relative px-4 py-2.5 border-b border-border/30">
            <div className="flex items-center gap-2">
              <AtSign className="h-3.5 w-3.5 text-primary" />
              <span className="text-sm font-medium text-foreground">Mention an agent</span>
              <span className="ml-auto flex items-center gap-1 text-[11px] text-muted-foreground">
                <ChevronsUpDown className="h-3 w-3" />
                Scroll to view agents
              </span>
            </div>
          </div>

          {/* Agent list with scroll */}
          <div ref={listRef} className="py-1 max-h-52 overflow-y-auto">
            {filteredAgents.length > 0 ? (
              filteredAgents.map((agent, index) => (
                <button
                  key={agent.id}
                  onClick={() => insertMention(agent)}
                  className={cn(
                    "w-full text-left px-3 py-2.5 transition-all duration-100 flex items-center gap-3 mx-1 rounded-lg",
                    index === selectedAgentIndex
                      ? 'bg-accent text-accent-foreground'
                      : 'text-foreground hover:bg-muted'
                  )}
                  onMouseEnter={() => setSelectedAgentIndex(index)}
                  style={{ width: 'calc(100% - 8px)' }}
                >
                  {/* Agent avatar */}
                  <div className="w-8 h-8 rounded-full shrink-0 overflow-hidden bg-muted flex items-center justify-center">
                    {agent.iconUrl ? (
                      <Image
                        src={agent.iconUrl}
                        alt={agent.name}
                        width={32}
                        height={32}
                        className="w-full h-full object-cover"
                        onError={(e) => {
                          e.currentTarget.style.display = 'none'
                          e.currentTarget.nextElementSibling?.classList.remove('hidden')
                        }}
                      />
                    ) : null}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={getAgentAvatarUri(agent.id)} alt="" className={cn("w-full h-full", agent.iconUrl && "hidden")} />
                  </div>

                  <span className="font-medium truncate text-sm flex-1">
                    {agent.name}
                  </span>

                  {/* Keyboard hint for selected item */}
                  {index === selectedAgentIndex && (
                    <span className="text-[10px] px-2 py-1 bg-muted rounded font-medium text-muted-foreground whitespace-nowrap">
                      Press Enter To Select
                    </span>
                  )}
                </button>
              ))
            ) : (
              <div className="px-4 py-6 text-center text-sm text-muted-foreground">
                <div className="font-medium text-foreground">No agents available</div>
                <div className="mt-1 text-xs">Try changing the agent team below.</div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Main input container */}
      <div
        onDrop={(e) => {
          e.preventDefault()
          const files = Array.from(e.dataTransfer.files)
          if (files.length > 0) addFiles(files)
        }}
        onDragOver={(e) => e.preventDefault()}
        className={cn(
          "group/input relative flex flex-col rounded-xl transition-colors duration-200",
        )}>
        {/* Inner container with actual border */}
        <div className="relative flex flex-col rounded-xl bg-muted backdrop-blur-sm shadow-sm overflow-hidden" style={{ border: '1px solid var(--conversation-border-light)' }}>
          {/* Top slot (e.g. HITL Questions panel) */}
          {topSlot}

          {/* Attachment previews */}
          <AttachmentPreview attachments={attachments} onRemove={removeAttachment} />

          {/* Quote preview */}
          {quote && (
            <div className="mx-4 mt-3 flex items-start gap-2 rounded-lg bg-muted/60 px-3 py-2 text-sm">
              <div className="w-0.5 shrink-0 self-stretch rounded-full bg-primary" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 mb-0.5">
                  <Quote className="h-3 w-3 text-primary shrink-0" />
                  <span className="text-xs font-semibold text-primary truncate">
                    {quote.senderName}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 break-words">
                  {quote.content}
                </p>
              </div>
              <button
                type="button"
                onClick={onClearQuote}
                className="shrink-0 p-0.5 rounded hover:bg-muted-foreground/20 text-muted-foreground hover:text-foreground transition-colors"
                aria-label="Remove quote"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          {/* Contenteditable div */}
          <div className="flex-1 px-5 py-3">
            <div
              ref={editorRef}
              contentEditable={!disabled}
              onInput={handleInput}
              onFocus={onEditorFocus}
              onPaste={handlePaste}
              onCopy={handleCopy}
              onCut={handleCut}
              onKeyDown={handleKeyDown}
              className={cn(
                "w-full overflow-y-auto resize-none transition-[max-height] duration-200 ease-out",
                "border-0 bg-transparent text-[15px] leading-7 text-foreground",
                "focus:outline-none placeholder-editor",
                !isEditorExpanded && "min-h-[28px]",
                disabled && "opacity-40 cursor-not-allowed"
              )}
              data-testid="chat-input"
              data-placeholder="Type a message... Use @ to mention agents"
              suppressContentEditableWarning
              style={{
                caretColor: 'hsl(var(--color-primary))',
                wordBreak: 'break-word',
                whiteSpace: 'pre-wrap',
                minHeight: isEditorExpanded ? '200px' : undefined,
                maxHeight: isEditorExpanded ? '60vh' : 'var(--chat-input-editor-collapsed-max-height)',
              }}
            />
          </div>

          {/* Controls: GroupSelector left, utility actions + Send/Stop right */}
          <div
            className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 px-4 pb-3 pt-2 max-[520px]:grid-cols-1"
            data-testid="composer-controls"
          >
            {/* Group selector + mode selector (left) */}
            <div className="flex min-w-0 items-center gap-3 text-sm text-muted-foreground max-[520px]:w-full max-[520px]:gap-2 max-[520px]:overflow-hidden" data-testid="composer-selectors">
              {showGroupSelector && (
                <GroupSelector
                  className="w-36 min-w-0 shrink-0 max-[520px]:w-[calc(50%-0.25rem)] max-[520px]:max-w-36"
                  selectedGroup={selectedGroup}
                  selectedGroupName={selectedGroupName}
                  roomMembershipLabel={roomMembershipLabel}
                  onGroupChange={handleGroupChange}
                  groups={groups}
                  loadingGroups={loadingGroups}
                  mentionedAgents={mentionedAgents}
                  onCreateGroup={onCreateGroup}
                  onEditGroup={onEditGroup}
                  onDeleteGroup={onDeleteGroup}
                  agentNameMap={agentNameMap}
                  disabled={disabled}
                  readOnly={disableGroupSelector}
                />
              )}
              {onChatModeChange && (
                <ModeSelector
                  className="min-w-0 max-w-[9rem] max-[520px]:max-w-[calc(50%-0.25rem)]"
                  mode={chatMode ?? DEFAULT_CHAT_MODE}
                  onModeChange={handleChatModeChange}
                  disabled={disabled || sending || processing}
                  readOnly={disableModeSelector}
                />
              )}
            </div>

            {/* Upload + @ + Send / Stop button (right) */}
            <div className="flex items-center justify-self-end" data-testid="composer-actions">
              {isStorageOverLimit && !isDisplayOverLimit && plainTextLength < COUNTER_VISIBLE_THRESHOLD ? (
                <span
                  className="text-xs font-medium text-red-600 dark:text-red-400 transition-colors duration-200 mr-2"
                  data-testid="char-counter"
                >
                  Message too large (mentions)
                </span>
              ) : plainTextLength >= COUNTER_VISIBLE_THRESHOLD ? (
                <span
                  className={cn(
                    "text-xs font-medium tabular-nums transition-colors duration-200 mr-2",
                    isOverLimit
                      ? "text-red-600 dark:text-red-400"
                      : plainTextLength >= WARNING_THRESHOLD
                        ? "text-red-500/80 dark:text-red-400/80"
                        : "text-amber-600 dark:text-amber-400"
                  )}
                  data-testid="char-counter"
                >
                  {plainTextLength.toLocaleString()}/{MAX_MESSAGE_LENGTH.toLocaleString()}
                </span>
              ) : null}
              <div className="flex items-center gap-1.5" data-testid="composer-utilities">
                {(isOverflowing || isEditorExpanded) && (
                  <TooltipProvider delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          onClick={() => setIsEditorExpanded(prev => !prev)}
                          data-testid="expand-editor"
                          className={cn(
                            actionButtonClass,
                            "text-muted-foreground hover:text-foreground hover:bg-muted/80 transition-colors",
                          )}
                        >
                          {isEditorExpanded ? (
                            <Minimize2 className="h-3.5 w-3.5" />
                          ) : (
                            <Maximize2 className="h-3.5 w-3.5" />
                          )}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top">
                        {isEditorExpanded ? 'Collapse' : 'Expand'}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
                <FileAttachmentButton
                  onFiles={addFiles}
                  disabled={disabled || sending || processing}
                  readOnly={disableAttachmentButton}
                  className="hover:text-foreground hover:bg-muted/70"
                />
                <TooltipProvider delayDuration={200}>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        aria-label="Mention an agent"
                        disabled={disabled || sending || processing}
                        className={cn(
                          'h-8 w-8 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/70 transition-colors',
                          disableMentionButton && 'cursor-default hover:text-muted-foreground hover:bg-transparent',
                        )}
                        onClick={() => {
                          if (disableMentionButton || !editorRef.current) return
                          editorRef.current.focus()
                          document.execCommand('insertText', false, '@')
                          handleInput()
                        }}
                      >
                        <AtSign className="h-4 w-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent side="top">
                      Mention (@)
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
              <div className="ml-2" data-testid="composer-primary-action">
                {sending ? (
                  <div className="relative">
                    <TooltipProvider delayDuration={200}>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span>
                            <Button
                              disabled
                              size="icon"
                              data-testid="sending-button"
                              className={actionButtonClass}
                            >
                              <div className="h-4 w-4 border-2 border-primary-foreground border-t-transparent rounded-full animate-spin" />
                            </Button>
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top">
                          Sending...
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    <span className="absolute inset-0 rounded-[var(--chat-input-radius)] bg-primary/20 animate-ping" />
                  </div>
                ) : cancelling && processing ? (
                  <TooltipProvider delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span>
                          <Button
                            disabled
                            size="icon"
                            data-testid="cancelling-button"
                            className={cn(
                              actionButtonClass,
                              "bg-destructive/60",
                            )}
                          >
                            <div className="h-4 w-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                          </Button>
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top">
                        Stopping...
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                ) : processing ? (
                  <TooltipProvider delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          onClick={onCancel}
                          size="icon"
                          className={cn(
                            actionButtonClass,
                            "bg-muted text-muted-foreground",
                            "hover:bg-muted/80 transition-colors duration-200",
                          )}
                          data-testid="stop-processing"
                        >
                          <Square className="h-3.5 w-3.5 fill-current" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top">
                        Stop
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                ) : (
                  <TooltipProvider delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          onClick={handleSubmit}
                          disabled={!isReadyToSend}
                          size="icon"
                          aria-label="Send message"
                          data-testid="send-button"
                          className={cn(
                            actionButtonClass,
                            "transition-all duration-300",
                            "disabled:shadow-none disabled:cursor-default",
                            isReadyToSend
                              ? "bg-primary text-primary-foreground hover:bg-primary/90"
                              : "bg-primary/40 text-primary-foreground/70"
                          )}
                        >
                          <ArrowUp className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top">
                        Send (Enter)
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
