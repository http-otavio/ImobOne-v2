'use client'

/**
 * hooks/useRealtimeConversation.ts — Supabase Realtime subscription for conversations
 *
 * Design: page loads with SSR data (authoritative snapshot). This hook adds
 * a Realtime subscription so new messages appear live without a page reload.
 * The subscription inherits RLS via the anon key + user's JWT passed as
 * the realtime access_token param.
 *
 * Separation of concerns:
 * - SSR fetch (server component) = authoritative truth on page load
 * - Realtime = live deltas for open tabs
 * - No cache to invalidate; no shared server state.
 */

import { useEffect, useState, useCallback } from 'react'
import { createClient } from '@supabase/supabase-js'
import type { Conversa } from '@/types'

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!

export function useRealtimeConversation(
  leadPhone: string,
  initialMessages: Conversa[],
  accessToken: string,
) {
  const [messages, setMessages] = useState<Conversa[]>(initialMessages)
  const [connected, setConnected] = useState(false)

  const appendMessage = useCallback((msg: Conversa) => {
    setMessages((prev) => {
      // Deduplicate by id
      if (prev.some((m) => m.id === msg.id)) return prev
      return [...prev, msg]
    })
  }, [])

  useEffect(() => {
    // Create a per-session Supabase client authenticated with the user's JWT
    // so Realtime channel inherits RLS
    const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      global: {
        headers: { Authorization: `Bearer ${accessToken}` },
      },
      realtime: {
        params: { apikey: SUPABASE_ANON_KEY },
      },
    })

    const channel = supabase
      .channel(`conversas:${leadPhone}`)
      .on(
        'postgres_changes',
        {
          event: 'INSERT',
          schema: 'public',
          table: 'conversas',
          filter: `lead_phone=eq.${leadPhone}`,
        },
        (payload) => {
          appendMessage(payload.new as Conversa)
        },
      )
      .subscribe((status) => {
        setConnected(status === 'SUBSCRIBED')
      })

    return () => {
      supabase.removeChannel(channel)
    }
  }, [leadPhone, accessToken, appendMessage])

  return { messages, connected }
}
