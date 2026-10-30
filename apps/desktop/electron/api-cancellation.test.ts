// @vitest-environment node
import { EventEmitter } from 'node:events'
import http from 'node:http'
import { afterEach, expect, it, vi } from 'vitest'
import type { IpcMain } from 'electron'

import { registerCancellableApi } from './api-cancellation'
import { bindRequestAbort } from './api-request-abort'
import { destroyKeepaliveAgents, withRetry } from './api-transport'
import { unwrapExpectedNotFound } from './api-expected-404'
import { CancellableApiRequests } from '../../shared/src/api-cancellation'

function gate() { let resolve!: () => void; const promise = new Promise<void>(res => { resolve = res }); return { promise, resolve } }
function ipcFixture(work: Parameters<typeof registerCancellableApi>[1]) {
  const callbacks = new Map<string, (...args: any[]) => any>()
  const ipc = { handle: (name: string, fn: (...args: any[]) => any) => callbacks.set(name, fn), on: (name: string, fn: (...args: any[]) => any) => callbacks.set(name, fn) }
  registerCancellableApi(ipc as unknown as Pick<IpcMain, 'handle' | 'on'>, work)
  return {
    invoke: (sender: EventEmitter, requestId: string) => callbacks.get('hermes:api')!({ sender }, { path: '/api/sessions/search?q=synthetic', requestId }),
    cancel: (sender: EventEmitter, requestId: string) => callbacks.get('hermes:api:cancel')!({ sender }, requestId)
  }
}
afterEach(() => destroyKeepaliveAgents())

it('cancels only the owning sender and drops completed token entries', async () => {
  const signals: AbortSignal[] = [], hold = gate()
  const ipc = ipcFixture(async (_event, _request, signal) => { signals.push(signal!); await hold.promise; return 7 })
  const a = new EventEmitter(), b = new EventEmitter()
  const pa = ipc.invoke(a, 'same'), pb = ipc.invoke(b, 'same')
  ipc.cancel(a, 'same')
  expect(signals.map(s => s.aborted)).toEqual([true, false])
  hold.resolve(); await Promise.all([pa, pb])
  ipc.cancel(b, 'same')
  expect(signals[1].aborted).toBe(false)
  expect(await ipc.invoke(b, 'same')).toBe(7)
})
it('cancellation while acquiring a backend prevents dispatch after acquisition', async () => {
  const hold = gate(), dispatch = vi.fn()
  const ipc = ipcFixture(async (_event, _request, signal) => { await hold.promise; signal!.throwIfAborted(); dispatch() })
  const sender = new EventEmitter(), promise = ipc.invoke(sender, 'waiting')
  const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  ipc.cancel(sender, 'waiting'); hold.resolve()
  await rejected
  expect(dispatch).not.toHaveBeenCalled()
})
it.each(['destroyed', 'render-process-gone', 'did-start-navigation'])('aborts on sender lifecycle %s', async event => {
  let signal!: AbortSignal; const hold = gate()
  const ipc = ipcFixture(async (_e, _r, s) => { signal = s!; await hold.promise })
  const sender = new EventEmitter(), promise = ipc.invoke(sender, event)
  sender.emit(event, {}, 'http://synthetic.invalid', false, true)
  expect(signal.aborted).toBe(true)
  hold.resolve(); await promise
})
it('destroys a dispatched real HTTP request, closes its response stream, and never retries abort', async () => {
  const started = gate(), closed = gate(), controller = new AbortController()
  let requests = 0
  const server = http.createServer((_req, res) => {
    requests++
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.write('{"results":[')
    res.on('close', closed.resolve); started.resolve()
  })
  await new Promise<void>(res => server.listen(0, '127.0.0.1', res))
  const address = server.address() as { port: number }
  try {
    const promise = withRetry(() => new Promise((resolve, reject) => {
      const request = http.get(`http://127.0.0.1:${address.port}/api/sessions/search?q=fixture`, response => {
        response.on('error', reject); response.on('end', resolve); response.resume()
      })
      request.on('error', reject)
      const unbind = bindRequestAbort(controller.signal, error => request.destroy(error))
      request.once('close', unbind)
    }), { signal: controller.signal })
    const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' })
    await started.promise
    controller.abort()
    await rejected; await closed.promise
    expect(requests).toBe(1)
  } finally {
    server.closeAllConnections()
    await new Promise<void>(res => server.close(() => res()))
  }
})
it('does not retry a transient failure after cancellation or dispatch an already-aborted operation', async () => {
  const controller = new AbortController(), work = vi.fn(async () => {
    controller.abort(); throw Object.assign(new Error('reset'), { code: 'ECONNRESET' })
  })
  await expect(withRetry(work, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(work).toHaveBeenCalledTimes(1)
  await expect(withRetry(work, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(work).toHaveBeenCalledTimes(1)
})
it('unwraps cancellation as rejection, not a successful empty result, and preserves regular payloads', () => {
  expect(() => unwrapExpectedNotFound({ __hermesApiAborted: true })).toThrow(expect.objectContaining({ name: 'AbortError' }))
  const empty = { results: [] }
  expect(unwrapExpectedNotFound(empty)).toBe(empty)
  expect(unwrapExpectedNotFound({ __hermesApiAborted: true, results: [] })).toEqual({ __hermesApiAborted: true, results: [] })
})

it('treats the typed HTTP budget response as terminal without retry', async () => {
  const work = vi.fn(async () => { throw Object.assign(new Error('504: {"detail":{"code":"session_search_timeout"}}'), { statusCode: 504 }) })
  await expect(withRetry(work)).rejects.toMatchObject({ statusCode: 504 })
  expect(work).toHaveBeenCalledTimes(1)
})

it('does not let a disposed generation delete a reused token from a later generation', async () => {
  const registry = new CancellableApiRequests(), one = gate(), two = gate()
  let signal!: AbortSignal
  const first = registry.run('same', async () => one.promise)
  registry.dispose()
  const second = registry.run('same', async s => { signal = s!; await two.promise })
  one.resolve(); await first
  registry.cancel('same')
  expect(signal.aborted).toBe(true)
  two.resolve(); await second
})

it('cancels before retry backoff resumes without resending', async () => {
  const controller = new AbortController(), pause = gate()
  const work = vi.fn(async () => { throw Object.assign(new Error('reset'), { code: 'ECONNRESET' }) })
  const promise = withRetry(work, { signal: controller.signal, delayFn: () => pause.promise })
  const rejection = expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  await Promise.resolve(); controller.abort(); pause.resolve(); await rejection
  expect(work).toHaveBeenCalledTimes(1)
})
