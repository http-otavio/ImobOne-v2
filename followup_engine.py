#!/usr/bin/env python3
"""
followup_engine.py — Engine de follow-up automatizado ImobOne

Cenários implementados:

  1. SILÊNCIO 24h  — lead parou de responder, Sofia foi a última a falar
  2. SILÊNCIO 48h  — segundo toque, ângulo diferente
  3. SILÊNCIO 7d   — toque leve de nutrição de longo prazo
  4. PÓS-VISITA    — 24h após visita confirmada sem retorno do lead
  5. NOVO IMÓVEL   — imóvel novo no portfólio → match com leads compatíveis
                     e disparo de outreach personalizado por IA

Arquitetura:
  - Lê leads e histórico de conversas do Supabase
  - Gera mensagem personalizada via Claude Haiku (não genérica)
  - Envia via Evolution API (WhatsApp)
  - Registra em followup_events (idempotência via TTL de cada cenário)
  - Respeita janela de horário (08h–21h, configurável)

Uso:
  # Roda silêncio (padrão — chamado pelo systemd timer a cada hora)
  python3 followup_engine.py

  # Dispara match de novo imóvel
  python3 followup_engine.py --new-property '{"id":"AV010","bairro":"Moema",...}'

  # Dry-run (mostra leads elegíveis sem enviar)
  python3 followup_engine.py --dry-run
"""

import csv
import json
import logging
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import anthropic

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] followup: %(message)s",
)
log = logging.getLogger("followup_engine")

# ─── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "")
EVOLUTION_URL      = os.getenv("EVOLUTION_URL", "")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "devlabz")
CLIENT_ID          = os.getenv("DEMO_CLIENT_ID", "demo_imobiliaria_vendas")
CONSULTANT_NAME    = os.getenv("CONSULTANT_NAME", "Sofia")
IMOBILIARIA_NAME   = os.getenv("IMOBILIARIA_NAME", "Ávora Imóveis")

# Janela de horário permitida (fuso Brasília UTC-3)
SEND_HOUR_START = int(os.getenv("FOLLOWUP_HOUR_START", "8"))   # 08:00
SEND_HOUR_END   = int(os.getenv("FOLLOWUP_HOUR_END", "21"))    # 21:00

# TTLs de cada cenário (em horas) — evita reenvio no mesmo período
TTL = {
    "silence_24h":  72,    # não reenviar por 3 dias
    "silence_48h":  120,   # não reenviar por 5 dias
    "silence_7d":   720,   # não reenviar por 30 dias
    "post_visit":   168,   # não reenviar por 7 dias
    "new_property": 99999, # nunca reenviar o mesmo imóvel para o mesmo lead
    "crm_reactivation": 168,
}

# Thresholds de silêncio
SILENCE_24H = timedelta(hours=24)
SILENCE_48H = timedelta(hours=48)
SILENCE_7D  = timedelta(days=7)


# ─── SSL helper ──────────────────────────────────────────────────────────────
def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─── Supabase REST client (síncrono) ─────────────────────────────────────────
def _sb_get(path: str, params: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{path}{'?' + params if params else ''}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase GET %s: %s", path, e)
        return []


def _sb_post(path: str, data: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15):
            return True
    except Exception as e:
        log.error("Supabase POST %s: %s", path, e)
        return False


# ─── WhatsApp sender ──────────────────────────────────────────────────────────
def send_whatsapp(to: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        log.info("[DRY-RUN] Enviaria para %s:\n%s", to, text)
        return True
    payload = json.dumps({"number": to, "text": text}).encode()
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
        data=payload,
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            log.info("WhatsApp enviado → %s | HTTP %s", to, r.status)
            return True
    except Exception as e:
        log.error("Falha WhatsApp → %s: %s", to, e)
        return False


# ─── Portfolio ─────────────────────────────────────────────────────────────────
_portfolio_cache: dict[str, dict] = {}


def load_portfolio() -> dict[str, dict]:
    global _portfolio_cache
    if _portfolio_cache:
        return _portfolio_cache
    candidates = [
        Path("/opt/ImobOne-v2/clients/demo_imobiliaria_vendas/portfolio.csv"),
        Path("/opt/ImobOne-v2/portfolio.csv"),
        Path("/opt/portfolio.csv"),
    ]
    for c in candidates:
        if c.exists():
            with open(c, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if iid := row.get("id", "").strip():
                        _portfolio_cache[iid] = row
            log.info("Portfólio carregado: %d imóveis de %s", len(_portfolio_cache), c)
            return _portfolio_cache
    log.warning("Portfólio CSV não encontrado — match de imóvel sem contexto de carteira")
    return {}


def portfolio_summary() -> str:
    """Sumário do portfólio para contexto do LLM."""
    portfolio = load_portfolio()
    if not portfolio:
        return ""
    linhas = []
    for iid, row in list(portfolio.items())[:20]:
        linhas.append(
            f"- {iid}: {row.get('tipo','?')} em {row.get('bairro','?')} | "
            f"{row.get('quartos','?')} quartos | R$ {row.get('valor','?')}"
        )
    return "\n".join(linhas)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def now_br() -> datetime:
    return datetime.now(timezone(timedelta(hours=-3)))


def is_send_window() -> bool:
    h = now_br().hour
    return SEND_HOUR_START <= h < SEND_HOUR_END


def parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def format_history(conversas: list) -> str:
    linhas = []
    for c in conversas[-24:]:
        role = "Lead" if c.get("role") == "user" else CONSULTANT_NAME
        linhas.append(f"{role}: {c.get('content', '')[:300]}")
    return "\n".join(linhas) if linhas else "(sem histórico registrado)"


# ─── Follow-up event tracking ─────────────────────────────────────────────────
def was_sent(lead_phone: str, event_type: str, imovel_id: Optional[str] = None) -> bool:
    """Verifica se já enviamos esse follow-up dentro do TTL configurado."""
    import urllib.parse
    since = (datetime.now(timezone.utc) - timedelta(hours=TTL[event_type])).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = (
        f"client_id=eq.{urllib.parse.quote(CLIENT_ID)}"
        f"&lead_phone=eq.{urllib.parse.quote(lead_phone)}"
        f"&event_type=eq.{urllib.parse.quote(event_type)}"
        f"&sent_at=gte.{since}"
        f"&select=id"
    )
    if imovel_id:
        params += f"&imovel_id=eq.{urllib.parse.quote(imovel_id)}"
    return len(_sb_get("followup_events", params)) > 0


def record_sent(lead_phone: str, event_type: str, message: str, imovel_id: Optional[str] = None):
    data = {
        "client_id":    CLIENT_ID,
        "lead_phone":   lead_phone,
        "event_type":   event_type,
        "message_sent": message[:2000],
    }
    if imovel_id:
        data["imovel_id"] = imovel_id
    _sb_post("followup_events", data)


# ─── Geração de mensagem via Haiku ───────────────────────────────────────────
_SCENARIO_PROMPTS = {
    "crm_reactivation": (
        "Este lead está parado há mais de 30 dias sem interação.\n"
        "Com base no histórico (se houver) e nos sinais de interesse registrados, "
        "identifique o que ele estava buscando e escreva uma mensagem de reativação.\n"
        "Tom: leve, sem pressão, como se fosse uma atualização natural do mercado. "
        "Se não há histórico: apresente-se brevemente e mencione uma oportunidade relevante "
        "do portfólio para o perfil inferido.\n"
        "Máximo 2 frases. Nunca mencione que ele ficou sumido ou que faz tempo."
    ),
    "silence_24h": (
        "O lead parou de responder há cerca de 24 horas. Sofia enviou a última mensagem.\n"
        "Escreva UMA mensagem curta retomando a conversa de forma natural.\n"
        "Referencie algo específico do que foi discutido — um imóvel, uma dúvida, uma preferência mencionada.\n"
        "Tom: leve, sem pressão. Não mencione que faz 24 horas."
    ),
    "silence_48h": (
        "O lead não responde há 48 horas. Já houve um toque ontem.\n"
        "Aborde com um ângulo novo: uma informação útil sobre o que ele busca, "
        "uma pergunta diferente, ou algo que ficou sem resposta na conversa.\n"
        "Tom: consultivo, discreto. Máximo 2 frases."
    ),
    "silence_7d": (
        "O lead está inativo há 7 dias. Tom exclusivamente de nutrição — sem pressão comercial.\n"
        "Mensagem de no máximo 2 frases, deixando porta aberta.\n"
        "Não mencione os contatos anteriores. Não force imóvel específico."
    ),
    "post_visit": (
        "O lead confirmou uma visita a um imóvel e não deu retorno desde então.\n"
        "Escreva uma mensagem verificando a impressão dele sobre o imóvel visitado.\n"
        "Tom: consultivo, não de venda. Pergunte sobre a experiência, não sobre a decisão."
    ),
}


def generate_message(
    scenario: str,
    lead_name: Optional[str],
    history: str,
    score_breakdown: dict,
    extra_context: str = "",
    dry_run: bool = False,
) -> Optional[str]:
    """
    Gera mensagem personalizada via Claude Haiku.
    Retorna None se Haiku decidir não enviar (SKIP) ou em caso de erro.
    """
    nome = lead_name or "lead"

    if scenario == "new_property":
        instruction = (
            f"Um novo imóvel entrou no portfólio da {IMOBILIARIA_NAME}:\n{extra_context}\n\n"
            "Passo 1 — avalie internamente (não escreva) a compatibilidade com o perfil do lead (1-10).\n"
            "Passo 2 — se nota ≥ 7: escreva UMA mensagem WhatsApp personalizada e concisa apresentando "
            "o imóvel, conectando diretamente com algo que o lead mencionou na conversa. "
            "A mensagem deve parecer natural, não um anúncio. Inclua um detalhe específico do histórico.\n"
            "Passo 2 — se nota < 7: responda apenas a palavra SKIP.\n"
            "IMPORTANTE: A resposta deve conter APENAS a mensagem final. "
            "Não inclua a nota, não inclua prefixos como 'Compatibilidade:' ou 'Mensagem:'."
        )
    else:
        instruction = _SCENARIO_PROMPTS.get(scenario, "Escreva uma mensagem de follow-up.")

    sinais = ", ".join(f"{k}:{v}" for k, v in score_breakdown.items()) if score_breakdown else "não mapeados"

    prompt = f"""Você é {CONSULTANT_NAME}, consultora de imóveis de alto padrão da {IMOBILIARIA_NAME}.
Tom obrigatório: sofisticado, preciso, pessoal. NUNCA genérico. NUNCA mencione que é IA.
Use o nome do lead quando disponível.

LEAD: {nome}
SINAIS DE INTERESSE: {sinais}

HISTÓRICO DA CONVERSA:
{history}

TAREFA:
{instruction}

Responda APENAS com o texto da mensagem WhatsApp. Sem aspas, sem introdução, sem explicação.
Máximo 3 frases."""

    if dry_run:
        log.info("[DRY-RUN] Prompt para %s:\n%s", scenario, prompt[:400])

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = response.content[0].text.strip()
        if msg.upper().startswith("SKIP"):
            log.debug("Haiku retornou SKIP para %s — lead sem match", scenario)
            return None
        return msg
    except Exception as e:
        log.error("Falha ao gerar mensagem Haiku (%s): %s", scenario, e)
        return None


# ─── Cenários 1–4: Follow-up por silêncio ────────────────────────────────────
def process_silence_followups(dry_run: bool = False):
    log.info("=== Silêncio: buscando leads elegíveis ===")
    now = datetime.now(timezone.utc)

    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&intention_score=gt.0"
            f"&select=lead_phone,lead_name,intention_score,score_breakdown,"
            f"ultima_interacao,visita_agendada,visita_confirmada_at"
        )
    )
    log.info("%d leads com histórico encontrados", len(leads))

    enviados = 0
    for lead in leads:
        phone     = lead.get("lead_phone", "")
        name      = lead.get("lead_name")
        breakdown = lead.get("score_breakdown") or {}
        ts_str    = lead.get("ultima_interacao", "")

        if not phone or not ts_str:
            continue

        ts = parse_ts(ts_str)
        if not ts:
            continue

        silence = now - ts

        # Busca histórico de conversas para verificar quem falou por último
        conversas = _sb_get(
            "conversas",
            (
                f"client_id=eq.{CLIENT_ID}"
                f"&lead_phone=eq.{phone}"
                f"&order=created_at.asc"
                f"&limit=30"
            )
        )

        # Se o lead foi o último a falar, ele não está em silêncio — Sofia está
        if conversas and conversas[-1].get("role") == "user":
            log.debug("%s — lead foi o último a falar, sem follow-up", phone)
            continue

        history = format_history(conversas)

        # Determina o cenário mais adequado (prioridade: mais grave primeiro)
        scenario = None

        if silence >= SILENCE_7D:
            if not was_sent(phone, "silence_7d"):
                scenario = "silence_7d"

        elif silence >= SILENCE_48H:
            if not was_sent(phone, "silence_48h"):
                scenario = "silence_48h"

        elif silence >= SILENCE_24H:
            # Pós-visita tem prioridade sobre silêncio genérico
            if lead.get("visita_agendada") and not was_sent(phone, "post_visit"):
                scenario = "post_visit"
            elif not was_sent(phone, "silence_24h"):
                scenario = "silence_24h"

        if not scenario:
            continue

        log.info("Lead %s → %s (silêncio: %s)", phone, scenario, str(silence).split(".")[0])

        msg = generate_message(scenario, name, history, breakdown, dry_run=dry_run)
        if not msg:
            continue

        sent = send_whatsapp(phone, msg, dry_run=dry_run)
        if sent:
            if not dry_run:
                record_sent(phone, scenario, msg)
            enviados += 1

    log.info("Silêncio: %d follow-ups enviados", enviados)
    return enviados


# ─── Cenário CRM: reativação de leads frios (> 30 dias) ─────────────────────
def process_crm_reactivation(dry_run: bool = False):
    """
    Busca leads com intention_score = 0 (vieram de CRM sem histórico com Sofia)
    ou leads que não interagem há mais de 30 dias, independente do score.
    Gera mensagem de reativação personalizada com base no portfólio atual.
    """
    log.info("=== CRM Reativação: buscando leads frios ===")
    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    # Leads sem interação há 30+ dias (inclui score 0 do CRM)
    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&ultima_interacao=lte.{cutoff_30d}"
            f"&select=lead_phone,lead_name,intention_score,score_breakdown,ultima_interacao"
        )
    )
    log.info("%d leads frios encontrados (> 30 dias)", len(leads))

    # Sumário do portfólio para enriquecer a mensagem quando não há histórico
    port_summary = portfolio_summary()
    enviados = 0

    for lead in leads:
        phone     = lead.get("lead_phone", "")
        name      = lead.get("lead_name")
        breakdown = lead.get("score_breakdown") or {}

        if not phone:
            continue

        if was_sent(phone, "crm_reactivation"):
            continue

        # Busca histórico se existir
        conversas = _sb_get(
            "conversas",
            (
                f"client_id=eq.{CLIENT_ID}"
                f"&lead_phone=eq.{phone}"
                f"&order=created_at.asc"
                f"&limit=20"
            )
        )
        history = format_history(conversas)

        # Contexto extra: portfólio para leads sem histórico
        extra = ""
        if not conversas and port_summary:
            extra = f"\nPortfólio atual (para referência):\n{port_summary[:600]}"

        msg = generate_message(
            "crm_reactivation", name, history, breakdown,
            extra_context=extra, dry_run=dry_run
        )
        if not msg:
            continue

        sent = send_whatsapp(phone, msg, dry_run=dry_run)
        if sent:
            if not dry_run:
                record_sent(phone, "crm_reactivation", msg)
            enviados += 1

    log.info("CRM reativação: %d mensagens enviadas", enviados)
    return enviados


# ─── Cenário 5: Novo imóvel no portfólio ─────────────────────────────────────
def process_new_property(imovel: dict, dry_run: bool = False):
    """
    Dado um novo imóvel, identifica leads com perfil compatível e dispara
    outreach personalizado para cada um.
    """
    imovel_id = imovel.get("id", "SEM_ID")
    imovel_desc = (
        f"ID: {imovel_id} | "
        f"{imovel.get('tipo', 'Apartamento')} em {imovel.get('bairro', '?')} | "
        f"{imovel.get('quartos', '?')} quartos | "
        f"{imovel.get('area_m2', '?')}m² | "
        f"R$ {imovel.get('valor', '?')} | "
        f"{imovel.get('diferenciais', '')}"
    )

    log.info("=== Novo imóvel: %s ===", imovel_desc)

    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&intention_score=gt.0"
            f"&select=lead_phone,lead_name,score_breakdown"
        )
    )
    log.info("Avaliando %d leads para match com %s", len(leads), imovel_id)

    notificados = 0
    sem_match   = 0

    for lead in leads:
        phone     = lead.get("lead_phone", "")
        name      = lead.get("lead_name")
        breakdown = lead.get("score_breakdown") or {}

        if not phone:
            continue

        # Idempotência — nunca re-enviar o mesmo imóvel para o mesmo lead
        if was_sent(phone, "new_property", imovel_id=imovel_id):
            log.debug("%s já notificado sobre %s", phone, imovel_id)
            continue

        # Busca histórico para personalizar o match
        conversas = _sb_get(
            "conversas",
            (
                f"client_id=eq.{CLIENT_ID}"
                f"&lead_phone=eq.{phone}"
                f"&order=created_at.asc"
                f"&limit=20"
            )
        )
        history = format_history(conversas)

        msg = generate_message(
            "new_property", name, history, breakdown,
            extra_context=imovel_desc, dry_run=dry_run
        )

        if not msg:
            sem_match += 1
            continue

        sent = send_whatsapp(phone, msg, dry_run=dry_run)
        if sent:
            if not dry_run:
                record_sent(phone, "new_property", msg, imovel_id=imovel_id)
            notificados += 1

    log.info(
        "Novo imóvel %s: %d notificados, %d sem match",
        imovel_id, notificados, sem_match
    )
    return notificados


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv

    log.info(
        "Follow-up engine iniciado | cliente: %s | dry_run: %s",
        CLIENT_ID, dry_run
    )

    if not dry_run and not is_send_window():
        log.info(
            "Fora da janela de envio (%dh–%dh Brasília). Encerrando sem envios.",
            SEND_HOUR_START, SEND_HOUR_END
        )
        return

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL/KEY não configurados.")
        sys.exit(1)

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY não configurada.")
        sys.exit(1)

    load_portfolio()

    if not EVOLUTION_URL or not EVOLUTION_API_KEY:
        log.error("EVOLUTION_URL/KEY não configurados.")
        sys.exit(1)

    # Modo de operação
    if "--new-property" in sys.argv:
        # Disparo de novo imóvel: python3 followup_engine.py --new-property '{...}'
        idx = sys.argv.index("--new-property")
        try:
            imovel = json.loads(sys.argv[idx + 1])
        except (IndexError, json.JSONDecodeError) as e:
            log.error("Argumento --new-property inválido: %s", e)
            sys.exit(1)
        process_new_property(imovel, dry_run=dry_run)

    elif "--crm" in sys.argv:
        # Reativação de CRM: roda manualmente ou sábado de manhã
        process_crm_reactivation(dry_run=dry_run)

    else:
        # Padrão (chamado pelo timer a cada hora): silêncio
        process_silence_followups(dry_run=dry_run)

    log.info("Follow-up engine concluído.")


if __name__ == "__main__":
    main()
