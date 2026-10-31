import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages, getSession, type SessionInfo } from '@/hermes'
import { chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearClarifyRequest } from '@/store/clarify'
import { requestGatewayForAgent, requestGatewayForProfile } from '@/store/gateway'
import {
  setActiveSessionId,
  setConnection,
  setMessages,
  setResumeFailedSessionId,
  setSessions,
  setSessionStartedAt
} from '@/store/session'
import { deferred } from '@/test/deferred'
import type { SessionMessage } from '@/types/hermes'

import { renderMessageStream } from '../use-message-stream/test-harness'

import { ResumeHarness } from './test-harness'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn().mockResolvedValue(undefined),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

vi.mock('@/store/gateway', async importOriginal => {
  const original = await importOriginal<Record<string, unknown>>()

  return {
    ...original,
    // Default-preserving spy: tests that route by the active source override it.
    activeGatewayConnectionId: vi.fn(original.activeGatewayConnectionId as () => null | string),
    requestGatewayForAgent: vi.fn(),
    requestGatewayForProfile: vi.fn(),
    retainGatewayForAgent: vi.fn(async () => () => undefined)
  }
})

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  noteActiveTreeGroup: vi.fn(),
  revealTreePane: vi.fn()
}))

function storedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'stored-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'stored',
    tool_call_count: 0,
    ...overrides
  }
}

const clientState = createClientSessionState

describe('resumeSession warm-cache mapping integrity', () => {
  beforeEach(() => {
    // Earlier describes (branchStoredSession) drive resumes through the
    // profile path on the SAME hoisted mock; drop their recorded calls so the
    // not-called assertions below only see this describe's traffic.
    vi.mocked(requestGatewayForProfile).mockReset()
    vi.mocked(requestGatewayForAgent).mockReset()
  })

  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setSessionStartedAt(null)
    setMessages([])
    setSessions([])
    vi.mocked(getSession).mockReset()
    vi.mocked(getLatestSessionMessages)
      .mockReset()
      .mockResolvedValue({ messages: [] } as never)
    vi.mocked(requestGatewayForAgent).mockReset()
    clearClarifyRequest()
    vi.mocked(requestGatewayForProfile).mockReset()
    setConnection(null)
    vi.restoreAllMocks()
  })

  it.each(
    (['cold', 'warm'] as const)
      .flatMap(mode =>
        (['matching', 'absent', 'segmented'] as const).flatMap(reasoning =>
          [false, true].flatMap(multipleCommentary =>
            [false, true].map(completeBeforeRest => ({
              mode,
              reasoning,
              multipleCommentary,
              completeBeforeRest,
              sealedBeforeRest: false,
              emptySnapshot: false
            }))
          )
        )
      )
      .concat(
        (['cold', 'warm'] as const).map(mode => ({
          mode,
          reasoning: 'matching' as const,
          multipleCommentary: false,
          completeBeforeRest: false,
          sealedBeforeRest: true,
          emptySnapshot: false
        }))
      )
      .concat(
        (['cold', 'warm'] as const).flatMap(mode =>
          (['matching', 'segmented'] as const).map(reasoning => ({
            mode,
            reasoning,
            multipleCommentary: false,
            completeBeforeRest: false,
            sealedBeforeRest: false,
            emptySnapshot: true
          }))
        )
      )
  )(
    'does not duplicate a running Codex tool block during $mode history refresh with $reasoning live reasoning (multiple commentary: $multipleCommentary, complete before REST: $completeBeforeRest, sealed before REST: $sealedBeforeRest, empty snapshot: $emptySnapshot)',
    async ({ mode, reasoning, multipleCommentary, completeBeforeRest, sealedBeforeRest, emptySnapshot }) => {
      const sid = 'rt-A'

      const commentary = emptySnapshot
        ? 'Inspecting an isolated synthetic sample.\n\nThe public update must occur once while completed tool output remains available.'
        : 'I will compare isolated frontend and backend worktrees.'

      const comments = multipleCommentary
        ? [commentary, 'The two delegates will use separate directories.']
        : [commentary]

      const publicText = comments.join('\n\n')
      const thinking = '**Preparing parallel worktrees**'
      const privateReasoning = 'PRIVATE RAW ANALYSIS: not authorized for display.'
      const prompt = 'Investigate the duplicate block.'

      const call = (id: string, name: string, args: Record<string, unknown> = {}) => ({
        id,
        type: 'function' as const,
        function: { name, arguments: JSON.stringify(args) }
      })

      const tasks = { tasks: [{ goal: 'FRONTEND' }, { goal: 'BACKEND' }] }

      const rows: SessionMessage[] = [
        { id: 1, role: 'user', content: prompt },
        {
          id: 2,
          role: 'assistant',
          content: '',
          reasoning: `${privateReasoning}\n\n${thinking}\n\n${publicText}`,
          display_reasoning: thinking,
          display_commentary: comments,
          codex_message_items: comments.map(text => ({
            type: 'message',
            role: 'assistant',
            phase: 'commentary',
            content: [{ type: 'output_text', text }]
          })),
          tool_calls: [call('read', 'read_file')]
        },
        { id: 3, role: 'tool', content: 'read', tool_call_id: 'read' },
        { id: 4, role: 'assistant', content: '', tool_calls: [call('delegate', 'delegate_task', tasks)] },
        { id: 5, role: 'tool', content: '{"status":"running"}', tool_call_id: 'delegate' },
        {
          id: 6,
          role: 'assistant',
          content: '',
          tool_calls: [call('cmd-1', 'terminal'), call('cmd-2', 'terminal'), call('cmd-3', 'terminal')]
        },
        // The history snapshot predates cmd-3's completion.
        ...[1, 2].map((n): SessionMessage => ({ id: 6 + n, role: 'tool', content: 'ok', tool_call_id: `cmd-${n}` }))
      ]

      setSessions([storedSession({ id: 'stored-A', message_count: rows.length })])
      const states = new Map([[sid, { ...clientState('stored-A'), messages: toChatMessages(rows.slice(0, 1)) }]])
      const stream = renderMessageStream(null, { states })

      const send = (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
        act(() => stream.handleEvent({ type, payload, session_id: sid }))

      await send('message.start')

      if (reasoning !== 'absent') {
        await send('reasoning.delta', { text: reasoning === 'segmented' ? '**Preparing' : thinking })
      }

      for (const text of comments) {
        await send('message.interim', { text })
      }

      if (reasoning === 'segmented') {
        await send('reasoning.delta', { text: ' parallel worktrees**' })
      }

      await send('tool.start', { name: 'read_file', tool_id: 'read', args: {} })
      await send('tool.complete', { name: 'read_file', tool_id: 'read', result: 'read' })
      await send('tool.start', { name: 'delegate_task', tool_id: 'delegate', args: tasks })
      await send('tool.complete', { name: 'delegate_task', tool_id: 'delegate', result: '{"status":"running"}' })

      for (const n of [1, 2, 3]) {
        await send('tool.start', { name: 'terminal', tool_id: `cmd-${n}`, args: {} })
      }

      const persisted = deferred<Awaited<ReturnType<typeof getLatestSessionMessages>>>()
      vi.mocked(getLatestSessionMessages).mockReturnValue(persisted.promise)

      const requestGateway = vi.fn(
        async () =>
          ({
            session_id: sid,
            session_key: 'stored-A',
            resumed: 'stored-A',
            messages: [],
            messages_omitted: true,
            running: true,
            info: {},
            inflight: { user: prompt, assistant: emptySnapshot ? '' : publicText, streaming: true }
          }) as never
      )

      let resume!: Parameters<Parameters<typeof ResumeHarness>[0]['onReady']>[0]
      render(
        <ResumeHarness
          onReady={ready => (resume = ready)}
          requestGateway={requestGateway}
          runtimeIdByStoredSessionIdRef={{ current: new Map(mode === 'warm' ? [['stored-A', sid]] : []) }}
          sessionStateByRuntimeIdRef={{ current: states }}
        />
      )
      let refreshing!: Promise<unknown>
      await act(async () => {
        refreshing = resume('stored-A', true)
      })
      await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalled())

      // Real stream reducers advance the cached row while the REST read is held.
      const completeTool = (n: number) =>
        send('tool.complete', {
          name: 'terminal',
          tool_id: `cmd-${n}`,
          result: 'ok',
          duration_s: 1.25,
          summary: 'Command completed.'
        })

      for (const n of sealedBeforeRest ? [1, 2] : [1, 2, 3]) {
        await completeTool(n)
      }

      const sharedTools = () =>
        states
          .get(sid)!
          .messages.flatMap(message => message.parts)
          .filter(part => part.type === 'tool-call' && part.toolCallId === 'cmd-3')

      if (sealedBeforeRest) {
        await send('message.interim', { text: 'Waiting for the last command.' })
        expect(sharedTools()).toHaveLength(1)
        expect(sharedTools()[0].completedAt).toEqual(expect.any(Number))
        expect(Object.hasOwn(sharedTools()[0], 'result')).toBe(false)
      } else {
        expect(sharedTools()[0]).toMatchObject({
          result: 'ok',
          completedAt: expect.any(Number),
          toolResultMetadata: { duration_s: 1.25, summary: 'Command completed.' }
        })
      }

      let completedTool = sharedTools()[0]

      if (multipleCommentary) {
        await send('reasoning.delta', { text: 'New live-only reasoning must survive.' })
        await send('message.interim', { text: commentary })
      }

      await send('tool.start', { name: 'delegate_task', tool_id: 'live-only', args: tasks })

      const complete = async () => {
        await send('tool.complete', { name: 'delegate_task', tool_id: 'live-only', result: 'finished' })
        await send('message.delta', { text: 'Comparison complete.' })
        await send('message.complete', { text: 'Comparison complete.' })
      }

      if (completeBeforeRest) {
        await complete()
        expect(states.get(sid)!.messages.some(message => message.pending)).toBe(false)
        expect(states.get(sid)!.messages.at(-1)?.completedAt).toEqual(expect.any(Number))
      }

      const completedAt = states.get(sid)!.messages.at(-1)?.completedAt

      await act(async () => {
        persisted.resolve({ session_id: 'stored-A', messages: rows })
        await refreshing
      })

      if (sealedBeforeRest) {
        expect(sharedTools()).toHaveLength(1)
        expect.soft(Object.hasOwn(sharedTools()[0], 'result')).toBe(false)

        const durableOwner = states.get(sid)!.messages.find(message => message.parts.includes(sharedTools()[0]))!

        expect(durableOwner.rowId).toBe(rows[1].id)
        await completeTool(3)
        expect(sharedTools()).toHaveLength(1)
        expect(states.get(sid)!.messages.find(message => message.parts.includes(sharedTools()[0]))?.id).toBe(
          durableOwner.id
        )
        completedTool = sharedTools()[0]
        expect(completedTool).toMatchObject({
          result: 'ok',
          completedAt: expect.any(Number),
          toolResultMetadata: { duration_s: 1.25, summary: 'Command completed.' }
        })
      }

      const messages = states.get(sid)!.messages
      const tools = messages.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))
      expect
        .soft(tools.map(part => part.toolCallId))
        .toEqual(['read', 'delegate', 'cmd-1', 'cmd-2', 'cmd-3', 'live-only'])
      expect(tools.filter(part => part.toolCallId === 'cmd-3')).toEqual([
        expect.objectContaining({
          result: 'ok',
          completedAt: completedTool.completedAt,
          toolResultMetadata: completedTool.toolResultMetadata
        })
      ])

      for (const text of comments) {
        expect
          .soft(messages.map(chatMessageText).join('\n').split(text))
          .toHaveLength(multipleCommentary && text === commentary ? 3 : 2)
      }

      expect(JSON.stringify(messages)).not.toContain(privateReasoning)

      if (multipleCommentary) {
        expect(JSON.stringify(messages)).toContain('New live-only reasoning must survive.')
      }

      expect.soft(messages.some(message => message.pending)).toBe(!completeBeforeRest)
      expect(messages.filter(message => message.role === 'assistant' && !message.parts.length)).toHaveLength(0)
      expect(
        messages.some(
          message =>
            message.pending && message.parts.some(part => part.type === 'tool-call' && part.toolCallId === 'live-only')
        )
      ).toBe(!completeBeforeRest)

      if (completeBeforeRest) {
        expect(messages.at(-1)?.completedAt).toBe(completedAt)
      } else {
        await complete()
      }

      const settled = states.get(sid)!.messages
      expect(settled.some(message => message.pending)).toBe(false)
      expect(settled.map(chatMessageText).join('\n').split('Comparison complete.')).toHaveLength(2)
      expect(
        settled
          .flatMap(message => message.parts)
          .find(part => part.type === 'tool-call' && part.toolCallId === 'live-only')
      ).toMatchObject({ result: 'finished' })
      expect(
        settled.flatMap(message => message.parts.filter(part => part.type === 'tool-call')).map(part => part.toolCallId)
      ).toEqual(tools.map(part => part.toolCallId))
    }
  )
})
