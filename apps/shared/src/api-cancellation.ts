/** Transport-local in-flight requests, never a cache of search results. */
export class CancellableApiRequests {
  private readonly requests = new Map<string, AbortController>()

  async run<T>(id: string | undefined, work: (signal?: AbortSignal) => Promise<T>): Promise<T> {
    if (!id) { return work() }
    if (typeof id !== 'string' || id.length > 128) { throw new Error('Invalid API request id') }
    if (this.requests.has(id)) { throw new Error('Duplicate API request id') }
    const controller = new AbortController()
    this.requests.set(id, controller)
    try {
      return await work(controller.signal)
    } finally {
      if (this.requests.get(id) === controller) { this.requests.delete(id) }
    }
  }

  cancel(id: string): void { this.requests.get(id)?.abort() }

  dispose(): void {
    for (const controller of this.requests.values()) { controller.abort() }
    this.requests.clear()
  }
}
