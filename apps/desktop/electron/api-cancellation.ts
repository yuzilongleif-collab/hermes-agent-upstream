import type { IpcMain, IpcMainInvokeEvent, WebContents } from 'electron'

import { CancellableApiRequests } from '../../shared/src/api-cancellation'

/** One registry per sender; renderer tokens never carry authority over a peer. */
export function registerCancellableApi(
  ipc: Pick<IpcMain, 'handle' | 'on'>,
  handle: (event: IpcMainInvokeEvent, request: any, signal?: AbortSignal) => Promise<unknown>
): void {
  const owners = new WeakMap<WebContents, CancellableApiRequests>()
  const forSender = (sender: WebContents): CancellableApiRequests => {
    let requests = owners.get(sender)
    if (!requests) {
      requests = new CancellableApiRequests()
      owners.set(sender, requests)
      const owned = requests
      sender.once('destroyed', () => owned.dispose())
      sender.on('render-process-gone', () => owned.dispose())
      sender.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
        if (isMainFrame && !isInPlace) { owned.dispose() }
      })
    }
    return requests
  }
  ipc.on('hermes:api:cancel', (event, id: unknown) => {
    if (typeof id === 'string') { owners.get(event.sender)?.cancel(id) }
  })
  ipc.handle('hermes:api', (event, request) => forSender(event.sender).run(request?.requestId, signal => handle(event, { ...request, signal }, signal)))
}
