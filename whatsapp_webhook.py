"""
Webhook server para demo do consultor de IA via WhatsApp (Evolution API).

Fluxo:
  Lead envia mensagem → Evolution API → POST /webhook → consultor LLM → Evolution API → resposta
"""

import asyncio
import json
import logging
import os
import ssl
import urllib.request
from collections import defaultdict
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import anthropic
from fastapi import FastAPI, Request, Response
from pathlib import Path

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("whatsapp_webhook")

# ─── Config ──────────────────────────────────────────────────────────────────
EVOLUTION_URL     = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "79ffc1f3960f03a27a67e2b1e678d98b")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
REDIS_URL          = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
CLIENT_ID          = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
MAX_HISTORY        = 20   # turnos máximos por conversa

# ─── Estado em memória (fallback se Redis indisponível) ──────────────────────
_memory_history: dict[str, list] = defaultdict(list)

# ─── Carrega system prompt do consultor ──────────────────────────────────────
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
            # Substitui placeholders do demo
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


SYSTEM_PROMPT = _load_system_prompt()

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
    # Mantém apenas os últimos MAX_HISTORY turnos
    history = history[-MAX_HISTORY:]
    if redis_client:
        try:
            await redis_client.set(
                f"whatsapp:history:{sender}",
                json.dumps(history, ensure_ascii=False),
                ex=86400,  # 24h TTL
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
def send_whatsapp_message(to: str, text: str):
    """Envio síncrono via urllib (não bloqueia o event loop por muito tempo)."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    payload = json.dumps({"number": to, "text": text}).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
        data=payload,
        headers={
            "apikey": EVOLUTION_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as r:
            log.info("Mensagem enviada para %s | status HTTP %s", to, r.status)
    except Exception as e:
        log.error("Falha ao enviar mensagem para %s: %s", to, e)


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
    return {"status": "ok", "instance": EVOLUTION_INSTANCE, "client": CLIENT_ID}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    event = body.get("event", "")

    # Ignora tudo que não é mensagem recebida
    if event != "messages.upsert":
        return Response(status_code=200)

    data = body.get("data", {})

    # Ignora mensagens enviadas pelo próprio bot
    if data.get("key", {}).get("fromMe"):
        return Response(status_code=200)

    # Ignora mensagens de grupo
    remote_jid = data.get("key", {}).get("remoteJid", "")
    if "@g.us" in remote_jid:
        return Response(status_code=200)

    # Extrai número do remetente e texto
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

    # Processa em background para não travar o webhook
    asyncio.create_task(_process_and_reply(sender, text))

    return Response(status_code=200)


async def _process_and_reply(sender: str, text: str):
    history = await get_history(redis_client, sender)
    reply = await run_consultant(history, text)

    # Atualiza histórico
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    await save_history(redis_client, sender, history)

    # Envia resposta (síncrono em thread para não bloquear)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_whatsapp_message, sender, reply)
    log.info("Resposta enviada para %s: %s", sender, reply[:80])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
