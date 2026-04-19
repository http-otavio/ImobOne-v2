"""
Webhook server para demo do consultor de IA via WhatsApp (Evolution API).

Fluxo:
  Lead envia mensagem → Evolution API → POST /webhook → consultor LLM → Evolution API → resposta
  Se resposta contiver [FOTOS:ID] → envia fotos + link via Evolution API sendMedia
  Se resposta contiver [AUDIO] → gera áudio PTT via ElevenLabs e envia

Suporte a tipos de mídia recebidos:
  - Texto (conversation / extendedTextMessage)
  - Áudio PTT / audioMessage → transcrição via OpenAI Whisper
  - Imagem (imageMessage) → descrição via Claude Vision
  - Documento (documentMessage) → caption ou notificação de recebimento

Score de intenção:
  - Calculado a cada mensagem do lead com base em sinais linguísticos
  - Armazenado em Redis (hot) e Supabase leads.intention_score (cold)
  - Quando score >= CORRETOR_SCORE_THRESHOLD → notifica corretor via WhatsApp

Persistência:
  - Redis (hot): histórico de conversa por sender, dedup de fotos, score de intenção
  - Supabase (cold): tabela leads (com score) + tabela conversas por cliente
"""

import asyncio
import base64
import contextvars
import csv
import json
import logging
import os
import re
import ssl
import tempfile
import urllib.request
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
import anthropic
from fastapi import FastAPI, Request, Response

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("whatsapp_webhook")

# ─── Config ──────────────────────────────────────────────────────────────────
EVOLUTION_URL        = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY    = os.getenv("EVOLUTION_API_KEY", "79ffc1f3960f03a27a67e2b1e678d98b")
EVOLUTION_INSTANCE   = os.getenv("EVOLUTION_INSTANCE", "devlabz")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY         = os.getenv("SUPABASE_KEY", "")
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID  = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVENLABS_MODEL     = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
REDIS_URL            = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
CLIENT_ID            = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
MAX_HISTORY          = 20   # turnos máximos por conversa

# Notificação ao corretor
CORRETOR_NUMBER          = os.getenv("CORRETOR_NUMBER", "")          # ex: 5511999999999
CORRETOR_SCORE_THRESHOLD = int(os.getenv("CORRETOR_SCORE_THRESHOLD", "8"))
CORRETOR_COOLDOWN_HOURS  = int(os.getenv("CORRETOR_COOLDOWN_HOURS", "24"))

# ─── Multi-tenant — context por request ─────────────────────────────────────
# Cada request carrega o contexto do cliente (instância Evolution → config).
# _client_ctx é copiado automaticamente para tasks/executors por asyncio (Py 3.9+).
_client_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("client_ctx", default={})


def _ctx_client_id() -> str:
    return _client_ctx.get().get("client_id", CLIENT_ID)


def _ctx_instance() -> str:
    return _client_ctx.get().get("evolution_instance", EVOLUTION_INSTANCE)


def _ctx_system_prompt() -> str:
    return _client_ctx.get().get("system_prompt", SYSTEM_PROMPT)


def _ctx_corretor_number() -> str:
    return _client_ctx.get().get("corretor_number", CORRETOR_NUMBER)


def _ctx_corretor_threshold() -> int:
    return int(_client_ctx.get().get("corretor_score_threshold", CORRETOR_SCORE_THRESHOLD))


def _ctx_corretor_cooldown() -> int:
    return int(_client_ctx.get().get("corretor_cooldown_hours", CORRETOR_COOLDOWN_HOURS))


def _ctx_corretor_email() -> str:
    """Retorna e-mail do corretor principal configurado no onboarding."""
    return _client_ctx.get().get("corretor_email", os.getenv("CORRETOR_EMAIL", ""))


def _ctx_voice_id() -> str:
    return _client_ctx.get().get("elevenlabs_voice_id", ELEVENLABS_VOICE_ID)


# ─── Registry de clientes (instance_name → config) ───────────────────────────
_CLIENTS_REGISTRY: dict[str, dict] = {}
_client_context_cache: dict[str, dict] = {}  # system_prompt cacheado por client_id


def _load_clients_registry() -> dict[str, dict]:
    """
    Carrega clients_registry.json.
    Formato: { "nome_instancia_evolution": { "client_id": ..., "nome_consultor": ..., ... } }
    Se não encontrado, retorna mapa com a instância demo configurada via env vars.
    """
    candidates = [
        Path("/opt/ImobOne-v2/clients_registry.json"),
        Path(__file__).parent / "clients_registry.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                # Filtra metadados (chaves começando com _)
                data = {k: v for k, v in raw.items() if not k.startswith("_")}
                log.info("clients_registry.json carregado: %d cliente(s) de %s", len(data), p)
                return data
            except Exception as e:
                log.warning("Erro ao carregar clients_registry.json: %s", e)
    # Fallback: instância demo a partir de env vars
    log.info("clients_registry.json não encontrado — usando config demo via env vars.")
    return {
        EVOLUTION_INSTANCE: {
            "client_id":                  CLIENT_ID,
            "evolution_instance":         EVOLUTION_INSTANCE,
            "nome_consultor":             "Sofia",
            "nome_imobiliaria":           "Ávora Imóveis",
            "cidade_atuacao":             "São Paulo",
            "tipo_atuacao":               "vendas de alto padrão",
            "palavras_proibidas":         "baratinho, promoção, urgente",
            "corretor_number":            CORRETOR_NUMBER,
            "corretor_score_threshold":   CORRETOR_SCORE_THRESHOLD,
            "corretor_cooldown_hours":    CORRETOR_COOLDOWN_HOURS,
            "corretor_email":             os.getenv("CORRETOR_EMAIL", ""),
            "elevenlabs_voice_id":        ELEVENLABS_VOICE_ID,
        }
    }


def _build_client_context(instance: str) -> dict:
    """
    Resolve e cacheia o contexto completo de um cliente pela instância Evolution.
    Se a instância não estiver no registry, usa a instância demo como fallback.
    O system_prompt é construído uma vez por client_id e cacheado em _client_context_cache.
    """
    cfg = _CLIENTS_REGISTRY.get(instance)
    if not cfg:
        log.warning("Instância '%s' não encontrada no registry — fallback para demo.", instance)
        # Pega o primeiro cliente disponível (normalmente o demo)
        cfg = next(iter(_CLIENTS_REGISTRY.values()), {})

    # Garante que evolution_instance está no config
    cfg = {**cfg, "evolution_instance": instance}
    client_id = cfg.get("client_id", CLIENT_ID)

    # Retorna do cache se já construído
    if client_id in _client_context_cache:
        return {**cfg, "system_prompt": _client_context_cache[client_id]}

    # Constrói system prompt para esse cliente
    nome_consultor     = cfg.get("nome_consultor",    "Sofia")
    nome_imobiliaria   = cfg.get("nome_imobiliaria",  "Imobiliária")
    cidade             = cfg.get("cidade_atuacao",    "São Paulo")
    tipo_atuacao       = cfg.get("tipo_atuacao",      "imóveis")
    palavras_proibidas = cfg.get("palavras_proibidas", "")

    # Carrega portfólio deste cliente
    portfolio_ctx = ""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from setup_pipeline import _build_portfolio_context, carregar_onboarding
        onboarding_data = carregar_onboarding(client_id)
        portfolio_ctx   = _build_portfolio_context(onboarding_data)
        log.info("Portfólio carregado para cliente '%s' (%d chars)", client_id, len(portfolio_ctx))
    except Exception as e:
        log.warning("Portfólio não carregado para '%s': %s", client_id, e)

    # Carrega prompt base e substitui variáveis
    prompt_candidates = [
        Path("/app/prompts/base/consultant_base.md"),
        Path(__file__).parent / "_prompts_build" / "consultant_base.md",
        Path(__file__).parent / "prompts" / "base" / "consultant_base.md",
    ]
    system_prompt = None
    for c in prompt_candidates:
        if c.exists():
            raw = c.read_text(encoding="utf-8")
            system_prompt = (raw
                .replace("{{NOME_CONSULTOR}}",   nome_consultor)
                .replace("{{NOME_IMOBILIARIA}}", nome_imobiliaria)
                .replace("{{CIDADE_ATUACAO}}",   cidade)
                .replace("{{TIPO_ATUACAO}}",     tipo_atuacao)
                .replace("{{PALAVRAS_PROIBIDAS}}", palavras_proibidas)
                .replace("{{EXEMPLOS_SAUDACAO}}", "Boa tarde, seja bem-vindo.")
                .replace("{{REGRAS_ESPECIFICAS}}", "")
                .replace("{{PORTFOLIO_CONTEXTO}}", portfolio_ctx)
            )
            break

    if not system_prompt:
        system_prompt = (
            f"Você é {nome_consultor}, consultora de imóveis de alto padrão da "
            f"{nome_imobiliaria} em {cidade}. Responda com sofisticação e precisão."
        )

    _client_context_cache[client_id] = system_prompt
    log.info("Contexto construído para cliente '%s' (instância: %s)", client_id, instance)
    return {**cfg, "system_prompt": system_prompt}


# ─── Score de intenção — sinais e pontuação ──────────────────────────────────
# Cada tupla: (regex_pattern, pontos, label_para_breakdown)
_SCORE_SIGNALS: list[tuple[str, int, str]] = [
    # Alto sinal — intenção de visita / agendamento
    (r'visita|agendar|quero\s+conhecer|quando\s+posso\s+ver|quero\s+ver\s+o\s+im[oó]vel|marcar\s+uma\s+visita', 4, "horario_visita"),
    # Mencionou dados pessoais (nome, e-mail)
    (r'meu\s+nome\s+[eé]|me\s+chamo|sou\s+[A-ZÁÉÍÓÚ][a-záéíóú]+|meu\s+e[\-]?mail|meu\s+contato', 3, "dados_pessoais"),
    # Pergunta técnica específica sobre imóvel
    (r'quantos?\s+quartos?|tem\s+vaga|qual\s+o\s+andar|quantos?\s+banheiros?|[aá]rea\s+([\wé]+\s+)?m[²2]|suite?|lazer|piscina|academia|varanda', 3, "pergunta_especifica"),
    # Citou imóvel ou bairro específico do portfólio
    (r'AV\d{3}|Jardins|Itaim|Vila\s+Nova\s+Concei[cç][aã]o|Moema|Pinheiros|Higien[oó]polis|Perdizes', 3, "interesse_imovel"),
    # Solicitou fotos / materiais
    (r'foto|imagem|v[ií]deo|planta\s+baixa|tour\s+virtual|manda\s+mais', 2, "foto_solicitada"),
    # Pergunta de financiamento / crédito
    (r'financiam\w+|entrada|parcela|cr[eé]dito\s+imobili[aá]rio|fgts|banco|juros', 2, "financiamento"),
    # Pergunta direta de valor
    (r'\bvalor\b|\bpre[cç]o\b|\bcusto\b|quanto\s+custa|qual\s+o\s+pre[cç]o|R\$\s*\d', 2, "pergunta_valor"),
]

# ─── Estado em memória (fallback se Redis indisponível) ──────────────────────
_memory_history: dict[str, list] = defaultdict(list)

# ─── Portfolio em memória ─────────────────────────────────────────────────────
_portfolio_cache: dict[str, dict] = {}

# ─── Locks por sender — evita race condition quando mensagens chegam rápido ──
_sender_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _load_portfolio_dict() -> dict[str, dict]:
    """Carrega portfólio CSV como dict indexado por id do imóvel."""
    global _portfolio_cache
    if _portfolio_cache:
        return _portfolio_cache

    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from setup_pipeline import carregar_onboarding
        onboarding = carregar_onboarding(_ctx_client_id())
        portfolio_path = (
            onboarding.get("portfolio_path", "")
            or onboarding.get("portfolio", {}).get("portfolio_path", "")
        )
        candidates = [
            Path(portfolio_path),
            Path("/app") / str(portfolio_path).lstrip("/"),
            Path(__file__).parent / str(portfolio_path).lstrip("/"),
        ]
        for candidate in candidates:
            if candidate.exists():
                with open(candidate, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        imovel_id = row.get("id", "").strip()
                        if imovel_id:
                            _portfolio_cache[imovel_id] = row
                log.info("Portfolio dict carregado: %d imóveis", len(_portfolio_cache))
                return _portfolio_cache
    except Exception as e:
        log.warning("Não foi possível carregar portfolio dict: %s", e)
    return {}


def _load_onboarding_config() -> dict:
    """Carrega configuração do cliente (onboarding.json)."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from setup_pipeline import carregar_onboarding
        return carregar_onboarding(_ctx_client_id())
    except Exception as e:
        log.warning("Não foi possível carregar onboarding: %s", e)
        return {}


# ─── Carrega system prompt do consultor ──────────────────────────────────────
def _load_portfolio_context() -> str:
    """Carrega portfólio real do cliente demo."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from setup_pipeline import _build_portfolio_context, carregar_onboarding
        onboarding = carregar_onboarding(_ctx_client_id())
        ctx = _build_portfolio_context(onboarding)
        log.info("Portfólio carregado via setup_pipeline.")
        return ctx
    except Exception as e:
        log.warning("Não foi possível carregar portfólio: %s", e)
        return ""


def _load_system_prompt() -> str:
    candidates = [
        Path("/app/prompts/base/consultant_base.md"),
        Path(__file__).parent / "_prompts_build" / "consultant_base.md",
        Path(__file__).parent / "prompts" / "base" / "consultant_base.md",
    ]
    for c in candidates:
        if c.exists():
            log.info("System prompt carregado de %s", c)
            raw = c.read_text(encoding="utf-8")
            return (raw
                .replace("{{NOME_CONSULTOR}}", "Sofia")
                .replace("{{NOME_IMOBILIARIA}}", "Ávora Imóveis")
                .replace("{{CIDADE_ATUACAO}}", "São Paulo")
                .replace("{{TIPO_ATUACAO}}", "vendas de alto padrão")
                .replace("{{PALAVRAS_PROIBIDAS}}", "baratinho, promoção, urgente")
                .replace("{{EXEMPLOS_SAUDACAO}}", "Boa tarde, seja bem-vindo.")
                .replace("{{REGRAS_ESPECIFICAS}}", "")
                .replace("{{PORTFOLIO_CONTEXTO}}", _load_portfolio_context())
            )
    log.warning("consultant_base.md não encontrado — usando prompt mínimo.")
    return "Você é Sofia, consultora de imóveis de alto padrão da Ávora Imóveis em São Paulo. Responda com sofisticação e precisão."


SYSTEM_PROMPT = _load_system_prompt()
ONBOARDING    = _load_onboarding_config()
_load_portfolio_dict()        # pré-carrega portfólio demo
_CLIENTS_REGISTRY.update(_load_clients_registry())  # carrega registry multi-tenant


# ─── Histórico de conversa ────────────────────────────────────────────────────
async def get_history(redis_client, sender: str) -> list[dict]:
    if redis_client:
        try:
            raw = await redis_client.get(f"whatsapp:history:{sender}")
            return json.loads(raw) if raw else []
        except Exception:
            pass
    return _memory_history[sender].copy()


async def save_history(redis_client, sender: str, history: list[dict]):
    history = history[-MAX_HISTORY:]
    if redis_client:
        try:
            await redis_client.set(
                f"whatsapp:history:{sender}",
                json.dumps(history, ensure_ascii=False),
                ex=86400,
            )
            return
        except Exception:
            pass
    _memory_history[sender] = history


# ─── Data atual em português ─────────────────────────────────────────────────
def _data_hoje_pt() -> str:
    from datetime import date
    hoje = date.today()
    DIAS   = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
              "sexta-feira", "sábado", "domingo"]
    MESES  = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
              "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    return f"{DIAS[hoje.weekday()]}, {hoje.day} de {MESES[hoje.month - 1]} de {hoje.year}"


# ─── Supabase client (lazy) ──────────────────────────────────────────────────
_supabase_client = None

def _get_supabase():
    global _supabase_client
    if _supabase_client:
        return _supabase_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase conectado em %s", SUPABASE_URL)
        return _supabase_client
    except Exception as e:
        log.error("Falha ao conectar Supabase: %s", e)
        return None



# ─── Human takeover — Redis helpers ──────────────────────────────────────────

def _human_mode_key(sender: str) -> str:
    """Chave Redis para estado de human mode deste lead."""
    return f"whatsapp:human_mode:{_ctx_client_id()}:{sender}"


async def _is_human_mode(sender: str) -> bool:
    """Verifica se um lead está em modo humano (operador assumiu)."""
    if redis_client:
        try:
            return bool(await redis_client.exists(_human_mode_key(sender)))
        except Exception:
            pass
    return False


async def _set_human_mode(sender: str, active: bool, operator: str = "", note: str = ""):
    """
    Ativa ou desativa o human mode para um lead.
    - active=True: Sofia fica em silêncio, TTL de HUMAN_MODE_TTL_HOURS horas
    - active=False: Sofia retoma, chave Redis removida
    """
    key = _human_mode_key(sender)
    if redis_client:
        try:
            if active:
                ttl_seconds = HUMAN_MODE_TTL_HOURS * 3600
                await redis_client.setex(key, ttl_seconds, operator or "operador")
            else:
                await redis_client.delete(key)
        except Exception as e:
            log.warning("Falha ao setar human_mode Redis para %s: %s", sender, e)

    # Persiste no Supabase (visibilidade no dashboard)
    sb = _get_supabase()
    if sb:
        try:
            from datetime import datetime, timezone
            loop = asyncio.get_event_loop()
            data = {
                "client_id":    _ctx_client_id(),
                "lead_phone":   sender,
                "human_mode":   active,
            }
            if active:
                data["human_mode_at"]   = datetime.now(timezone.utc).isoformat()
                data["human_mode_by"]   = operator or "operador"
                data["human_mode_note"] = note or None
            else:
                data["human_mode_at"]   = None
                data["human_mode_by"]   = None
                data["human_mode_note"] = None

            await loop.run_in_executor(None, lambda: (
                sb.table("leads")
                  .upsert(data, on_conflict="client_id,lead_phone")
                  .execute()
            ))

            # Registra na auditoria
            action = "take" if active else "release"
            triggered = "api"
            await loop.run_in_executor(None, lambda: (
                sb.table("human_takeover_log")
                  .insert({
                      "client_id":    _ctx_client_id(),
                      "lead_phone":   sender,
                      "action":       action,
                      "triggered_by": triggered,
                      "operator":     operator or "operador",
                      "note":         note or None,
                  })
                  .execute()
            ))

            action_label = "assumida" if active else "devolvida"
            log.info("Conversa %s %s pelo operador %s", sender, action_label, operator or "?")
        except Exception as e:
            log.warning("Falha ao persistir human_mode Supabase para %s: %s", sender, e)


async def _set_human_mode_triggered_by(sender: str, active: bool, triggered_by: str,
                                        operator: str = "", note: str = ""):
    """Versão com triggered_by explícito para log de auditoria."""
    await _set_human_mode(sender, active, operator, note)
    # Atualiza o triggered_by no log de auditoria (último registro)
    sb = _get_supabase()
    if sb and triggered_by != "api":
        try:
            loop = asyncio.get_event_loop()
            # Busca o registro mais recente e atualiza
            await loop.run_in_executor(None, lambda: (
                sb.table("human_takeover_log")
                  .update({"triggered_by": triggered_by})
                  .eq("client_id", _ctx_client_id())
                  .eq("lead_phone", sender)
                  .order("created_at", desc=True)
                  .limit(1)
                  .execute()
            ))
        except Exception:
            pass


async def _supabase_upsert_lead(sender: str, name: str | None = None):
    """Cria ou atualiza registro do lead no Supabase."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        loop = asyncio.get_event_loop()
        data = {
            "client_id":        _ctx_client_id(),
            "lead_phone":       sender,
            "ultima_interacao": "now()",
            "origem":           "whatsapp",
        }
        if name:
            data["lead_name"] = name

        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert(data, on_conflict="client_id,lead_phone")
              .execute()
        ))
        log.info("Lead upserted no Supabase: %s", sender)
    except Exception as e:
        log.warning("Falha ao upsert lead Supabase: %s", e)


async def _supabase_append_conversa(sender: str, role: str, content: str, media_type: str = "text"):
    """Persiste uma mensagem da conversa no Supabase."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: (
            sb.table("conversas")
              .insert({
                  "client_id":  _ctx_client_id(),
                  "lead_phone": sender,
                  "role":       role,
                  "content":    content[:4000],  # trunca mensagens muito longas
                  "media_type": media_type,
              })
              .execute()
        ))
    except Exception as e:
        log.warning("Falha ao append conversa Supabase: %s", e)


# Padrões que indicam Sofia confirmando uma visita
_VISIT_CONFIRMATION_PATTERNS = [
    r'confirmo\s+(?:a\s+visita|para)?\s+(?:a\s+)?(?:segunda|terça|quarta|quinta|sexta|sábado|domingo)',
    r'visita\s+(?:está\s+)?confirmada',
    r'agendei\s+(?:a\s+)?visita',
    r'(?:às|as)\s+\d{1,2}h(?:\d{2})?\s+(?:está\s+)?confirmad',
    r'te\s+espero\s+(?:na|no|em)',
    r'até\s+(?:segunda|terça|quarta|quinta|sexta|sábado|domingo)',
]
_VISIT_CONFIRMATION_RE = re.compile(
    '|'.join(_VISIT_CONFIRMATION_PATTERNS), re.IGNORECASE
)


# Sinais de descarte — lead dizendo que não vai comprar / não é o momento
_DISCARD_SIGNALS: list[tuple[str, str]] = [
    # (regex, motivo)
    (r'\bnão\s+é\s+o\s+momento\b|\bainda\s+não\s+é\s+hora\b|\bnão\s+estou\s+pronto\b', 'nao_e_momento'),
    (r'\bjá\s+comprei\b|\bjá\s+fechei\b|\bjá\s+assinei\b|\bencontrei\s+(?:um|uma)\s+(?:apê|apto|apartamento|casa|imóvel)\b', 'ja_comprou'),
    (r'\bnão\s+tenho\s+(?:budget|dinheiro|condição|verba)\b|\bfora\s+do\s+(?:meu\s+)?budget\b|\bnão\s+cabe\s+no\b', 'sem_budget'),
    (r'\bdesisti\b|\bnão\s+vou\s+(?:mais\s+)?comprar\b|\bnão\s+quero\s+mais\b|\bcancelar?\b', 'desistencia'),
    (r'\bvou\s+esperar\b|\bpor\s+enquanto\s+não\b|\bnão\s+por\s+agora\b|\bainda\s+não\b', 'nao_e_momento'),
]
_DISCARD_RE = [(re.compile(pat, re.IGNORECASE), motivo) for pat, motivo in _DISCARD_SIGNALS]


def _detect_discard_signal(user_message: str) -> str | None:
    """
    Detecta se a mensagem do lead contém sinal de descarte.
    Retorna o motivo ou None.
    """
    for pattern, motivo in _DISCARD_RE:
        if pattern.search(user_message):
            return motivo
    return None


async def _supabase_mark_descartado(sender: str, motivo: str):
    """Marca lead como descartado no Supabase para iniciar nutrição de longo prazo."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        from datetime import datetime, timezone
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert({
                  "client_id":       _ctx_client_id(),
                  "lead_phone":      sender,
                  "descartado":      True,
                  "descartado_em":   datetime.now(timezone.utc).isoformat(),
                  "motivo_descarte": motivo,
              }, on_conflict="client_id,lead_phone")
              .execute()
        ))
        log.info("Lead %s marcado como descartado (%s)", sender, motivo)
    except Exception as e:
        log.warning("Falha ao marcar lead como descartado: %s", e)


def _detect_visit_confirmation(reply: str) -> bool:
    """Retorna True se a resposta da Sofia indica que uma visita foi confirmada."""
    return bool(_VISIT_CONFIRMATION_RE.search(reply))


# Meses em português para parsing de data
_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Padrão obrigatório do consultant_base.md para confirmação de visita:
# "Visita confirmada para [dia-semana], [DD] de [mês], às [H]h"
_VISIT_DATETIME_RE = re.compile(
    r'(?:segunda|terça|quarta|quinta|sexta|sábado|domingo)'
    r'(?:-feira)?[,\s]+'
    r'(\d{1,2})\s+de\s+(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)'
    r'[^à]*às\s*(\d{1,2})h(?:(\d{2}))?',
    re.IGNORECASE
)


def _parse_visit_datetime_from_reply(reply: str) -> "datetime | None":
    """
    Extrai a data e hora da visita do texto de confirmação da Sofia.
    Formato esperado: "...terça-feira, 31 de março, às 10h..."
    Retorna datetime em UTC (assume fuso Brasília UTC-3).
    Retorna None se não encontrar data válida.
    """
    from datetime import datetime, timezone, timedelta
    import re as _re

    m = _VISIT_DATETIME_RE.search(reply)
    if not m:
        return None

    day_str, month_str, hour_str, minute_str = m.groups()
    try:
        day   = int(day_str)
        month = _MESES_PT.get(month_str.lower())
        hour  = int(hour_str)
        minute = int(minute_str) if minute_str else 0
        if not month:
            return None
    except (ValueError, TypeError):
        return None

    # Determina o ano: usa o ano atual, mas se a data já passou usa o próximo ano
    now = datetime.now(timezone.utc)
    year = now.year
    try:
        # Brasília = UTC-3
        dt_brasilia = datetime(year, month, day, hour, minute)
        dt_utc = dt_brasilia.replace(tzinfo=timezone(timedelta(hours=-3))).astimezone(timezone.utc)
        # Se a data já passou (mais de 1 dia), assume ano seguinte
        if dt_utc < now - timedelta(days=1):
            dt_brasilia = datetime(year + 1, month, day, hour, minute)
            dt_utc = dt_brasilia.replace(tzinfo=timezone(timedelta(hours=-3))).astimezone(timezone.utc)
        return dt_utc
    except ValueError:
        return None


async def _supabase_confirm_visit(sender: str, reply: str = ""):
    """
    Marca visita_agendada=true e visita_confirmada_at=now() no lead.
    Também parseia a data/hora da visita da resposta da Sofia e salva em visit_scheduled_at.
    """
    sb = _get_supabase()
    if not sb:
        return
    try:
        from datetime import datetime, timezone
        loop = asyncio.get_event_loop()

        data = {
            "client_id":             _ctx_client_id(),
            "lead_phone":            sender,
            "visita_agendada":       True,
            "visita_confirmada_at":  datetime.now(timezone.utc).isoformat(),
        }

        # Tenta parsear a data/hora da visita da resposta da Sofia
        if reply:
            visit_dt = _parse_visit_datetime_from_reply(reply)
            if visit_dt:
                data["visit_scheduled_at"] = visit_dt.isoformat()
                log.info("Data da visita parseada para %s: %s", sender, visit_dt.isoformat())
            else:
                log.debug("Não foi possível parsear data da visita da resposta para %s", sender)

        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert(data, on_conflict="client_id,lead_phone")
              .execute()
        ))
        log.info("Visita confirmada registrada para %s%s",
                 sender, " com data agendada" if "visit_scheduled_at" in data else "")
    except Exception as e:
        log.warning("Falha ao registrar visita confirmada: %s", e)


# ─── Extração de perfil estruturado do lead ──────────────────────────────────
_PROFILE_EXTRACTION_TURNS = 5   # extrai após essa quantidade de turnos do lead

async def _extract_and_save_lead_profile(sender: str, history: list[dict]):
    """
    Após N turnos de conversa, usa Claude Haiku para extrair um perfil
    estruturado do lead e salva na tabela lead_profiles.
    Idempotente: só re-extrai se a conversa cresceu ≥ 3 turnos desde a última extração.
    """
    user_turns = [m for m in history if m.get("role") == "user"]
    total_turns = len(user_turns)

    if total_turns < _PROFILE_EXTRACTION_TURNS:
        return  # conversa ainda curta demais

    # Verifica se já extraímos recentemente (cache Redis)
    cache_key = f"whatsapp:profile_extracted:{sender}"
    if redis_client:
        try:
            cached_turns = await redis_client.get(cache_key)
            if cached_turns and (total_turns - int(cached_turns)) < 3:
                log.debug("Perfil de %s extraído há menos de 3 turnos — skip", sender)
                return
        except Exception:
            pass

    # Formata histórico para o Haiku
    hist_text = "\n".join(
        f"{'Lead' if m['role'] == 'user' else 'Consultora'}: {m.get('content', '')[:300]}"
        for m in history[-30:]
    )

    prompt = f"""Analise essa conversa de atendimento imobiliário de alto padrão e extraia o perfil estruturado do lead.

CONVERSA:
{hist_text}

Responda APENAS com um JSON válido no formato abaixo (sem markdown, sem texto extra):
{{
  "budget_min": null,
  "budget_max": null,
  "budget_label": "",
  "financing_interest": null,
  "payment_preference": "",
  "property_type": "",
  "bedrooms_desired": null,
  "area_min_m2": null,
  "area_max_m2": null,
  "neighborhoods": [],
  "city": "",
  "family_profile": "",
  "children_ages": "",
  "has_pets": null,
  "purchase_purpose": "",
  "timeline_months": null,
  "main_motivation": "",
  "key_objections": [],
  "competing_properties": [],
  "decision_blockers": [],
  "confidence_score": 0.0
}}

Regras:
- Use null para campos sem informação na conversa
- budget_min/max em números (ex: 2000000 para R$ 2M)
- neighborhoods como lista de strings
- confidence_score de 0 a 1 (quanta informação você tem sobre esse lead)
- family_profile: "casal sem filhos", "família com filhos", "investidor", "solteiro", "indefinido"
- purchase_purpose: "moradia", "investimento", "segunda residência", "indefinido"
- timeline_months: prazo estimado em meses (ex: 3, 6, 12, 24)"""

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON
        import json as _json
        profile_data = _json.loads(raw)

        # Salva no Supabase
        sb = _get_supabase()
        if sb:
            loop = asyncio.get_event_loop()
            record = {
                "client_id":          _ctx_client_id(),
                "lead_phone":         sender,
                "extraction_turns":   total_turns,
                "last_extracted_at":  "now()",
                **{k: v for k, v in profile_data.items() if v is not None and v != "" and v != []}
            }
            await loop.run_in_executor(None, lambda: (
                sb.table("lead_profiles")
                  .upsert(record, on_conflict="client_id,lead_phone")
                  .execute()
            ))
            log.info("Perfil estruturado extraído e salvo para %s (confiança: %.1f)",
                     sender, profile_data.get("confidence_score", 0))

        # Atualiza cache Redis para evitar re-extração desnecessária
        if redis_client:
            try:
                await redis_client.setex(cache_key, 3600, str(total_turns))
            except Exception:
                pass

    except Exception as e:
        log.warning("Falha ao extrair perfil de %s: %s", sender, e)


def _extract_name_from_reply(reply: str) -> str | None:
    """
    Tenta extrair o nome do lead da resposta da Sofia.
    Quando Sofia usa o nome, normalmente aparece como 'Prazer, {Nome}' ou '{Nome},'
    Esta é uma heurística simples — extração robusta requer NER.
    """
    # Se Sofia menciona o nome no início da resposta (padrão comum)
    patterns = [
        r'^(?:Prazer|Ótimo|Perfeito|Que bom|Olá|Oi|Boa tarde|Boa noite|Bom dia),?\s+([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][a-záéíóúàâêôãõç]+)',
        r'(?:Prazer|Ótimo|Perfeito|Que bom),\s+([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][a-záéíóúàâêôãõç]+)',
        r'^([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][a-záéíóúàâêôãõç]+),\s',
    ]
    for pat in patterns:
        m = re.search(pat, reply)
        if m:
            name = m.group(1)
            # Filtra palavras que não são nomes
            if name not in ("Boa", "Que", "Não", "Sim", "Ok", "Claro", "Com", "Para",
                            "Olá", "Ótimo", "Perfeito", "Excelente", "Entendido"):
                return name
    return None


# ─── Score de intenção — cálculo e persistência ──────────────────────────────

async def _get_lead_score(sender: str) -> int:
    """Recupera score acumulado do lead (Redis → memória)."""
    if redis_client:
        try:
            val = await redis_client.get(f"whatsapp:score:{sender}")
            return int(val) if val else 0
        except Exception:
            pass
    return int(_memory_history.get(f"score:{sender}", 0))


async def _update_lead_score(sender: str, user_message: str) -> tuple[int, int, dict]:
    """
    Calcula delta do score para a mensagem atual e atualiza o acumulado.
    Retorna (novo_score, delta, breakdown_dict).
    """
    delta = 0
    breakdown: dict[str, int] = {}

    for pattern, points, label in _SCORE_SIGNALS:
        if re.search(pattern, user_message, re.IGNORECASE):
            delta += points
            breakdown[label] = breakdown.get(label, 0) + points

    current = await _get_lead_score(sender)
    new_score = current + delta

    if delta > 0:
        if redis_client:
            try:
                await redis_client.set(
                    f"whatsapp:score:{sender}", new_score,
                    ex=86400 * 7,  # 7 dias
                )
            except Exception:
                pass
        _memory_history[f"score:{sender}"] = new_score

    if delta > 0:
        log.info("Score %s: +%d → %d | sinais: %s", sender, delta, new_score, breakdown)

    return new_score, delta, breakdown


async def _supabase_update_score(
    sender: str,
    score: int,
    breakdown: dict,
    corretor_notified: bool = False,
    corretor_score: int = 0,
):
    """Atualiza score e breakdown no registro do lead no Supabase."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        loop = asyncio.get_event_loop()
        data: dict = {
            "client_id":       _ctx_client_id(),
            "lead_phone":      sender,
            "intention_score": score,
            "score_breakdown": breakdown,
        }
        if corretor_notified:
            from datetime import datetime, timezone
            data["corretor_notified_at"]    = datetime.now(timezone.utc).isoformat()
            data["corretor_notified_score"] = corretor_score

        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert(data, on_conflict="client_id,lead_phone")
              .execute()
        ))
        log.info("Score persistido no Supabase: %s → %d", sender, score)
    except Exception as e:
        log.warning("Falha ao atualizar score Supabase: %s", e)


# ─── Human Takeover — verificação antes de responder ─────────────────────────

async def _is_human_takeover_active(sender: str) -> bool:
    """
    Verifica se o corretor assumiu a conversa com este sender.
    Estratégia dual:
      1. Redis key "human_takeover:{sender}" (TTL 24h) — caminho rápido
      2. Fallback: consulta Supabase leads.human_takeover diretamente
    Retorna True se takeover ativo, False caso contrário.
    """
    # Caminho 1: Redis (hot, sem latência extra)
    if redis_client:
        try:
            val = await redis_client.get(f"human_takeover:{sender}")
            if val is not None:
                return val.decode() == "1"
        except Exception:
            pass  # Redis indisponível — fallback para Supabase

    # Caminho 2: Supabase (cold, authoritative)
    sb = _get_supabase()
    if not sb:
        return False
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .select("human_takeover")
              .eq("lead_phone", sender)
              .eq("human_takeover", True)
              .limit(1)
              .execute()
        ))
        active = bool(result.data)
        # Popula Redis para as próximas mensagens (TTL 24h)
        if redis_client:
            try:
                await redis_client.set(
                    f"human_takeover:{sender}",
                    "1" if active else "0",
                    ex=86400
                )
            except Exception:
                pass
        return active
    except Exception as e:
        log.warning("Falha ao verificar human_takeover para %s: %s", sender, e)
        return False


# ─── Notificação ao corretor ──────────────────────────────────────────────────

async def _assign_corretor_to_lead(sender: str, corretor_phone: str) -> None:
    """
    Resolve o UUID do corretor em profiles pelo telefone e seta assigned_corretor_id no lead.
    Operação fire-and-forget — não bloqueia o fluxo de atendimento.
    Usa service_role (SUPABASE_KEY) para contornar RLS — operação interna, não via JWT.
    """
    sb = _get_supabase()
    if not sb:
        return
    try:
        loop = asyncio.get_event_loop()

        # Normaliza telefone: remove '+' para comparação
        normalized = corretor_phone.lstrip("+")

        # Busca UUID do corretor pelo telefone cadastrado em profiles
        result = await loop.run_in_executor(None, lambda: (
            sb.table("profiles")
              .select("id")
              .eq("corretor_phone", normalized)
              .eq("is_active", True)
              .limit(1)
              .execute()
        ))

        if not result.data:
            log.info(
                "Corretor %s não encontrado em profiles — assigned_corretor_id não setado para %s",
                normalized, sender
            )
            return

        corretor_uuid = result.data[0]["id"]

        # Seta assigned_corretor_id no lead (upsert pela PK composta)
        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert(
                  {
                      "client_id":            _ctx_client_id(),
                      "lead_phone":           sender,
                      "assigned_corretor_id": corretor_uuid,
                  },
                  on_conflict="client_id,lead_phone"
              )
              .execute()
        ))
        log.info(
            "assigned_corretor_id=%s setado para lead %s (corretor %s)",
            corretor_uuid, sender, normalized
        )
    except Exception as e:
        log.warning("Falha ao atribuir corretor ao lead %s: %s", sender, e)


async def _should_notify_corretor(sender: str, score: int) -> bool:
    """
    Verifica se o corretor deve ser notificado.
    Condições: score >= threshold E cooldown expirado E número configurado.
    """
    if not _ctx_corretor_number():
        return False
    if score < _ctx_corretor_threshold():
        return False
    # Verifica cooldown (Redis TTL)
    cooldown_key = f"whatsapp:corretor_notified:{sender}"
    if redis_client:
        try:
            exists = await redis_client.exists(cooldown_key)
            return not bool(exists)
        except Exception:
            pass
    # Sem Redis: usa memória local (sem cooldown persistente — aceita duplicatas em restart)
    return f"notified:{sender}" not in _memory_history


async def _gerar_resumo_estrategico(history: list[dict], lead_name: str | None) -> str:
    """
    Usa Claude Haiku para gerar um resumo estratégico da conversa para o corretor.
    Foco em: perfil do lead, intenção real, objeções, próximo passo.
    """
    if not history:
        return "Conversa ainda sem histórico disponível."

    # Monta transcrição compacta para o Haiku
    linhas = []
    for msg in history[-20:]:  # últimas 20 mensagens — suficiente para contexto
        role_label = "Lead" if msg["role"] == "user" else "Sofia"
        content = msg["content"][:300]  # trunca mensagens muito longas
        linhas.append(f"{role_label}: {content}")
    transcricao = "\n".join(linhas)

    nome_ctx = f"O lead se identificou como {lead_name}." if lead_name else "O lead não se identificou pelo nome."

    prompt = f"""Você é um analista de CRM especializado em imóveis de alto padrão.
Analise a conversa abaixo entre Sofia (consultora IA) e um lead no WhatsApp.
{nome_ctx}

CONVERSA:
{transcricao}

Gere um briefing estratégico CONCISO para o corretor humano, no seguinte formato exato:

*Perfil:* [comprador / investidor / locatário / indefinido] — [uma frase sobre o perfil]
*Busca:* [tipo de imóvel, bairro preferido, tamanho, outros requisitos mencionados]
*Budget:* [valor mencionado ou "não informado"]
*Prazo:* [urgência percebida: imediato / 30-60 dias / sem prazo / não informado]
*Sinais quentes:* [2-3 sinais de intenção real que apareceram na conversa]
*Objeções / dúvidas:* [principais resistências ou perguntas não respondidas]
*Próximo passo:* [ação concreta recomendada para o corretor — específica e direta]

Seja direto. Máximo 10 linhas. Sem introdução ou conclusão."""

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        resumo = response.content[0].text.strip()
        log.info("Resumo estratégico gerado para corretor (%d chars)", len(resumo))
        return resumo
    except Exception as e:
        log.error("Falha ao gerar resumo estratégico: %s", e)
        return "Não foi possível gerar o resumo automático. Verifique o histórico da conversa."


async def _notify_corretor(
    sender: str,
    score: int,
    history: list[dict],
    lead_name: str | None,
):
    """
    Envia alerta ao corretor via WhatsApp quando lead atinge threshold de intenção.
    Inclui resumo estratégico da conversa gerado por IA (Claude Haiku).
    """
    corretor_num = _ctx_corretor_number()
    if not corretor_num:
        return

    nome_display = lead_name or "não identificado"
    n_trocas = len([m for m in history if m["role"] == "user"])

    # Gera resumo estratégico antes de enviar
    resumo = await _gerar_resumo_estrategico(history, lead_name)

    # Verifica se há dados de permuta para incluir no briefing
    permuta_section = ""
    permuta_redis_key = f"whatsapp:permuta_detectada:{sender}"
    has_permuta = False
    if redis_client:
        try:
            has_permuta = bool(await redis_client.get(permuta_redis_key))
        except Exception:
            pass
    if has_permuta:
        try:
            from tools.permuta import format_permuta_briefing_section
            sb = _get_supabase()
            if sb:
                loop_p = asyncio.get_event_loop()
                lead_row = await loop_p.run_in_executor(None, lambda: (
                    sb.table("leads")
                      .select("permuta_dados")
                      .eq("lead_phone", sender)
                      .eq("client_id", _ctx_client_id())
                      .limit(1)
                      .execute()
                ))
                rows = lead_row.data if lead_row else []
                if rows and rows[0].get("permuta_dados"):
                    permuta_data = rows[0]["permuta_dados"]
                    permuta_section = "\n\n━━━━━━━━━━━━━━━\n" + format_permuta_briefing_section(permuta_data)
        except Exception as e:
            log.warning("[PERMUTA] Falha ao gerar seção de permuta no briefing: %s", e)

    msg = (
        f"🔔 *Lead Quente — Sofia IA*\n\n"
        f"📱 *Número:* {sender}\n"
        f"👤 *Nome:* {nome_display}\n"
        f"⚡ *Score de intenção:* {score} pts\n"
        f"💬 *Mensagens trocadas:* {n_trocas}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"*BRIEFING ESTRATÉGICO*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{resumo}"
        f"{permuta_section}\n\n"
        f"_Gerado automaticamente por Sofia IA_"
    )

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_whatsapp_message, corretor_num, msg)
    log.info("Corretor notificado sobre lead %s (score=%d, resumo gerado)", sender, score)

    # Atribui corretor ao lead no Supabase para RLS do painel admin
    asyncio.create_task(_assign_corretor_to_lead(sender, corretor_num))

    # Marca cooldown no Redis
    cooldown_key = f"whatsapp:corretor_notified:{sender}"
    cooldown_secs = _ctx_corretor_cooldown() * 3600
    if redis_client:
        try:
            await redis_client.set(cooldown_key, "1", ex=cooldown_secs)
        except Exception:
            pass
    _memory_history[f"notified:{sender}"] = True


# ─── ElevenLabs TTS → PTT ────────────────────────────────────────────────────
def _generate_audio_ptt(text: str) -> bytes | None:
    """
    Gera áudio MP3 via ElevenLabs e retorna os bytes.
    Retorna None se ElevenLabs não estiver configurado ou falhar.
    """
    if not ELEVENLABS_API_KEY:
        log.warning("ELEVENLABS_API_KEY não configurada — áudio desativado")
        return None

    # Limpa tags do texto antes de gerar o áudio
    clean_text = re.sub(r'\[FOTOS:[A-Z0-9]+\]', '', text).strip()
    clean_text = re.sub(r'\[AUDIO\]', '', clean_text).strip()
    if not clean_text:
        return None

    payload = json.dumps({
        "text":           clean_text,
        "model_id":       ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }).encode()

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{_ctx_voice_id()}",
        data=payload,
        headers={
            "xi-api-key":   ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept":       "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=30) as r:
            audio_bytes = r.read()
            log.info("Áudio gerado via ElevenLabs: %d bytes", len(audio_bytes))
            return audio_bytes
    except Exception as e:
        log.error("Falha ao gerar áudio ElevenLabs: %s", e)
        return None


def _send_audio_ptt(to: str, audio_bytes: bytes):
    """Envia áudio PTT via Evolution API usando base64."""
    b64 = base64.b64encode(audio_bytes).decode()
    payload = json.dumps({
        "number":    to,
        "audio":     b64,
        "encoding":  True,
        "ptt":       True,
    }).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendWhatsAppAudio/{_ctx_instance()}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=30) as r:
            log.info("PTT enviado para %s | HTTP %s", to, r.status)
    except Exception as e:
        log.error("Falha ao enviar PTT para %s: %s", to, e)


# ─── SSL helper ──────────────────────────────────────────────────────────────
def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─── Busca mídia na Evolution API (base64) ───────────────────────────────────
def _fetch_media_base64(message_key: dict, convert_to_mp4: bool = False) -> tuple[str, str]:
    """
    Busca base64 de mídia (áudio/imagem) via Evolution API.
    Retorna (base64_string, mimetype).
    """
    payload = json.dumps({
        "key": message_key,
        "convertToMp4": convert_to_mp4,
    }).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{_ctx_instance()}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=30) as r:
            data = json.loads(r.read())
            b64  = data.get("base64", "")
            mime = data.get("mimetype", "application/octet-stream")
            return b64, mime
    except Exception as e:
        log.error("Falha ao buscar mídia da Evolution API: %s", e)
        return "", ""


# ─── Transcrição de áudio via OpenAI Whisper ─────────────────────────────────
async def _transcribe_audio(message_key: dict) -> str:
    """
    Baixa áudio PTT da Evolution API e transcreve via OpenAI Whisper.
    Retorna o texto transcrito ou string vazia em caso de falha.
    """
    loop = asyncio.get_event_loop()
    b64, mime = await loop.run_in_executor(None, _fetch_media_base64, message_key, True)

    if not b64:
        log.warning("Áudio não pôde ser baixado — base64 vazio")
        return ""

    try:
        audio_bytes = base64.b64decode(b64)
    except Exception as e:
        log.error("Falha ao decodificar base64 do áudio: %s", e)
        return ""

    # Determina extensão pelo mimetype
    ext = ".ogg"
    if "mp4" in mime:
        ext = ".mp4"
    elif "mpeg" in mime or "mp3" in mime:
        ext = ".mp3"
    elif "webm" in mime:
        ext = ".webm"

    try:
        import openai
        key = OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")
        if not key:
            log.warning("OPENAI_API_KEY não configurada — transcrição indisponível")
            return ""

        oai = openai.OpenAI(api_key=key)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as audio_file:
                transcript = oai.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="pt",
                )
            text = transcript.text.strip()
            log.info("Áudio transcrito (%d chars): %s", len(text), text[:80])
            return text
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        log.error("Erro na transcrição Whisper: %s", e)
        return ""


# ─── Descrição de imagem via Claude Vision ───────────────────────────────────
async def _describe_image(message_key: dict, caption: str = "") -> str:
    """
    Baixa imagem da Evolution API e descreve via Claude Vision.
    Retorna descrição textual para uso como input do consultor.
    """
    loop = asyncio.get_event_loop()
    b64, mime = await loop.run_in_executor(None, _fetch_media_base64, message_key)

    if not b64:
        log.warning("Imagem não pôde ser baixada")
        return caption or "[Cliente enviou uma imagem que não pôde ser carregada]"

    # Normaliza mimetype
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime = "image/jpeg"

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        content: list = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        ]
        prompt = (
            "Descreva esta imagem de forma objetiva e concisa, em português, "
            "focando no que é relevante para uma conversa sobre imóveis. "
            "Se for foto de um imóvel, ambiente, documento ou planta: descreva o que vê. "
            "Se não for relacionado a imóveis: diga apenas o que é a imagem brevemente."
        )
        if caption:
            prompt += f" Legenda enviada pelo usuário: '{caption}'"
        content.append({"type": "text", "text": prompt})

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        description = response.content[0].text.strip()
        log.info("Imagem descrita: %s", description[:100])

        # Formata para o consultor entender o contexto
        if caption:
            return f"[Cliente enviou uma imagem com legenda '{caption}': {description}]"
        return f"[Cliente enviou uma imagem: {description}]"

    except Exception as e:
        log.error("Erro ao descrever imagem: %s", e)
        return caption or "[Cliente enviou uma imagem]"


# ─── Consultor LLM ───────────────────────────────────────────────────────────
async def run_consultant(history: list[dict], user_message: str) -> str:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    messages = history + [{"role": "user", "content": user_message}]

    # Injeta data real no topo do system prompt — evita datas erradas no agendamento
    data_hoje = _data_hoje_pt()
    system = (
        f"HOJE É: {data_hoje}\n"
        f"Ao confirmar ou sugerir datas de visita, calcule sempre a partir dessa data. "
        f"Nunca invente datas. Use o dia da semana + data completa (ex: terça-feira, 31 de março de 2026).\n\n"
        + _ctx_system_prompt()
    )

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        log.error("Erro no consultor LLM: %s", e)
        return "Desculpe, estou com uma instabilidade no momento. Pode repetir sua pergunta em instantes?"


# ─── Envio via Evolution API ─────────────────────────────────────────────────
def send_whatsapp_message(to: str, text: str):
    """Envia mensagem de texto via Evolution API (síncrono)."""
    payload = json.dumps({"number": to, "text": text}).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendText/{_ctx_instance()}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=15) as r:
            log.info("Mensagem enviada para %s | status HTTP %s", to, r.status)
    except Exception as e:
        log.error("Falha ao enviar mensagem para %s: %s", to, e)


def send_whatsapp_media(to: str, media_url: str, caption: str = ""):
    """Envia imagem via Evolution API (síncrono)."""
    payload = json.dumps({
        "number": to,
        "mediatype": "image",
        "mimetype": "image/jpeg",
        "media": media_url,
        "caption": caption,
    }).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendMedia/{_ctx_instance()}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=20) as r:
            log.info("Foto enviada para %s | status HTTP %s | url: %s", to, r.status, media_url[:60])
    except Exception as e:
        log.error("Falha ao enviar foto para %s: %s", to, e)


def dispatch_photos(to: str, imovel_id: str):
    """Envia fotos + link do imóvel conforme foto_config do cliente."""
    midia = ONBOARDING.get("midia", {})
    foto_config   = midia.get("foto_config", "4_fotos_mais_link")
    max_fotos     = int(midia.get("max_fotos_envio", 4))
    msg_link      = midia.get("mensagem_link", "Veja detalhes completos em nosso site:")

    portfolio = _portfolio_cache
    imovel = portfolio.get(imovel_id, {})
    link_fotos = imovel.get("link_fotos", "")
    bairro  = imovel.get("bairro", "")
    quartos = imovel.get("quartos", "")
    area    = imovel.get("area_m2", "")
    valor   = imovel.get("valor", "")

    # URLs de fotos — imagens reais de apartamentos de alto padrão (Unsplash, uso livre)
    # Cada imóvel tem 4 fotos fixas e consistentes, temáticas com o tipo e bairro
    FOTOS_POR_IMOVEL: dict[str, list[str]] = {
        "AV001": [  # Jardins — apartamento sofisticado, vista urbana
            "https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?w=800&q=80",  # sala luxo
            "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=800&q=80",  # sala integrada
            "https://images.unsplash.com/photo-1560185007-c5ca9d2c014d?w=800&q=80",  # varanda
            "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=800&q=80",  # cozinha gourmet
        ],
        "AV002": [  # Itaim — cobertura duplex, rooftop
            "https://images.unsplash.com/photo-1600566753190-17f0baa2a6c3?w=800&q=80",  # cobertura
            "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?w=800&q=80",  # fachada luxo
            "https://images.unsplash.com/photo-1600047509807-ba8f99d2cdde?w=800&q=80",  # piscina rooftop
            "https://images.unsplash.com/photo-1600566753086-00f18fb6b3ea?w=800&q=80",  # sala moderna
        ],
        "AV003": [  # Vila Nova Conceição — apartamento espaçoso
            "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?w=800&q=80",  # sala moderna
            "https://images.unsplash.com/photo-1600585154526-990dced4db0d?w=800&q=80",  # quarto master
            "https://images.unsplash.com/photo-1584622650111-993a426fbf0a?w=800&q=80",  # banheiro spa
            "https://images.unsplash.com/photo-1556909172-54557c7e4fb7?w=800&q=80",  # cozinha
        ],
        "AV005": [  # Moema — reformado, próximo Ibirapuera
            "https://images.unsplash.com/photo-1600607687644-c7171b42498f?w=800&q=80",  # sala aberta
            "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=800&q=80",  # varanda verde
            "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=800&q=80",  # sala minimalista
            "https://images.unsplash.com/photo-1556909212-d5b604d0c90d?w=800&q=80",  # cozinha integrada
        ],
    }

    # Fallback para imóveis sem fotos específicas — galeria genérica de alto padrão
    FOTOS_FALLBACK = [
        "https://images.unsplash.com/photo-1600448204-e02f11c3d0e2?w=800&q=80",
        "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?w=800&q=80",
        "https://images.unsplash.com/photo-1600566753376-12c8ab7fb75b?w=800&q=80",
        "https://images.unsplash.com/photo-1600210492493-0946911123ea?w=800&q=80",
    ]

    photo_urls = FOTOS_POR_IMOVEL.get(imovel_id, FOTOS_FALLBACK)

    # Legenda da primeira foto
    try:
        valor_fmt = f"R$ {int(float(valor)):,.0f}".replace(",", ".")
    except Exception:
        valor_fmt = f"R$ {valor}"
    first_caption = f"[{imovel_id}] {bairro} — {quartos} quartos | {area}m² | {valor_fmt}"

    if foto_config == "somente_link":
        if link_fotos:
            send_whatsapp_message(to, f"{msg_link}\n{link_fotos}")

    elif foto_config == "todas_fotos":
        for i, url in enumerate(photo_urls):
            caption = first_caption if i == 0 else ""
            send_whatsapp_media(to, url, caption)
        if link_fotos:
            send_whatsapp_message(to, f"{msg_link}\n{link_fotos}")

    else:  # "4_fotos_mais_link" (padrão)
        for i, url in enumerate(photo_urls[:max_fotos]):
            caption = first_caption if i == 0 else ""
            send_whatsapp_media(to, url, caption)
        if link_fotos:
            send_whatsapp_message(to, f"{msg_link}\n{link_fotos}")

    log.info("Fotos despachadas para %s | imóvel: %s | config: %s", to, imovel_id, foto_config)


# ─── FastAPI app ──────────────────────────────────────────────────────────────
redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        log.info("Redis conectado em %s", REDIS_URL)
    except Exception as e:
        log.warning("Redis indisponível (%s) — usando memória local.", e)
        redis_client = None
    yield
    if redis_client:
        await redis_client.aclose()


app = FastAPI(title="ImobOne WhatsApp Webhook", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status":              "ok",
        "clients_registered":  list(_CLIENTS_REGISTRY.keys()),
        "default_instance":    EVOLUTION_INSTANCE,
        "default_client":      CLIENT_ID,
        "portfolio_size":      len(_portfolio_cache),
        "foto_config":         ONBOARDING.get("midia", {}).get("foto_config", "não configurado"),
        "audio_transcription": bool(OPENAI_API_KEY),
        "supabase":            bool(SUPABASE_URL and SUPABASE_KEY),
        "elevenlabs_tts":      bool(ELEVENLABS_API_KEY),
        "corretor_notify": {
            "configured":  bool(CORRETOR_NUMBER),
            "number":      f"...{CORRETOR_NUMBER[-4:]}" if CORRETOR_NUMBER else None,
            "threshold":   CORRETOR_SCORE_THRESHOLD,
            "cooldown_h":  CORRETOR_COOLDOWN_HOURS,
        },
    }


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    event = body.get("event", "")
    if event != "messages.upsert":
        return Response(status_code=200)

    data = body.get("data", {})

    if data.get("key", {}).get("fromMe"):
        return Response(status_code=200)

    # ── Resolve contexto do cliente pela instância Evolution ─────────────────
    instance = body.get("instance", EVOLUTION_INSTANCE)
    client_context = _build_client_context(instance)
    _client_ctx.set(client_context)

    remote_jid = data.get("key", {}).get("remoteJid", "")
    if "@g.us" in remote_jid:
        return Response(status_code=200)

    sender       = remote_jid.replace("@s.whatsapp.net", "")
    message_key  = data.get("key", {})
    message_obj  = data.get("message", {})
    message_type = data.get("messageType", "")

    # ── Extrai conteúdo conforme tipo de mensagem ─────────────────────────────
    text    = ""
    is_media = False
    media_info: dict = {}

    # 1. Texto puro
    text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()

    # 2. Áudio PTT / audioMessage
    if not text and (
        "audioMessage" in message_obj
        or "pttMessage" in message_obj
        or message_type in ("audioMessage", "pttMessage")
    ):
        is_media = True
        media_info = {"type": "audio", "key": message_key}
        log.info("Áudio recebido de %s", sender)

    # 3. Imagem
    elif not text and (
        "imageMessage" in message_obj
        or message_type == "imageMessage"
    ):
        is_media = True
        img_msg  = message_obj.get("imageMessage", {})
        caption  = img_msg.get("caption", "")
        media_info = {"type": "image", "key": message_key, "caption": caption}
        log.info("Imagem recebida de %s | caption: %s", sender, caption[:60])

    # 4. Documento
    elif not text and (
        "documentMessage" in message_obj
        or message_type in ("documentMessage", "documentWithCaptionMessage")
    ):
        doc_msg  = message_obj.get("documentMessage", {})
        caption  = doc_msg.get("caption", "")
        filename = doc_msg.get("fileName", "documento")
        # Para documentos, usa o caption como texto ou notifica o tipo
        text = caption if caption else f"[Cliente enviou um documento: {filename}]"
        log.info("Documento recebido de %s: %s", sender, filename)

    # Nada reconhecível
    if not text and not is_media:
        return Response(status_code=200)

    # ── Off-market: corretor envia áudio → ingestão de pocket listing ────────
    # Intercept ANTES de passar para o fluxo normal do lead.
    # Só ativa quando: remetente é corretor cadastrado E mensagem é áudio.
    if is_media and media_info.get("type") == "audio":
        asyncio.create_task(
            _maybe_process_off_market_audio(sender, message_key)
        )
        # Se for corretor, não processa como mensagem de lead
        try:
            from tools.off_market import is_corretor_sender
            if is_corretor_sender(sender, ONBOARDING):
                return Response(status_code=200)
        except ImportError:
            pass

    # Dispara processamento assíncrono — lock por sender garante ordem
    asyncio.create_task(_process_media_and_reply(sender, text, media_info if is_media else None))
    return Response(status_code=200)


# ─── Deduplicação de fotos ────────────────────────────────────────────────────
async def _fotos_ja_enviadas(sender: str, imovel_id: str) -> bool:
    key = f"whatsapp:fotos_enviadas:{sender}"
    if redis_client:
        try:
            return await redis_client.sismember(key, imovel_id)
        except Exception:
            pass
    return imovel_id in _memory_history.get(f"fotos:{sender}", set())


async def _marcar_fotos_enviadas(sender: str, imovel_id: str):
    key = f"whatsapp:fotos_enviadas:{sender}"
    if redis_client:
        try:
            await redis_client.sadd(key, imovel_id)
            await redis_client.expire(key, 86400)  # 24h
            return
        except Exception:
            pass
    if f"fotos:{sender}" not in _memory_history:
        _memory_history[f"fotos:{sender}"] = set()
    _memory_history[f"fotos:{sender}"].add(imovel_id)




async def _create_calendar_event_for_visit(
    sender: str,
    reply: str,
    history: list[dict],
) -> None:
    """
    Cria evento no Google Calendar do corretor quando visita é confirmada.
    Fire-and-forget — nunca bloqueia o fluxo principal.
    """
    try:
        from tools.calendar import create_calendar_event, format_imovel_descricao
    except ImportError:
        log.warning("calendar: tools/calendar.py não disponível")
        return

    corretor_email = _ctx_corretor_email()
    if not corretor_email:
        log.debug("calendar: corretor_email não configurado — pulando criação de evento")
        return

    try:
        # Dados do lead
        lead_data = _memory_leads.get(sender, {}) if hasattr(sys.modules[__name__], "_memory_leads") else {}
        lead_name  = lead_data.get("lead_name", "") or ""
        lead_phone = sender

        # Imóvel de interesse (último enviado via FOTOS)
        portfolio = _load_portfolio_dict()
        imovel_id = ""
        imovel_descricao = ""
        if redis_client:
            try:
                key = f"whatsapp:fotos_enviadas:{sender}"
                sent = await redis_client.smembers(key)
                ids = sorted(m.decode() if isinstance(m, bytes) else m for m in sent)
                if ids:
                    imovel_id = ids[-1]
            except Exception:
                pass
        if not imovel_id:
            # Tenta pegar do histórico: último [FOTOS:ID] mencionado
            import re as _re
            for msg in reversed(history or []):
                m = _re.search(r"\[FOTOS:([A-Z0-9]+)\]", msg.get("content", ""))
                if m:
                    imovel_id = m.group(1)
                    break

        imovel = portfolio.get(imovel_id, {})
        imovel_descricao = format_imovel_descricao(imovel) if imovel else imovel_id

        # Parseia data/hora da visita
        visit_dt = _parse_visit_datetime_from_reply(reply) if reply else None

        # Resumo via Haiku (fire-and-forget, aceita falha)
        resumo = ""
        if history and ANTHROPIC_API_KEY:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                hist_text = "\n".join(
                    f"{m['role'].upper()}: {m['content'][:200]}"
                    for m in (history or [])[-8:]
                )
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    system=(
                        "Você resume conversas de atendimento imobiliário em 3 bullets curtos. "
                        "Formato: • Interesse: ... • Budget: ... • Observação: ..."
                        "A resposta deve conter APENAS o resumo. Não inclua prefixos."
                    ),
                    messages=[{"role": "user", "content": f"Histórico:\n{hist_text}"}],
                )
                resumo = resp.content[0].text.strip()
            except Exception as e:
                log.debug("calendar: resumo Haiku falhou: %s", e)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: create_calendar_event(
                corretor_email=corretor_email,
                lead_name=lead_name,
                lead_phone=lead_phone,
                imovel_id=imovel_id,
                imovel_descricao=imovel_descricao,
                visit_dt=visit_dt,
                resumo_conversa=resumo,
            ),
        )
        if result.success:
            log.info("Calendar: evento criado para %s | link: %s", corretor_email, result.event_link)
        else:
            log.debug("Calendar: %s", result.error)

    except Exception as e:
        log.warning("_create_calendar_event_for_visit: %s", e)


async def _generate_and_send_dossie(sender: str, history: list[dict]) -> None:
    """
    Gera o Dossiê de Caviar e envia ao corretor quando Sofia confirma visita.

    Pipeline:
        1. Busca dados do lead no Supabase (nome, score, pipeline, visit_date)
        2. Resolve número do corretor via contexto
        3. Delega geração + PDF + envio para tools/dossie.py (via run_in_executor)

    Fire-and-forget — nunca bloqueia o fluxo principal.
    """
    try:
        from tools.dossie import build_and_send_dossie
    except ImportError:
        log.warning("dossie: tools/dossie.py não disponível — dossiê não gerado")
        return

    corretor_phone = _ctx_corretor_number()
    if not corretor_phone:
        log.info("dossie: CORRETOR_NUMBER não configurado — pulando")
        return

    # Busca dados complementares do lead
    sb = _get_supabase()
    lead_name_db:   str | None   = None
    score:          int          = 0
    pipeline:       float | None = None
    visit_date_str: str | None   = None

    try:
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: (
            sb.table("leads")
              .select("lead_name,intention_score,pipeline_value_brl,visita_confirmada_at")
              .eq("lead_phone", sender)
              .eq("client_id", _ctx_client_id())
              .limit(1)
              .execute()
        ))
        if r.data:
            row           = r.data[0]
            lead_name_db  = row.get("lead_name") or None
            score         = int(row.get("intention_score") or 0)
            pipeline      = row.get("pipeline_value_brl")
            raw_dt        = row.get("visita_confirmada_at")
            if raw_dt:
                # Normaliza timestamp Postgres → legível
                visit_date_str = raw_dt[:16].replace("T", " ") + "h"
    except Exception as e:
        log.warning("dossie: falha ao buscar dados do lead %s: %s", sender, e)

    imobiliaria = ONBOARDING.get("nome_imobiliaria", "Imobiliária")
    client_id   = _ctx_client_id()

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: build_and_send_dossie(
            history=history,
            lead_name=lead_name_db,
            lead_phone=sender,
            corretor_phone=corretor_phone,
            score=score,
            pipeline=float(pipeline) if pipeline else None,
            visit_date=visit_date_str,
            client_id=client_id,
            imobiliaria=imobiliaria,
        ))
    except Exception as e:
        log.error("dossie: falha no pipeline para %s: %s", sender, e)


async def _check_and_process_permuta(
    sender: str,
    user_message: str,
    history: list[dict],
) -> None:
    """
    Detecta menção a permuta/troca na mensagem do lead.
    Se detectado E ainda não registrado no Redis para este sender:
        1. Seta flag permuta_detectada no Redis (TTL 30 dias)
        2. Extrai dados do ativo via Claude Haiku (run_in_executor)
        3. Calcula bonus de score (+3 pts para ativo de alto padrão)
        4. Salva em leads.permuta_dados no Supabase

    A seção 'Análise de Permuta' é injetada no briefing do corretor via
    _build_corretor_briefing quando permuta_detectada=true no Redis.
    Fire-and-forget — nunca bloqueia o fluxo principal.
    """
    try:
        from tools.permuta import (
            detect_permuta,
            extract_permuta_data,
            calculate_permuta_score_bonus,
            save_permuta_data,
        )
    except ImportError:
        log.warning("[PERMUTA] tools.permuta não disponível — skip")
        return

    try:
        # 1. Detecção rápida via regex
        if not detect_permuta(user_message):
            return

        # 2. Idempotência: já detectamos permuta para este lead?
        permuta_redis_key = f"whatsapp:permuta_detectada:{sender}"
        if redis_client:
            try:
                already = await redis_client.get(permuta_redis_key)
                if already:
                    log.debug("[PERMUTA] Já detectado para %s — skip extração", sender)
                    return
            except Exception:
                pass

        log.info("[PERMUTA] Menção a permuta detectada para %s", sender)

        # 3. Seta flag no Redis imediatamente (TTL 30 dias)
        if redis_client:
            try:
                await redis_client.set(permuta_redis_key, "1", ex=86400 * 30)
            except Exception:
                pass

        # 4. Extrai dados do ativo via Claude Haiku (síncrono em executor)
        loop = asyncio.get_event_loop()
        try:
            permuta_data = await loop.run_in_executor(
                None, extract_permuta_data, history
            )
        except Exception as e:
            log.warning("[PERMUTA] Falha ao extrair dados do ativo: %s", e)
            from datetime import datetime as _dt, timezone as _tz
            permuta_data = {
                "tipo_ativo":     None,
                "descricao_lead": user_message[:300],
                "extracted_at":   _dt.now(_tz.utc).isoformat(),
            }

        # 5. Calcula bonus de score
        score_bonus = calculate_permuta_score_bonus(permuta_data, ONBOARDING)
        log.info("[PERMUTA] Score bonus: +%d para %s", score_bonus, sender)

        # 6. Persiste no Supabase
        client_id = _ctx_client_id()
        await loop.run_in_executor(
            None,
            save_permuta_data,
            sender,
            client_id,
            permuta_data,
            score_bonus,
        )

        # 7. Acumula bonus de score no Redis
        if redis_client and score_bonus > 0:
            try:
                current_score_key = f"whatsapp:score:{sender}"
                current_raw = await redis_client.get(current_score_key)
                current = int(current_raw) if current_raw else 0
                new_total = current + score_bonus
                await redis_client.set(current_score_key, new_total, ex=86400 * 7)
                log.info("[PERMUTA] Score atualizado: %s → %d (+%d permuta)",
                         sender, new_total, score_bonus)
            except Exception:
                pass

    except Exception as e:
        log.error("[PERMUTA] Erro inesperado no pipeline de permuta: %s", e)


async def _maybe_process_off_market_audio(sender: str, message_key: dict) -> None:
    """
    Se o remetente for um corretor cadastrado, transcreve o áudio via Whisper
    e aciona o pipeline off-market (extração → pgvector → matchmaking).
    Fire-and-forget — nunca bloqueia o fluxo principal.
    """
    try:
        from tools.off_market import is_corretor_sender, process_off_market_audio
    except ImportError:
        return

    if not is_corretor_sender(sender, ONBOARDING):
        return  # não é corretor — áudio segue o fluxo normal de lead

    log.info("[OFF_MARKET] Áudio de corretor %s detectado — iniciando ingestão", sender)

    # 1. Transcreve via Whisper (já implementado no webhook)
    transcription = await _transcribe_audio(message_key)
    if not transcription:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, send_whatsapp_message, sender,
            "❌ Não consegui transcrever o áudio. Por favor, tente novamente ou envie por texto."
        )
        return

    log.info("[OFF_MARKET] Transcrição: %s", transcription[:120])

    # Confirmação imediata ao corretor
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, send_whatsapp_message, sender,
        "🔍 Recebi o imóvel. Analisando e buscando matches na base de leads VIP..."
    )

    # 2. Pipeline off-market (síncrono em executor para não bloquear o event loop)
    client_id = _ctx_client_id()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: process_off_market_audio(
                transcription=transcription,
                corretor_phone=sender,
                client_id=client_id,
                onboarding=ONBOARDING,
            )
        )
        matches = result.get("matches_sent", 0)
        imovel_id = result.get("imovel_id", "")
        imovel    = result.get("imovel_data", {})
        bairro    = imovel.get("bairro", "?")
        tipologia = imovel.get("tipologia", "?")

        if matches > 0:
            await loop.run_in_executor(
                None, send_whatsapp_message, sender,
                f"✅ Imóvel registrado (`{imovel_id}`).\n"
                f"📍 {tipologia.title()} em {bairro}\n"
                f"🎯 {matches} lead{'s' if matches != 1 else ''} compatível{'is' if matches != 1 else ''} "
                f"identificado{'s' if matches != 1 else ''}. Drafts de abordagem enviados acima."
            )
        else:
            await loop.run_in_executor(
                None, send_whatsapp_message, sender,
                f"✅ Imóvel registrado (`{imovel_id}`).\n"
                f"📍 {tipologia.title()} em {bairro}\n"
                f"ℹ️ Nenhum lead VIP com perfil compatível no momento. "
                f"O imóvel está indexado — quando um lead adequado entrar, você será notificado."
            )

    except Exception as e:
        log.error("[OFF_MARKET] Falha no pipeline para corretor %s: %s", sender, e)
        await loop.run_in_executor(
            None, send_whatsapp_message, sender,
            "⚠️ Erro ao processar o imóvel. Tente novamente em alguns minutos."
        )


async def _update_pipeline_value(sender: str, imovel_id: str) -> None:
    """
    Recalcula pipeline_value_brl do lead somando os valores dos imóveis
    cujas fotos já foram enviadas + o imóvel atual.
    Atualiza Supabase fire-and-forget.
    """
    sb = _get_supabase()
    portfolio = _load_portfolio_dict()
    if not sb or not portfolio:
        return
    try:
        key = f"whatsapp:fotos_enviadas:{sender}"
        if redis_client:
            try:
                existing = await redis_client.smembers(key)
                imovel_ids = {m.decode() if isinstance(m, bytes) else m for m in existing}
            except Exception:
                imovel_ids = _memory_history.get(f"fotos:{sender}", set()).copy()
        else:
            imovel_ids = _memory_history.get(f"fotos:{sender}", set()).copy()

        imovel_ids.add(imovel_id)

        total_brl = 0.0
        ids_validos = []
        for iid in sorted(imovel_ids):
            imovel = portfolio.get(iid, {})
            valor_raw = imovel.get("valor", "")
            if not valor_raw:
                continue
            try:
                valor = float(str(valor_raw).replace("R$", "").replace(".", "").replace(",", ".").strip())
                total_brl += valor
                ids_validos.append(iid)
            except (ValueError, TypeError):
                log.warning("pipeline: valor invalido para imovel %s: %s", iid, valor_raw)

        if not ids_validos:
            return

        from datetime import datetime, timezone
        data = {
            "client_id":           _ctx_client_id(),
            "lead_phone":          sender,
            "pipeline_value_brl":  round(total_brl, 2),
            "pipeline_imovel_ids": ids_validos,
            "pipeline_updated_at": datetime.now(timezone.utc).isoformat(),
        }
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: (
            sb.table("leads")
              .upsert(data, on_conflict="client_id,lead_phone")
              .execute()
        ))
        log.info("Pipeline atualizado: %s -> R$ %,.2f (%s)", sender, total_brl, ", ".join(ids_validos))
    except Exception as e:
        log.warning("Falha ao atualizar pipeline_value_brl: %s", e)



# ─── Pipeline principal — com lock por sender ─────────────────────────────────

# ─── Comandos de operador via WhatsApp ───────────────────────────────────────
# O corretor envia para o número da Sofia:
#   #assumir 5511999998888         → ativa human mode para esse lead
async def _send_takeover_context_brief(operator_phone: str, lead_phone: str) -> None:
    """
    Gera e envia ao corretor um resumo de contexto da conversa com o lead.
    Chamado via fire-and-forget quando o corretor usa /assumir.
    Usa Claude Haiku — custo ~$0.001. Não bloqueia o fluxo do operador.
    """
    try:
        history = await get_history(redis_client, lead_phone) if redis_client else []
        if not history:
            send_whatsapp_message(
                operator_phone,
                f"📋 *Contexto — {lead_phone}*\nNenhuma conversa registrada ainda."
            )
            return

        # Busca nome do lead no Supabase
        lead_name = None
        sb = _get_supabase()
        if sb:
            try:
                r = await asyncio.get_event_loop().run_in_executor(None, lambda: (
                    sb.table("leads")
                      .select("lead_name,intention_score,pipeline_value_brl,visita_agendada")
                      .eq("lead_phone", lead_phone)
                      .eq("client_id", _ctx_client_id())
                      .limit(1)
                      .execute()
                ))
                if r.data:
                    lead_name = r.data[0].get("lead_name")
                    score = r.data[0].get("intention_score", 0)
                    pipeline = r.data[0].get("pipeline_value_brl")
                    visita = r.data[0].get("visita_agendada", False)
                else:
                    score, pipeline, visita = 0, None, False
            except Exception:
                score, pipeline, visita = 0, None, False
        else:
            score, pipeline, visita = 0, None, False

        # Monta histórico reduzido (últimos 10 turnos)
        recent = history[-10:]
        history_text = "\n".join(
            f"{'Lead' if m['role'] == 'user' else 'Sofia'}: {m['content'][:200]}"
            for m in recent
        )

        prompt = f"""Você é um assistente de briefing para corretores de imóveis de alto padrão.
Analise esta conversa entre Sofia (IA) e o lead, e produza um briefing conciso.

Lead: {lead_name or lead_phone}
Score de intenção: {score}/20{' — VISITA CONFIRMADA' if visita else ''}
{f'Pipeline estimado: R$ {pipeline:,.0f}'.replace(',', '.') if pipeline else ''}

Conversa recente:
{history_text}

Produza um briefing para o corretor que assume agora, em formato WhatsApp (sem markdown pesado).
Inclua exatamente estas seções:
🎯 *O que o lead quer* — em 1-2 linhas
💰 *Budget e prazo* — o que foi declarado
🔥 *Sinal mais quente* — o sinal de intenção mais forte detectado
⚠️ *Risco ou objeção* — a principal objeção ou hesitação (se houver)
⚡ *Próximo passo sugerido* — ação concreta para o corretor fazer agora

Seja direto. Máximo 250 palavras. Não use asteriscos extras nem bullet points."""

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
        )
        brief = msg.content[0].text.strip() if msg.content else "Sem contexto disponível."

        header = f"📋 *Briefing — {lead_name or lead_phone}*\n\n"
        send_whatsapp_message(operator_phone, header + brief)
        log.info("[TAKEOVER] Briefing enviado para %s sobre lead %s", operator_phone, lead_phone)

    except Exception as e:
        log.warning("[TAKEOVER] Falha ao gerar briefing de contexto: %s", e)
        try:
            send_whatsapp_message(
                operator_phone,
                f"📋 Contexto de {lead_phone}: falha ao gerar resumo automático. "
                "Verifique o histórico no dashboard."
            )
        except Exception:
            pass


# ── Comandos do operador via WhatsApp ────────────────────────────────────────
#   #assumir 5511999998888 motivo  → com nota opcional (alias: /assumir)
#   #devolver 5511999998888        → devolve para Sofia (alias: /sofia, /devolver)
#   #status 5511999998888          → verifica se está em human mode
#   #leads                         → lista leads quentes
#
# Sintaxe com / também aceita:
#   /assumir 5511999998888
#   /sofia 5511999998888

_OPERATOR_CMD_RE = re.compile(
    r'^[#/](assumir|devolver|sofia|status|leads)\s*(\d{10,15})?\s*(.*)?$',
    re.IGNORECASE
)


async def _handle_operator_command(operator_phone: str, text: str) -> bool:
    """
    Processa comandos do operador enviados via WhatsApp.
    Retorna True se era um comando (e foi processado), False caso contrário.
    """
    text_stripped = text.strip()
    if not (text_stripped.startswith('#') or text_stripped.startswith('/')):
        return False

    m = _OPERATOR_CMD_RE.match(text_stripped)
    if not m:
        return False

    cmd, lead_phone, note = m.groups()
    cmd = cmd.lower()
    note = (note or "").strip()

    # Normaliza aliases: /sofia → devolver
    if cmd == "sofia":
        cmd = "devolver"

    loop = asyncio.get_event_loop()

    if cmd == "assumir":
        if not lead_phone:
            await loop.run_in_executor(None, send_whatsapp_message,
                operator_phone,
                "Formato: /assumir 5511999998888 [motivo opcional]\n"
                "Também aceito: #assumir 5511999998888"
            )
            return True

        await _set_human_mode_triggered_by(
            lead_phone, True,
            triggered_by="whatsapp_command",
            operator=operator_phone,
            note=note or "Assumido via WhatsApp"
        )

        # Renova TTL no Redis
        if redis_client:
            try:
                key = f"whatsapp:human_mode:{_ctx_client_id()}:{lead_phone}"
                await redis_client.setex(key, HUMAN_MODE_TTL_HOURS * 3600, operator_phone)
            except Exception:
                pass

        # ── Mensagem de transição neutra para o lead ──────────────────────────
        # Enviada pela Sofia antes de silenciar — o lead não percebe a troca
        transition_msg = (
            "Um momento — vou te conectar com um dos nossos consultores "
            "para os próximos detalhes. 🤝"
        )
        await loop.run_in_executor(None, send_whatsapp_message, lead_phone, transition_msg)

        # ── Resumo de contexto para o corretor (Haiku, fire-and-forget) ──────
        asyncio.create_task(
            _send_takeover_context_brief(operator_phone, lead_phone)
        )

        # ── Confirmação para o operador ───────────────────────────────────────
        await loop.run_in_executor(None, send_whatsapp_message,
            operator_phone,
            f"✅ Conversa com {lead_phone} assumida.\n"
            f"Sofia em silêncio por até {HUMAN_MODE_TTL_HOURS}h.\n"
            f"Contexto da conversa sendo enviado...\n\n"
            f"Para devolver: /sofia {lead_phone}"
        )
        log.info("[CMD] Operador %s assumiu conversa com %s", operator_phone, lead_phone)
        return True

    elif cmd == "devolver":
        if not lead_phone:
            await loop.run_in_executor(None, send_whatsapp_message,
                operator_phone,
                "Formato: /sofia 5511999998888\n"
                "Também aceito: #devolver 5511999998888"
            )
            return True

        await _set_human_mode_triggered_by(
            lead_phone, False,
            triggered_by="whatsapp_command",
            operator=operator_phone,
            note="Devolvido para Sofia via WhatsApp"
        )

        # ── Mensagem de retomada para o lead (opcional, sutil) ────────────────
        # Sofia reassume sem o lead perceber descontinuidade
        await loop.run_in_executor(None, send_whatsapp_message,
            operator_phone,
            f"✅ Sofia reativada para {lead_phone}.\n"
            f"Ela retoma a conversa normalmente na próxima mensagem do lead."
        )
        log.info("[CMD] Operador %s devolveu %s para Sofia", operator_phone, lead_phone)
        return True

    elif cmd == "status":
        if not lead_phone:
            await loop.run_in_executor(None, send_whatsapp_message,
                operator_phone, "Formato: #status 5511999998888"
            )
            return True
        human = await _is_human_mode(lead_phone)
        status_text = f"{lead_phone}: {'HUMANO (Sofia em silencio)' if human else 'SOFIA (automatico)'}"
        await loop.run_in_executor(None, send_whatsapp_message, operator_phone, status_text)
        return True

    elif cmd == "leads":
        # Lista top 5 leads quentes não em human mode
        sb = _get_supabase()
        if sb:
            try:
                result = await asyncio.get_event_loop().run_in_executor(None, lambda: (
                    sb.table("leads")
                      .select("lead_phone,lead_name,intention_score")
                      .eq("client_id", _ctx_client_id())
                      .eq("descartado", False)
                      .gte("intention_score", 5)
                      .order("intention_score", desc=True)
                      .limit(5)
                      .execute()
                ))
                leads_list = result.data or []
                if leads_list:
                    lines = ["Leads quentes:"]
                    for l in leads_list:
                        name  = l.get("lead_name") or "?"
                        phone = l.get("lead_phone", "")[-8:] + "..."
                        score = l.get("intention_score", 0)
                        lines.append(f"  {name} ({phone}) score {score}")
                    msg = "\n".join(lines)
                else:
                    msg = "Nenhum lead quente no momento."
                await loop.run_in_executor(None, send_whatsapp_message, operator_phone, msg)
            except Exception as e:
                log.warning("Falha ao buscar leads para comando #leads: %s", e)
        return True

    return False


async def _process_media_and_reply(sender: str, text: str, media_info: dict | None):
    """
    Processa entrada (texto, áudio ou imagem), chama o consultor LLM e responde.
    Lock por sender garante que mensagens do mesmo número são processadas em série,
    evitando race condition no histórico Redis.
    """
    async with _sender_locks[sender]:
        # ── Verifica human_takeover — não responde se corretor assumiu ────────
        if await _is_human_takeover_active(sender):
            # Persiste mensagem do lead para histórico sem gerar resposta automática
            asyncio.create_task(_supabase_append_conversa(sender, "user", text or "[mídia]"))
            log.info("Mensagem de %s ignorada pela Sofia — human_takeover ativo", sender)
            return

        # ── Resolve mídia para texto ──────────────────────────────────────────
        user_message = text

        if media_info:
            mtype = media_info.get("type")
            mkey  = media_info.get("key", {})

            if mtype == "audio":
                transcribed = await _transcribe_audio(mkey)
                if transcribed:
                    user_message = f"[Áudio transcrito]: {transcribed}"
                    log.info("Áudio de %s processado como texto: %s", sender, transcribed[:80])
                else:
                    # Whisper indisponível — informa o consultor genericamente
                    user_message = "[Cliente enviou um áudio. Não foi possível transcrever. Peça para repetir por texto.]"

            elif mtype == "image":
                caption = media_info.get("caption", "")
                user_message = await _describe_image(mkey, caption)
                log.info("Imagem de %s processada: %s", sender, user_message[:80])

        if not user_message:
            return

        log.info("Mensagem processada de %s: %s", sender, user_message[:100])

        # ── Detecta comandos de operador (via WhatsApp do corretor) ──────────
        corretor_num = _ctx_corretor_number()
        if corretor_num and sender == corretor_num:
            handled = await _handle_operator_command(sender, user_message)
            if handled:
                return   # comando processado — não passa pelo LLM

        # ── Human mode: se operador assumiu esta conversa, fica em silêncio ──
        if await _is_human_mode(sender):
            # Persiste a mensagem no histórico mas não responde
            history = await get_history(redis_client, sender)
            history.append({"role": "user", "content": user_message})
            await save_history(redis_client, sender, history)
            asyncio.create_task(_supabase_upsert_lead(sender))
            asyncio.create_task(_supabase_append_conversa(sender, "user", user_message,
                                                           media_info.get("type", "text") if media_info else "text"))
            log.info("[HUMAN MODE] Mensagem de %s salva mas sem resposta automática", sender)
            return

        # ── Atualiza score de intenção ─────────────────────────────────────────
        new_score, score_delta, score_breakdown = await _update_lead_score(sender, user_message)

        # ── Consulta LLM ──────────────────────────────────────────────────────
        history = await get_history(redis_client, sender)
        reply   = await run_consultant(history, user_message)

        # ── Detecta tag [FOTOS:ID] e remove do texto ─────────────────────────
        foto_match     = re.search(r'\[FOTOS:([A-Z0-9]+)\]', reply)
        imovel_id_foto = foto_match.group(1) if foto_match else None

        if foto_match:
            reply = re.sub(r'\s*\[FOTOS:[A-Z0-9]+\]\s*', ' ', reply).strip()

        # Deduplica fotos
        if imovel_id_foto:
            ja_enviou = await _fotos_ja_enviadas(sender, imovel_id_foto)
            if ja_enviou:
                log.info("Fotos de %s já enviadas para %s — ignorando duplicata", imovel_id_foto, sender)
                imovel_id_foto = None
            else:
                log.info("Tag [FOTOS:%s] detectada — fotos serão enviadas após o texto", imovel_id_foto)

        # ── Detecta tag [AUDIO] na resposta ──────────────────────────────────
        send_audio = "[AUDIO]" in reply
        reply_clean = re.sub(r'\s*\[AUDIO\]\s*', ' ', reply).strip() if send_audio else reply

        # ── Salva histórico (com lock ativo — sem race condition) ─────────────
        history.append({"role": "user",      "content": user_message})
        history.append({"role": "assistant", "content": reply_clean})
        await save_history(redis_client, sender, history)

        loop = asyncio.get_event_loop()

        # ── Detecta sinal de descarte na mensagem do lead ────────────────────
        motivo_descarte = _detect_discard_signal(user_message)
        if motivo_descarte:
            asyncio.create_task(_supabase_mark_descartado(sender, motivo_descarte))

        # ── Detecta menção a permuta/troca no lead ────────────────────────────
        asyncio.create_task(_check_and_process_permuta(sender, user_message, list(history)))

        # ── Detecta confirmação de visita na resposta da Sofia ───────────────
        if _detect_visit_confirmation(reply_clean):
            asyncio.create_task(_supabase_confirm_visit(sender, reply_clean))
            asyncio.create_task(_create_calendar_event_for_visit(sender, reply_clean, history))
            asyncio.create_task(_generate_and_send_dossie(sender, list(history)))

        # ── Extrai perfil estruturado do lead após N turnos ──────────────────────
        if len([m for m in history if m.get("role") == "user"]) >= _PROFILE_EXTRACTION_TURNS:
            asyncio.create_task(_extract_and_save_lead_profile(sender, history))

        # ── Persiste no Supabase (fire-and-forget, não bloqueia resposta) ─────
        lead_name = _extract_name_from_reply(reply_clean)
        asyncio.create_task(_supabase_upsert_lead(sender, lead_name))
        asyncio.create_task(_supabase_append_conversa(sender, "user", user_message,
                                                       media_info.get("type", "text") if media_info else "text"))
        asyncio.create_task(_supabase_append_conversa(sender, "assistant", reply_clean))

        # ── Notifica corretor se lead atingiu threshold ───────────────────────
        should_notify = await _should_notify_corretor(sender, new_score)
        if should_notify:
            # Passa histórico completo (já inclui a mensagem atual) para o resumo
            full_history = history + [
                {"role": "user",      "content": user_message},
                {"role": "assistant", "content": reply_clean},
            ]
            asyncio.create_task(_notify_corretor(
                sender, new_score, full_history, lead_name
            ))
            asyncio.create_task(_supabase_update_score(
                sender, new_score, score_breakdown,
                corretor_notified=True, corretor_score=new_score
            ))
        elif score_delta > 0:
            # Score mudou mas ainda não notifica — persiste atualização silenciosa
            asyncio.create_task(_supabase_update_score(sender, new_score, score_breakdown))

        # ── Envia resposta de texto ───────────────────────────────────────────
        await loop.run_in_executor(None, send_whatsapp_message, sender, reply_clean)
        log.info("Resposta enviada para %s: %s", sender, reply_clean[:80])

        # ── Envia PTT de áudio se Sofia marcou [AUDIO] ────────────────────────
        if send_audio and ELEVENLABS_API_KEY:
            await asyncio.sleep(0.5)
            audio_bytes = await loop.run_in_executor(None, _generate_audio_ptt, reply_clean)
            if audio_bytes:
                await loop.run_in_executor(None, _send_audio_ptt, sender, audio_bytes)

        # ── Envia fotos em seguida (uma vez por imóvel por conversa) ──────────
        if imovel_id_foto:
            await asyncio.sleep(1)
            await loop.run_in_executor(None, dispatch_photos, sender, imovel_id_foto)
            await _marcar_fotos_enviadas(sender, imovel_id_foto)
            asyncio.create_task(_update_pipeline_value(sender, imovel_id_foto))


# Alias para compatibilidade com código legado
async def _process_and_reply(sender: str, text: str):
    await _process_media_and_reply(sender, text, None)


@app.post("/human-takeover")
async def human_takeover_endpoint(request: Request):
    """
    Assume ou devolve uma conversa para o operador humano.

    Body: {
        "phone": "5511999998888",      # obrigatório
        "action": "take" | "release",  # obrigatório
        "operator": "Nome/número",     # opcional
        "note": "motivo",              # opcional
        "instance": "devlabz"          # opcional (para multi-tenant)
    }

    Header (opcional): X-Setup-Secret para autenticação

    Resposta:
      200 → { "status": "ok", "phone": "...", "action": "...", "human_mode": true/false }
      400 → parâmetros inválidos
    """
    # Auth opcional — usa o mesmo secret do setup
    secret = request.headers.get("X-Setup-Secret", "")
    if SETUP_SECRET and secret != SETUP_SECRET:
        return Response(status_code=403, content="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="JSON invalido")

    phone  = (body.get("phone") or "").strip().replace("+", "").replace(" ", "")
    action = (body.get("action") or "").strip().lower()
    operator = body.get("operator", "dashboard") or "dashboard"
    note     = body.get("note", "") or ""
    instance = body.get("instance", EVOLUTION_INSTANCE)

    if not phone:
        return Response(status_code=400, content="Campo 'phone' obrigatorio")
    if action not in ("take", "release"):
        return Response(status_code=400, content="Campo 'action' deve ser 'take' ou 'release'")

    # Resolve contexto do cliente via instance
    ctx = _build_client_context(instance)
    _client_ctx.set(ctx)

    active = (action == "take")
    await _set_human_mode_triggered_by(
        phone, active,
        triggered_by="api",
        operator=operator,
        note=note
    )

    log.info("[API] Human takeover: phone=%s action=%s operator=%s", phone, action, operator)

    return {
        "status":     "ok",
        "phone":      phone,
        "action":     action,
        "human_mode": active,
        "operator":   operator,
        "message":    f"Conversa {'assumida pelo operador' if active else 'devolvida para Sofia'}.",
    }


@app.get("/human-mode/{phone}")
async def human_mode_status(phone: str):
    """Verifica se um lead está em human mode."""
    phone = phone.strip().replace("+", "").replace(" ", "")
    is_human = await _is_human_mode(phone)
    return {"phone": phone, "human_mode": is_human}


@app.post("/new-property")
async def new_property_endpoint(request: Request):
    """
    Dispara o match de novo imóvel contra todos os leads ativos.
    Payload: JSON com os campos do imóvel (id, bairro, quartos, valor, etc.)

    Exemplo:
      curl -X POST http://vps:8001/new-property \\
           -H "Content-Type: application/json" \\
           -d '{"id":"AV010","tipo":"Apartamento","bairro":"Moema","quartos":3,"valor":"3800000"}'
    """
    try:
        imovel = await request.json()
    except Exception:
        return Response(status_code=400, content="JSON inválido")

    imovel_id = imovel.get("id", "SEM_ID")
    log.info("Novo imóvel recebido via endpoint: %s", imovel_id)

    # Atualiza cache do portfólio com o novo imóvel (para sessões futuras)
    if imovel_id != "SEM_ID":
        _portfolio_cache[imovel_id] = imovel

    # Dispara o engine em background — não bloqueia o response
    # Captura valores do contexto antes de passar para o executor (thread pool não herda contextvars)
    _client_id_snap = _ctx_client_id()
    _instance_snap  = _ctx_instance()
    _ctx_snap       = _client_ctx.get({})

    loop = asyncio.get_event_loop()
    asyncio.create_task(
        loop.run_in_executor(None, _run_new_property_engine, imovel, _client_id_snap, _instance_snap, _ctx_snap)
    )

    return {"status": "triggered", "imovel_id": imovel_id}


def _run_new_property_engine(imovel: dict, client_id: str, instance: str, ctx: dict):
    """Chama o followup_engine no mesmo processo para evitar cold-start."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        import followup_engine as fe
        onboarding_data = ONBOARDING if client_id == CLIENT_ID else {}
        fe.CLIENT_ID          = client_id
        fe.CONSULTANT_NAME    = ctx.get("nome_consultor") or onboarding_data.get("consultor", {}).get("nome", "Sofia")
        fe.IMOBILIARIA_NAME   = ctx.get("nome_imobiliaria") or onboarding_data.get("nome_imobiliaria", "Imobiliária")
        fe.ANTHROPIC_API_KEY  = ANTHROPIC_API_KEY
        fe.SUPABASE_URL       = SUPABASE_URL
        fe.SUPABASE_KEY       = SUPABASE_KEY
        fe.EVOLUTION_URL      = EVOLUTION_URL
        fe.EVOLUTION_API_KEY  = EVOLUTION_API_KEY
        fe.EVOLUTION_INSTANCE = instance
        fe.process_new_property(imovel)
    except Exception as e:
        log.error("Erro no new-property engine: %s", e)


# ─── Setup client endpoint ────────────────────────────────────────────────────
SETUP_SECRET = os.getenv("SETUP_SECRET", "imob-setup-2026")


@app.post("/setup-client")
async def setup_client(request: Request):
    """
    Recebe o JSON do formulário de onboarding e dispara o pipeline de agentes.

    Fluxo:
      1. Valida o header X-Setup-Secret
      2. Salva onboarding.json em clients/{client_id}/
      3. Dispara setup_pipeline.py em background
      4. Retorna job_id para acompanhamento

    Chamado automaticamente pelo onboarding_form.html ao clicar em "Iniciar configuração".
    """
    # Autenticação simples por header
    secret = request.headers.get("X-Setup-Secret", "")
    if secret != SETUP_SECRET:
        log.warning("setup-client: token inválido")
        return Response(status_code=403, content="Acesso não autorizado")

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400, content="JSON inválido")

    client_id = data.get("client_id", "").strip()
    if not client_id:
        return Response(status_code=400, content="client_id obrigatório")

    log.info("Setup iniciado para cliente '%s'", client_id)

    # Salva onboarding.json
    import uuid
    job_id = str(uuid.uuid4())[:8]
    try:
        base_dir = Path(__file__).parent
        client_dir = base_dir / "clients" / client_id
        client_dir.mkdir(parents=True, exist_ok=True)
        onboarding_path = client_dir / "onboarding.json"
        onboarding_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        log.info("onboarding.json salvo em %s", onboarding_path)
    except Exception as e:
        log.error("Erro ao salvar onboarding.json: %s", e)
        return Response(status_code=500, content=f"Erro ao salvar configuração: {e}")

    # Dispara setup_pipeline em background (não bloqueia response)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_setup_pipeline, client_id, str(onboarding_path), job_id)

    return {
        "status":    "accepted",
        "client_id": client_id,
        "job_id":    job_id,
        "message":   "Configuração iniciada. Você receberá uma confirmação em breve.",
    }


IMOB_DIR = os.getenv("IMOB_DIR", "/opt/ImobOne-v2")

# Human takeover
HUMAN_MODE_TTL_HOURS = int(os.getenv("HUMAN_MODE_TTL_HOURS", "4"))   # auto-release após Nh sem atividade do operador


def _run_setup_pipeline(client_id: str, onboarding_path: str, job_id: str):
    """Executa setup_pipeline.py para o novo cliente em thread separada."""
    import subprocess
    try:
        log.info("[job:%s] Iniciando setup_pipeline para '%s'", job_id, client_id)
        venv_python = "/opt/webhook-venv/bin/python3"
        pipeline_script = str(Path(IMOB_DIR) / "setup_pipeline.py")

        result = subprocess.run(
            [venv_python, pipeline_script, "--client", client_id, "--skip", "qa_integration"],
            capture_output=True,
            text=True,
            timeout=900,  # 15 minutos máximo
            cwd=IMOB_DIR,
        )
        if result.returncode == 0:
            log.info("[job:%s] Setup concluído com sucesso para '%s'", job_id, client_id)
        else:
            log.error("[job:%s] Setup falhou para '%s':\n%s", job_id, client_id, result.stderr[-2000:])
    except subprocess.TimeoutExpired:
        log.error("[job:%s] Setup timeout para '%s'", job_id, client_id)
    except Exception as e:
        log.error("[job:%s] Erro no setup_pipeline: %s", job_id, e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
