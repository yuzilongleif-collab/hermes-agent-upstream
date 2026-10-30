import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const { search } = vi.hoisted(() => ({ search: vi.fn() }))
vi.mock('@/hermes', () => ({ searchSessions: search }))
vi.mock('@/api/client', async () => {
  const { map } = await import('nanostores')
  return { $apiRequestScope: map({ profile: null, connectionId: null as string | null }) }
})
vi.mock('@/store/profile', () => ({ ALL_PROFILES: '__all__', normalizeProfileKey: (p: string) => p }))

import { useServerSessionSearch, type ServerSessionSearch } from './use-server-session-search'
import { $apiRequestScope } from '@/api/client'

let root: Root
let host: HTMLDivElement
let latest: ServerSessionSearch
function Probe({ query, profile = 'default' }: { query: string; profile?: string }) {
  latest = useServerSessionSearch(query, profile)
  return <output>{JSON.stringify(latest)}</output>
}
function deferred() {
  let resolve!: (value: { results: Array<{ session_id: string }> }) => void
  let reject!: (error: Error) => void
  const promise = new Promise((res, rej) => { resolve = res as typeof resolve; reject = rej })
  return { promise, resolve, reject }
}
async function show(query: string, profile?: string) { await act(() => root.render(<Probe query={query} profile={profile} />)) }
async function dispatch() { await act(() => vi.advanceTimersByTimeAsync(200)) }
const hits = (id: string) => ({ results: [{ session_id: id }] })

beforeEach(() => {
  vi.useFakeTimers()
  search.mockReset()
  $apiRequestScope.setKey('connectionId', null)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => { await act(() => root.unmount()); host.remove(); vi.useRealTimers() })

it('aborts an already-dispatched search on query replacement and on unmount', async () => {
  search.mockImplementation(() => new Promise(() => {}))
  await show('alpha'); await dispatch()
  const first = search.mock.calls[0][2].signal as AbortSignal
  expect(first.aborted).toBe(false)
  await show('beta')
  expect(first.aborted).toBe(true)
  await dispatch()
  const second = search.mock.calls[1][2].signal as AbortSignal
  await act(() => root.unmount())
  expect(second.aborted).toBe(true)
  root = createRoot(host)
})
it('clears debounce without dispatching and aborts on clear/profile change', async () => {
  search.mockImplementation(() => new Promise(() => {}))
  await show('a'); await show('b'); await dispatch()
  expect(search).toHaveBeenCalledTimes(1)
  expect(search.mock.calls[0][0]).toBe('b')
  const first = search.mock.calls[0][2].signal as AbortSignal
  await show('b', 'research')
  expect(first.aborted).toBe(true)
  await dispatch()
  const second = search.mock.calls[1][2].signal as AbortSignal
  await show('')
  expect(second.aborted).toBe(true)
  expect(latest.searchPending).toBe(false)
  expect(latest.serverMatches).toEqual([])
})
it('isolates ABA late success/finally and never retains previous-query hits after failure', async () => {
  const a1 = deferred(), b = deferred(), a2 = deferred()
  search.mockReturnValueOnce(a1.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(a2.promise)
  await show('a'); await dispatch(); await show('b'); await dispatch(); await show('a'); await dispatch()
  await act(async () => { a1.resolve(hits('old-a')) })
  expect(latest.serverMatches).toEqual([])
  expect(latest.searchPending).toBe(true)
  await act(async () => { a2.resolve(hits('new-a')) })
  expect(latest.serverMatches.map(x => x.session_id)).toEqual(['new-a'])
  await act(async () => { b.resolve(hits('old-b')) })
  expect(latest.serverMatches.map(x => x.session_id)).toEqual(['new-a'])
  const error = deferred(); search.mockReturnValueOnce(error.promise)
  await show('failed'); await dispatch(); await act(async () => { error.reject(new Error('503: search budget exhausted')) })
  expect(latest.serverMatches).toEqual([])
  expect(latest.searchPending).toBe(false)
  expect(latest.searchError).toBe(true)
})
it('distinguishes normal hits, successful empty, and failed search', async () => {
  search.mockResolvedValueOnce(hits('success')).mockResolvedValueOnce({ results: [] }).mockRejectedValueOnce(new Error('network failed'))
  await show('success'); await dispatch()
  expect(latest.serverMatches.map(x => x.session_id)).toEqual(['success'])
  expect(latest.searchError).toBe(false)
  await show('empty'); await dispatch()
  expect(latest.serverMatches).toEqual([])
  expect(latest.searchError).toBe(false)
  await show('error'); await dispatch()
  expect(latest.searchError).toBe(true)
})

it('aborts on connection change with the same query/profile and ignores the old owner', async () => {
  const old = deferred(), next = deferred()
  search.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
  await show('same'); await dispatch()
  const signal = search.mock.calls[0][2].signal as AbortSignal
  await act(() => $apiRequestScope.setKey('connectionId', 'other-gateway'))
  expect(signal.aborted).toBe(true)
  await dispatch()
  await act(async () => { old.resolve(hits('wrong-owner')) })
  expect(latest.serverMatches).toEqual([])
  expect(latest.searchPending).toBe(true)
  await act(async () => { next.resolve(hits('correct-owner')) })
  expect(latest.serverMatches.map(x => x.session_id)).toEqual(['correct-owner'])
})

it('distinguishes the typed backend timeout from other failures without preserving old hits', async () => {
  search.mockResolvedValueOnce(hits('previous')).mockRejectedValueOnce(new Error('Error invoking remote method \'hermes:api\': Error: 504: {"detail":{"code":"session_search_timeout","message":"synthetic"}}'))
  await show('previous'); await dispatch()
  await show('timeout'); await dispatch()
  expect(latest.searchError).toBe(true)
  expect(latest.searchTimedOut).toBe(true)
  expect(latest.serverMatches).toEqual([])
  search.mockRejectedValueOnce(new Error('503: {"detail":{"code":"unavailable"}}'))
  await show('error'); await dispatch()
  expect(latest.searchTimedOut).toBe(false)
  expect(latest.searchError).toBe(true)
})

it('removes an undispatched debounce when unmounted', async () => {
  await show('not-dispatched')
  await act(() => root.unmount())
  await dispatch()
  expect(search).not.toHaveBeenCalled()
  root = createRoot(host)
})
