'use client'

import { useEffect, useRef } from 'react'
import { useRealtimeConversation } from '@/hooks/useRealtimeConversation'
import { formatDateTime } from '@/lib/dateUtils'
import type { Lead, Conversa } from '@/types'
import { Wifi, WifiOff } from 'lucide-react'

interface Props {
  lead: Lead
  initialMessages: Conversa[]
  accessToken: string
}

export default function ConversationView({ lead, initialMessages, accessToken }: Props) {
  const { messages, connected } = useRealtimeConversation(
    lead.lead_phone,
    initialMessages,
    accessToken,
  )
  const bottomRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to newest message
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl flex flex-col" style={{ height: 'calc(100vh - 180px)', minHeight: '500px' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-800">
        <div>
          <p className="text-sm font-medium text-slate-200">Conversa com Sofia</p>
          <p className="text-xs text-slate-500">{messages.length} mensagens</p>
        </div>
        <div className="flex items-center gap-1.5 text-xs">
          {connected ? (
            <>
              <Wifi className="w-3.5 h-3.5 text-green-400" />
              <span className="text-green-400">tempo real</span>
            </>
          ) : (
            <>
              <WifiOff className="w-3.5 h-3.5 text-slate-600" />
              <span className="text-slate-600">conectando…</span>
            </>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {messages.length === 0 ? (
          <div className="flex items-center justify-center h-full text-slate-600 text-sm">
            Nenhuma mensagem ainda.
          </div>
        ) : (
          messages.map((msg) => (
            <MessageBubble key={msg.id} msg={msg} />
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function MessageBubble({ msg }: { msg: Conversa }) {
  const isUser = msg.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-start' : 'justify-end'}`}>
      <div className={`
        max-w-[75%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed
        ${isUser
          ? 'bg-slate-800 text-slate-200 rounded-tl-sm'
          : 'bg-gradient-to-br from-amber-600/80 to-amber-700/80 text-white rounded-tr-sm'
        }
      `}>
        <p className="whitespace-pre-wrap break-words">{msg.content}</p>
        <p className={`text-xs mt-1.5 ${isUser ? 'text-slate-500' : 'text-amber-200/70'}`}>
          {formatDateTime(msg.created_at)}
        </p>
      </div>
    </div>
  )
}
