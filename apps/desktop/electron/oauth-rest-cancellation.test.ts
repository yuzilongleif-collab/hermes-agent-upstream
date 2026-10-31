// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { requestWithOauthFallback } from './oauth-rest-request'

it.each(['bearer', 'cookie', 'refresh-error'])('does not dispatch %s after this caller cancels a shared token acquisition', async mode => {
  const controller = new AbortController()
  let resolve!: (token: string | null) => void
  let reject!: (error: Error) => void
  const shared = new Promise<string | null>((res, rej) => { resolve = res; reject = rej })
  const bearer = vi.fn(), cookie = vi.fn()
  const pending = requestWithOauthFallback('http://synthetic.invalid', {
    signal: controller.signal, ensureNativeAccessToken: () => shared,
    requestWithBearer: bearer, requestWithCookie: cookie
  })
  const rejection = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  controller.abort()
  if (mode === 'refresh-error') { reject(new Error('synthetic refresh unavailable')) }
  else { resolve(mode === 'bearer' ? 'synthetic-token' : null) }
  await rejection
  expect(bearer).not.toHaveBeenCalled()
  expect(cookie).not.toHaveBeenCalled()
})
it('does not fall back or retry after a bearer budget terminal response', async () => {
  const error = Object.assign(new Error('504: {"detail":{"code":"session_search_timeout"}}'), { statusCode: 504 })
  const bearer = vi.fn(async () => { throw error }), cookie = vi.fn()
  await expect(requestWithOauthFallback('http://synthetic.invalid', {
    ensureNativeAccessToken: async () => 'synthetic-token', requestWithBearer: bearer, requestWithCookie: cookie
  })).rejects.toBe(error)
  expect(bearer).toHaveBeenCalledTimes(1)
  expect(cookie).not.toHaveBeenCalled()
})
