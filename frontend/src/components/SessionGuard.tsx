'use client'

/**
 * SessionGuard — mounts the useRefreshSession hook.
 * A thin client wrapper so the Server Component layout can mount the hook.
 */

import { useRefreshSession } from '@/hooks/useRefreshSession'

interface Props {
  expiresAt: number
}

export default function SessionGuard({ expiresAt }: Props) {
  useRefreshSession(expiresAt)
  return null
}
