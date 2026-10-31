import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { hermesApi, setApiRequestConnection } from './client'

beforeEach(() => setApiRequestConnection('synthetic-gateway'))
afterEach(() => vi.unstubAllGlobals())
it('sends a cloneable request token and cancels the exact dispatched request', async () => {
  let sent: any, finish!: (x: unknown) => void
  const api = vi.fn((request: unknown) => { sent = structuredClone(request); return new Promise(resolve => { finish = resolve }) })
  const cancelApi = vi.fn()
  vi.stubGlobal('hermesDesktop', { api, cancelApi })
  const controller = new AbortController()
  const promise = hermesApi({ path: '/api/sessions/search?q=fixture', profile: 'research' }, { signal: controller.signal })
  const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  expect(sent.connectionId).toBe('synthetic-gateway')
  expect(sent.profile).toBe('research')
  expect(typeof sent.requestId).toBe('string')
  expect(sent.signal).toBeUndefined()
  controller.abort()
  expect(cancelApi).toHaveBeenCalledExactlyOnceWith(sent.requestId)
  finish({ results: [{ session_id: 'late' }] })
  await rejected
})
it('preserves legacy calls and does not dispatch an already-aborted request', async () => {
  const api = vi.fn(async () => ({ results: [] })), cancelApi = vi.fn()
  vi.stubGlobal('hermesDesktop', { api, cancelApi })
  expect(await hermesApi({ path: '/legacy' })).toEqual({ results: [] })
  expect(api.mock.calls[0]).toEqual([{ connectionId: 'synthetic-gateway', path: '/legacy' }])
  const controller = new AbortController(); controller.abort()
  await expect(hermesApi({ path: '/api/sessions/search?q=never' }, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(api).toHaveBeenCalledTimes(1)
})
it('does not swallow actual errors and removes the abort listener after completion', async () => {
  const error = new Error('503: {"detail":{"code":"session_search_budget_exceeded"}}')
  const api = vi.fn().mockRejectedValueOnce(error).mockResolvedValueOnce({ results: [] }), cancelApi = vi.fn()
  vi.stubGlobal('hermesDesktop', { api, cancelApi })
  const one = new AbortController()
  await expect(hermesApi({ path: '/error' }, { signal: one.signal })).rejects.toBe(error)
  one.abort(); expect(cancelApi).not.toHaveBeenCalled()
  const two = new AbortController()
  expect(await hermesApi({ path: '/empty' }, { signal: two.signal })).toEqual({ results: [] })
  two.abort(); expect(cancelApi).not.toHaveBeenCalled()
})
