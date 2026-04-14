'use client'

import { useState, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { Send, UserCheck, Bot, Loader2 } from 'lucide-react'
import type { Lead } from '@/types'

interface Props {
  lead: Lead
  token: string  // not used client-side; proxy reads from HttpOnly cookie
}

export default function TakeoverPanel({ lead }: Props) {
  const router = useRouter()
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const phoneEncoded = encodeURIComponent(lead.lead_phone)

  async function handleTakeover() {
    setLoading(true)
    const res = await fetch(`/api/proxy/admin/leads/${phoneEncoded}/takeover`, {
      method: 'POST',
    })
    setLoading(false)
    if (res.ok) router.refresh()
  }

  async function handleReturn() {
    setLoading(true)
    const res = await fetch(`/api/proxy/admin/leads/${phoneEncoded}/takeover/return`, {
      method: 'POST',
    })
    setLoading(false)
    if (res.ok) router.refresh()
  }

  async function handleSendMessage(e: React.FormEvent) {
    e.preventDefault()
    if (!message.trim()) return
    setSendError(null)
    setSending(true)

    const res = await fetch(`/api/proxy/admin/leads/${phoneEncoded}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message.trim() }),
    })

    setSending(false)

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Erro ao enviar.' }))
      setSendError(err.detail)
      return
    }

    setMessage('')
    router.refresh()
  }

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-medium text-slate-300">Controle de Atendimento</h3>

      {lead.human_takeover ? (
        <div className="space-y-4">
          {/* Active takeover indicator */}
          <div className="flex items-center gap-2 bg-blue-900/20 border border-blue-800/40 rounded-lg px-3 py-2.5">
            <UserCheck className="w-4 h-4 text-blue-400 flex-shrink-0" />
            <span className="text-blue-300 text-xs">
              Você está no controle desta conversa. Sofia está pausada.
            </span>
          </div>

          {/* Send message form */}
          <form onSubmit={handleSendMessage} className="space-y-2">
            <textarea
              ref={textareaRef}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSendMessage(e)
                }
              }}
              placeholder="Escreva uma mensagem como corretor…"
              rows={3}
              className="
                w-full px-3 py-2.5 rounded-lg text-sm
                bg-slate-800 border border-slate-700
                text-slate-100 placeholder:text-slate-500
                focus:outline-none focus:ring-2 focus:ring-blue-500/40 focus:border-blue-600
                resize-none transition-colors
              "
            />
            {sendError && (
              <p className="text-red-400 text-xs">{sendError}</p>
            )}
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={sending || !message.trim()}
                className="
                  flex-1 flex items-center justify-center gap-1.5
                  bg-blue-600 hover:bg-blue-500 disabled:opacity-50
                  text-white text-sm font-medium py-2 px-4 rounded-lg
                  transition-colors
                "
              >
                {sending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                Enviar
              </button>
              <button
                type="button"
                onClick={handleReturn}
                disabled={loading}
                className="
                  flex items-center justify-center gap-1.5
                  bg-slate-700 hover:bg-slate-600 disabled:opacity-50
                  text-slate-300 text-sm font-medium py-2 px-3 rounded-lg
                  transition-colors
                "
                title="Devolver para Sofia"
              >
                <Bot className="w-3.5 h-3.5" />
              </button>
            </div>
          </form>

          <button
            onClick={handleReturn}
            disabled={loading}
            className="
              w-full flex items-center justify-center gap-2
              border border-slate-700 hover:border-slate-600 hover:bg-slate-800
              text-slate-400 hover:text-slate-200 text-sm font-medium
              py-2 px-4 rounded-lg transition-colors
              disabled:opacity-50
            "
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Bot className="w-4 h-4" />}
            Devolver para Sofia
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-slate-500 text-xs">
            Sofia está respondendo automaticamente. Assuma o controle para responder manualmente.
          </p>
          <button
            onClick={handleTakeover}
            disabled={loading || lead.descartado}
            className="
              w-full flex items-center justify-center gap-2
              bg-blue-600/20 hover:bg-blue-600/30 border border-blue-700/50
              text-blue-300 hover:text-blue-200 text-sm font-medium
              py-2.5 px-4 rounded-lg transition-colors
              disabled:opacity-40 disabled:cursor-not-allowed
            "
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <UserCheck className="w-4 h-4" />}
            Assumir Atendimento
          </button>
        </div>
      )}
    </div>
  )
}
