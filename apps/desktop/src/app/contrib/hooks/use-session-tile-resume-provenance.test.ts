import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearSingleFlightSessionResumeState,
  singleFlightSessionResume
} from '@/app/session/hooks/use-prompt-actions/single-flight-resume'
import { primaryResumeParams } from '@/app/session/hooks/use-session-actions/resume-owner'
import { getLatestSessionMessages } from '@/hermes'
import { requestGatewayForAgent } from '@/store/gateway'
import { _resetSessionOwnerHintsForTests, setSessions } from '@/store/session'
import { requestForSessionProfile } from '@/store/session-request-router'
import { $sessionTiles, sessionTileDelegate } from '@/store/session-states'
import type { SessionInfo, SessionResumeResult } from '@/types/hermes'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'stored' }))
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

const owner = { connectionId: 'remote-owner', profile: 'work' }
const ambient = vi.fn(async () => {
  throw new Error('must use the resolved owner')
})
const omitted: SessionResumeResult = { session_id: 'runtime', messages: [], message_count: 0, resumed: 'stored' }

function mountTile() {
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(),
      branchLoadedSession: vi.fn(),
      branchStoredSession: vi.fn(),
      executeSlashCommand: vi.fn(),
      removeSession: vi.fn(),
      requestGateway: ambient,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn()
    })
  )
}

function primary(authoritativeSnapshot = false) {
  return singleFlightSessionResume(
    'stored',
    () =>
      requestForSessionProfile<SessionResumeResult>(
        owner,
        ambient,
        'session.resume',
        primaryResumeParams('stored', false, authoritativeSnapshot, owner.profile)
      ),
    { scope: owner, requiresMessages: authoritativeSnapshot }
  )
}

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  _resetSessionOwnerHintsForTests()
  $sessionTiles.set([])
  setSessions([{ id: 'stored', profile: owner.profile, connection_id: owner.connectionId, cwd: null } as SessionInfo])
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  clearSingleFlightSessionResumeState()
  _resetSessionOwnerHintsForTests()
  setSessions([])
  $sessionTiles.set([])
})

describe('Desktop tile/primary resume provenance', () => {
  it.each(['tile', 'primary'] as const)('keeps Desktop deferred attach with %s arriving first', async first => {
    let release!: (value: SessionResumeResult) => void
    vi.mocked(requestGatewayForAgent).mockImplementation(
      () => new Promise(resolve => { release = resolve as typeof release })
    )
    mountTile()
    const tile = () => sessionTileDelegate()!.resumeTile('stored')
    const a = first === 'tile' ? tile() : primary()
    await vi.waitFor(() => expect(requestGatewayForAgent).toHaveBeenCalledTimes(1))
    const b = first === 'tile' ? primary() : tile()
    await vi.waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledTimes(1))
    release(omitted)
    await Promise.all([a, b])

    expect(requestGatewayForAgent).toHaveBeenCalledTimes(1)
    const [connection, profile, method, params] = vi.mocked(requestGatewayForAgent).mock.calls[0]
    expect([connection, profile, method]).toEqual([owner.connectionId, owner.profile, 'session.resume'])
    expect(params).toEqual({
      session_id: 'stored', cols: 96, source: 'desktop', defer_history: true,
      omit_messages: true, profile: owner.profile
    })
    expect(params).not.toHaveProperty('cwd')
    expect(ambient).not.toHaveBeenCalled()
  })

  it('queues a full primary snapshot after an omitted tile flight, never in parallel', async () => {
    let release!: (value: SessionResumeResult) => void
    vi.mocked(requestGatewayForAgent)
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve as typeof release }))
      .mockResolvedValueOnce({ ...omitted, messages: [{ role: 'user', content: 'retained' }] } as never)
    mountTile()
    const tile = sessionTileDelegate()!.resumeTile('stored')
    await vi.waitFor(() => expect(requestGatewayForAgent).toHaveBeenCalledTimes(1))
    const snapshot = primary(true)
    await Promise.resolve()
    expect(requestGatewayForAgent).toHaveBeenCalledTimes(1)
    release(omitted)
    await tile
    expect((await snapshot).messages).toEqual([{ role: 'user', content: 'retained' }])
    expect(requestGatewayForAgent).toHaveBeenCalledTimes(2)
    const params = vi.mocked(requestGatewayForAgent).mock.calls.map(call => call[3])
    expect(params[0]).toMatchObject({ source: 'desktop', defer_history: true, omit_messages: true })
    expect(params[1]).toMatchObject({ source: 'desktop', defer_history: false, omit_messages: false })
  })
})
