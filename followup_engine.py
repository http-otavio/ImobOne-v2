#!/usr/bin/env python3
"""
followup_engine.py — Engine de follow-up automatizado ImobOne

Cenários implementados:

  1. SILÊNCIO 24h       — lead parou de responder, Sofia foi a última a falar
  2. SILÊNCIO 48h       — segundo toque, ângulo diferente
  3. SILÊNCIO 7d        — toque leve de nutrição de longo prazo
  4. PÓS-VISITA         — 24h após visita confirmada sem retorno do lead
  5. NOVO IMÓVEL        — imóvel novo no portfólio → match com leads compatíveis
                          e disparo de outreach personalizado por IA
  6. PÓS-VISITA PESQUISA — 48h após visita_agendada=true, envia pesquisa de satisfação

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
    "silence_24h":           72,    # não reenviar por 3 dias
    "silence_48h":           120,   # não reenviar por 5 dias
    "silence_7d":            720,   # não reenviar por 30 dias
    "post_visit":            168,   # não reenviar por 7 dias
    "new_property":          99999, # nunca reenviar o mesmo imóvel para o mesmo lead
    "crm_reactivation":      168,
    "discard_30d":           99999, # enviado uma vez, nunca repetir
    "discard_60d":           99999,
    "discard_90d":           99999,
    "pre_visit_reminder":    99999, # enviado uma vez, nunca repetir
    "weekly_report":         168,   # re-enviar no ciclo seguinte (1 semana)
    "pos_visita_pesquisa":   99999, # enviado uma vez por visita, nunca repetir
}

# Thresholds de silêncio
SILENCE_24H = timedelta(hours=24)
SILENCE_48H = timedelta(hours=48)
SILENCE_7D  = timedelta(days=7)

# Threshold para disparar pesquisa pós-visita
POS_VISITA_DELAY = timedelta(hours=48)

# Ajustes de score por sentimento
SENTIMENT_SCORE_DELTA = {
    "positivo": 3,
    "neutro":   0,
    "negativo": -2,
}

# Mapa de objeções para sentimento negativo
NEGATIVE_OBJECTION_MAP = {
    "preco":       "objecao_preco",
    "localizacao": "objecao_localizacao",
    "tamanho":     "objecao_tamanho",
    "default":     "objecao_geral_pos_visita",
}


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


def _sb_post(path: str, payload: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase POST %s: %s", path, e)
        return {}


def _sb_patch(path: str, params: str, payload: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}?{params}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="PATCH", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Supabase PATCH %s: %s", path, e)
        return {}


# ─── Evolution API ───────────────────────────────────────────────────────────
def _send_whatsapp(phone: str, message: str, instance: str = "") -> bool:
    inst = instance or EVOLUTION_INSTANCE
    url = f"{EVOLUTION_URL}/message/sendText/{inst}"
    payload = {"number": phone, "text": message}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=15) as r:
            return r.status < 300
    except Exception as e:
        log.error("Evolution API send %s: %s", phone, e)
        return False


# ─── followup_events helpers ─────────────────────────────────────────────────
def _already_sent(lead_id: str, event_type: str, ttl_hours: int) -> bool:
    """Retorna True se já existe evento recente do mesmo tipo para o lead."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
    rows = _sb_get(
        "followup_events",
        f"lead_id=eq.{lead_id}&event_type=eq.{event_type}&created_at=gte.{cutoff}&select=id",
    )
    return len(rows) > 0


def _register_event(lead_id: str, event_type: str, metadata: Optional[dict] = None) -> None:
    """Registra evento de follow-up para idempotência."""
    _sb_post("followup_events", {
        "lead_id": lead_id,
        "event_type": event_type,
        "client_id": CLIENT_ID,
        "metadata": json.dumps(metadata or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Janela de horário ───────────────────────────────────────────────────────
def _within_send_window() -> bool:
    now_brt = datetime.now(timezone.utc) - timedelta(hours=3)
    return SEND_HOUR_START <= now_brt.hour < SEND_HOUR_END


# ─── Claude Haiku helper ─────────────────────────────────────────────────────
def _haiku(system: str, user: str, max_tokens: int = 512) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


# ─── Cenário: PÓS-VISITA PESQUISA (48h após visita_agendada=true) ────────────
def classify_sentiment(response_text: str) -> str:
    """
    Classifica sentimento de uma resposta de lead usando Claude Haiku.
    Retorna: 'positivo', 'neutro' ou 'negativo'.
    """
    system = (
        "Você é um analisador de sentimento especializado em feedback imobiliário. "
        "Classifique o sentimento da mensagem do cliente em EXATAMENTE uma palavra: "
        "'positivo', 'neutro' ou 'negativo'. "
        "Responda APENAS com uma dessas três palavras, sem pontuação ou explicação."
    )
    result = _haiku(system, response_text, max_tokens=10)
    normalized = result.lower().strip().rstrip(".")
    if normalized in ("positivo", "neutro", "negativo"):
        return normalized
    # fallback seguro
    lower = response_text.lower()
    if any(w in lower for w in ["ótimo", "excelente", "gostei", "adorei", "perfeito", "incrível"]):
        return "positivo"
    if any(w in lower for w in ["ruim", "péssimo", "não gostei", "horrível", "decepcionante"]):
        return "negativo"
    return "neutro"


def detect_objection_type(response_text: str) -> str:
    """
    Detecta o tipo de objeção principal em um feedback negativo.
    Retorna chave de NEGATIVE_OBJECTION_MAP.
    """
    lower = response_text.lower()
    if any(w in lower for w in ["caro", "preço", "valor", "custo", "financiamento"]):
        return "preco"
    if any(w in lower for w in ["longe", "localização", "bairro", "acesso", "transporte"]):
        return "localizacao"
    if any(w in lower for w in ["pequeno", "tamanho", "área", "quarto", "espaço"]):
        return "tamanho"
    return "default"


def update_lead_sentiment(lead_id: str, sentiment: str, current_score: int) -> int:
    """
    Atualiza post_visit_sentiment e intention_score no Supabase.
    Retorna novo score.
    """
    delta = SENTIMENT_SCORE_DELTA.get(sentiment, 0)
    new_score = max(0, min(10, current_score + delta))

    payload: dict = {
        "post_visit_sentiment": sentiment,
        "intention_score": new_score,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    _sb_patch("leads", f"id=eq.{lead_id}", payload)
    log.info("Lead %s sentiment=%s score %d→%d", lead_id, sentiment, current_score, new_score)
    return new_score


def advance_lead_pipeline(lead_id: str) -> None:
    """Move lead positivo para próximo estágio no pipeline."""
    rows = _sb_get("leads", f"id=eq.{lead_id}&select=pipeline_stage")
    if not rows:
        return
    current_stage = rows[0].get("pipeline_stage", "interesse")
    pipeline_order = [
        "novo", "interesse", "qualificado", "visita_realizada",
        "proposta", "negociacao", "fechado"
    ]
    try:
        idx = pipeline_order.index(current_stage)
        next_stage = pipeline_order[min(idx + 1, len(pipeline_order) - 1)]
    except ValueError:
        next_stage = "qualificado"

    _sb_patch("leads", f"id=eq.{lead_id}", {
        "pipeline_stage": next_stage,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    log.info("Lead %s pipeline: %s → %s", lead_id, current_stage, next_stage)


def schedule_objection_followup(lead_id: str, objection_type: str) -> None:
    """
    Registra objeção mapeada para re-entrada no followup_engine.
    Cria evento especial para processamento de objeção específica.
    """
    _sb_post("followup_events", {
        "lead_id": lead_id,
        "event_type": NEGATIVE_OBJECTION_MAP.get(objection_type, "objecao_geral_pos_visita"),
        "client_id": CLIENT_ID,
        "metadata": json.dumps({
            "objection_type": objection_type,
            "scheduled_for": (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(),
            "source": "pos_visita_pesquisa",
        }),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    log.info("Lead %s objeção '%s' agendada para follow-up", lead_id, objection_type)


def generate_pos_visita_message(lead: dict) -> str:
    """Gera mensagem personalizada de pesquisa pós-visita via Claude Haiku."""
    nome = lead.get("nome", "").split()[0] if lead.get("nome") else "cliente"
    imovel = lead.get("imovel_interesse", "o imóvel")
    bairro = lead.get("bairro_interesse", "")

    system = (
        f"Você é {CONSULTANT_NAME}, consultora de imóveis da {IMOBILIARIA_NAME}. "
        "Escreva UMA mensagem curta e calorosa de pesquisa de satisfação pós-visita para WhatsApp. "
        "Seja natural, use o nome do cliente, pergunte como foi a experiência na visita. "
        "Peça para o cliente compartilhar o que achou em 1-2 frases. "
        "Não use emojis em excesso. Máximo 3 frases. Não inclua saudação formal."
    )
    user = (
        f"Cliente: {nome}\n"
        f"Imóvel visitado: {imovel}"
        + (f" em {bairro}" if bairro else "")
        + "\nGere a mensagem de pesquisa de satisfação."
    )
    return _haiku(system, user, max_tokens=150)


def run_pos_visita_pesquisa(dry_run: bool = False) -> int:
    """
    Cenário 6: Envia pesquisa de satisfação 48h após visita_agendada=true.
    Retorna número de mensagens enviadas.
    """
    if not _within_send_window() and not dry_run:
        log.info("Fora da janela de envio — pulando pos_visita_pesquisa")
        return 0

    cutoff = (datetime.now(timezone.utc) - POS_VISITA_DELAY).isoformat()

    # Leads com visita agendada há pelo menos 48h e sem pesquisa enviada ainda
    leads = _sb_get(
        "leads",
        (
            f"client_id=eq.{CLIENT_ID}"
            "&visita_agendada=eq.true"
            f"&visita_agendada_at=lte.{cutoff}"
            "&status=neq.descartado"
            # Não processar leads que já responderam pesquisa
            "&post_visit_sentiment=is.null"
            "&select=id,nome,phone,imovel_interesse,bairro_interesse,"
            "intention_score,pipeline_stage,visita_agendada_at"
        ),
    )

    sent = 0
    for lead in leads:
        lead_id = str(lead.get("id", ""))
        phone = lead.get("phone", "")
        if not lead_id or not phone:
            continue

        # Idempotência: verifica se já enviou pesquisa para este lead
        if _already_sent(lead_id, "pos_visita_pesquisa", TTL["pos_visita_pesquisa"]):
            log.debug("Lead %s já recebeu pos_visita_pesquisa", lead_id)
            continue

        message = generate_pos_visita_message(lead)

        if dry_run:
            log.info("[DRY-RUN] pos_visita_pesquisa → %s (%s): %s", lead_id, phone, message)
            sent += 1
            continue

        success = _send_whatsapp(phone, message)
        if success:
            _register_event(lead_id, "pos_visita_pesquisa", {
                "message": message,
                "visita_agendada_at": lead.get("visita_agendada_at"),
            })
            log.info("pos_visita_pesquisa enviado → lead %s (%s)", lead_id, phone)
            sent += 1
        else:
            log.warning("Falha ao enviar pos_visita_pesquisa para lead %s", lead_id)

    return sent


def process_pos_visita_response(lead_id: str, response_text: str) -> dict:
    """
    Processa resposta do lead à pesquisa pós-visita.
    - Classifica sentimento via Claude Haiku
    - Atualiza post_visit_sentiment + intention_score no Supabase
    - Positivo: avança no pipeline
    - Negativo: mapeia objeção e agenda follow-up específico
    Retorna dict com resultado do processamento.
    """
    # Busca dados atuais do lead
    rows = _sb_get("leads", f"id=eq.{lead_id}&select=id,nome,intention_score,pipeline_stage")
    if not rows:
        log.error("Lead %s não encontrado para processar resposta pós-visita", lead_id)
        return {"error": "lead_not_found", "lead_id": lead_id}

    lead = rows[0]
    current_score = int(lead.get("intention_score") or 5)

    # Classifica sentimento
    sentiment = classify_sentiment(response_text)
    log.info("Lead %s resposta pós-visita: sentiment=%s", lead_id, sentiment)

    # Atualiza Supabase
    new_score = update_lead_sentiment(lead_id, sentiment, current_score)

    result = {
        "lead_id": lead_id,
        "sentiment": sentiment,
        "previous_score": current_score,
        "new_score": new_score,
        "action": None,
    }

    if sentiment == "positivo":
        advance_lead_pipeline(lead_id)
        result["action"] = "pipeline_advanced"

    elif sentiment == "negativo":
        objection_type = detect_objection_type(response_text)
        schedule_objection_followup(lead_id, objection_type)
        result["action"] = "objection_scheduled"
        result["objection_type"] = objection_type
        result["followup_event"] = NEGATIVE_OBJECTION_MAP.get(objection_type, "objecao_geral_pos_visita")

    else:  # neutro
        result["action"] = "no_action"

    # Registra evento de resposta processada
    _register_event(lead_id, "pos_visita_resposta_processada", {
        "sentiment": sentiment,
        "response_preview": response_text[:100],
        "score_delta": new_score - current_score,
    })

    return result


# ─── Entry point ─────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv

    if "--new-property" in sys.argv:
        # Manter compatibilidade com cenário de novo imóvel
        log.info("Modo novo imóvel — não implementado neste módulo")
        return

    if "--pos-visita-pesquisa" in sys.argv:
        sent = run_pos_visita_pesquisa(dry_run=dry_run)
        log.info("pos_visita_pesquisa: %d mensagens enviadas", sent)
        return

    # Roda todos os cenários padrão
    sent_survey = run_pos_visita_pesquisa(dry_run=dry_run)
    log.info("Ciclo completo — pos_visita_pesquisa: %d", sent_survey)


if __name__ == "__main__":
    main()