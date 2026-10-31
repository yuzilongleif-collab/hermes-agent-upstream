import { HERMES_API_EXPECTED_404 } from './api-expected-404'

interface ApiRequestLog {
  rememberLog: (message: string) => void
  flushDesktopLogBufferSync: () => void
}

// A 404 raised by `fetchJson` — the shape is `404: <body>` (see fetchJson).
// Session lookups are a probe ladder: "not on this profile" is a normal rung
// outcome, not a failure.
function isNotFoundApiError(error: unknown) {
  return /(?:^|\s)404\b/.test(String((error as any)?.message ?? error))
}

// Format an api-request failure for desktop.log. The renderer only ever sees
// the invoke rejection; the real stack lives here in main, so persist it
// before rethrowing. Clamp: paths and error detail must not bloat the log.
function formatApiRequestFailure(
  request: { method?: string; path?: string } | null | undefined,
  error: unknown
): string {
  const method = String(request?.method ?? 'GET').toUpperCase()
  const path = String(request?.path ?? '(no path)').slice(0, 500)
  const detail = error instanceof Error ? (error.stack ?? error.message) : String(error)

  return `[hermes:api ${method} ${path}] ${detail}`.slice(0, 6000)
}

export function handleApiRequestFailure(
  request: { method?: string; path?: string } | null | undefined,
  error: unknown,
  signal: AbortSignal | undefined,
  log: ApiRequestLog
) {
  // Electron logs "Error occurred in handler for 'hermes:api'" with a full
  // stack for EVERY rejected invoke, and there is no opt-out on the handler.
  // Session resolution is a deliberate probe ladder (`resolveStoredSession`:
  // cache → active backend → each other profile) and the renderer already
  // handles a miss by falling to the next rung — so an expected 404 is not an
  // error condition. Left rejecting, it printed a multi-line stack per probe
  // on every startup and session switch: pure noise that buries genuine
  // handler failures.
  //
  // So don't reject for that one case — RESOLVE with a sentinel and let
  // preload (our own code, the other side of the same seam) turn it back into
  // a rejection with the identical `404: <body>` message. The renderer
  // contract is unchanged; only Electron's logging is bypassed. Every other
  // failure still rejects and still logs in full.
  if (signal?.aborted) { return { __hermesApiAborted: true } }
  if (isNotFoundApiError(error)) {
    return { [HERMES_API_EXPECTED_404]: String((error as any)?.message ?? error) }
  }

  // Persist the failure (full stack) before the rejection crosses to the
  // renderer, where the invoke wrapper strips it to a one-line message.
  log.rememberLog(formatApiRequestFailure(request, error))
  log.flushDesktopLogBufferSync()

  throw error
}
