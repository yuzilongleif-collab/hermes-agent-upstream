// @vitest-environment node
import { EventEmitter } from 'node:events'
import { expect, it, vi } from 'vitest'
import { registerCancellableApi } from './api-cancellation'

const { host } = vi.hoisted(() => ({ host: { bridge: null as any, handlers: new Map<string, (...args: any[]) => any>(), sender: null as any } }))
vi.mock('electron', () => ({
  contextBridge: { exposeInMainWorld: (_name: string, bridge: unknown) => { host.bridge = bridge } },
  ipcRenderer: {
    sendSync: () => null,
    invoke: (channel: string, request: unknown) => host.handlers.get(channel)!({ sender: host.sender }, structuredClone(request)),
    send: (channel: string, id: unknown) => host.handlers.get(channel)!({ sender: host.sender }, id)
  },
  webFrame: {}, webUtils: {}
}))

it('executes the real preload request/cancel path through the registered sender-scoped IPC handlers', async () => {
  host.sender = new EventEmitter()
  let captured!: AbortSignal
  let finish!: () => void
  const ipc = {
    handle: (channel: string, fn: (...args: any[]) => any) => host.handlers.set(channel, fn),
    on: (channel: string, fn: (...args: any[]) => any) => host.handlers.set(channel, fn)
  }
  registerCancellableApi(ipc as any, async (_event, request, signal) => {
    expect(request.path).toBe('/api/sessions/search?q=synthetic')
    captured = signal!
    await new Promise<void>(resolve => { finish = resolve })
    if (signal?.aborted) { return { __hermesApiAborted: true } }
    return { results: [] }
  })
  await import('./preload')
  const pending = host.bridge.api({ path: '/api/sessions/search?q=synthetic', requestId: 'immediate-cancel' })
  const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  host.bridge.cancelApi('immediate-cancel')
  expect(captured.aborted).toBe(true)
  finish(); await rejected
  const regular = host.bridge.api({ path: '/api/sessions/search?q=synthetic', requestId: 'completed' })
  finish(); expect(await regular).toEqual({ results: [] })
})
