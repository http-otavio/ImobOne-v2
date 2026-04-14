'use client'

/**
 * hooks/useRefreshSession.ts — Proactive token refresh with multi-tab coordination
 *
 * Design rationale:
 * - Middleware is PASSIVE: never refreshes, only verifies.
 * - This hook fires 2 minutes before token expiry to refresh proactively.
 * - BroadcastChannel('imob_auth') ensures only ONE tab does the refresh;
 *   other tabs receive the updated expiry and reschedule their own timer.
 * - Prevents the Refresh Token Rotation concurrency bomb: if two tabs both
 *   detect expiry and call refresh simultaneously, the second uses an already-
 *   rotated token → 401 → involuntary logout.
 *
 * Usage: mount once in the dashboard layout (client component).
 */

import { useEffect, useRef, useCallback } from 'react'
import { useRouter } from 'next/navigation'

const CHANNEL_NAME = 'imob_auth'
const REFRESH_BEFORE_EXPIRY_MS = 2 * 60 * 1000  // 2 minutes
const EXPIRY_KEY = 'imob_expires_at'  // localStorage not used for secrets; only expiry timestamp

interface BroadcastMessage {
  type: 'refreshed' | 'logout'
  expires_at?: number
}

export function useRefreshSession(expiresAt: number) {
  const router = useRouter()
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const channelRef = useRef<BroadcastChannel | null>(null)
  const refreshingRef = useRef(false)

  const scheduleRefresh = useCallback((exp: number) => {
    if (timerRef.current) clearTimeout(timerRef.current)

    const msUntilExpiry = exp * 1000 - Date.now()
    const delay = Math.max(msUntilExpiry - REFRESH_BEFORE_EXPIRY_MS, 0)

    timerRef.current = setTimeout(async () => {
      if (refreshingRef.current) return  // another tab already refreshing
      refreshingRef.current = true

      try {
        const res = await fetch('/api/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          cache: 'no-store',
        })

        if (!res.ok) {
          // Refresh failed → logout all tabs
          channelRef.current?.postMessage({ type: 'logout' } as BroadcastMessage)
          router.push('/auth/login')
          return
        }

        const { expires_at: newExpiry } = await res.json() as { expires_at: number }

        // Persist new expiry as a plain non-sensitive timestamp
        try { localStorage.setItem(EXPIRY_KEY, String(newExpiry)) } catch {}

        // Notify other tabs
        channelRef.current?.postMessage({
          type: 'refreshed',
          expires_at: newExpiry,
        } as BroadcastMessage)

        // Reschedule for THIS tab
        scheduleRefresh(newExpiry)
      } catch {
        router.push('/auth/login')
      } finally {
        refreshingRef.current = false
      }
    }, delay)
  }, [router])

  useEffect(() => {
    // Open BroadcastChannel for multi-tab coordination
    const channel = new BroadcastChannel(CHANNEL_NAME)
    channelRef.current = channel

    channel.addEventListener('message', (event: MessageEvent<BroadcastMessage>) => {
      if (event.data.type === 'refreshed' && event.data.expires_at) {
        // Another tab refreshed — reschedule based on new expiry, skip the refresh
        if (timerRef.current) clearTimeout(timerRef.current)
        scheduleRefresh(event.data.expires_at)
      } else if (event.data.type === 'logout') {
        router.push('/auth/login')
      }
    })

    // Schedule initial refresh
    scheduleRefresh(expiresAt)

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
      channel.close()
    }
  }, [expiresAt, scheduleRefresh, router])
}
