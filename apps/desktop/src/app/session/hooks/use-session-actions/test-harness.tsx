import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionProfileRoute } from '@/store/session-request-router'

import { suppressTranscriptForView, transcriptRowContentKey, type TranscriptViewCutoff } from './transcript-provenance'

import { useSessionActions } from './index'

export function ResumeHarness({
  onStateUpdate,
  onViewSync,
  onReady,
  getRouteToken = () => 'token',
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  selectedStoredSessionId = null,
  sessionStateByRuntimeIdRef
}: {
  getRouteToken?: () => string
  onStateUpdate?: (sessionId: string, state: ClientSessionState) => void
  onViewSync?: (sessionId: string, state: ClientSessionState) => void
  onReady: (
    resume: (
      storedSessionId: string,
      replaceRoute?: boolean,
      ownerRoute?: SessionProfileRoute,
      options?: { authoritativeSnapshot?: boolean }
    ) => Promise<unknown>
  ) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  runtimeIdByStoredSessionIdRef?: MutableRefObject<Map<string, string>>
  selectedStoredSessionId?: string | null
  sessionStateByRuntimeIdRef?: MutableRefObject<Map<string, ClientSessionState>>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })
  const runtimeMapRef = runtimeIdByStoredSessionIdRef ?? ref(new Map<string, string>())
  const stateMapRef = sessionStateByRuntimeIdRef ?? ref(new Map<string, ClientSessionState>())
  // Stand in for the real hook's gate: without it this stub would publish the
  // unproven warm cache before REST authority lands. Kept here (not per-test)
  // so every resume through this harness sees one shared suppression policy.
  const heldCutoffsByRuntimeIdRef = ref(new Map<string, TranscriptViewCutoff>())

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken,
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: vi.fn(),
    routedSessionId: null,
    runtimeIdByStoredSessionIdRef: runtimeMapRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef: ref<string | null>(selectedStoredSessionId),
    sessionStateByRuntimeIdRef: stateMapRef,
    holdSessionTranscriptView: runtimeId => {
      const rows = stateMapRef.current.get(runtimeId)?.messages ?? []

      const cutoff: TranscriptViewCutoff = {
        cutoffIds: new Set(rows.map(message => message.id)),
        cutoffKeys: new Set(rows.map(transcriptRowContentKey))
      }

      heldCutoffsByRuntimeIdRef.current.set(runtimeId, cutoff)

      return () => {
        heldCutoffsByRuntimeIdRef.current.delete(runtimeId)
      }
    },
    syncSessionStateToView: (sessionId, state) => {
      const cutoff = heldCutoffsByRuntimeIdRef.current.get(sessionId) ?? null

      onViewSync?.(sessionId, suppressTranscriptForView(state, cutoff))
    },
    updateSessionState: (sessionId, updater, storedSessionId) => {
      // Full default shape (not a bare {} cast) so seeded/derived fields like
      // turnStartedAt behave as in production state updates.
      const current = stateMapRef.current.get(sessionId) ?? createClientSessionState(storedSessionId ?? null)
      const next = updater(current)

      stateMapRef.current.set(sessionId, next)
      onStateUpdate?.(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}
