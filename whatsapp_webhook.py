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

Persistência:
  - Redis (hot): histórico de conversa por sender, dedup de fotos
  - Supabase (cold): tabela leads + tabela conversas por cliente
"""

import asyncio
import base64
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
        onboarding = carregar_onboarding(CLIENT_ID)
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
        return carregar_onboarding(CLIENT_ID)
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
        onboarding = carregar_onboarding(CLIENT_ID)
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
_load_portfolio_dict()   # pré-carrega


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


async def _supabase_upsert_lead(sender: str, name: str | None = None):
    """Cria ou atualiza registro do lead no Supabase."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        loop = asyncio.get_event_loop()
        data = {
            "client_id":        CLIENT_ID,
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
                  "client_id":  CLIENT_ID,
                  "lead_phone": sender,
                  "role":       role,
                  "content":    content[:4000],  # trunca mensagens muito longas
                  "media_type": media_type,
              })
              .execute()
        ))
    except Exception as e:
        log.warning("Falha ao append conversa Supabase: %s", e)


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
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
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
        f"{EVOLUTION_URL}/message/sendWhatsAppAudio/{EVOLUTION_INSTANCE}",
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
        f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}",
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
        + SYSTEM_PROMPT
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
        f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
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
        f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}",
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
        "status":             "ok",
        "instance":           EVOLUTION_INSTANCE,
        "client":             CLIENT_ID,
        "portfolio_size":     len(_portfolio_cache),
        "foto_config":        ONBOARDING.get("midia", {}).get("foto_config", "não configurado"),
        "audio_transcription": bool(OPENAI_API_KEY),
        "supabase":           bool(SUPABASE_URL and SUPABASE_KEY),
        "elevenlabs_tts":     bool(ELEVENLABS_API_KEY),
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


# ─── Pipeline principal — com lock por sender ─────────────────────────────────
async def _process_media_and_reply(sender: str, text: str, media_info: dict | None):
    """
    Processa entrada (texto, áudio ou imagem), chama o consultor LLM e responde.
    Lock por sender garante que mensagens do mesmo número são processadas em série,
    evitando race condition no histórico Redis.
    """
    async with _sender_locks[sender]:
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

        # ── Persiste no Supabase (fire-and-forget, não bloqueia resposta) ─────
        lead_name = _extract_name_from_reply(reply_clean)
        asyncio.create_task(_supabase_upsert_lead(sender, lead_name))
        asyncio.create_task(_supabase_append_conversa(sender, "user", user_message,
                                                       media_info.get("type", "text") if media_info else "text"))
        asyncio.create_task(_supabase_append_conversa(sender, "assistant", reply_clean))

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


# Alias para compatibilidade com código legado
async def _process_and_reply(sender: str, text: str):
    await _process_media_and_reply(sender, text, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
