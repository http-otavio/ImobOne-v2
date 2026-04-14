'use client'

import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Loader2, ShieldCheck, ShieldX } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'

type Phase = 'loading' | 'scan' | 'verifying' | 'error'

export default function MfaPage() {
  const router = useRouter()

  const [phase, setPhase]       = useState<Phase>('loading')
  const [factorId, setFactorId] = useState('')
  const [totpUri, setTotpUri]   = useState('')
  const [code, setCode]         = useState('')
  const [errorMsg, setErrorMsg] = useState('')

  const codeRef = useRef<HTMLInputElement>(null)

  // ── On mount: read temp token from sessionStorage and call setup ─────────
  useEffect(() => {
    const tempToken = sessionStorage.getItem('mfa_temp_token')

    if (!tempToken) {
      // No token → user navigated here directly without logging in first
      router.replace('/auth/login')
      return
    }

    ;(async () => {
      try {
        const res  = await fetch('/api/auth/mfa/setup', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ access_token: tempToken }),
        })
        const data = await res.json()

        if (!res.ok) {
          setErrorMsg(data.detail ?? 'Falha ao iniciar MFA.')
          setPhase('error')
          return
        }

        setFactorId(data.factor_id)
        setTotpUri(data.totp_uri)
        setPhase('scan')

        // Auto-focus the code input after QR renders
        setTimeout(() => codeRef.current?.focus(), 100)
      } catch {
        setErrorMsg('Erro de conexão ao iniciar MFA.')
        setPhase('error')
      }
    })()
  }, [router])

  // ── Submit TOTP code ──────────────────────────────────────────────────────
  async function handleVerify(e: React.FormEvent) {
    e.preventDefault()
    if (code.length !== 6 || verifying) return

    const tempToken = sessionStorage.getItem('mfa_temp_token')
    if (!tempToken) {
      router.replace('/auth/login')
      return
    }

    setPhase('verifying')
    setErrorMsg('')

    try {
      const res  = await fetch('/api/auth/mfa/challenge', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ access_token: tempToken, factor_id: factorId, code }),
      })
      const data = await res.json()

      if (!res.ok) {
        setErrorMsg(data.detail ?? 'Código inválido. Tente novamente.')
        setPhase('scan')
        setCode('')
        setTimeout(() => codeRef.current?.focus(), 50)
        return
      }

      // Success — clear temp tokens and redirect based on role
      sessionStorage.removeItem('mfa_temp_token')
      sessionStorage.removeItem('mfa_temp_refresh')

      try {
        localStorage.setItem('imob_expires_at', String(data.expires_at))
        localStorage.setItem('imob_user_role', data.user?.role ?? 'corretor')
      } catch {}

      const role = data.user?.role ?? 'corretor'
      router.push(role === 'dono' ? '/dashboard/dono' : '/dashboard/corretor')
      router.refresh()

    } catch {
      setErrorMsg('Erro de conexão. Tente novamente.')
      setPhase('scan')
    }
  }

  const verifying = phase === 'verifying'

  // ── Allow only digits in the code input ──────────────────────────────────
  function handleCodeChange(e: React.ChangeEvent<HTMLInputElement>) {
    const v = e.target.value.replace(/\D/g, '').slice(0, 6)
    setCode(v)
  }

  // ─── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-950 px-4">
      <div className="w-full max-w-md">

        {/* Header */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center gap-2 mb-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-yellow-500 to-amber-600 flex items-center justify-center">
              <span className="text-slate-900 font-bold text-sm">I</span>
            </div>
            <span className="text-2xl font-semibold tracking-tight text-slate-100">
              ImobOne
            </span>
          </div>
          <p className="text-slate-400 text-sm">Autenticação em dois fatores</p>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl">

          {/* Loading */}
          {phase === 'loading' && (
            <div className="flex flex-col items-center gap-4 py-8">
              <Loader2 className="w-8 h-8 animate-spin text-amber-500" />
              <p className="text-slate-400 text-sm">Preparando MFA…</p>
            </div>
          )}

          {/* Error */}
          {phase === 'error' && (
            <div className="flex flex-col items-center gap-4 py-6">
              <ShieldX className="w-10 h-10 text-red-400" />
              <p className="text-red-300 text-sm text-center">{errorMsg}</p>
              <button
                onClick={() => router.replace('/auth/login')}
                className="text-amber-400 text-sm underline hover:text-amber-300"
              >
                Voltar ao login
              </button>
            </div>
          )}

          {/* Scan + Verify */}
          {(phase === 'scan' || phase === 'verifying') && (
            <>
              <div className="flex items-center gap-3 mb-6">
                <ShieldCheck className="w-5 h-5 text-amber-500 flex-shrink-0" />
                <h1 className="text-lg font-semibold text-slate-100">
                  Configure o autenticador
                </h1>
              </div>

              <ol className="text-slate-400 text-sm space-y-1 mb-6 list-decimal list-inside">
                <li>Abra Google Authenticator, Authy ou similar</li>
                <li>Escaneie o QR code abaixo</li>
                <li>Digite o código de 6 dígitos gerado</li>
              </ol>

              {/* QR Code */}
              <div className="flex justify-center mb-6">
                <div className="bg-white p-3 rounded-xl inline-block">
                  {totpUri && (
                    <QRCodeSVG
                      value={totpUri}
                      size={180}
                      bgColor="#ffffff"
                      fgColor="#0f172a"
                      level="M"
                    />
                  )}
                </div>
              </div>

              {/* TOTP input */}
              <form onSubmit={handleVerify} className="space-y-4">
                <div>
                  <label
                    htmlFor="totp-code"
                    className="block text-sm font-medium text-slate-300 mb-1.5"
                  >
                    Código de verificação
                  </label>
                  <input
                    ref={codeRef}
                    id="totp-code"
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    placeholder="000000"
                    value={code}
                    onChange={handleCodeChange}
                    disabled={verifying}
                    className="
                      w-full px-4 py-3 rounded-lg text-center tracking-[0.4em]
                      text-xl font-mono
                      bg-slate-800 border border-slate-700
                      text-slate-100 placeholder:text-slate-600
                      focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500
                      disabled:opacity-50 transition-colors
                    "
                  />
                </div>

                {errorMsg && (
                  <div className="bg-red-900/30 border border-red-800/50 rounded-lg px-4 py-3 text-red-300 text-sm">
                    {errorMsg}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={code.length !== 6 || verifying}
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
                  {verifying ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Verificando…
                    </>
                  ) : 'Verificar e acessar'}
                </button>
              </form>

              <p className="text-center text-slate-600 text-xs mt-5">
                Este dispositivo ficará registrado no seu aplicativo autenticador.
              </p>
            </>
          )}
        </div>

        <p className="text-center text-slate-600 text-xs mt-6">
          Acesso restrito. Apenas usuários autorizados.
        </p>
      </div>
    </div>
  )
}
