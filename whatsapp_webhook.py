"""
Webhook server para demo do consultor de IA via WhatsApp (Evolution API).

Fluxo:
  Lead envia mensagem → Evolution API → POST /webhook → consultor LLM → Evolution API → resposta
  Se resposta contiver [FOTOS:ID] → envia fotos + link via Evolution API sendMedia
"""

import asyncio
import csv
import json
import logging
import os
import re
import ssl
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
EVOLUTION_URL      = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "79ffc1f3960f03a27a67e2b1e678d98b")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
REDIS_URL          = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
CLIENT_ID          = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
MAX_HISTORY        = 20   # turnos máximos por conversa

# ─── Estado em memória (fallback se Redis indisponível) ──────────────────────
_memory_history: dict[str, list] = defaultdict(list)

# ─── Portfolio em memória ─────────────────────────────────────────────────────
_portfolio_cache: dict[str, dict] = {}


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


# ─── Consultor LLM ───────────────────────────────────────────────────────────
async def run_consultant(history: list[dict], user_message: str) -> str:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    messages = history + [{"role": "user", "content": user_message}]
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        log.error("Erro no consultor LLM: %s", e)
        return "Desculpe, estou com uma instabilidade no momento. Pode repetir sua pergunta em instantes?"


# ─── Envio via Evolution API ─────────────────────────────────────────────────
def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
        "status": "ok",
        "instance": EVOLUTION_INSTANCE,
        "client": CLIENT_ID,
        "portfolio_size": len(_portfolio_cache),
        "foto_config": ONBOARDING.get("midia", {}).get("foto_config", "não configurado"),
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

    sender = remote_jid.replace("@s.whatsapp.net", "")
    message_obj = data.get("message", {})
    text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()

    if not text:
        return Response(status_code=200)

    log.info("Mensagem recebida de %s: %s", sender, text[:80])
    asyncio.create_task(_process_and_reply(sender, text))
    return Response(status_code=200)


async def _fotos_ja_enviadas(sender: str, imovel_id: str) -> bool:
    """Verifica se fotos desse imóvel já foram enviadas para esse lead."""
    key = f"whatsapp:fotos_enviadas:{sender}"
    if redis_client:
        try:
            return await redis_client.sismember(key, imovel_id)
        except Exception:
            pass
    return imovel_id in _memory_history.get(f"fotos:{sender}", set())


async def _marcar_fotos_enviadas(sender: str, imovel_id: str):
    """Registra que fotos desse imóvel já foram enviadas para esse lead."""
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


async def _process_and_reply(sender: str, text: str):
    history = await get_history(redis_client, sender)
    reply   = await run_consultant(history, text)

    # ── Detecta tag [FOTOS:ID] e remove do texto ──────────────────────────
    foto_match = re.search(r'\[FOTOS:([A-Z0-9]+)\]', reply)
    imovel_id_foto = foto_match.group(1) if foto_match else None

    # Remove a tag do texto independentemente de enviar ou não
    if foto_match:
        reply = re.sub(r'\s*\[FOTOS:[A-Z0-9]+\]\s*', ' ', reply).strip()

    # Deduplica: só envia fotos se ainda não enviou para esse imóvel nessa conversa
    if imovel_id_foto:
        ja_enviou = await _fotos_ja_enviadas(sender, imovel_id_foto)
        if ja_enviou:
            log.info("Fotos de %s já enviadas para %s — ignorando duplicata", imovel_id_foto, sender)
            imovel_id_foto = None
        else:
            log.info("Tag [FOTOS:%s] detectada — fotos serão enviadas após o texto", imovel_id_foto)

    # Atualiza histórico (sem a tag)
    history.append({"role": "user",      "content": text})
    history.append({"role": "assistant", "content": reply})
    await save_history(redis_client, sender, history)

    loop = asyncio.get_event_loop()

    # Envia texto da resposta
    await loop.run_in_executor(None, send_whatsapp_message, sender, reply)
    log.info("Resposta enviada para %s: %s", sender, reply[:80])

    # Envia fotos em seguida (apenas uma vez por imóvel por conversa)
    if imovel_id_foto:
        await asyncio.sleep(1)
        await loop.run_in_executor(None, dispatch_photos, sender, imovel_id_foto)
        await _marcar_fotos_enviadas(sender, imovel_id_foto)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
