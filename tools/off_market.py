"""
tools/off_market.py — Motor de Pocket Listings e Matchmaking Sigiloso

Pipeline quando corretor envia áudio no WhatsApp:
    1. Identifica que o remetente é um corretor cadastrado no onboarding
    2. Claude Haiku transcreve + extrai JSON estruturado do imóvel off-market
    3. OpenAI gera embedding → salva em imoveis_embeddings com is_off_market=True
    4. Carrega leads VIP (score >= 6) com perfil do lead_profiles
    5. Claude Haiku avalia compatibilidade em batch
    6. Gera draft personalizado para cada match
    7. Envia ao corretor via WhatsApp
    8. Registra em off_market_matches (idempotência)

Design decisions (CLAUDE.md):
    - Claude Haiku como classificador de compatibilidade — custo ~$0,001/batch
    - Matching via LLM, não regex — leads premium usam linguagem implícita
    - is_off_market=True na mesma tabela imoveis_embeddings — sem busca pública
    - Fire-and-forget via asyncio.create_task no webhook — não bloqueia a resposta
    - Idempotência via unique (imovel_id, lead_phone) na off_market_matches
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
import uuid
from datetime import datetime, timezone

log = logging.getLogger("off_market")

# ─── Configuração ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "")
EVOLUTION_URL     = os.getenv("EVOLUTION_URL", "https://api.otaviolabs.com")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None  # type: ignore[assignment]

try:
    import openai as _openai_module
except ImportError:
    _openai_module = None  # type: ignore[assignment]


# ─── Prompt de extração do imóvel off-market ──────────────────────────────────

_EXTRACTION_PROMPT = """\
Você é um assistente especializado em imóveis de alto padrão.
Analise a transcrição abaixo de um corretor descrevendo um imóvel off-market (pocket listing)
e extraia os dados estruturados.

TRANSCRIÇÃO DO CORRETOR:
{transcription}

Responda APENAS com JSON válido com EXATAMENTE esta estrutura:
{{
  "tipologia":      "apartamento | cobertura | casa | sobrado | terreno | sala_comercial | outro",
  "bairro":         "bairro principal declarado (string)",
  "cidade":         "cidade (default São Paulo se não informado)",
  "metragem":       número em m² ou null,
  "quartos":        número inteiro ou null,
  "vagas":          número inteiro ou null,
  "valor":          número em reais sem pontuação ou null,
  "caracteristicas": ["lista de características mencionadas"],
  "descricao":      "resumo conciso em 2-3 frases do imóvel como descrito pelo corretor",
  "sigiloso":       true
}}

Regras:
- Extraia apenas o que o corretor disse explicitamente.
- Use null para campos não mencionados.
- caracteristicas: lista de strings (ex: ["piscina", "home theater", "vista panorâmica"]).
- Responda APENAS com o JSON. Nenhum texto antes ou depois.
"""

_COMPATIBILITY_PROMPT = """\
Você é um especialista em matchmaking de imóveis de alto padrão.

IMÓVEL OFF-MARKET:
{imovel_desc}

LEADS VIP (cada um com perfil e histórico resumido):
{leads_json}

Para cada lead, avalie a compatibilidade com o imóvel.
Responda APENAS com um array JSON:
[
  {{
    "lead_phone": "telefone do lead",
    "compativel": true | false,
    "score_match": número de 0 a 10,
    "motivo": "frase curta explicando a compatibilidade ou incompatibilidade"
  }}
]

Critérios de compatibilidade:
- Bairro/região de interesse do lead coincide ou é próxima
- Budget do lead cobre o valor do imóvel (±20%)
- Tipologia bate com a preferência do lead
- Metragem está dentro da expectativa (quando informada)

score_match >= 7 = compatível. Abaixo de 7 = incompatível.
Responda APENAS com o array JSON.
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Remove +, espaços, hífens — retorna apenas dígitos."""
    return re.sub(r"[^\d]", "", phone)


def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _supabase_headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def _supabase_post(path: str, payload: dict) -> dict:
    """POST para Supabase REST API. Retorna dict com 'ok' e 'data'/'error'."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                  headers=_supabase_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as r:
            body = r.read().decode("utf-8")
            return {"ok": True, "data": json.loads(body) if body else {}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode("utf-8")}


def _supabase_get(path: str) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {**_supabase_headers(), "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as r:
            body = r.read().decode("utf-8")
            return {"ok": True, "data": json.loads(body) if body else []}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode("utf-8")}


# ─── 1. Detecção de corretor ───────────────────────────────────────────────────

def is_corretor_sender(sender: str, onboarding: dict) -> bool:
    """
    Retorna True se o número remetente é um corretor cadastrado no onboarding.
    Normaliza ambos os lados (remove +, espaços) antes de comparar.
    """
    sender_norm = _normalize_phone(sender)
    for corretor in onboarding.get("corretores", []):
        phone = corretor.get("telefone_whatsapp", "")
        if _normalize_phone(phone) == sender_norm:
            return True
    return False


def get_corretor_info(sender: str, onboarding: dict) -> dict | None:
    """Retorna o dict do corretor ou None se não encontrado."""
    sender_norm = _normalize_phone(sender)
    for corretor in onboarding.get("corretores", []):
        phone = corretor.get("telefone_whatsapp", "")
        if _normalize_phone(phone) == sender_norm:
            return corretor
    return None


# ─── 2. Extração do imóvel via Claude Haiku ───────────────────────────────────

def extract_imovel_from_transcription(transcription: str) -> dict:
    """
    Claude Haiku extrai JSON estruturado da transcrição do corretor.
    Síncrono — projetado para run_in_executor.
    Lança exceção se JSON não puder ser parseado.
    """
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": _EXTRACTION_PROMPT.format(transcription=transcription[:2000]),
        }],
    )
    raw = msg.content[0].text.strip()

    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    data = json.loads(raw)

    # Garante campo obrigatório
    if "descricao" not in data:
        data["descricao"] = transcription[:200]
    data["sigiloso"] = True
    return data


# ─── 3. Embedding + persistência no Supabase pgvector ─────────────────────────

def _build_imovel_text(imovel: dict) -> str:
    """Texto canônico do imóvel para geração de embedding."""
    parts = []
    if imovel.get("tipologia"):
        parts.append(f"Tipo: {imovel['tipologia']}")
    if imovel.get("bairro"):
        parts.append(f"Bairro: {imovel['bairro']}, {imovel.get('cidade', 'São Paulo')}")
    metrics = []
    if imovel.get("metragem"):
        metrics.append(f"{imovel['metragem']}m²")
    if imovel.get("quartos"):
        metrics.append(f"{imovel['quartos']} quartos")
    if imovel.get("vagas"):
        metrics.append(f"{imovel['vagas']} vagas")
    if metrics:
        parts.append(" | ".join(metrics))
    if imovel.get("valor"):
        parts.append(f"Valor: R$ {imovel['valor']:,.0f}".replace(",", "."))
    if imovel.get("caracteristicas"):
        parts.append(f"Características: {', '.join(imovel['caracteristicas'])}")
    if imovel.get("descricao"):
        parts.append(f"Descrição: {imovel['descricao'][:400]}")
    return "\n".join(parts)


def _generate_embedding(text: str) -> list[float]:
    """Gera embedding via OpenAI text-embedding-3-small. Síncrono."""
    if _openai_module is None:
        raise ImportError("openai não instalado")
    client = _openai_module.OpenAI(api_key=OPENAI_API_KEY)
    resp = client.embeddings.create(
        input=text.replace("\n", " "),
        model="text-embedding-3-small",
    )
    return resp.data[0].embedding


def save_off_market_imovel(
    imovel_data: dict,
    client_id: str,
    corretor_phone: str,
) -> str:
    """
    Gera embedding e salva o imóvel off-market em imoveis_embeddings.
    Retorna o imovel_id gerado (UUID).
    is_off_market=True — invisível em buscas públicas de leads.
    Síncrono — projetado para run_in_executor.
    """
    imovel_id  = f"offmkt_{uuid.uuid4().hex[:12]}"
    texto      = _build_imovel_text(imovel_data)
    embedding  = _generate_embedding(texto)

    payload = {
        "client_id":      client_id,
        "imovel_id":      imovel_id,
        "conteudo":       texto,
        "embedding":      embedding,
        "is_off_market":  True,
        "corretor_phone": corretor_phone,
        "ingested_at":    datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "is_off_market": True,
            "tipologia":     imovel_data.get("tipologia"),
            "bairro":        imovel_data.get("bairro"),
            "valor":         imovel_data.get("valor"),
            "metragem":      imovel_data.get("metragem"),
            "quartos":       imovel_data.get("quartos"),
            "sigiloso":      True,
        },
    }

    result = _supabase_post("imoveis_embeddings", payload)
    if not result["ok"]:
        raise RuntimeError(f"Falha ao salvar off-market no Supabase: {result.get('error')}")

    log.info("[OFF_MARKET] Imóvel %s salvo para cliente %s", imovel_id, client_id)
    return imovel_id


# ─── 4. Carrega leads VIP com perfil ──────────────────────────────────────────

def load_vip_leads(client_id: str, min_score: int = 6) -> list[dict]:
    """
    Carrega leads com score >= min_score que não foram descartados,
    enriquecidos com dados de lead_profiles quando disponível.
    """
    # Busca leads VIP
    query = (
        f"leads?select=lead_phone,lead_name,intention_score,pipeline_value_brl,"
        f"objections_detected,regioes_interesse,pipeline_imovel_ids"
        f"&client_id=eq.{client_id}"
        f"&intention_score=gte.{min_score}"
        f"&descartado=eq.false"
        f"&order=intention_score.desc"
        f"&limit=20"
    )
    result = _supabase_get(query)
    if not result["ok"]:
        log.warning("[OFF_MARKET] Falha ao carregar leads VIP: %s", result.get("error"))
        return []

    leads = result["data"] or []

    # Enriquece com lead_profiles
    for lead in leads:
        phone = lead.get("lead_phone", "")
        profile_q = (
            f"lead_profiles?lead_phone=eq.{phone}"
            f"&client_id=eq.{client_id}&limit=1"
        )
        pr = _supabase_get(profile_q)
        if pr["ok"] and pr["data"]:
            lead["profile"] = pr["data"][0]
        else:
            lead["profile"] = {}

    return leads


# ─── 5. Avaliação de compatibilidade via Claude Haiku ─────────────────────────

def assess_lead_compatibility(
    imovel_data: dict,
    leads: list[dict],
) -> list[dict]:
    """
    Claude Haiku avalia compatibilidade de cada lead com o imóvel off-market.
    Retorna lista filtrada de leads compatíveis (score_match >= 7).
    Processa em batch de até 10 leads — 1 chamada Haiku.
    """
    if not leads:
        return []
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    imovel_desc = _build_imovel_text(imovel_data)

    # Prepara resumo de cada lead para o prompt
    leads_summary = []
    for lead in leads[:10]:  # max 10 por batch
        profile = lead.get("profile", {})
        summary = {
            "lead_phone":    lead.get("lead_phone"),
            "nome":          lead.get("lead_name") or "Não informado",
            "score":         lead.get("intention_score", 0),
            "bairros":       profile.get("neighborhoods") or lead.get("regioes_interesse") or [],
            "tipologia":     profile.get("property_type") or "não informado",
            "budget_max":    profile.get("budget_max") or lead.get("pipeline_value_brl"),
            "quartos":       profile.get("bedrooms_desired"),
            "metragem_min":  profile.get("area_min_m2"),
            "proposito":     profile.get("purchase_purpose") or "não informado",
        }
        leads_summary.append(summary)

    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": _COMPATIBILITY_PROMPT.format(
                imovel_desc=imovel_desc,
                leads_json=json.dumps(leads_summary, ensure_ascii=False, indent=2),
            ),
        }],
    )
    raw = msg.content[0].text.strip()

    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    assessments = json.loads(raw)

    # Filtra compatíveis e monta resultado final
    compatible = []
    phone_to_lead = {l["lead_phone"]: l for l in leads}
    for a in assessments:
        if a.get("compativel") and a.get("score_match", 0) >= 7:
            phone = a["lead_phone"]
            lead  = phone_to_lead.get(phone, {})
            compatible.append({
                **lead,
                "score_match": a["score_match"],
                "motivo_match": a.get("motivo", ""),
            })

    log.info("[OFF_MARKET] %d/%d leads compatíveis encontrados", len(compatible), len(leads))
    return compatible


# ─── 6. Geração do draft personalizado ────────────────────────────────────────

def generate_match_draft(
    lead: dict,
    imovel_data: dict,
    corretor_nome: str = "Corretor",
) -> str:
    """
    Claude Haiku gera draft de abordagem personalizado para o corretor enviar ao lead.
    Tom: consultor de luxo, discrição total, sem revelar que é off-market explicitamente.
    """
    if _anthropic_module is None:
        raise ImportError("anthropic não instalado")

    profile      = lead.get("profile", {})
    lead_name    = lead.get("lead_name") or "o cliente"
    score_match  = lead.get("score_match", 7)
    motivo_match = lead.get("motivo_match", "")

    imovel_desc = _build_imovel_text(imovel_data)
    bairros     = profile.get("neighborhoods") or lead.get("regioes_interesse") or []

    prompt = f"""\
Você é um consultor de imóveis de alto padrão.
Escreva UMA mensagem de WhatsApp para o corretor {corretor_nome} enviar para o lead {lead_name}.

IMÓVEL DISPONÍVEL (não anunciado publicamente — tratar com discrição):
{imovel_desc}

PERFIL DO LEAD:
- Nome: {lead_name}
- Score de intenção: {lead.get('intention_score', 0)}/20
- Bairros de interesse: {', '.join(bairros) if bairros else 'não especificado'}
- Budget: R$ {profile.get('budget_max'):,.0f} (máx)
- Score de compatibilidade: {score_match}/10 — {motivo_match}

Regras da mensagem:
- Máximo 3-4 frases. Tom sofisticado e discreto.
- Mencione o bairro e tipologia do imóvel.
- Sugira uma ligação ou visita rápida "para ver antes que fique disponível no mercado".
- NÃO use "pocket listing" ou "off-market" explicitamente.
- NÃO use saudações genéricas.
- Responda APENAS com o texto da mensagem. Nenhum prefixo ou rótulo.
"""
    client = _anthropic_module.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    draft = msg.content[0].text.strip()
    # Remove prefixos comuns
    for prefix in ("Mensagem:", "Draft:", "Texto:", "WhatsApp:"):
        if draft.lower().startswith(prefix.lower()):
            draft = draft[len(prefix):].strip()
    return draft


# ─── 7. Envio da notificação ao corretor ──────────────────────────────────────

def send_off_market_match_to_corretor(
    corretor_phone: str,
    lead_name: str | None,
    lead_phone: str,
    draft: str,
    imovel_id: str,
    instance: str = "devlabz",
    score_match: int = 0,
) -> None:
    """
    Envia ao corretor via Evolution API a notificação de match + draft.
    Síncrono — projetado para run_in_executor.
    """
    ev_url = EVOLUTION_URL
    ev_key = EVOLUTION_API_KEY
    nome   = lead_name or lead_phone[-4:] + "..."

    msg = (
        f"🏠 *Match Off-Market* — score {score_match}/10\n"
        f"Lead: *{nome}* ({lead_phone[-6:]}...)\n\n"
        f"_Sugestão de abertura:_\n{draft}\n\n"
        f"Imóvel: `{imovel_id}`"
    )

    payload = json.dumps({
        "number":  corretor_phone,
        "text":    msg,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{ev_url}/message/sendText/{instance}",
        data=payload,
        headers={"apikey": ev_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=15) as r:
            log.info("[OFF_MARKET] Notificação enviada ao corretor %s | lead: %s",
                     corretor_phone, lead_phone)
    except Exception as e:
        log.error("[OFF_MARKET] Falha ao notificar corretor %s: %s", corretor_phone, e)
        raise


# ─── 8. Idempotência — registra match ─────────────────────────────────────────

def record_off_market_match(
    imovel_id: str,
    lead_phone: str,
    corretor_phone: str,
    draft: str,
    client_id: str,
) -> bool:
    """
    Registra o match em off_market_matches. Retorna False se já enviado (upsert).
    """
    payload = {
        "client_id":      client_id,
        "imovel_id":      imovel_id,
        "lead_phone":     lead_phone,
        "corretor_phone": corretor_phone,
        "draft_sent":     draft[:500],
        "sent_at":        datetime.now(timezone.utc).isoformat(),
    }
    # Prefer upsert — ignora duplicata silenciosamente
    url = f"{SUPABASE_URL}/rest/v1/off_market_matches"
    headers = {
        **_supabase_headers(),
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10):
            return True
    except Exception as e:
        log.warning("[OFF_MARKET] Falha ao registrar match %s/%s: %s",
                    imovel_id, lead_phone, e)
        return False


# ─── 9. Orquestrador principal ────────────────────────────────────────────────

def process_off_market_audio(
    transcription: str,
    corretor_phone: str,
    client_id: str,
    onboarding: dict,
    min_score: int = 6,
    evolution_instance: str = "devlabz",
) -> dict:
    """
    Pipeline completo para processar áudio de corretor como ingestão off-market.
    Síncrono — projetado para asyncio.run_in_executor no webhook.

    Retorna dict com:
        imovel_id:       str — ID do imóvel salvo
        matches_found:   int — leads compatíveis encontrados
        matches_sent:    int — drafts enviados ao corretor
        imovel_data:     dict — dados extraídos do imóvel
    """
    log.info("[OFF_MARKET] Iniciando pipeline para corretor %s", corretor_phone)

    # 1. Extrai dados do imóvel
    imovel_data = extract_imovel_from_transcription(transcription)
    log.info("[OFF_MARKET] Imóvel extraído: %s em %s | R$ %s",
             imovel_data.get("tipologia"), imovel_data.get("bairro"),
             imovel_data.get("valor"))

    # 2. Persiste no pgvector
    imovel_id = save_off_market_imovel(imovel_data, client_id, corretor_phone)

    # 3. Corretor info
    corretor_info = get_corretor_info(corretor_phone, onboarding) or {}
    corretor_nome = corretor_info.get("nome", "Corretor")
    ev_instance   = onboarding.get("evolution_instance", evolution_instance)

    # 4. Carrega leads VIP
    vip_leads = load_vip_leads(client_id, min_score=min_score)
    log.info("[OFF_MARKET] %d leads VIP carregados", len(vip_leads))

    if not vip_leads:
        log.info("[OFF_MARKET] Nenhum lead VIP disponível. Pipeline encerrado.")
        return {"imovel_id": imovel_id, "matches_found": 0,
                "matches_sent": 0, "imovel_data": imovel_data}

    # 5. Avalia compatibilidade
    compatible = assess_lead_compatibility(imovel_data, vip_leads)

    matches_sent = 0
    for lead in compatible:
        lead_phone = lead.get("lead_phone", "")
        lead_name  = lead.get("lead_name")
        score_match = lead.get("score_match", 7)

        # 6. Gera draft personalizado
        try:
            draft = generate_match_draft(lead, imovel_data, corretor_nome)
        except Exception as e:
            log.warning("[OFF_MARKET] Falha ao gerar draft para %s: %s", lead_phone, e)
            continue

        # 7. Envia ao corretor
        try:
            send_off_market_match_to_corretor(
                corretor_phone=corretor_phone,
                lead_name=lead_name,
                lead_phone=lead_phone,
                draft=draft,
                imovel_id=imovel_id,
                instance=ev_instance,
                score_match=score_match,
            )
        except Exception as e:
            log.error("[OFF_MARKET] Falha ao enviar notificação: %s", e)
            continue

        # 8. Registra (idempotência)
        record_off_market_match(
            imovel_id=imovel_id,
            lead_phone=lead_phone,
            corretor_phone=corretor_phone,
            draft=draft,
            client_id=client_id,
        )
        matches_sent += 1
        log.info("[OFF_MARKET] Match enviado: %s ↔ %s", imovel_id, lead_phone)

    log.info("[OFF_MARKET] Pipeline completo: %d matches, %d enviados",
             len(compatible), matches_sent)

    return {
        "imovel_id":     imovel_id,
        "matches_found": len(compatible),
        "matches_sent":  matches_sent,
        "imovel_data":   imovel_data,
    }
