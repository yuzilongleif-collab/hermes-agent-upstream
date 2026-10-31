// @vitest-environment node
import { expect, it, vi } from 'vitest'

import { unwrapExpectedNotFound } from './api-expected-404'
import { handleApiRequestFailure } from './api-request-failure'

function logger() {
  return { rememberLog: vi.fn(), flushDesktopLogBufferSync: vi.fn() }
}

it('keeps cancellation ahead of a simultaneous 404 and never logs it', () => {
  const controller = new AbortController()
  controller.abort()
  const log = logger()
  const result = handleApiRequestFailure({ path: '/api/sessions/search' }, new Error('404: missing'), controller.signal, log)

  expect(() => unwrapExpectedNotFound(result)).toThrow(expect.objectContaining({ name: 'AbortError' }))
  expect(log.rememberLog).not.toHaveBeenCalled()
  expect(log.flushDesktopLogBufferSync).not.toHaveBeenCalled()
})

it.each([new Error('404: {"detail":"missing"}'), '404: missing'])('keeps a normal lookup miss silent and preserves its message: %s', error => {
  const log = logger()
  const result = handleApiRequestFailure(undefined, error, undefined, log)

  expect(() => unwrapExpectedNotFound(result)).toThrow(error instanceof Error ? error.message : error)
  expect(log.rememberLog).not.toHaveBeenCalled()
  expect(log.flushDesktopLogBufferSync).not.toHaveBeenCalled()
})

it('logs and flushes the full stack before rethrowing the identical failure', () => {
  const log = logger()
  const error = new Error('503: unavailable')
  const controller = new AbortController()

  expect(() => handleApiRequestFailure({ method: 'post', path: '/api/sessions/search' }, error, controller.signal, log)).toThrow(error)
  expect(log.rememberLog).toHaveBeenCalledExactlyOnceWith(`[hermes:api POST /api/sessions/search] ${error.stack}`)
  expect(log.flushDesktopLogBufferSync).toHaveBeenCalledOnce()
  expect(log.rememberLog.mock.invocationCallOrder[0]).toBeLessThan(log.flushDesktopLogBufferSync.mock.invocationCallOrder[0]!)
})

it('keeps a non-aborted AbortError a real rejection, not a cancelled owner', () => {
  const log = logger()
  const error = new DOMException('another operation aborted', 'AbortError')

  expect(() => handleApiRequestFailure(null, error, undefined, log)).toThrow(error)
  expect(log.rememberLog).toHaveBeenCalledOnce()
  expect(log.rememberLog.mock.calls[0]![0]).toContain('[hermes:api GET (no path)]')
})

it('clamps log size without mutating the thrown failure or misclassifying embedded 404 digits', () => {
  const log = logger()
  const error = new Error('1404: ' + 'x'.repeat(9000))

  expect(() => handleApiRequestFailure({ path: 'p'.repeat(800) }, error, undefined, log)).toThrow(error)
  const message = log.rememberLog.mock.calls[0]![0]
  expect(message).toHaveLength(6000)
  expect(message).toContain('p'.repeat(500) + ']')
  expect(log.flushDesktopLogBufferSync).toHaveBeenCalledOnce()
})
