/** Minimum trailing spacer (matches legacy pb-52 breathing room). */
export const FOCUS_SCROLL_MIN_SPACER_PX = 208

/** Offset from viewport top when anchoring the active user message (ChatGPT-style). */
export const FOCUS_SCROLL_TOP_OFFSET_PX = 50

/** Keep in sync with --conversation-focus-scroll-top in conversation-tokens.css */

export function scrollUserMessageToFocus(
  container: HTMLElement,
  userMessageEl: HTMLElement,
  topOffset = FOCUS_SCROLL_TOP_OFFSET_PX,
): void {
  const containerTop = container.getBoundingClientRect().top
  const messageTop = userMessageEl.getBoundingClientRect().top
  const delta = messageTop - containerTop - topOffset
  container.scrollTo({ top: Math.max(0, container.scrollTop + delta), behavior: 'auto' })
}
