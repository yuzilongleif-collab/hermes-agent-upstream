import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isSharedPreferenceKey } from '@/lib/ui-preferences/keys'
import { desktopSettings } from '@/sdk/settings'

vi.mock('@/store/native-notifications', async () => {
  const { atom } = await import('nanostores')

  return {
    $nativeNotifyPrefs: atom({ enabled: false, kinds: {} }),
    NATIVE_NOTIFICATION_KINDS: [],
    sendTestNativeNotification: vi.fn(),
    setNativeNotifyEnabled: vi.fn(),
    setNativeNotifyKind: vi.fn()
  }
})
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/store/translucency', () => ({ TRANSLUCENCY_SUPPORTED: false }))
vi.mock('@/store/backdrop', async () => {
  const { atom } = await import('nanostores')
  const $backdrop = atom(false)

  return { $backdrop, setBackdrop: (value: boolean) => $backdrop.set(value) }
})
vi.mock('@/store/desktop-metrics', () => ({
  recordFriction: vi.fn(),
  noticeIdForToast: (id: string) => id
}))
import { NotificationsSettings } from '@/app/settings/notifications-settings'
import { NotificationStack } from '@/components/notifications'

import { $notificationMinimumDuration, setNotificationMinimumDuration } from './notification-duration'
import {
  $notifications,
  clearNotifications,
  dismissNotification,
  type NotificationInput,
  notify
} from './notifications'

const key = 'hermes.desktop.notifications.minimumDurationMs'
const exists = (id: string) => $notifications.get().some(item => item.id === id)

beforeEach(() => {
  vi.useFakeTimers()
  setNotificationMinimumDuration(0)

  for (const row of $notifications.get()) {
    dismissNotification(row.id)
  }
})
afterEach(() => {
  cleanup()

  for (const row of $notifications.get()) {
    dismissNotification(row.id)
  }

  setNotificationMinimumDuration(0)
  vi.useRealTimers()
})

describe('the native notify timer, not a transcribed resolver', () => {
  const kinds = ['info', 'success'] as const

  for (const kind of kinds) {
    it.each([undefined, 1500, 2000, 5000, 10000, 15000])(`${kind}: opt-in floors duration %s`, durationMs => {
      setNotificationMinimumDuration(10000)
      const id = notify({ message: kind, kind, durationMs })
      const ttl = Math.max(durationMs ?? 5000, 10000)
      vi.advanceTimersByTime(ttl - 1)
      expect(exists(id)).toBe(true)
      vi.advanceTimersByTime(1)
      expect(exists(id)).toBe(false)
    })
    it.each([undefined, 1500, 15000])(`${kind}: off preserves explicit/default duration %s`, durationMs => {
      const id = notify({ message: kind, kind, durationMs })
      vi.advanceTimersByTime((durationMs ?? 5000) - 1)
      expect(exists(id)).toBe(true)
      vi.advanceTimersByTime(1)
      expect(exists(id)).toBe(false)
    })
    it.each([0, -1, NaN])(`${kind}: nonpositive/NaN %s never becomes a positive timer`, durationMs => {
      setNotificationMinimumDuration(10000)
      const id = notify({ message: kind, kind, durationMs })
      vi.advanceTimersByTime(60000)
      expect(exists(id)).toBe(true)
    })
  }

  it.each([
    { kind: 'warning' },
    { kind: 'error' },
    { durationMs: 0 },
    { action: { label: 'Act', onClick() {} }, durationMs: 0 },
    { persistent: true, durationMs: 0 }
  ] satisfies Partial<NotificationInput>[])('does not add a deletion to sticky input %j', input => {
    setNotificationMinimumDuration(10000)
    const id = notify({ message: 'protected', ...input })
    vi.advanceTimersByTime(60000)
    expect(exists(id)).toBe(true)
  })
  it.each([
    { kind: 'warning', durationMs: 2000 },
    { kind: 'error', durationMs: 2000 },
    { action: { label: 'Act', onClick() {} } },
    { secondaryAction: { label: 'Later', onClick() {} } },
    { persistent: true }
  ] satisfies Partial<NotificationInput>[])('does not redefine protected caller contract %j', input => {
    setNotificationMinimumDuration(10000)
    const id = notify({ message: 'protected', ...input })
    vi.advanceTimersByTime((input.durationMs ?? 5000) - 1)
    expect(exists(id)).toBe(true)
    vi.advanceTimersByTime(1)
    expect(exists(id)).toBe(false)
  })
  it('replacement cancels old timer and 0 replacement never inherits it', () => {
    setNotificationMinimumDuration(10000)
    notify({ id: 'same', message: 'first' })
    vi.advanceTimersByTime(9000)
    notify({ id: 'same', message: 'second' })
    vi.advanceTimersByTime(1000)
    expect(
      $notifications
        .get()
        .filter(row => row.id === 'same')
        .map(row => row.message)
    ).toEqual(['second'])
    vi.advanceTimersByTime(8999)
    expect(exists('same')).toBe(true)
    notify({ id: 'same', message: 'progress', durationMs: 0 })
    vi.advanceTimersByTime(60000)
    expect(exists('same')).toBe(true)
    notify({ id: 'same', message: 'done', kind: 'success' })
    vi.advanceTimersByTime(10000)
    expect(exists('same')).toBe(false)
  })
  it('manual dismiss clears timer exactly once, even after replacement', () => {
    const callback = vi.fn()
    setNotificationMinimumDuration(10000)
    const id = notify({ message: 'manual', onDismiss: callback })
    dismissNotification(id)
    vi.advanceTimersByTime(60000)
    expect(callback).toHaveBeenCalledTimes(1)
    expect(exists(id)).toBe(false)
  })
  it('clear keeps persistent rows and cancels only removed timers', () => {
    setNotificationMinimumDuration(10000)
    const callback = vi.fn()
    notify({ message: 'ordinary', onDismiss: callback })
    notify({
      id: 'keep',
      message: 'standing',
      persistent: true,
      durationMs: 15000
    })
    clearNotifications()
    expect($notifications.get().map(row => row.id)).toEqual(['keep'])
    vi.advanceTimersByTime(15000)
    expect(exists('keep')).toBe(false)
    expect(callback).toHaveBeenCalledTimes(1)
  })
  it('preference changes apply only to new notifications, never restart old timers', () => {
    const old = notify({ message: 'old' })
    vi.advanceTimersByTime(4000)
    setNotificationMinimumDuration(10000)
    const fresh = notify({ message: 'fresh' })
    vi.advanceTimersByTime(1000)
    expect(exists(old)).toBe(false)
    expect(exists(fresh)).toBe(true)
    setNotificationMinimumDuration(0)
    vi.advanceTimersByTime(9000)
    expect(exists(fresh)).toBe(false)
  })
  it('does not invent progress classification: an existing 8s pending input is only lengthened', () => {
    setNotificationMinimumDuration(10000)

    const id = notify({
      id: 'compress-fixture',
      message: 'pending',
      durationMs: 8000
    })

    vi.advanceTimersByTime(8000)
    expect(exists(id)).toBe(true)
    vi.advanceTimersByTime(2000)
    expect(exists(id)).toBe(false)
  })
})

describe('the actual settings/UI owners', () => {
  it('client preference stays outside the machine-shared allowlist', () => {
    expect(isSharedPreferenceKey(key)).toBe(false)
    desktopSettings.set('notifications.minimumDurationMs', 10000)
    expect(localStorage.getItem(key)).toBe('10000')
    expect(isSharedPreferenceKey(key)).toBe(false)
  })
  it('typed binding validates, persists and unsubscribes without affecting OS preferences', () => {
    const listener = vi.fn()
    const stop = desktopSettings.subscribe('notifications.minimumDurationMs', listener)
    desktopSettings.set('notifications.minimumDurationMs', 10000)
    expect($notificationMinimumDuration.get()).toBe(10000)
    expect(localStorage.getItem(key)).toBe('10000')
    expect(listener).toHaveBeenLastCalledWith(10000)

    for (const bad of [5000, '10000', true, null, Infinity, NaN]) {
      expect(() => desktopSettings.set('notifications.minimumDurationMs', bad as never)).toThrow()
    }

    stop()
    desktopSettings.set('notifications.minimumDurationMs', 0)
    expect(localStorage.getItem(key)).toBeNull()
    expect(listener).toHaveBeenCalledTimes(2)
  })
  it('uses current storage for peer writes, invalid/reset values fall back to off, no echo', () => {
    localStorage.setItem(key, '10000')
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: '0' }))
    expect(desktopSettings.get('notifications.minimumDurationMs')).toBe(10000)
    localStorage.setItem(key, '9999')
    window.dispatchEvent(new StorageEvent('storage', { key }))
    expect($notificationMinimumDuration.get()).toBe(0)
    expect(localStorage.getItem(key)).toBe('9999')
    localStorage.setItem(key, '10000')
    window.dispatchEvent(new StorageEvent('storage', { key }))
    localStorage.clear()
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    expect($notificationMinimumDuration.get()).toBe(0)
  })
  it('native Settings control changes the same owner and survives remount', () => {
    render(
      <MemoryRouter>
        <NotificationsSettings subpage="alerts" />
      </MemoryRouter>
    )

    const toggle = screen.getByRole('switch', {
      name: 'Keep ordinary toasts for at least 10 seconds'
    })

    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    expect(desktopSettings.get('notifications.minimumDurationMs')).toBe(10000)
    cleanup()
    render(
      <MemoryRouter>
        <NotificationsSettings subpage="alerts" />
      </MemoryRouter>
    )
    expect(screen.getByRole('switch', { name: /Keep ordinary toasts/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText(/Extends explicit short timings from enqueue/)).toBeTruthy()
  })
  it('real stack exposes polite status and labelled close; unmount does not own/store-cancel timers', () => {
    setNotificationMinimumDuration(10000)
    notify({ message: 'Readable notice' })
    const view = render(<NotificationStack />)
    expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite')
    expect(screen.getByRole('button', { name: 'Dismiss notification' }).tagName).toBe('BUTTON')
    view.unmount()
    act(() => vi.advanceTimersByTime(9999))
    expect($notifications.get()).toHaveLength(1)
    act(() => vi.advanceTimersByTime(1))
    expect($notifications.get()).toHaveLength(0)
    render(<NotificationStack />)
    expect(screen.queryByRole('status')).toBeNull()
  })
})
