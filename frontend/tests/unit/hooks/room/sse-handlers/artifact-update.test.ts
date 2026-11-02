import { describe, expect, it, beforeEach } from 'vitest'
import { handleArtifactUpdate } from '@/hooks/room/sse-handlers/handlers/artifact-update'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import { TASK_STATE } from '@/lib/types/sse'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'

function makeLifecycle(): ProcessingLifecycle {
  return {
    placeholderId: () => 'placeholder',
    isPlaceholderDismissed: () => true,
    dismissPlaceholder: () => {},
    disarmCancelTimeout: () => {},
    hasCancelTimedOut: () => false,
  } as unknown as ProcessingLifecycle
}

describe('handleArtifactUpdate', () => {
  beforeEach(() => {
    useStreamingStore.setState({ buffers: {} })
    useMessageStore.setState({ entities: {}, orderedIds: [] })
  })

  it('defaults append to true for terminal frames when the same artifact id already exists in the buffer', () => {
    useStreamingStore.getState().append('msg-1', 'room-1', {
      artifactId: 'stream-1',
      parts: [{ kind: 'text', text: 'Earlier paragraph. ' }],
    }, false)

    handleArtifactUpdate(
      { conversationId: 'room-1', lifecycle: makeLifecycle() },
      {
        type: 'artifact_update',
        conversation_id: 'room-1',
        timestamp: new Date().toISOString(),
        data: {
          message_id: 'msg-1',
          agent_id: 'agent-1',
          client_request_id: 'req-1',
          artifact: {
            artifact_id: 'stream-1',
            parts: [{ kind: 'text', text: 'Later paragraph.' }],
          },
          append: undefined as unknown as boolean,
          last_chunk: true,
        },
      },
      'req-1',
    )

    expect(useStreamingStore.getState().buffers['msg-1']?.text).toBe(
      'Earlier paragraph. Later paragraph.',
    )
    expect(useStreamingStore.getState().buffers['msg-1']?.isComplete).toBe(true)
    expect(useStreamingStore.getState().buffers['msg-1']?.clientRequestId).toBe('req-1')
  })

  it.each([
    ['false', false],
    ['omitted', undefined],
  ])(
    'rejects partial artifact_update frames with last_chunk %s without mutating public state',
    (_label, lastChunk) => {
      const privateText = `PRIVATE_SENTINEL_artifact_text_${_label}`
      const privateMetadata = `PRIVATE_SENTINEL_artifact_metadata_${_label}`
      const privateBytes = `PRIVATE_SENTINEL_artifact_bytes_${_label}`

      useStreamingStore.getState().append('existing-msg', 'room-1', {
        artifactId: 'existing-artifact',
        parts: [{ kind: 'text', text: 'Public baseline.' }],
      }, false, { clientRequestId: 'existing-req' })
      useMessageStore.getState().upsertMessage({
        id: 'existing-msg',
        conversationId: 'room-1',
        messageType: 'agent',
        content: 'Public baseline.',
        senderName: 'Agent',
        timestamp: '2026-07-13T00:00:00.000Z',
      }, 'sse')

      const buffersBefore = useStreamingStore.getState().buffers
      const entitiesBefore = useMessageStore.getState().entities
      const data: Record<string, unknown> = {
        message_id: 'private-msg',
        agent_id: 'agent-1',
        client_request_id: 'req-1',
        artifact: {
          artifact_id: 'private-artifact',
          name: privateMetadata,
          parts: [
            { kind: 'text', text: privateText },
            { kind: 'data', data: { sentinel: privateMetadata } },
            {
              kind: 'file',
              file: {
                bytes: privateBytes,
                mime_type: 'text/plain',
                name: 'private.txt',
              },
            },
          ],
        },
        append: false,
      }
      if (lastChunk !== undefined) data.last_chunk = lastChunk

      handleArtifactUpdate(
        { conversationId: 'room-1', lifecycle: makeLifecycle() },
        {
          type: 'artifact_update',
          conversation_id: 'room-1',
          timestamp: '2026-07-13T00:00:01.000Z',
          data,
        } as unknown as Parameters<typeof handleArtifactUpdate>[1],
        'req-1',
      )

      const streamingState = useStreamingStore.getState().buffers
      const entityState = useMessageStore.getState().entities
      const publicState = JSON.stringify({
        streamingState,
        entityState,
      })

      expect(streamingState).toEqual(buffersBefore)
      expect(entityState).toEqual(entitiesBefore)
      expect(publicState).not.toContain(privateText)
      expect(publicState).not.toContain(privateMetadata)
      expect(publicState).not.toContain(privateBytes)
    },
  )

  it('ignores late artifact_update after the agent task is terminal', () => {
    useStreamingStore.getState().append('msg-1', 'room-1', {
      artifactId: 'stream-1',
      parts: [{ kind: 'text', text: 'Final answer.' }],
    }, false)
    useMessageStore.getState().upsertMessage({
      id: 'msg-1',
      conversationId: 'room-1',
      messageType: 'agent',
      content: 'Final answer.',
      senderName: 'Agent',
      timestamp: new Date().toISOString(),
      taskStatus: TASK_STATE.COMPLETED,
    }, 'sse')

    handleArtifactUpdate(
      { conversationId: 'room-1', lifecycle: makeLifecycle() },
      {
        type: 'artifact_update',
        conversation_id: 'room-1',
        timestamp: new Date().toISOString(),
        data: {
          message_id: 'msg-1',
          agent_id: 'agent-1',
          client_request_id: 'req-1',
          artifact: {
            artifact_id: 'stream-1',
            parts: [{ kind: 'text', text: 'late chunk' }],
          },
          append: true,
          last_chunk: true,
        },
      },
      'req-1',
    )

    expect(useStreamingStore.getState().buffers['msg-1']).toBeUndefined()
  })
})
