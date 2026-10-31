/** Attach cancellation to the native HTTP request, including response-body reads. */
export function bindRequestAbort(signal: AbortSignal | undefined, abort: (error: DOMException) => void): () => void {
  if (!signal) { return () => {} }
  const cancel = () => abort(new DOMException('The request was aborted', 'AbortError'))
  signal.addEventListener('abort', cancel, { once: true })
  if (signal.aborted) { cancel() }
  return () => signal.removeEventListener('abort', cancel)
}
