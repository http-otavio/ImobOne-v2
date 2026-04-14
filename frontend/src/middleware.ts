/**
 * middleware.ts — Passive JWT verification
 *
 * This middleware ONLY verifies the session cookie and redirects to /auth/login
 * when absent or expired. It NEVER calls Supabase to refresh tokens.
 *
 * Rationale: Refresh Token Rotation means if two tabs both detect expiry
 * and concurrently call refreshSession(), the second call uses an already-rotated
 * refresh token → 401 → user logged out. Refreshes must happen from a single
 * coordinated client-side hook (useRefreshSession + BroadcastChannel).
 *
 * Token injection: the decrypted access_token is injected as x-access-token
 * header so Server Components can read it via headers() without decrypting again.
 */

import { NextRequest, NextResponse } from 'next/server'
import { decryptSession, isExpired, COOKIE_NAME } from '@/lib/session'

// ─── Public routes (no auth required) ────────────────────────────────────────

const PUBLIC_PREFIXES = [
  '/auth/login',
  '/auth/mfa',       // MFA enrollment — user has no cookie yet
  '/api/auth/',      // login, logout, refresh, mfa route handlers
  '/api/webhooks/',  // inbound webhook tunnel (Evolution API, portals)
  '/_next/',
  '/favicon.ico',
  '/health',
]

function isPublic(pathname: string): boolean {
  return PUBLIC_PREFIXES.some((p) => pathname.startsWith(p))
}

// ─── Middleware ───────────────────────────────────────────────────────────────

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl

  // Allow public paths without any auth check
  if (isPublic(pathname)) {
    return NextResponse.next()
  }

  // ── Read session cookie ────────────────────────────────────────────────────
  const raw = request.cookies.get(COOKIE_NAME)?.value
  if (!raw) {
    return redirectToLogin(request)
  }

  const session = await decryptSession(raw)
  if (!session) {
    return redirectToLogin(request)
  }

  // ── Expired → redirect (passive — client hook handles refresh) ────────────
  if (isExpired(session)) {
    return redirectToLogin(request)
  }

  // ── Inject access_token for Server Components via request header ──────────
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('x-access-token', session.access_token)
  requestHeaders.set('x-refresh-token', session.refresh_token)
  requestHeaders.set('x-expires-at', String(session.expires_at))

  return NextResponse.next({ request: { headers: requestHeaders } })
}

function redirectToLogin(request: NextRequest): NextResponse {
  const url = request.nextUrl.clone()
  url.pathname = '/auth/login'
  // Preserve intended destination for post-login redirect
  const callbackUrl = request.nextUrl.pathname
  if (callbackUrl !== '/' && callbackUrl !== '/auth/login') {
    url.searchParams.set('callbackUrl', callbackUrl)
  }
  return NextResponse.redirect(url)
}

export const config = {
  matcher: [
    /*
     * Match all request paths except static assets.
     * This is the recommended pattern from Next.js docs.
     */
    '/((?!_next/static|_next/image|favicon.ico).*)',
  ],
}
