import { expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessage, SessionResumeResult } from '@/types/hermes'

import { reconcilePersistedLiveTurn } from './persisted-live-turn'

const TEXT = 'Inspecting a synthetic isolated sample.\n\nKeep this public paragraph once and retain every tool result.'
const PROMPT = 'Continue with the approved work.'
const calls = Array.from({ length: 7 }, (_, i) => ({ id: `call-${i}`, type: 'function', function: { name: 'demo', arguments: '{}' } }))

function scenario(content: string, commentary: string[], snapshot: string, previous: ChatMessage[] = []) {
  const rows: SessionMessage[] = [
    { id: 11, role: 'user', content: PROMPT, timestamp: 100 },
    { id: 12, role: 'assistant', content, display_commentary: commentary, timestamp: 118.880, tool_calls: calls,
      codex_message_items: [{ type: 'message', role: 'assistant', phase: 'commentary', content: [{ type: 'output_text', text: TEXT }] }] }
  ]

  const projection = {
    session_id: 'synthetic-runtime',
    inflight: { user: PROMPT, assistant: snapshot, streaming: true }
  } as SessionResumeResult

  return reconcilePersistedLiveTurn(toChatMessages(rows), previous, rows, projection)!
}

const visible = (rows: ChatMessage[]) => rows.filter(row => row.role === 'assistant').map(row => chatMessageText(row).trim()).filter(Boolean)

it('control: canonical content is subtracted from inflight exactly once', () => {
  expect(visible(scenario(TEXT, [], TEXT))).toEqual([TEXT])
})

it('Codex commentary-only durable row is not painted again from inflight', () => {
  const result = scenario('', [TEXT], TEXT)
  console.log('commentary-only-visible', JSON.stringify(visible(result)))
  expect(visible(result)).toEqual([TEXT])
})

it('Codex commentary plus a newer snapshot tail keeps only the new tail', () => {
  const TAIL = 'A new result follows.'
  expect(visible(scenario('', [TEXT], TEXT + '\n\n' + TAIL))).toEqual([TEXT, TAIL])
})

it('control: genuinely repeated text in a later segment is retained', () => {
  expect(visible(scenario(TEXT, [], TEXT + '\n\n' + TEXT))).toEqual([TEXT, TEXT])
})

it('control: absent public projection never promotes raw provider sidecar', () => {
  expect(visible(scenario('', [], 'New visible output'))).toEqual(['New visible output'])
})
