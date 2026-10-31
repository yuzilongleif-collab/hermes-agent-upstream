import { afterEach, expect, it, vi } from 'vitest'

import { hermesApi, setApiRequestConnection } from '@/api/client'
import { clearWebappSession, seedWebappSession } from '@/test/webapp-session'

import { installBrowserDesktopBridge } from './browser-desktop-bridge'

afterEach(() => {
  window.dispatchEvent(new Event('beforeunload'))
  Reflect.deleteProperty(window, 'hermesDesktop')
  clearWebappSession()
  setApiRequestConnection(null)
  document.documentElement.removeAttribute('data-hermes-desktop-host')
  vi.unstubAllGlobals()
})

it('wires renderer cancellation through the installed browser bridge without aborting its peer', async () => {
  seedWebappSession('synthetic-session-token')
  setApiRequestConnection('local')
  const calls: { signal: AbortSignal; finish: (response: Response) => void }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url: URL, init: RequestInit) =>
        new Promise<Response>((resolve, reject) => {
          const signal = init.signal!
          calls.push({ signal, finish: resolve })
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
        })
    )
  )
  expect(installBrowserDesktopBridge()).toBe(true)
  const controller = new AbortController()
  const cancelled = hermesApi(
    { path: '/api/sessions/search?q=cancelled', profile: 'research' },
    { signal: controller.signal }
  )
  const rejected = expect(cancelled).rejects.toMatchObject({ name: 'AbortError' })
  const peer = hermesApi(
    { path: '/api/sessions/search?q=peer', profile: 'research' },
    { signal: new AbortController().signal }
  )
  const settled = Promise.allSettled([cancelled, peer])
  try {
    expect(calls).toHaveLength(2)
    controller.abort()
    expect(calls[0].signal.aborted).toBe(true)
    expect(calls[1].signal.aborted).toBe(false)
    calls[1].finish(new Response(JSON.stringify({ results: [] }), { status: 200 }))
    await rejected
    await expect(peer).resolves.toEqual({ results: [], connection_id: 'local' })
  } finally {
    calls.forEach(call => call.finish(new Response(JSON.stringify({ results: [] }), { status: 200 })))
    await settled
  }
})
