import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { $apiRequestScope } from '@/api/client'
import { searchSessions, type SessionSearchResult } from '@/hermes'
import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'

const SEARCH_DEBOUNCE_MS = 200

export interface ServerSessionSearch {
  serverMatches: SessionSearchResult[]
  searchPending: boolean
  searchError: boolean
  searchTimedOut: boolean
}

/**
 * Debounced full-text search across *all* sessions (not just the loaded page)
 * so old sessions stay findable. Hits come from the profile the sidebar shows:
 * an unscoped request searches the primary backend's launch profile, whatever
 * profile is on screen. "All profiles" asks the primary as before.
 */
export function useServerSessionSearch(query: string, profileScope: string): ServerSessionSearch {
  const { connectionId } = useStore($apiRequestScope)
  const profile = profileScope === ALL_PROFILES ? null : normalizeProfileKey(profileScope)
  const [result, setResult] = useState({ query, profile, connectionId, serverMatches: [] as SessionSearchResult[], searchPending: !!query, searchError: false, searchTimedOut: false })

  useEffect(() => {
    const empty = { query, profile, connectionId, serverMatches: [] as SessionSearchResult[], searchPending: !!query, searchError: false, searchTimedOut: false }
    setResult(empty)
    if (!query) { return }
    const controller = new AbortController()

    const id = window.setTimeout(() => {
      void searchSessions(query, profile, { signal: controller.signal })
        .then(res => {
          if (!controller.signal.aborted) {
            setResult({ ...empty, serverMatches: res.results, searchPending: false })
          }
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) {
            const message = error instanceof Error ? error.message : ''
            let searchTimedOut = /Timed out connecting to Hermes backend/i.test(message)
            try {
              const body = JSON.parse(message.slice(message.indexOf('{')))
              searchTimedOut ||= body?.detail?.code === 'session_search_timeout'
            } catch { /* Non-JSON transport failures remain failures, never empty hits. */ }
            setResult({ ...empty, searchError: true, searchTimedOut, searchPending: false })
          }
        })
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      controller.abort()
      window.clearTimeout(id)
    }
  }, [query, profile, connectionId])

  // Never expose another query/owner's hits in the render before effect cleanup.
  if (!query || result.query !== query || result.profile !== profile || result.connectionId !== connectionId) {
    return { serverMatches: [], searchPending: !!query, searchError: false, searchTimedOut: false }
  }
  return result
}
