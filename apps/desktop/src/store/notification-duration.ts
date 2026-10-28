import { atom } from 'nanostores'

import { readKey, writeKey } from '@/lib/storage'

const STORAGE_KEY = 'hermes.desktop.notifications.minimumDurationMs'

/** 0 preserves caller timing; 10000 opts into a minimum ordinary-toast reading window. */
export type NotificationMinimumDuration = 0 | 10_000

export function isNotificationMinimumDuration(value: unknown): value is NotificationMinimumDuration {
  return value === 0 || value === 10_000
}

function readMinimum(): NotificationMinimumDuration {
  return readKey(STORAGE_KEY) === '10000' ? 10_000 : 0
}

// Local to this client's storage origin/partition, not all installations on a machine.
// Do not publish to a backend/profile or the machine-shared preference allowlist.
export const $notificationMinimumDuration = atom<NotificationMinimumDuration>(readMinimum())

export function setNotificationMinimumDuration(value: NotificationMinimumDuration) {
  if (!isNotificationMinimumDuration(value)) {
    throw new Error('Invalid notification minimum duration')
  }

  $notificationMinimumDuration.set(value)
  writeKey(STORAGE_KEY, value === 0 ? null : String(value))
}

// A separate settings window must update the main window without a reload.
// Read current storage, not a queued event's older payload; do not echo peer writes.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY || event.key === null) {
      $notificationMinimumDuration.set(readMinimum())
    }
  })
}
