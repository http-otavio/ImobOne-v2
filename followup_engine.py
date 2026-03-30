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
    "silence_24h":      72,    # não reenviar por 3 dias
    "silence_48h":      120,   # não reenviar por 5 dias
    "silence_7d":       720,   # não reenviar por 30 dias
    "post_visit":       168,   # não reenviar por 7 dias
    "new_property":     99999, # nunca reenviar o mesmo imóvel para o mesmo lead
    "crm_reactivation": 168,
    "discard_30d":      99999, # enviado uma vez, nunca repetir
    "discard_60d":      99999,
    "discard_90d":      99999,
    "pre_visit_reminder": 99999,  # enviado uma vez, nunca repetir
    "weekly_report":    168,      # re-enviar no ciclo seguinte (1 semana)
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
_DISCARD_PROMPTS = {
    "discard_30d": (
        "Este lead disse que não era o momento ou que ia esperar, há cerca de 30 dias.\n"
        "Escreva UMA mensagem extremamente leve — não comercial, não pushy.\n"
        "Ângulo: algo mudou no mercado ou no portfólio que pode ser relevante para ele.\n"
        "Exemplos de abordagem: nova opção que encaixa no perfil dele, contexto de mercado, "
        "queda de juros, novo lançamento na região de interesse.\n"
        "Tom: quase informal, como uma dica de amigo que trabalha no mercado. "
        "Máximo 2 frases. Nunca mencione que ele disse 'não' antes."
    ),
    "discard_60d": (
        "Este lead está inativo há 60 dias após sinalizar que não era o momento.\n"
        "Escreva UMA mensagem com ângulo completamente diferente do primeiro toque.\n"
        "Aborde pelo lado do contexto pessoal/temporal: o mercado mudou, "
        "uma oportunidade específica surgiu, ou simplesmente um 'check-in' humano.\n"
        "Tom: genuíno, sem agenda de vendas aparente. 1-2 frases."
    ),
    "discard_90d": (
        "Este lead está inativo há 90 dias. Último toque desta sequência.\n"
        "Mensagem de 'porta sempre aberta' — deixa o caminho livre para quando ele estiver pronto.\n"
        "Sem referência a imóveis específicos. Sem pressão. Tom: humano e respeitoso.\n"
        "Ideia: 'quando você estiver pronto para retomar, estarei aqui com as melhores opções.'\n"
        "Máximo 2 frases. Depois disso, lead vai para nutrição passiva (sem mais toques ativos)."
    ),
}

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

    if scenario in _DISCARD_PROMPTS:
        instruction = _DISCARD_PROMPTS[scenario]
    elif scenario == "new_property":
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


# ─── Cenário 5: Nutrição de descartados (30/60/90 dias) ─────────────────────
def process_discard_nurture(dry_run: bool = False):
    """
    Sequência de nutrição para leads que sinalizaram descarte.
    Cada etapa tem um ângulo diferente e é enviada uma única vez.

    Lógica de progressão:
      descartado_em + 30d → discard_30d (se não enviado)
      descartado_em + 60d → discard_60d (se 30d já foi e não enviado)
      descartado_em + 90d → discard_90d (se 60d já foi e não enviado)
    """
    log.info("=== Nutrição de descartados: buscando leads ===")
    now = datetime.now(timezone.utc)

    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&descartado=eq.true"
            f"&descartado_em=not.is.null"
            f"&select=lead_phone,lead_name,score_breakdown,descartado_em,motivo_descarte"
        )
    )
    log.info("%d leads descartados encontrados", len(leads))

    enviados = 0
    for lead in leads:
        phone        = lead.get("lead_phone", "")
        name         = lead.get("lead_name")
        breakdown    = lead.get("score_breakdown") or {}
        motivo       = lead.get("motivo_descarte", "")
        descartado_em = parse_ts(lead.get("descartado_em", ""))

        if not phone or not descartado_em:
            continue

        dias_desde_descarte = (now - descartado_em).days

        # Determina qual etapa deve ser enviada
        # Regra: só avança para a próxima se a anterior já foi enviada
        etapa = None
        if dias_desde_descarte >= 90 and was_sent(phone, "discard_60d") and not was_sent(phone, "discard_90d"):
            etapa = "discard_90d"
        elif dias_desde_descarte >= 60 and was_sent(phone, "discard_30d") and not was_sent(phone, "discard_60d"):
            etapa = "discard_60d"
        elif dias_desde_descarte >= 30 and not was_sent(phone, "discard_30d"):
            etapa = "discard_30d"

        if not etapa:
            continue

        log.info("Lead %s → %s (descartado há %dd, motivo: %s)", phone, etapa, dias_desde_descarte, motivo)

        # Busca histórico para personalizar o ângulo
        conversas = _sb_get(
            "conversas",
            f"client_id=eq.{CLIENT_ID}&lead_phone=eq.{phone}&order=created_at.asc&limit=20"
        )
        history = format_history(conversas)

        # Contexto extra: portfólio resumido para referenciar oportunidades
        extra = portfolio_summary()[:400] if portfolio_summary() else ""

        msg = generate_message(etapa, name, history, breakdown, extra_context=extra, dry_run=dry_run)
        if not msg:
            continue

        sent = send_whatsapp(phone, msg, dry_run=dry_run)
        if sent:
            if not dry_run:
                record_sent(phone, etapa, msg)
            enviados += 1

    log.info("Nutrição de descartados: %d mensagens enviadas", enviados)
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




# ─── Cenário 8: Lembrete pré-visita (24h antes) ─────────────────────────────
def process_pre_visit_reminders(dry_run: bool = False):
    """
    Envia lembrete ao lead 24h antes da visita confirmada:
    - Mensagem de confirmação e detalhes
    - Envia briefing completo do lead ao corretor (nome, perfil, sinais, histórico)
    
    Depende do campo visit_scheduled_at em leads (preenchido quando Sofia confirma visita).
    """
    log.info("=== Lembrete pré-visita: buscando leads com visita marcada ===")
    now = datetime.now(timezone.utc)

    # Janela: visitas entre 20h e 28h a partir de agora (24h ± 4h)
    window_start = (now + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end   = (now + timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    import urllib.parse

    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&visita_agendada=eq.true"
            f"&visit_scheduled_at=gte.{urllib.parse.quote(window_start)}"
            f"&visit_scheduled_at=lte.{urllib.parse.quote(window_end)}"
            f"&visit_reminder_sent=eq.false"
            f"&select=lead_phone,lead_name,intention_score,score_breakdown,visit_scheduled_at"
        )
    )
    log.info("%d leads com visita nas próximas 24-28h", len(leads))

    enviados = 0
    for lead in leads:
        phone         = lead.get("lead_phone", "")
        name          = lead.get("lead_name") or "cliente"
        breakdown     = lead.get("score_breakdown") or {}
        visit_at_str  = lead.get("visit_scheduled_at", "")

        if not phone:
            continue

        if was_sent(phone, "pre_visit_reminder"):
            continue

        # Formata data/hora da visita para o lembrete
        visit_dt = parse_ts(visit_at_str)
        if visit_dt:
            # Converte para Brasília (UTC-3)
            brasilia_dt = visit_dt - timedelta(hours=3)
            visit_fmt = brasilia_dt.strftime("%A, %d/%m, às %Hh%M")
        else:
            visit_fmt = "conforme combinado"

        # Busca histórico para o briefing
        conversas = _sb_get(
            "conversas",
            f"client_id=eq.{CLIENT_ID}&lead_phone=eq.{phone}&order=created_at.asc&limit=30"
        )
        history_text = "\n".join(
            f"{'Lead' if c.get('role') == 'user' else CONSULTANT_NAME}: {c.get('content', '')[:250]}"
            for c in conversas[-20:]
        ) if conversas else "(sem histórico)"

        sinais = ", ".join(f"{k}:{v}" for k, v in breakdown.items()) if breakdown else "não mapeados"

        # 1. Mensagem de lembrete para o lead
        reminder_prompt = f"""Você é {CONSULTANT_NAME}, consultora de imóveis de alto padrão da {IMOBILIARIA_NAME}.
Escreva UMA mensagem WhatsApp de lembrete de visita para {name}.

A visita é {visit_fmt}.

Tom: elegante, confirmador, breve. Mencione o horário. Ofereça verificar se há dúvida.
NUNCA mencione que é IA. Máximo 3 frases. Sem emojis."""

        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": reminder_prompt}],
            )
            reminder_msg = resp.content[0].text.strip()
        except Exception as e:
            log.error("Falha ao gerar lembrete pré-visita: %s", e)
            continue

        # 2. Briefing completo para o corretor
        corretor_number = os.getenv("CORRETOR_NUMBER", "")
        if corretor_number:
            briefing_prompt = f"""Gere um briefing estratégico em português para o corretor imobiliário sobre este lead.

LEAD: {name} | {phone}
SINAIS DE INTERESSE: {sinais}
VISITA: {visit_fmt}

HISTÓRICO:
{history_text}

Formato:
📋 BRIEFING PRÉ-VISITA — {name}
Visita: {visit_fmt}

PERFIL: [2 frases sobre quem é e o que busca]
MOTIVAÇÃO PRINCIPAL: [1 frase]
SINAIS QUENTES: [2-3 pontos objetivos]
OBJEÇÕES MAPEADAS: [o que pode travar]
PONTO DE ATENÇÃO: [algo específico para focar na visita]
PRÓXIMO PASSO RECOMENDADO: [1 ação clara pós-visita]

Máximo 200 palavras. Direto e acionável."""

            try:
                resp2 = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=400,
                    messages=[{"role": "user", "content": briefing_prompt}],
                )
                briefing_msg = resp2.content[0].text.strip()

                if not dry_run:
                    _send_whatsapp_message_sync(corretor_number, briefing_msg)
                    log.info("Briefing pré-visita enviado ao corretor para lead %s", phone)
                else:
                    log.info("[DRY-RUN] Briefing corretor para %s:\n%s", phone, briefing_msg[:200])
            except Exception as e:
                log.error("Falha ao gerar/enviar briefing ao corretor: %s", e)

        # Envia lembrete ao lead
        sent = send_whatsapp(phone, reminder_msg, dry_run=dry_run)
        if sent:
            if not dry_run:
                record_sent(phone, "pre_visit_reminder", reminder_msg)
                # Marca reminder como enviado no lead
                try:
                    _sb_patch(
                        f"leads?client_id=eq.{CLIENT_ID}&lead_phone=eq.{phone}",
                        {"visit_reminder_sent": True}
                    )
                except Exception:
                    pass
            enviados += 1

    log.info("Lembrete pré-visita: %d enviados", enviados)
    return enviados


def _sb_patch(path: str, data: dict) -> dict:
    """PATCH (update) em registro Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, context=_ssl()) as r:
            return {}
    except Exception as e:
        log.warning("PATCH %s falhou: %s", path, e)
        return {}


def _send_whatsapp_message_sync(phone: str, text: str) -> bool:
    """Envia mensagem WhatsApp via Evolution API (síncrono, para follow-up engine)."""
    if not EVOLUTION_URL or not EVOLUTION_API_KEY:
        return False
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    payload = json.dumps({"number": phone, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "apikey": EVOLUTION_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return r.status < 300
    except Exception as e:
        log.error("Falha ao enviar WhatsApp para %s: %s", phone, e)
        return False


# ─── Cenário 9: Relatório semanal de inteligência ────────────────────────────
def process_weekly_intelligence_report(dry_run: bool = False):
    """
    Toda segunda-feira às 8h, gera e envia ao gestor um relatório de inteligência com:
    - Métricas da semana (novos leads, score médio, visitas, conversões)
    - Leads quentes que precisam de ação
    - Padrões detectados (bairros mais buscados, faixas de valor, objeções comuns)
    - Recomendações de ação da semana
    
    Usa Claude Haiku para síntese inteligente. Envia via WhatsApp ao CORRETOR_NUMBER.
    """
    log.info("=== Relatório semanal de inteligência: gerando ===")
    now = datetime.now(timezone.utc)

    corretor_number = os.getenv("CORRETOR_NUMBER", "")
    if not corretor_number:
        log.warning("CORRETOR_NUMBER não configurado — relatório semanal não pode ser enviado")
        return 0

    # Janela da última semana
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    import urllib.parse

    # 1. Leads novos na semana
    new_leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&created_at=gte.{urllib.parse.quote(week_start)}"
            f"&select=lead_phone,lead_name,intention_score,score_breakdown,visita_agendada,descartado"
        )
    )

    # 2. Leads quentes (score >= 6) — todos, não só os da semana
    hot_leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&intention_score=gte.6"
            f"&descartado=eq.false"
            f"&select=lead_phone,lead_name,intention_score,score_breakdown,ultima_interacao,visita_agendada"
            f"&order=intention_score.desc"
            f"&limit=10"
        )
    )

    # 3. Atividade de conversas na semana
    weekly_conversas = _sb_get(
        "conversas",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&created_at=gte.{urllib.parse.quote(week_start)}"
            f"&role=eq.user"
            f"&select=lead_phone,content"
            f"&limit=200"
        )
    )

    # 4. Perfis estruturados para padrões
    profiles = _sb_get(
        "lead_profiles",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&select=neighborhoods,family_profile,budget_label,purchase_purpose,key_objections"
            f"&limit=50"
        )
    )

    # 5. Eventos de follow-up da semana
    followup_events = _sb_get(
        "followup_events",
        (
            f"client_id=eq.{CLIENT_ID}"
            f"&sent_at=gte.{urllib.parse.quote(week_start)}"
            f"&select=event_type,lead_phone"
        )
    )

    # Compila dados para o Haiku
    n_new = len(new_leads)
    n_hot = len([l for l in hot_leads if not l.get("visita_agendada")])
    n_visitas = len([l for l in new_leads + hot_leads if l.get("visita_agendada")])
    n_descartados = len([l for l in new_leads if l.get("descartado")])
    n_conversas = len(set(c.get("lead_phone") for c in weekly_conversas))
    n_followups = len(followup_events)

    # Extrai padrões de bairros
    bairros_counter: dict = {}
    for p in profiles:
        for bairro in (p.get("neighborhoods") or []):
            bairros_counter[bairro] = bairros_counter.get(bairro, 0) + 1
    top_bairros = sorted(bairros_counter.items(), key=lambda x: -x[1])[:5]

    # Extrai objeções mais comuns
    objecoes_counter: dict = {}
    for p in profiles:
        for obj in (p.get("key_objections") or []):
            objecoes_counter[obj[:50]] = objecoes_counter.get(obj[:50], 0) + 1
    top_objecoes = sorted(objecoes_counter.items(), key=lambda x: -x[1])[:3]

    # Extrai perfis familiares
    perfis_counter: dict = {}
    for p in profiles:
        fp = p.get("family_profile", "indefinido") or "indefinido"
        perfis_counter[fp] = perfis_counter.get(fp, 0) + 1

    # Leads quentes resumidos
    hot_summary = "\n".join(
        f"- {l.get('lead_name', l.get('lead_phone', '?')[:8]+'...')} "
        f"| Score {l.get('intention_score', 0)} "
        f"| {'visita agendada' if l.get('visita_agendada') else 'sem visita'}"
        for l in hot_leads[:5]
    ) if hot_leads else "(nenhum lead quente no momento)"

    # Amostra de conteúdo das conversas para análise
    sample_msgs = " | ".join(
        c.get("content", "")[:100]
        for c in weekly_conversas[:20]
    )

    report_context = f"""Semana encerrada em: {now.strftime('%d/%m/%Y')}
Imobiliária: {IMOBILIARIA_NAME} | Consultor: {CONSULTANT_NAME}

MÉTRICAS DA SEMANA:
- Novos leads: {n_new}
- Leads ativos em conversa: {n_conversas}
- Leads quentes (score ≥ 6): {n_hot}
- Visitas confirmadas: {n_visitas}
- Leads descartados: {n_descartados}
- Follow-ups automáticos enviados: {n_followups}

LEADS QUE PRECISAM DE AÇÃO HUMANA:
{hot_summary}

PADRÕES DETECTADOS:
Bairros mais buscados: {', '.join(f"{b}({n})" for b, n in top_bairros) or 'dados insuficientes'}
Perfis: {', '.join(f"{p}({n})" for p, n in perfis_counter.items()) or 'dados insuficientes'}
Objeções mais comuns: {', '.join(f"{o}({n})" for o, n in top_objecoes) or 'dados insuficientes'}

AMOSTRA DE CONVERSAS:
{sample_msgs[:800]}"""

    prompt = f"""Você é um analista de inteligência imobiliária. Gere um relatório semanal executivo para o gestor/corretor.

DADOS DA SEMANA:
{report_context}

Escreva em português brasileiro. Tom: executivo, direto, acionável.
Estrutura:
[Cabeçalho com data e nome da imobiliária]

RESUMO EXECUTIVO: [2-3 frases com o estado geral da semana]

NÚMERO QUE IMPORTA: [uma métrica que merece destaque especial e por quê]

LEADS PRIORITÁRIOS ESTA SEMANA: [lista dos 3-5 leads que precisam de ação humana, com o que fazer]

PADRÃO DETECTADO: [1-2 insights sobre o que os leads estão buscando — use os dados de bairros e perfis]

RECOMENDAÇÃO DA SEMANA: [1 ação concreta que o gestor pode tomar para melhorar performance]

Máximo 350 palavras. Sem emojis. Sem bullet points excessivos — escreva em prosa quando possível."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        report_text = resp.content[0].text.strip()
        log.info("Relatório semanal gerado: %d chars", len(report_text))
    except Exception as e:
        log.error("Falha ao gerar relatório semanal: %s", e)
        return 0

    # Salva no Supabase para histórico
    week_start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        _sb_post("weekly_reports", {
            "client_id":   CLIENT_ID,
            "week_start":  week_start_date,
            "report_data": {
                "text":       report_text,
                "metrics": {
                    "new_leads":       n_new,
                    "active_leads":    n_conversas,
                    "hot_leads":       n_hot,
                    "visits":          n_visitas,
                    "discarded":       n_descartados,
                    "followups_sent":  n_followups,
                },
                "top_bairros":   top_bairros,
                "top_objecoes":  top_objecoes,
            }
        })
    except Exception as e:
        log.warning("Falha ao salvar relatório no Supabase: %s", e)

    if dry_run:
        log.info("[DRY-RUN] Relatório semanal:\n%s", report_text)
        return 1

    sent = send_whatsapp(corretor_number, report_text, dry_run=False)
    log.info("Relatório semanal %s ao corretor", "enviado" if sent else "FALHOU")
    return 1 if sent else 0


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
        # Reativação de leads frios do CRM (> 30 dias sem interação)
        process_crm_reactivation(dry_run=dry_run)

    elif "--discard" in sys.argv:
        # Nutrição de leads descartados (sequência 30/60/90 dias)
        process_discard_nurture(dry_run=dry_run)

    elif "--weekly-report" in sys.argv:
        # Relatório semanal de inteligência (chamado toda segunda via timer especial)
        process_weekly_intelligence_report(dry_run=dry_run)

    elif "--pre-visit" in sys.argv:
        # Lembretes pré-visita (pode ser chamado a cada hora junto com silêncio)
        process_pre_visit_reminders(dry_run=dry_run)

    else:
        # Padrão (timer a cada hora): silêncio + descartados + lembretes pré-visita
        process_silence_followups(dry_run=dry_run)
        process_discard_nurture(dry_run=dry_run)
        process_pre_visit_reminders(dry_run=dry_run)

    log.info("Follow-up engine concluído.")


if __name__ == "__main__":
    main()
