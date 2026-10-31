import { expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, toChatMessages, upsertToolPart } from '@/lib/chat-messages'
import type { SessionMessage, SessionResumeResult } from '@/types/hermes'

import { reconcilePersistedLiveTurn } from './persisted-live-turn'

const text = 'Inspecting a synthetic isolated sample.\n\nKeep this public paragraph once and retain every tool result.'
const prompt = 'Continue.'
const calls = Array.from({ length: 7 }, (_, i) => ({ id: `tool-${i}`, type: 'function', function: { name: 'demo', arguments: '{}' } }))

// Every string is synthetic. No production reasoning was read or replayed.
it.each([false, true])('reconcile cached commentary with shared tools; reasoning layout differs=%s', differs => {
  const rows: SessionMessage[] = [
    { id: 1, role: 'user', content: prompt, timestamp: 100 },
    { id: 2, role: 'assistant', content: '', display_commentary: [text], display_reasoning: 'Synthetic stored layout', tool_calls: calls, timestamp: 118.880 }
  ]

  const hydrated = toChatMessages(rows)

  const local: ChatMessage[] = [
    hydrated[0],
    { id: 'assistant-stream-interim', role: 'assistant', pending: false, interim: true, timestamp: 118.141,
      parts: [
        { type: 'reasoning', text: differs ? 'Synthetic streamed layout' : 'Synthetic stored layout', timestamp: 117 },
        { type: 'text', text, timestamp: 118.141 }
      ] },
    { id: 'assistant-stream-tools', role: 'assistant', pending: true,
      parts: hydrated[1].parts.filter(part => part.type === 'tool-call').map(part =>
        upsertToolPart([part], { tool_id: part.toolCallId, name: 'demo', result: 'late synthetic result' }, 'complete', 119)[0]) }
  ]

  const projection = { session_id: 'runtime', inflight: { user: prompt, assistant: '', streaming: true } } as SessionResumeResult
  const result = reconcilePersistedLiveTurn(hydrated, local, rows, projection)!
  const visible = result.filter(row => row.role === 'assistant').map(row => chatMessageText(row).trim()).filter(Boolean)
  console.log('cache-shape', JSON.stringify({ differs, visible, timestamps: result.filter(row => chatMessageText(row) === text).map(row => row.timestamp) }))
  expect(visible).toEqual([text])
  const tools = result.flatMap(row => row.parts.filter(part => part.type === 'tool-call'))
  expect(tools).toHaveLength(7)
  expect(tools.every(part => part.type === 'tool-call' && part.result === 'late synthetic result')).toBe(true)
})

it.each([false, true])('control: keeps equal public commentary across distinct user turns; repeated prompt=%s', repeatedPrompt => {
  const secondPrompt = repeatedPrompt ? prompt : 'Continue with a second task.'

  const rows: SessionMessage[] = [
    { id: 1, role: 'user', content: prompt },
    { id: 2, role: 'assistant', content: '', display_commentary: [text], tool_calls: [calls[0]] },
    { id: 3, role: 'tool', content: 'first task result', tool_call_id: calls[0].id },
    { id: 4, role: 'user', content: secondPrompt },
    { id: 5, role: 'assistant', content: '', display_commentary: [text], tool_calls: [calls[1]] }
  ]

  const history = toChatMessages(rows)

  const local = history.map(message => message.role === 'assistant' && message.parts.some(part => part.type === 'tool-call' && part.toolCallId === calls[1].id)
    ? { ...message, parts: message.parts.flatMap(part => part.type === 'tool-call'
      ? upsertToolPart([part], { name: 'demo', tool_id: part.toolCallId, result: 'second task result' }, 'complete', 120)
      : [part]) }
    : message)

  const result = reconcilePersistedLiveTurn(history, local, rows, {
    session_id: 'runtime', inflight: { user: secondPrompt, assistant: '', streaming: true }
  })!

  expect(result.filter(message => message.role === 'assistant').map(chatMessageText).filter(Boolean)).toEqual([text, text])
  expect(result.filter(message => message.role === 'user').map(message => message.rowId)).toEqual([1, 4])
  expect(result.flatMap(message => message.parts).filter(part => part.type === 'tool-call').map(part => [part.toolCallId, part.result]))
    .toEqual([[calls[0].id, 'first task result'], [calls[1].id, 'second task result']])
})
