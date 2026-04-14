'use client'

import { useState, useTransition } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { Loader2 } from 'lucide-react'

export default function LoginForm() {
  const router = useRouter()
  const params = useSearchParams()
  const callbackUrl = params.get('callbackUrl') ?? '/dashboard'

  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState<string | null>(null)
  const [isPending, startTransition] = useTransition()

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)

    startTransition(async () => {
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
        })

        const data = await res.json()

        // ── MFA required: save temp token and go to enrollment ──────────
        if (res.status === 403 && data.mfa_required) {
          try {
            sessionStorage.setItem('mfa_temp_token',   data.access_token  ?? '')
            sessionStorage.setItem('mfa_temp_refresh',  data.refresh_token ?? '')
          } catch {}
          router.push('/auth/mfa')
          return
        }

        if (!res.ok) {
          setError(data.detail ?? 'Erro ao fazer login.')
          return
        }

        // Store expires_at for refresh hook (not sensitive)
        try {
          localStorage.setItem('imob_expires_at', String(data.expires_at))
          localStorage.setItem('imob_user_role', data.user?.role ?? 'corretor')
        } catch {}

        // Redirect based on role
        const role = data.user?.role ?? 'corretor'
        if (callbackUrl !== '/dashboard') {
          router.push(callbackUrl)
        } else {
          router.push(role === 'dono' ? '/dashboard/dono' : '/dashboard/corretor')
        }
        router.refresh()

      } catch {
        setError('Falha de conexão. Tente novamente.')
      }
    })
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-950 px-4">
      <div className="w-full max-w-md">
        {/* Logo / branding */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center gap-2 mb-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-yellow-500 to-amber-600 flex items-center justify-center">
              <span className="text-slate-900 font-bold text-sm">I</span>
            </div>
            <span className="text-2xl font-semibold tracking-tight text-slate-100">
              ImobOne
            </span>
          </div>
          <p className="text-slate-400 text-sm">Painel Administrativo</p>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl">
          <h1 className="text-xl font-semibold text-slate-100 mb-6">Entrar</h1>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-slate-300 mb-1.5">
                E-mail
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="seu@email.com.br"
                className="
                  w-full px-4 py-2.5 rounded-lg
                  bg-slate-800 border border-slate-700
                  text-slate-100 placeholder:text-slate-500
                  focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500
                  transition-colors text-sm
                "
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-slate-300 mb-1.5">
                Senha
              </label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="
                  w-full px-4 py-2.5 rounded-lg
                  bg-slate-800 border border-slate-700
                  text-slate-100 placeholder:text-slate-500
                  focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500
                  transition-colors text-sm
                "
              />
            </div>

            {error && (
              <div className="bg-red-900/30 border border-red-800/50 rounded-lg px-4 py-3 text-red-300 text-sm">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={isPending}
              className="
                w-full py-2.5 px-4 rounded-lg
                bg-gradient-to-r from-amber-500 to-yellow-500
                hover:from-amber-400 hover:to-yellow-400
                text-slate-900 font-semibold text-sm
                focus:outline-none focus:ring-2 focus:ring-amber-500/50
                disabled:opacity-50 disabled:cursor-not-allowed
                transition-all flex items-center justify-center gap-2
              "
            >
              {isPending ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Entrando…
                </>
              ) : 'Entrar'}
            </button>
          </form>
        </div>

        <p className="text-center text-slate-600 text-xs mt-6">
          Acesso restrito. Apenas usuários autorizados.
        </p>
      </div>
    </div>
  )
}
