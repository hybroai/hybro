import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { RoomChatInput, MAX_MESSAGE_LENGTH } from '@/components/room-chat-input'

vi.mock('@/components/group-selector', () => ({
  GroupSelector: ({ className }: { className?: string }) => <div data-testid="group-selector" className={className} />,
}))

// jsdom doesn't implement URL.createObjectURL / revokeObjectURL
if (!globalThis.URL.createObjectURL) {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock')
}
if (!globalThis.URL.revokeObjectURL) {
  globalThis.URL.revokeObjectURL = vi.fn()
}

const defaultProps = {
  onSubmit: vi.fn(),
  agents: [
    { id: 'a-1', name: 'Alpha Agent' },
    { id: 'a-2', name: 'Beta Agent' },
  ],
}

function renderInput(overrides: Record<string, unknown> = {}) {
  return render(<RoomChatInput {...defaultProps} {...overrides} />)
}

describe('RoomChatInput', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  describe('rendering', () => {
    it('should render contenteditable editor', () => {
      const { container } = renderInput()
      const editor = container.querySelector('[contenteditable="true"]')
      expect(editor).toBeTruthy()
    })

    it('should render send button', () => {
      renderInput()
      expect(screen.getAllByTestId('send-button').length).toBeGreaterThan(0)
    })

    it('should render group selector by default', () => {
      renderInput()
      expect(screen.getAllByTestId('group-selector').length).toBeGreaterThan(0)
    })

    it('should hide group selector when showGroupSelector=false', () => {
      renderInput({ showGroupSelector: false })
      expect(screen.queryByTestId('group-selector')).toBeNull()
    })

    it('should disable editor when disabled=true', () => {
      const { container } = renderInput({ disabled: true })
      const editor = container.querySelector('[contenteditable="false"]')
      expect(editor).toBeTruthy()
    })

    it('renders the upload entry directly instead of a plus options button', () => {
      renderInput()

      const uploadButton = screen.getByRole('button', { name: /add photos and files/i })
      expect(uploadButton).toBeTruthy()
      expect(screen.getByTestId('attachment-upload-icon')).toBeTruthy()
      expect(screen.queryByTestId('attachment-plus-icon')).toBeNull()
    })

    it('renders subdued composer chrome without glow-heavy shell classes', () => {
      const { container } = renderInput()

      const shell = container.querySelector('.group\\/input') as HTMLElement
      const panel = shell.firstElementChild as HTMLElement

      expect(shell).toBeTruthy()
      expect(shell.className).not.toContain('bg-gradient-to-b')
      expect(shell.className).not.toContain('shadow-xl')
      expect(shell.className).not.toContain('hover:shadow-2xl')
      expect(shell.className).not.toContain('rgba(0,255,255')
      expect(shell.className).not.toContain('shadow-[0_8px')
      expect(panel.style.border).toContain('var(--conversation-border-light)')
      expect(panel.className).toContain('bg-muted')
      expect(panel.className).not.toContain('bg-background')
    })

    it('keeps composer actions visible in a responsive controls layout', () => {
      renderInput({ onChatModeChange: vi.fn() })

      const controls = screen.getByTestId('composer-controls')
      const actions = screen.getByTestId('composer-actions')
      const selectors = screen.getByTestId('composer-selectors')
      const utilities = screen.getByTestId('composer-utilities')
      const primaryAction = screen.getByTestId('composer-primary-action')
      const uploadButton = screen.getByRole('button', { name: /add photos and files/i })
      const mentionButton = screen.getByRole('button', { name: /mention an agent/i })
      const sendButton = screen.getByTestId('send-button')

      expect(controls.className).toContain('grid-cols-[minmax(0,1fr)_auto]')
      expect(controls.className).toContain('max-[520px]:grid-cols-1')
      expect(selectors.className).toContain('gap-3')
      expect(selectors.className).toContain('min-w-0')
      expect(selectors.className).toContain('max-[520px]:w-full')
      expect(screen.getByTestId('group-selector').className).toContain('min-w-0')
      expect(screen.getByTestId('group-selector').className).toContain('w-36')
      expect(screen.getByTestId('group-selector').className).toContain('shrink-0')
      expect(actions.className).not.toContain('gap-1')
      expect(actions.className).toContain('justify-self-end')
      expect(utilities.className).toContain('gap-1.5')
      expect(primaryAction.className).toContain('ml-2')
      expect(utilities.contains(uploadButton)).toBe(true)
      expect(utilities.contains(mentionButton)).toBe(true)
      expect(primaryAction.contains(sendButton)).toBe(true)
      expect(actions.compareDocumentPosition(uploadButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(uploadButton.compareDocumentPosition(mentionButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(mentionButton.compareDocumentPosition(sendButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  })

  describe('button states', () => {
    function expectSquareActionButton(button: HTMLElement) {
      expect(button.className).toContain('size-8')
      expect(button.className).not.toContain('size-9')
      expect(button.className).toContain('rounded-[var(--chat-input-radius)]')
      expect(button.className).toContain('p-0')
      expect(button.className).not.toContain('rounded-full')
    }

    it('renders the send button as a composer-radius square', () => {
      renderInput()
      expectSquareActionButton(screen.getByTestId('send-button'))
    })

    it('keeps composer action buttons low-emphasis except for the primary fill', () => {
      renderInput()

      const uploadButton = screen.getByRole('button', { name: /add photos and files/i })
      const mentionButton = screen.getByRole('button', { name: /mention an agent/i })
      const sendButton = screen.getByTestId('send-button')

      expect(uploadButton.className).not.toContain('hover:text-primary')
      expect(mentionButton.className).not.toContain('hover:text-primary')
      expect(sendButton.className).not.toContain('shadow-md')
      expect(sendButton.className).not.toContain('hover:shadow-lg')
      expect(sendButton.className).not.toContain('hover:scale-105')
      expect(sendButton.className).not.toContain('active:scale-95')
    })

    it('keeps mention and attach buttons visible but inert when disabled for demo', async () => {
      const execCommand = vi.fn()
      Object.defineProperty(document, 'execCommand', {
        configurable: true,
        value: execCommand,
      })

      renderInput({
        disableAttachmentButton: true,
        disableMentionButton: true,
      })

      const uploadButton = screen.getByRole('button', { name: /add photos and files/i })
      const mentionButton = screen.getByRole('button', { name: /mention an agent/i })

      expect(uploadButton).toBeInTheDocument()
      expect(mentionButton).toBeInTheDocument()
      expect(document.querySelector('input[type="file"]')).toBeNull()

      fireEvent.click(uploadButton)
      fireEvent.click(mentionButton)

      expect(execCommand).not.toHaveBeenCalled()
    })

    it('should show spinner when sending=true', () => {
      renderInput({ sending: true })
      const button = screen.getByTestId('sending-button')
      expectSquareActionButton(button)
      const ping = button.closest('.relative')?.querySelector('.animate-ping')
      expect(ping?.className).toContain('rounded-[var(--chat-input-radius)]')
      expect(ping?.className).not.toContain('rounded-full')
    })

    it('should show stop button when processing=true', () => {
      renderInput({ processing: true })
      expectSquareActionButton(screen.getByTestId('stop-processing'))
    })

    it('should show cancelling spinner when processing and cancelling', () => {
      renderInput({ processing: true, cancelling: true })
      expectSquareActionButton(screen.getByTestId('cancelling-button'))
    })

    it('should call onCancel when stop button is clicked', () => {
      const onCancel = vi.fn()
      renderInput({ processing: true, onCancel })
      fireEvent.click(screen.getByTestId('stop-processing'))
      expect(onCancel).toHaveBeenCalledOnce()
    })
  })

  describe('submit', () => {
    it('should not submit when message is empty', () => {
      const onSubmit = vi.fn()
      renderInput({ onSubmit })
      const sendBtns = screen.getAllByTestId('send-button')
      fireEvent.click(sendBtns[0])
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('submits the supplied room snapshot scope independently of the displayed team', () => {
      const onSubmit = vi.fn()
      const { container } = renderInput({
        onSubmit,
        selectedGroup: 'source-team',
        selectedGroupName: 'Source Team',
        selectedGroupDispatch: { message_target_mode: 'room_default' },
      })

      const editor = container.querySelector('[contenteditable]') as HTMLElement
      editor.textContent = 'Hello snapshot'
      fireEvent.input(editor)
      fireEvent.click(screen.getAllByTestId('send-button')[0])

      expect(onSubmit).toHaveBeenCalledOnce()
      expect(onSubmit.mock.calls[0][1]).toEqual({ message_target_mode: 'room_default' })
    })

    it('should not submit when disableSend=true', () => {
      const onSubmit = vi.fn()
      const { container } = renderInput({ onSubmit, disableSend: true })

      const editor = container.querySelector('[contenteditable]') as HTMLElement
      editor.textContent = 'Hello'
      fireEvent.input(editor)

      const sendBtns = screen.getAllByTestId('send-button')
      fireEvent.click(sendBtns[0])

      expect(onSubmit).not.toHaveBeenCalled()
    })
  })

  describe('quote preview', () => {
    it('should show quote preview when quote prop is set', () => {
      renderInput({
        quote: { messageId: 'q-1', content: 'Quoted text', senderName: 'Alice' },
      })
      expect(screen.getByText('Quoted text')).toBeTruthy()
      expect(screen.getByText('Alice')).toBeTruthy()
    })

    it('should not show quote when quote is null', () => {
      renderInput({ quote: null })
      expect(screen.queryByLabelText('Remove quote')).toBeNull()
    })

    it('should call onClearQuote when remove button is clicked', () => {
      const onClearQuote = vi.fn()
      renderInput({
        quote: { messageId: 'q-1', content: 'text', senderName: 'Bob' },
        onClearQuote,
      })
      fireEvent.click(screen.getByLabelText('Remove quote'))
      expect(onClearQuote).toHaveBeenCalledOnce()
    })
  })

  describe('expand/collapse', () => {
    it('should not show expand button when content is not overflowing', () => {
      const { container } = renderInput()
      expect(container.querySelector('[data-testid="expand-editor"]')).toBeNull()
    })

    it('keeps the collapsed editor to two lines and scrolls from the third line', () => {
      const tokens = readFileSync('src/components/conversation/conversation-tokens.css', 'utf8')
      expect(tokens).toContain('--chat-input-editor-collapsed-max-height: 56px;')

      const { container } = renderInput()
      const editor = container.querySelector('[data-testid="chat-input"]') as HTMLElement

      expect(editor.className).toContain('leading-7')
      expect(editor.style.maxHeight).toBe('var(--chat-input-editor-collapsed-max-height)')
      expect(editor.className).not.toContain('max-h-[200px]')

      Object.defineProperty(editor, 'scrollHeight', { configurable: true, value: 84 })
      Object.defineProperty(editor, 'clientHeight', { configurable: true, value: 56 })
      editor.textContent = ['one', 'two', 'three'].join('\n')
      fireEvent.input(editor)

      const expandButton = screen.getByTestId('expand-editor')
      const actions = screen.getByTestId('composer-actions')
      expect(actions.contains(expandButton)).toBe(true)

      fireEvent.click(expandButton)

      expect(editor.style.maxHeight).toBe('60vh')
      expect(editor.style.minHeight).toBe('200px')
    })
  })

  describe('message size validation', () => {
    function setEditorText(container: HTMLElement, text: string) {
      const editor = container.querySelector('[contenteditable]') as HTMLElement
      editor.textContent = text
      fireEvent.input(editor)
      return editor
    }

    it('should not show counter for short messages', () => {
      const { container } = renderInput()
      setEditorText(container, 'Hello world')
      expect(screen.queryByTestId('char-counter')).toBeNull()
    })

    it('should show counter when approaching limit', () => {
      const { container } = renderInput()
      const text = 'a'.repeat(MAX_MESSAGE_LENGTH - 400)
      setEditorText(container, text)
      const counter = screen.getByTestId('char-counter')
      expect(counter).toBeTruthy()
      expect(counter.textContent).toContain(MAX_MESSAGE_LENGTH.toLocaleString())
    })

    it('should disable send button when over limit', () => {
      const { container } = renderInput()
      const text = 'a'.repeat(MAX_MESSAGE_LENGTH + 1)
      setEditorText(container, text)
      const sendBtn = screen.getByTestId('send-button')
      expect(sendBtn.hasAttribute('disabled')).toBe(true)
    })

    it('should apply red styling when over limit', () => {
      const { container } = renderInput()
      const text = 'a'.repeat(MAX_MESSAGE_LENGTH + 1)
      setEditorText(container, text)
      const counter = screen.getByTestId('char-counter')
      expect(counter.className).toContain('text-red-600')
    })

    it('should apply amber styling when near but under warning threshold', () => {
      const { container } = renderInput()
      const text = 'a'.repeat(MAX_MESSAGE_LENGTH - 400)
      setEditorText(container, text)
      const counter = screen.getByTestId('char-counter')
      expect(counter.className).toContain('text-amber-600')
    })
  })

  describe('failed send', () => {
    function typeDraft(container: HTMLElement, text: string) {
      const editor = container.querySelector('[contenteditable]') as HTMLElement
      editor.textContent = text
      fireEvent.input(editor)
      return editor
    }

    function dropFile(container: HTMLElement, name: string) {
      const dropZone = container.querySelector('.group\\/input') || container.firstElementChild!
      fireEvent.drop(dropZone, {
        dataTransfer: { files: [new File(['data'], name, { type: 'image/png' })], types: ['Files'] },
      })
    }

    const revokeSpy = vi.fn()
    let restoreUrlSpies: () => void = () => {}

    beforeEach(() => {
      const createSpy = vi.spyOn(globalThis.URL, 'createObjectURL').mockReturnValue('blob:draft')
      const revoke = vi.spyOn(globalThis.URL, 'revokeObjectURL').mockImplementation(revokeSpy)
      restoreUrlSpies = () => {
        createSpy.mockRestore()
        revoke.mockRestore()
      }
    })

    afterEach(() => {
      restoreUrlSpies()
    })

    it('restores the text and attachments when the send fails', async () => {
      const onSubmit = vi.fn().mockResolvedValue(false)
      const { container } = renderInput({ onSubmit })
      const editor = typeDraft(container, 'Keep this draft')
      dropFile(container, 'photo.png')

      fireEvent.click(screen.getAllByTestId('send-button')[0])

      await waitFor(() => expect(editor.textContent).toBe('Keep this draft'))
      expect(onSubmit).toHaveBeenCalledOnce()
      expect(screen.getByText('photo.png')).toBeTruthy()
      expect(revokeSpy).not.toHaveBeenCalled()
      expect((screen.getAllByTestId('send-button')[0] as HTMLButtonElement).disabled).toBe(false)
    })

    it('clears at once and stays cleared when the send succeeds', async () => {
      let resolveSend: (value: boolean) => void = () => {}
      const onSubmit = vi.fn(() => new Promise<boolean>((resolve) => { resolveSend = resolve }))
      const { container } = renderInput({ onSubmit })
      const editor = typeDraft(container, 'Sent message')

      fireEvent.click(screen.getAllByTestId('send-button')[0])
      expect(editor.textContent).toBe('')

      resolveSend(true)
      await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
      await Promise.resolve()
      expect(editor.textContent).toBe('')
    })

    it('keeps a new draft typed while the failed send was in flight', async () => {
      let resolveSend: (value: boolean) => void = () => {}
      const onSubmit = vi.fn(() => new Promise<boolean>((resolve) => { resolveSend = resolve }))
      const { container } = renderInput({ onSubmit })
      typeDraft(container, 'First draft')
      dropFile(container, 'photo.png')

      fireEvent.click(screen.getAllByTestId('send-button')[0])
      const editor = typeDraft(container, 'Second draft')
      resolveSend(false)

      await waitFor(() => expect(revokeSpy).toHaveBeenCalledWith('blob:draft'))
      expect(editor.textContent).toBe('Second draft')
      expect(screen.queryByText('photo.png')).toBeNull()
    })
  })

  describe('attachment limits', () => {
    it('should cap attachments at MAX_ATTACHMENTS (10)', () => {
      const createSpy = vi.spyOn(globalThis.URL, 'createObjectURL')
      createSpy.mockReturnValue('blob:mock')

      const { container } = renderInput()
      const dropZone = container.querySelector('.group\\/input') || container.firstElementChild!

      const files = Array.from({ length: 15 }, (_, i) =>
        new File([`data-${i}`], `file-${i}.png`, { type: 'image/png' })
      )

      fireEvent.drop(dropZone, {
        dataTransfer: { files, types: ['Files'] },
      })

      expect(createSpy).toHaveBeenCalledTimes(10)
      createSpy.mockRestore()
    })
  })

  describe('mention clipboard behavior', () => {
    const MENTION_CLIPBOARD_MIME = 'application/x-hybro-mentions'

    function selectEditorContents(editor: HTMLElement) {
      const range = document.createRange()
      range.selectNodeContents(editor)
      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)
    }

    function createClipboardData({
      plainText = '',
      mentionText = '',
    }: {
      plainText?: string
      mentionText?: string
    } = {}) {
      const setData = vi.fn()
      const getData = vi.fn((type: string) => {
        if (type === MENTION_CLIPBOARD_MIME) return mentionText
        if (type === 'text/plain') return plainText
        return ''
      })
      return { setData, getData, items: [] as DataTransferItem[] }
    }

    function pasteMention(editor: HTMLElement, storageText: string, plainText: string) {
      editor.focus()
      const clipboardData = createClipboardData({
        plainText,
        mentionText: storageText,
      })
      fireEvent.paste(editor, { clipboardData })
    }

    it('copies selected mention text as plain + mention MIME formats', async () => {
      const { container } = renderInput()

      const editor = container.querySelector('[data-testid="chat-input"]') as HTMLElement
      pasteMention(editor, 'Hello <@a-1|Alpha Agent>', 'Hello @Alpha Agent')
      expect(editor.innerHTML).toContain('room-mention')
      selectEditorContents(editor)

      const clipboardData = createClipboardData()
      fireEvent.copy(editor, { clipboardData })

      expect(clipboardData.setData).toHaveBeenCalledWith('text/plain', 'Hello @Alpha Agent')
      expect(clipboardData.setData).toHaveBeenCalledWith(MENTION_CLIPBOARD_MIME, 'Hello <@a-1|Alpha Agent>')
    })

    it('cuts selected mention text, writes clipboard formats, and removes content', async () => {
      const { container } = renderInput()

      const editor = container.querySelector('[data-testid="chat-input"]') as HTMLElement
      pasteMention(editor, '<@a-2|Beta Agent> world', '@Beta Agent world')
      expect(editor.innerHTML).toContain('room-mention')
      selectEditorContents(editor)

      const clipboardData = createClipboardData()
      fireEvent.cut(editor, { clipboardData })

      expect(clipboardData.setData).toHaveBeenCalledWith('text/plain', '@Beta Agent world')
      expect(clipboardData.setData).toHaveBeenCalledWith(MENTION_CLIPBOARD_MIME, '<@a-2|Beta Agent> world')
      expect(editor.textContent?.trim()).toBe('')
    })

    it('pastes custom mention MIME and submits storage format with mention IDs', async () => {
      const onSubmit = vi.fn()
      const { container } = renderInput({ onSubmit })

      const editor = container.querySelector('[data-testid="chat-input"]') as HTMLElement
      editor.focus()

      const clipboardData = createClipboardData({
        plainText: '@Alpha Agent',
        mentionText: '<@a-1|Alpha Agent>',
      })
      fireEvent.paste(editor, { clipboardData })

      await waitFor(() => {
        const sendBtn = screen.getByTestId('send-button')
        expect(sendBtn.hasAttribute('disabled')).toBe(false)
      })

      fireEvent.click(screen.getByTestId('send-button'))
      expect(onSubmit).toHaveBeenCalledWith(
        '<@a-1|Alpha Agent>',
        { mentioned_agent_ids: ['a-1'] },
        undefined,
        undefined,
      )
    })
  })
})
