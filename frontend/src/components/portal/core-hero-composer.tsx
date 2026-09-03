'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useUser, useAuth } from '@/lib/auth'
import { RoomChatInput } from '@/components/room-chat-input'
import { banner } from '@/components/ui/banner'
import { getAllAgents } from '@/lib/api/agent'
import { useChatRoomCreation } from '@/hooks/useChatRoomCreation'
import {
  buildTemplateDemoMessage,
  useCaseTemplates,
  type UseCaseTemplate,
} from '@/lib/use-case-templates'
import type { MessageDispatchInput } from '@/lib/types/agent-group'
import { BUILTIN_GROUP_ALL_AGENTS } from '@/lib/types/agent-group'
import { DEFAULT_CHAT_MODE, chatModeToExecutionMode } from '@/lib/types/chat-mode'
import type { Agent } from '@/lib/types/agent'
import type { PendingAttachment } from '@/lib/types/attachments'
import type { QuoteData } from '@/lib/types/quote'

export function CoreHeroComposer() {
  const { user } = useUser()
  const { getToken } = useAuth()
  const [availableAgents, setAvailableAgents] = useState<Agent[]>([])
  const [templateIndex, setTemplateIndex] = useState(0)
  const [charIndex, setCharIndex] = useState(0)
  const [isDeleting, setIsDeleting] = useState(false)
  const [demoActive, setDemoActive] = useState(true)

  const currentTemplate = useCaseTemplates[templateIndex]
  const userId = user?.id ?? 'user_local_developer'
  const userName = user?.firstName || user?.username || 'User'

  const {
    creating,
    createAndNavigate,
    createFromTemplate,
  } = useChatRoomCreation({
    userId,
    userName,
    getToken,
  })

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const response = await getAllAgents({ activeOnly: true, getToken })
        if (!cancelled && response.success && response.agents) {
          setAvailableAgents(response.agents)
        }
      } catch {
        // Demo still renders; send may fail until agents load
      }
    })()
    return () => {
      cancelled = true
    }
  }, [getToken])

  useEffect(() => {
    if (!demoActive) return

    const current = currentTemplate.prefillMessage
    let delay = isDeleting ? 28 : 55
    if (!isDeleting && charIndex === current.length) delay = 1500
    if (isDeleting && charIndex === 0) delay = 300

    const timer = setTimeout(() => {
      if (!isDeleting && charIndex === current.length) {
        setIsDeleting(true)
      } else if (isDeleting && charIndex === 0) {
        setIsDeleting(false)
        setTemplateIndex((p) => (p + 1) % useCaseTemplates.length)
      } else {
        setCharIndex((c) => c + (isDeleting ? -1 : 1))
      }
    }, delay)

    return () => clearTimeout(timer)
  }, [charIndex, isDeleting, templateIndex, demoActive, currentTemplate.prefillMessage])

  const demoExternalValue = useMemo(() => {
    if (!demoActive) return undefined
    return buildTemplateDemoMessage(currentTemplate, charIndex)
  }, [demoActive, currentTemplate, charIndex])

  const agentListForMentions = useMemo(
    () =>
      availableAgents.map((agent) => ({
        id: agent.agent_id,
        name: agent.agent_card.name,
        iconUrl: agent.agent_card.iconUrl,
      })),
    [availableAgents],
  )

  const handleEditorFocus = useCallback(() => {
    setDemoActive(false)
  }, [])

  const sendFromTemplate = useCallback(
    async (template: UseCaseTemplate) => {
      try {
        await createFromTemplate(template, availableAgents)
      } catch (error) {
        console.error('Failed to create room from template:', error)
        banner.error('Failed to start chat')
      }
    },
    [createFromTemplate, availableAgents],
  )

  const handleSubmit = useCallback(
    async (
      value: string,
      dispatch: MessageDispatchInput,
      _quote?: QuoteData | null,
      attachments?: PendingAttachment[],
    ) => {
      if (!value.trim() && (!attachments || attachments.length === 0)) {
        if (demoActive) {
          await sendFromTemplate(currentTemplate)
          return
        }
        banner.error('Please enter a message')
        return
      }
      if (demoActive) {
        await sendFromTemplate(currentTemplate)
        return
      }

      try {
        await createAndNavigate(value, {
          useSupervisor: chatModeToExecutionMode(DEFAULT_CHAT_MODE) === 'supervisor',
          dispatch,
          attachments,
        })
      } catch {
        banner.error('Some agents in this template are unavailable')
      }
    },
    [
      demoActive,
      currentTemplate,
      sendFromTemplate,
      createAndNavigate,
    ],
  )

  return (
    <div className="w-full max-w-3xl mx-auto mb-8 text-left">
      <RoomChatInput
        onSubmit={handleSubmit}
        disableSend={creating}
        agents={agentListForMentions}
        showGroupSelector={true}
        disableGroupSelector={true}
        groups={[]}
        selectedGroup={BUILTIN_GROUP_ALL_AGENTS}
        externalValue={demoExternalValue}
        continuousExternalUpdate={demoActive}
        onEditorFocus={handleEditorFocus}
        chatMode={DEFAULT_CHAT_MODE}
        onChatModeChange={() => {}}
        disableModeSelector={true}
        disableAttachmentButton={true}
        disableMentionButton={true}
      />
    </div>
  )
}
