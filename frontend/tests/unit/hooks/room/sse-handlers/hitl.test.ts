import { beforeEach, describe, expect, it, vi } from 'vitest'
import { handleHitlRequest, handleHitlResponse } from '@/hooks/room/sse-handlers/handlers/hitl'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { useMessageStore } from '@/stores/message-store'
import {
  hitlQuestionEntityId,
  hitlRequestKey,
} from '@/lib/hitl/hitl-message-projection'
import { selectPendingHitls } from '@/lib/selectors/select-hitl'

function makeLifecycle(): ProcessingLifecycle {
  return {
    placeholderId: () => 'placeholder-room-1',
    getPendingRunEventAck: () => 'current-cr',
    getMessageId: () => 'current-user',
    dismissPlaceholder: vi.fn(),
    markProcessingResolved: vi.fn(),
    stopProcessing: vi.fn(),
  } as unknown as ProcessingLifecycle
}

describe('handleHitlRequest', () => {
  beforeEach(() => {
    useMessageStore.setState({ entities: {}, orderedIds: [], conversationId: 'room-1' })
  })

  it('does not clear the active processing placeholder for a stale durable HITL event', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()

    useMessageStore.getState().upsertMany(
      [
        {
          id: 'placeholder-room-1',
          conversationId: 'room-1',
          messageType: 'agent',
          content: 'Working...',
          senderName: 'Hybro',
          timestamp,
        },
        {
          id: 'current-user',
          conversationId: 'room-1',
          messageType: 'user',
          content: 'current turn',
          senderName: 'User',
          timestamp,
          clientRequestId: 'current-cr',
        },
      ],
      'sse',
    )

    await handleHitlRequest(
      {
        conversationId: 'room-1',
        lifecycle,
        getAgentName: async () => 'Agent',
        getAgentSource: () => undefined,
        reconcileWithDb: async () => {},
        hitlRequestIndex: { current: new Map() },
        setCancelling: () => {},
      },
      {
        type: 'hitl_request',
        conversation_id: 'room-1',
        timestamp,
        data: {
          request_id: 'old-hitl',
          message_id: 'old-agent-message',
          client_request_id: 'old-cr',
          related_message_id: 'old-user',
          source: 'agent',
          agent_id: 'agent-1',
          agent_name: 'Agent',
          prompt: 'Need stale input',
          prompt_type: 'text',
          question_count: 1,
          question_index: 0,
        },
      },
      'req-1',
    )

    expect(useMessageStore.getState().entities['placeholder-room-1']).toBeDefined()
    expect(lifecycle.dismissPlaceholder).not.toHaveBeenCalled()
    expect(lifecycle.markProcessingResolved).not.toHaveBeenCalled()
    expect(lifecycle.stopProcessing).not.toHaveBeenCalled()
    const staleId = hitlQuestionEntityId(
      'old-agent-message', undefined, 'old-hitl', 1,
    )
    expect(useMessageStore.getState().entities[staleId]).toMatchObject({
      hitlRequestId: 'old-hitl',
      taskStatus: 'input-required',
    })
  })

  it('keeps every question when one interaction shares an Agent message id', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()
    const index = { current: new Map<string, string>() }
    const ctx = {
      conversationId: 'room-1',
      lifecycle,
      getAgentName: async () => 'Cyber Broker Agent',
      getAgentSource: () => undefined,
      reconcileWithDb: async () => {},
      hitlRequestIndex: index,
      setCancelling: () => {},
    }
    useMessageStore.getState().upsertMessage({
      id: 'orchestrator:run-1:inv_broker',
      conversationId: 'room-1',
      messageType: 'agent',
      content: '',
      senderName: 'Cyber Broker Agent',
      timestamp,
      clientRequestId: 'client-1',
      relatedMessageId: 'user-1',
      taskStatus: 'input-required',
    }, 'sse')

    for (const [requestId, prompt, questionIndex] of [
      ['security_training', 'Is security training in place?', 0],
      ['cloud_providers', 'Which cloud providers does the customer use?', 1],
    ] as const) {
      await handleHitlRequest(ctx, {
        type: 'hitl_request',
        conversation_id: 'room-1',
        timestamp,
        data: {
          request_id: requestId,
          message_id: 'orchestrator:run-1:inv_broker',
          client_request_id: 'client-1',
          related_user_message_id: 'user-1',
          source: 'agent',
          agent_label: 'Cyber Broker Agent',
          prompt,
          prompt_type: 'text',
          interaction_id: 'interaction-1',
          question_count: 2,
          question_index: questionIndex,
        },
      }, 'req-1')
    }

    const trainingEntityId = hitlQuestionEntityId(
      'orchestrator:run-1:inv_broker',
      'interaction-1',
      'security_training',
      2,
    )
    const providerEntityId = hitlQuestionEntityId(
      'orchestrator:run-1:inv_broker',
      'interaction-1',
      'cloud_providers',
      2,
    )
    const store = useMessageStore.getState()
    expect(store.entities['orchestrator:run-1:inv_broker'].hitlRequestId).toBeUndefined()
    expect(store.entities[trainingEntityId]).toMatchObject({
      hitlRequestId: 'security_training',
      hitlMessageId: 'orchestrator:run-1:inv_broker',
      hitlGroupIndex: 0,
    })
    expect(store.entities[providerEntityId]).toMatchObject({
      hitlRequestId: 'cloud_providers',
      hitlMessageId: 'orchestrator:run-1:inv_broker',
      hitlGroupIndex: 1,
    })
    expect(index.current).toEqual(new Map([
      [hitlRequestKey('interaction-1', 'security_training'), trainingEntityId],
      [hitlRequestKey('interaction-1', 'cloud_providers'), providerEntityId],
    ]))
    expect(selectPendingHitls('room-1', store.entities, store.orderedIds).map(hitl => (
      [hitl.hitlId, hitl.messageId]
    ))).toEqual([
      ['security_training', 'orchestrator:run-1:inv_broker'],
      ['cloud_providers', 'orchestrator:run-1:inv_broker'],
    ])

    handleHitlResponse(ctx, {
      type: 'hitl_response',
      conversation_id: 'room-1',
      timestamp,
      data: {
        request_id: 'security_training',
        message_id: 'orchestrator:run-1:inv_broker',
        source: 'agent',
        status: 'responded',
        interaction_id: 'interaction-1',
        question_count: 2,
        question_index: 0,
      },
    }, 'req-1')

    expect(useMessageStore.getState().entities[trainingEntityId].hitlResolved).toBe(true)
    expect(useMessageStore.getState().entities[providerEntityId].hitlResolved).toBe(false)
    expect(index.current.has(hitlRequestKey(
      'interaction-1', 'security_training',
    ))).toBe(false)
    expect(index.current.get(hitlRequestKey(
      'interaction-1', 'cloud_providers',
    ))).toBe(providerEntityId)
  })

  it('scopes a reused question id to its interaction', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()
    const index = { current: new Map<string, string>() }
    const ctx = {
      conversationId: 'room-1', lifecycle,
      getAgentName: async () => 'Agent', getAgentSource: () => undefined,
      reconcileWithDb: async () => {}, hitlRequestIndex: index, setCancelling: () => {},
    }
    for (const interactionId of ['interaction-1', 'interaction-2']) {
      await handleHitlRequest(ctx, {
        type: 'hitl_request', conversation_id: 'room-1', timestamp,
        data: {
          request_id: 'cloud_providers', message_id: 'agent-message', source: 'agent',
          prompt: `Providers for ${interactionId}?`, prompt_type: 'text',
          interaction_id: interactionId, question_count: 2, question_index: 0,
        },
      }, 'req-1')
    }
    const firstId = hitlQuestionEntityId(
      'agent-message', 'interaction-1', 'cloud_providers', 2,
    )
    const secondId = hitlQuestionEntityId(
      'agent-message', 'interaction-2', 'cloud_providers', 2,
    )

    handleHitlResponse(ctx, {
      type: 'hitl_response', conversation_id: 'room-1', timestamp,
      data: {
        request_id: 'cloud_providers', message_id: 'agent-message', source: 'agent',
        status: 'responded', interaction_id: 'interaction-1',
        question_count: 2, question_index: 0,
      },
    }, 'req-1')

    expect(useMessageStore.getState().entities[firstId].hitlResolved).toBe(true)
    expect(useMessageStore.getState().entities[secondId].hitlResolved).toBe(false)
    expect(index.current.has(hitlRequestKey(
      'interaction-1', 'cloud_providers',
    ))).toBe(false)
    expect(index.current.get(hitlRequestKey(
      'interaction-2', 'cloud_providers',
    ))).toBe(secondId)
  })

  it('preserves interaction-scoped singleton projections across follow-up rounds', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()
    const index = { current: new Map<string, string>() }
    const ctx = {
      conversationId: 'room-1', lifecycle,
      getAgentName: async () => 'Agent', getAgentSource: () => undefined,
      reconcileWithDb: async () => {}, hitlRequestIndex: index, setCancelling: () => {},
    }
    for (const requestId of ['round-1', 'round-2']) {
      await handleHitlRequest(ctx, {
        type: 'hitl_request', conversation_id: 'room-1', timestamp,
        data: {
          request_id: requestId, message_id: 'agent-message', source: 'agent',
          prompt: `Question ${requestId}`, prompt_type: 'text',
          interaction_id: `interaction-${requestId}`, question_count: 1, question_index: 0,
        },
      }, 'req-1')
    }

    const firstId = hitlQuestionEntityId(
      'agent-message', 'interaction-round-1', 'round-1', 1,
    )
    const secondId = hitlQuestionEntityId(
      'agent-message', 'interaction-round-2', 'round-2', 1,
    )
    expect(index.current).toEqual(new Map([
      [hitlRequestKey('interaction-round-1', 'round-1'), firstId],
      [hitlRequestKey('interaction-round-2', 'round-2'), secondId],
    ]))
    expect(useMessageStore.getState().entities[firstId]).toMatchObject({
      hitlRequestId: 'round-1', hitlPrompt: 'Question round-1',
    })
    expect(useMessageStore.getState().entities[secondId]).toMatchObject({
      hitlRequestId: 'round-2', hitlPrompt: 'Question round-2',
    })
  })

  it('terminalizes an errored response and removes its request mapping', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()
    const index = { current: new Map<string, string>() }
    const ctx = {
      conversationId: 'room-1', lifecycle,
      getAgentName: async () => 'Agent', getAgentSource: () => undefined,
      reconcileWithDb: async () => {}, hitlRequestIndex: index, setCancelling: () => {},
    }
    await handleHitlRequest(ctx, {
      type: 'hitl_request', conversation_id: 'room-1', timestamp,
      data: {
        request_id: 'request-error', message_id: 'agent-message', source: 'agent',
        prompt: 'Question?', prompt_type: 'text', interaction_id: 'interaction-error',
        question_count: 1, question_index: 0,
      },
    }, 'req-1')

    handleHitlResponse(ctx, {
      type: 'hitl_response', conversation_id: 'room-1', timestamp,
      data: {
        request_id: 'request-error', message_id: 'agent-message', source: 'agent',
        status: 'error', error_message: 'Unable to continue',
        interaction_id: 'interaction-error', question_count: 1, question_index: 0,
      },
    }, 'req-1')

    const errorId = hitlQuestionEntityId(
      'agent-message', 'interaction-error', 'request-error', 1,
    )
    expect(useMessageStore.getState().entities[errorId]).toMatchObject({
      hitlResolved: true, hitlInteractionStatus: 'error',
      hitlApplicationStatus: 'error', taskStatus: 'failed', taskError: 'Unable to continue',
    })
    expect(index.current.has(hitlRequestKey(
      'interaction-error', 'request-error',
    ))).toBe(false)
    expect(selectPendingHitls(
      'room-1', useMessageStore.getState().entities, useMessageStore.getState().orderedIds,
    )).toHaveLength(0)
  })

  it('preserves auth semantics and same-request nonterminal transitions', async () => {
    const lifecycle = makeLifecycle()
    const timestamp = new Date().toISOString()
    const index = { current: new Map<string, string>() }
    const ctx = {
      conversationId: 'room-1',
      lifecycle,
      getAgentName: async () => 'Auth Agent',
      getAgentSource: () => undefined,
      reconcileWithDb: async () => {},
      hitlRequestIndex: index,
      setCancelling: () => {},
    }

    await handleHitlRequest(ctx, {
      type: 'hitl_request',
      conversation_id: 'room-1',
      timestamp,
      data: {
        request_id: 'auth-hitl',
        message_id: 'auth-message',
        client_request_id: 'client-1',
        source: 'agent',
        prompt: 'Authenticate with the carrier',
        prompt_type: 'authentication',
        interaction_id: 'interaction-1',
        interaction_status: 'open',
        question_count: 1,
        question_index: 0,
      },
    }, 'req-1')

    const authId = hitlQuestionEntityId(
      'auth-message', 'interaction-1', 'auth-hitl', 1,
    )
    expect(useMessageStore.getState().entities[authId]).toMatchObject({
      hitlPromptType: 'authentication',
      clientRequestId: 'client-1',
      hitlResolved: false,
    })

    handleHitlResponse(ctx, {
      type: 'hitl_response',
      conversation_id: 'room-1',
      timestamp,
      data: {
        request_id: 'auth-hitl',
        message_id: 'auth-message',
        source: 'agent',
        status: 'answer_recorded',
        interaction_id: 'interaction-1',
        interaction_status: 'applying',
        application_status: 'applying',
        question_count: 1,
        question_index: 0,
        client_request_id: 'client-1',
      },
    }, 'req-1')

    expect(useMessageStore.getState().entities[authId]).toMatchObject({
      hitlResolved: false,
      hitlInteractionStatus: 'applying',
      hitlApplicationStatus: 'applying',
      clientRequestId: 'client-1',
    })
    expect(index.current.get(hitlRequestKey(
      'interaction-1', 'auth-hitl',
    ))).toBe(authId)

    handleHitlResponse(ctx, {
      type: 'hitl_response',
      conversation_id: 'room-1',
      timestamp,
      data: {
        request_id: 'auth-hitl',
        message_id: 'auth-message',
        source: 'agent',
        status: 'responded',
        interaction_id: 'interaction-1',
        interaction_status: 'applied',
        application_status: 'applied',
        question_count: 1,
        question_index: 0,
        client_request_id: 'client-1',
      },
    }, 'req-1')

    expect(useMessageStore.getState().entities[authId].hitlResolved).toBe(true)
    expect(index.current.has(hitlRequestKey(
      'interaction-1', 'auth-hitl',
    ))).toBe(false)
  })
})
