/**
 * lib/session.ts — HttpOnly cookie session via jose AES-GCM encryption
 *
 * Cookie payload: ONLY access_token + refresh_token + expires_at.
 * No role, no profile state, no MFA state — keeps payload under 4KB
 * and avoids stale security decisions.
 *
 * Role and profile are fetched fresh on each SSR request from admin_api.
 */

import { EncryptJWT, jwtDecrypt, type JWTPayload } from 'jose'
import { cookies } from 'next/headers'
import { NextResponse } from 'next/server'
import type { SessionPayload } from '@/types'

// ─── Constants ────────────────────────────────────────────────────────────────

export const COOKIE_NAME = 'imob_session'
const COOKIE_TTL_SECONDS = 60 * 60 * 24 * 7  // 7 days

function getSecret(): Uint8Array {
  const raw = process.env.SESSION_SECRET
  if (!raw || raw.length < 32) {
    throw new Error('SESSION_SECRET must be set and at least 32 characters')
  }
  // Derive a 32-byte key from the string
  return new TextEncoder().encode(raw.slice(0, 32).padEnd(32, '0'))
}

// ─── Encrypt / Decrypt ────────────────────────────────────────────────────────

export async function encryptSession(payload: SessionPayload): Promise<string> {
  const secret = getSecret()
  return new EncryptJWT(payload as unknown as JWTPayload)
    .setProtectedHeader({ alg: 'dir', enc: 'A256GCM' })
    .setIssuedAt()
    .setExpirationTime('7d')
    .encrypt(secret)
}

export async function decryptSession(token: string): Promise<SessionPayload | null> {
  try {
    const secret = getSecret()
    const { payload } = await jwtDecrypt(token, secret)
    const p = payload as unknown as SessionPayload
    if (!p.access_token || !p.refresh_token || !p.expires_at) return null
    return p
  } catch {
    return null
  }
}

// ─── Cookie helpers ───────────────────────────────────────────────────────────

/** Read and decrypt the session from the current request's cookies. */
export async function getSession(): Promise<SessionPayload | null> {
  const cookieStore = await cookies()
  const raw = cookieStore.get(COOKIE_NAME)?.value
  if (!raw) return null
  return decryptSession(raw)
}

/** Returns true if the session token is expired (with 30-second buffer). */
export function isExpired(session: SessionPayload): boolean {
  return Date.now() / 1000 > session.expires_at - 30
}

/** Set the session cookie on a NextResponse. */
export function setSessionCookie(response: NextResponse, encrypted: string): void {
  response.cookies.set(COOKIE_NAME, encrypted, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: COOKIE_TTL_SECONDS,
    path: '/',
  })
}

/** Clear the session cookie (logout). */
export function clearSessionCookie(response: NextResponse): void {
  response.cookies.set(COOKIE_NAME, '', {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: 0,
    path: '/',
  })
}

/**
 * Decode the Supabase access_token JWT to extract user metadata.
 * Does NOT verify signature — we trust it because it came from our
 * encrypted HttpOnly session cookie. Signature verification happens
 * inside admin_api (FastAPI validates token with Supabase on each call).
 */
export function decodeSupabaseJwt(accessToken: string): {
  sub: string
  email: string
  app_metadata?: { role?: string }
  user_metadata?: Record<string, unknown>
} | null {
  try {
    const parts = accessToken.split('.')
    if (parts.length !== 3) return null
    // Base64url decode the payload
    const payload = Buffer.from(parts[1], 'base64url').toString('utf-8')
    return JSON.parse(payload)
  } catch {
    return null
  }
}
